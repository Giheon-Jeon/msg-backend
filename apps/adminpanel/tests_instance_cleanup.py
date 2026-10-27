import io
import json
import threading
import uuid
from datetime import timedelta
from http.client import IncompleteRead, RemoteDisconnected
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.adminpanel import tests as existing
from apps.adminpanel.models import AdminEvent
from apps.instances.models import Instance, InstanceStatus
from apps.instances.services import SchedulerError, call_scheduler_force_cleanup, update_instance_from_scheduler


@override_settings(CACHES=existing.LOCMEM, SCHEDULER_API_TOKEN="test-scheduler-token")
class AdminCleanupTests(TestCase):
    setUp = existing.AdminInstanceTests.setUp
    auth = existing.AdminInstanceTests.auth
    _instance = existing.AdminInstanceTests._instance

    def snapshot(self, instance, status="FAILED"):
        data = {"instance_id": str(instance.pk), "team_id": str(instance.team_id),
                "challenge_id": str(instance.challenge_id), "status": status,
                "delete_reason": "CREATE_FAILED_CLEANUP"}
        if instance.release_id is not None:
            data["release_id"] = str(instance.release_id)
        return data

    @patch("apps.instances.services.scheduler_request")
    def test_failed_create_cleanup_uses_guarded_operator_endpoint(self, request):
        instance = self._instance(status=InstanceStatus.CLEANUP_PENDING)
        request.return_value = self.snapshot(instance)
        response = self.client.delete(f"/api/v1/admin/instances/{instance.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["status"], "FAILED")
        instance.refresh_from_db()
        self.assertEqual(instance.status, "FAILED")
        self.assertIsNone(instance.delete_reason)
        self.assertTrue(AdminEvent.objects.filter(instance_id=instance.pk).exists())
        self.assertEqual(request.call_args.args, ("POST", f"/api/instances/{instance.pk}/force-cleanup"))

    @patch("apps.instances.services.scheduler_request")
    def test_confirmed_deleted_workload_preserves_cleaned_status(self, request):
        instance = self._instance(status=InstanceStatus.CLEANUP_PENDING)
        request.return_value = self.snapshot(instance, "CLEANED")
        response = self.client.delete(f"/api/v1/admin/instances/{instance.pk}")
        self.assertEqual((response.status_code, response.data["data"]["status"]), (200, "CLEANED"))

    @patch("apps.instances.services.scheduler_request")
    def test_runtime_guards_keep_pending_state_and_do_not_record_success(self, request):
        instance = self._instance(status=InstanceStatus.CLEANUP_PENDING)
        for code, status in [("WORKLOAD_STILL_EXISTS", 409), ("CREATE_RESULT_UNKNOWN", 409), ("RUNTIME_DELETE_FAILED", 503)]:
            with self.subTest(code=code):
                request.side_effect = SchedulerError(code, "정리 여부를 확인하지 못했습니다", status)
                response = self.client.delete(f"/api/v1/admin/instances/{instance.pk}")
                self.assertEqual((response.status_code, response.data["code"]), (status, code))
                instance.refresh_from_db()
                self.assertEqual(instance.status, "CLEANUP_PENDING")
                self.assertFalse(AdminEvent.objects.filter(instance_id=instance.pk).exclude(severity=AdminEvent.Severity.CRITICAL).exists())

    @patch("apps.instances.services.scheduler_request")
    def test_wrong_identity_or_nonterminal_result_is_rejected(self, request):
        instance = self._instance(status=InstanceStatus.CLEANUP_PENDING)
        for key, value in [("instance_id", str(uuid.uuid4())), ("team_id", str(uuid.uuid4())),
                           ("challenge_id", str(uuid.uuid4())), ("status", "RUNNING")]:
            with self.subTest(field=key):
                data = self.snapshot(instance)
                data[key] = value
                request.return_value = data
                response = self.client.delete(f"/api/v1/admin/instances/{instance.pk}")
                self.assertEqual(response.status_code, 503)
                instance.refresh_from_db()
                self.assertEqual(instance.status, "CLEANUP_PENDING")
                self.assertFalse(AdminEvent.objects.filter(instance_id=instance.pk).exclude(severity=AdminEvent.Severity.CRITICAL).exists())

    @patch("apps.instances.services.scheduler_request")
    def test_release_identity_is_required(self, request):
        instance = self._instance(status=InstanceStatus.CLEANUP_PENDING)
        instance.release = self.release
        instance.save(update_fields=["release"])
        request.return_value = {key: value for key, value in self.snapshot(instance).items() if key != "release_id"}
        with self.assertRaises(SchedulerError):
            call_scheduler_force_cleanup(instance, "Bearer test")

    @patch("apps.instances.services.scheduler_request")
    def test_participant_cannot_force_cleanup(self, request):
        instance = self._instance(status=InstanceStatus.CLEANUP_PENDING)
        self.auth("player")
        response = self.client.delete(f"/api/v1/admin/instances/{instance.pk}")
        self.assertEqual(response.status_code, 403)
        request.assert_not_called()

    def test_stopping_is_not_reported_as_already_terminated(self):
        instance = self._instance(status=InstanceStatus.STOPPING)
        response = self.client.delete(f"/api/v1/admin/instances/{instance.pk}")
        self.assertEqual((response.status_code, response.data["code"]), (400, "INVALID_STATE_TRANSITION"))

    def test_repeated_poll_does_not_reset_state_age(self):
        instance = self._instance(status=InstanceStatus.STOPPING)
        before = timezone.now() - timedelta(minutes=12)
        Instance.objects.filter(pk=instance.pk).update(updated_at=before)
        instance.refresh_from_db()
        update_instance_from_scheduler(instance, {"status": "STOPPING"})
        instance.refresh_from_db()
        self.assertEqual(instance.updated_at, before)
        update_instance_from_scheduler(instance, {"status": "CLEANED"})
        instance.refresh_from_db()
        self.assertGreater(instance.updated_at, before)

    def test_admin_list_includes_stable_state_timestamp(self):
        instance = self._instance(status=InstanceStatus.CLEANUP_PENDING)
        response = self.client.get("/api/v1/admin/instances?status=CLEANUP_PENDING")
        row = response.data["data"]["instances"][0]
        self.assertEqual(row["instance_id"], str(instance.pk))
        self.assertIn("updated_at", row)
        self.assertNotIn("updatedAt", row)


@override_settings(CACHES=existing.LOCMEM, SCHEDULER_API_TOKEN="test-scheduler-token")
class AdminCleanupRetryTests(TestCase):
    setUp = existing.AdminInstanceTests.setUp
    auth = existing.AdminInstanceTests.auth
    _instance = existing.AdminInstanceTests._instance
    snapshot = AdminCleanupTests.snapshot

    def retry(self, instance_id, body=None):
        return self.client.post(f"/api/v1/admin/instances/{instance_id}/retry-cleanup",
                                {} if body is None else body, format="json")

    @patch("apps.instances.services.scheduler_request")
    def test_retry_uses_operator_endpoint_and_records_acceptance_not_completion(self, request):
        instance = self._instance(status=InstanceStatus.FAILED)
        before = Instance.objects.values().get(pk=instance.pk)
        request.return_value = self.snapshot(instance, "CLEANUP_PENDING")
        response = self.retry(instance.pk)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["data"]["status"], "CLEANUP_PENDING")
        self.assertEqual(response.data["data"]["requested_by"], "root")
        self.assertEqual(request.call_args.args, ("POST", f"/api/instances/{instance.pk}/retry-cleanup"))
        self.assertEqual(request.call_args.kwargs["body"], {})
        self.assertEqual(request.call_args.kwargs["auth_header"], "Bearer test-scheduler-token")
        instance.refresh_from_db()
        self.assertEqual(instance.status, InstanceStatus.CLEANUP_PENDING)
        for field in ("release_id", "host", "endpoints", "expires_at", "delete_reason"):
            self.assertEqual(getattr(instance, field), before[field])
        event = AdminEvent.objects.get(instance_id=instance.pk)
        self.assertEqual(event.actor, "root")
        self.assertIn("재시도", event.message)
        self.assertNotIn("완료", event.message)
        self.assertEqual(self.retry(instance.pk).status_code, 409)
        request.assert_called_once()

    @patch("apps.instances.services.scheduler_request")
    def test_retry_rejects_other_states_without_scheduler_call(self, request):
        for state in (InstanceStatus.RUNNING, InstanceStatus.STOPPING, InstanceStatus.CLEANUP_PENDING,
                      InstanceStatus.CLEANED, InstanceStatus.EXPIRED, InstanceStatus.STOPPED):
            instance = self._instance(status=state)
            self.assertEqual(self.retry(instance.pk).status_code, 409)
        request.assert_not_called()
        self.assertFalse(AdminEvent.objects.exists())

    @patch("apps.instances.services.scheduler_request")
    def test_scheduler_errors_preserve_failed_state_without_fallback(self, request):
        instance = self._instance(status=InstanceStatus.FAILED)
        for code, status in (("INVALID_STATE_TRANSITION", 400), ("INSTANCE_NOT_FOUND", 404),
                             ("SCHEDULER_UNAVAILABLE", 503)):
            with self.subTest(code=code):
                request.side_effect = SchedulerError(code, "retry unavailable", status)
                response = self.retry(instance.pk)
                self.assertEqual((response.status_code, response.data["code"]), (status, code))
                instance.refresh_from_db()
                self.assertEqual(instance.status, InstanceStatus.FAILED)
                self.assertEqual(request.call_args.args[1], f"/api/instances/{instance.pk}/retry-cleanup")
        self.assertEqual(request.call_count, 3)
        self.assertFalse(AdminEvent.objects.exclude(severity=AdminEvent.Severity.CRITICAL).exists())

    @patch("apps.instances.services.scheduler_request")
    def test_invalid_reply_does_not_change_state(self, request):
        instance = self._instance(status=InstanceStatus.FAILED)
        invalid = [None, [], {}, self.snapshot(instance, "RUNNING"), self.snapshot(instance, "CLEANED")]
        for field in ("instance_id", "team_id", "challenge_id", "release_id"):
            invalid.extend(({**self.snapshot(instance, "CLEANUP_PENDING"), field: str(uuid.uuid4())},
                            {key: value for key, value in self.snapshot(instance, "CLEANUP_PENDING").items() if key != field}))
        invalid.append({**self.snapshot(instance, "CLEANUP_PENDING"), "user_id": str(uuid.uuid4())})
        for data in invalid:
            with self.subTest(data=data):
                request.return_value = data
                response = self.retry(instance.pk)
                self.assertEqual(response.status_code, 503, response.data)
                instance.refresh_from_db()
                self.assertEqual(instance.status, InstanceStatus.FAILED)
        self.assertFalse(AdminEvent.objects.exclude(severity=AdminEvent.Severity.CRITICAL).exists())

    @patch("apps.instances.services.scheduler_request")
    def test_only_admin_can_retry(self, request):
        instance = self._instance(status=InstanceStatus.FAILED)
        self.auth("player")
        self.assertEqual(self.retry(instance.pk).status_code, 403)
        self.client.credentials()
        self.assertEqual(self.retry(instance.pk).status_code, 401)
        request.assert_not_called()

    @patch("apps.instances.services.scheduler_request")
    def test_missing_instance_and_unexpected_input_do_not_call_scheduler(self, request):
        self.assertEqual(self.retry(uuid.uuid4()).status_code, 404)
        instance = self._instance(status=InstanceStatus.FAILED)
        self.assertEqual(self.retry(instance.pk, {"force": True}).status_code, 400)
        request.assert_not_called()

    @override_settings(SCHEDULER_BASE_URL="https://scheduler.example")
    @patch("apps.instances.services.urlopen")
    def test_retry_http_request_uses_service_authentication(self, urlopen):
        instance = self._instance(status=InstanceStatus.FAILED)
        urlopen.return_value = io.BytesIO(json.dumps({"data": self.snapshot(instance, "CLEANUP_PENDING")}).encode())
        response = self.retry(instance.pk)
        self.assertEqual(response.status_code, 202, response.data)
        outgoing = urlopen.call_args.args[0]
        self.assertEqual(outgoing.full_url, f"https://scheduler.example/api/instances/{instance.pk}/retry-cleanup")
        self.assertEqual(outgoing.get_method(), "POST")
        self.assertEqual(outgoing.get_header("Authorization"), "Bearer test-scheduler-token")
        self.assertEqual(json.loads(outgoing.data), {})

    @override_settings(SCHEDULER_BASE_URL="https://scheduler.example")
    @patch("apps.instances.services.urlopen")
    def test_malformed_http_reply_preserves_failed_state_and_hides_body(self, urlopen):
        instance = self._instance(status=InstanceStatus.FAILED)
        for payload in (b"private-invalid-json", b"[]", b'{"data": []}', b"\xff", b""):
            with self.subTest(payload=payload):
                urlopen.return_value = io.BytesIO(payload)
                response = self.retry(instance.pk)
                self.assertEqual((response.status_code, response.data["code"]), (503, "SCHEDULER_UNAVAILABLE"))
                instance.refresh_from_db()
                self.assertEqual(instance.status, InstanceStatus.FAILED)
                self.assertNotIn("private-invalid-json", response.content.decode())
        self.assertFalse(AdminEvent.objects.filter(message__contains="private-invalid-json").exists())

    @override_settings(SCHEDULER_BASE_URL="https://scheduler.example")
    @patch("apps.instances.services.urlopen")
    def test_transport_disconnect_returns_503_and_records_failure(self, urlopen):
        for stage, error in (
            ("headers", RemoteDisconnected("private-connection-detail")),
            ("headers", ConnectionResetError("private-connection-detail")),
            ("body", IncompleteRead(b"private-response-body", 100)),
            ("body", ConnectionResetError("private-connection-detail")),
            ("error_body_400", IncompleteRead(b"private-response-body", 100)),
            ("error_body_503", IncompleteRead(b"private-response-body", 100)),
        ):
            with self.subTest(stage=stage, error=type(error).__name__):
                instance = self._instance(status=InstanceStatus.FAILED)
                before = Instance.objects.values().get(pk=instance.pk)
                urlopen.reset_mock(return_value=True, side_effect=True)
                if stage == "headers":
                    urlopen.side_effect = error
                elif stage == "body":
                    urlopen.return_value.__enter__.return_value.read.side_effect = error
                else:
                    http_error = HTTPError("https://scheduler.example", int(stage[-3:]),
                                           "private-http-detail", {}, None)
                    http_error.read = Mock(side_effect=error)
                    urlopen.side_effect = http_error
                response = self.retry(instance.pk)
                self.assertEqual((response.status_code, response.data["code"]),
                                 (503, "SCHEDULER_UNAVAILABLE"))
                self.assertEqual(Instance.objects.values().get(pk=instance.pk), before)
                event = AdminEvent.objects.get(instance_id=instance.pk)
                self.assertEqual(event.severity, AdminEvent.Severity.CRITICAL)
                self.assertEqual(event.actor, "root")
                self.assertIn("SCHEDULER_UNAVAILABLE", event.message)
                self.assertNotIn("private-", event.message)
                self.assertNotIn("private-", response.content.decode())
                urlopen.assert_called_once()

    @override_settings(SCHEDULER_BASE_URL="https://scheduler.example")
    @patch("apps.instances.services.urlopen")
    def test_complete_http_errors_keep_existing_status_and_audit_behavior(self, urlopen):
        for status, code, expected_status, expected_code in (
            (400, "INVALID_STATE_TRANSITION", 400, "INVALID_STATE_TRANSITION"),
            (404, "INSTANCE_NOT_FOUND", 404, "INSTANCE_NOT_FOUND"),
            (502, "INTERNAL_ERROR", 503, "SCHEDULER_UNAVAILABLE"),
        ):
            with self.subTest(status=status):
                instance = self._instance(status=InstanceStatus.FAILED)
                urlopen.side_effect = HTTPError(
                    "https://scheduler.example", status, "upstream error", {},
                    io.BytesIO(json.dumps({"code": code, "message": "retry unavailable"}).encode()),
                )
                response = self.retry(instance.pk)
                self.assertEqual((response.status_code, response.data["code"]),
                                 (expected_status, expected_code))
                instance.refresh_from_db()
                self.assertEqual(instance.status, InstanceStatus.FAILED)
                event = AdminEvent.objects.get(instance_id=instance.pk)
                self.assertEqual(event.severity, AdminEvent.Severity.CRITICAL)

    @override_settings(SCHEDULER_API_TOKEN="")
    @patch("apps.instances.services.scheduler_request")
    def test_missing_service_token_does_not_use_admin_jwt(self, request):
        instance = self._instance(status=InstanceStatus.FAILED)
        self.assertEqual(self.retry(instance.pk).status_code, 503)
        instance.refresh_from_db()
        self.assertEqual(instance.status, InstanceStatus.FAILED)
        request.assert_not_called()

    @patch("apps.instances.services.scheduler_request")
    def test_legacy_unversioned_failed_instance_can_retry(self, request):
        instance = self._instance(status=InstanceStatus.FAILED)
        instance.release = None
        instance.save(update_fields=["release"])
        request.return_value = self.snapshot(instance, "CLEANUP_PENDING")
        self.assertEqual(self.retry(instance.pk).status_code, 202)


@override_settings(CACHES=existing.LOCMEM, SCHEDULER_API_TOKEN="test-scheduler-token")
class AdminCleanupRetryConcurrencyTests(TransactionTestCase):
    setUp = existing.AdminInstanceTests.setUp
    auth = existing.AdminInstanceTests.auth
    _instance = existing.AdminInstanceTests._instance
    snapshot = AdminCleanupTests.snapshot

    @patch("apps.instances.services.scheduler_request")
    def test_concurrent_retries_only_submit_one_cleanup_generation(self, request):
        instance = self._instance(status=InstanceStatus.FAILED)
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []

        def scheduler_reply(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Test did not release Scheduler response")
            return self.snapshot(instance, "CLEANUP_PENDING")

        def retry():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=self.admin)
                response = client.post(f"/api/v1/admin/instances/{instance.pk}/retry-cleanup", {}, format="json")
                results.append(response.status_code)
            except Exception as error:
                errors.append(error)
            finally:
                connections.close_all()

        request.side_effect = scheduler_reply
        first = threading.Thread(target=retry, daemon=True)
        second = threading.Thread(target=retry, daemon=True)
        first.start()
        try:
            self.assertTrue(entered.wait(10))
            second.start()
        finally:
            release.set()
            first.join(15)
            if second.ident is not None:
                second.join(15)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [202, 409])
        request.assert_called_once()
        instance.refresh_from_db()
        self.assertEqual(instance.status, InstanceStatus.CLEANUP_PENDING)
        self.assertEqual(AdminEvent.objects.filter(instance_id=instance.pk).count(), 1)
