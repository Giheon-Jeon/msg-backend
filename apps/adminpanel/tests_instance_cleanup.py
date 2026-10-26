import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

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
