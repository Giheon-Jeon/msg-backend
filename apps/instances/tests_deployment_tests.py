import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.instances.deployment_tests import test_team_id
from apps.instances.models import ChallengeRelease, DeploymentTest, Instance, InstanceStatus
from apps.instances.services import SchedulerError, isoformat_z
from apps.instances.tests_releases import ReleaseTestBase, artifact_payload


@override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()],
                   RUNTIME_SECRET_API_TOKEN="deployment-test-runtime-worker-token", SCHEDULER_API_TOKEN="deployment-test-scheduler-token")
class AdminDeploymentTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.auth("root")
        old = self.register()
        self.old_id = old.data["data"]["release_id"]
        self.activate(self.old_id)
        self.client.post(f"/api/v1/admin/challenges/{self.challenge.pk}/runtime-secrets",
                         {"name": "flag", "value": "MSG{flag}"}, format="json")
        body = artifact_payload(revision=2)
        body["artifact"]["schema_version"] = "2.1"
        body["artifact"]["workload"]["containers"][0].update({"env": {"APP_MODE": "preview"}, "secret_env": {"FLAG": "flag"}})
        created = self.client.post(self.base_url, body, format="json")
        self.release = ChallengeRelease.objects.get(pk=created.data["data"]["release_id"])
        self.create_url = f"{self.base_url}/{self.release.pk}/deployment_tests"
        self.list_url = f"/api/v1/admin/challenges/{self.challenge.pk}/deployment_tests"
        self.calls = []
        self.stage = InstanceStatus.REQUESTED
        self.instance_id = uuid.uuid4()
        self.patcher = patch("apps.instances.services.scheduler_request", side_effect=self.scheduler)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def scheduler(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        test = DeploymentTest.objects.latest("created_at")
        if path.endswith("/active"):
            self.assertEqual(kwargs["query"]["user_id"], str(test.pk))
        return self.response(test)

    def response(self, test):
        return {"instance_id": str(self.instance_id), "team_id": str(test_team_id(test)),
                "challenge_id": str(self.challenge.pk), "release_id": str(self.release.pk), "registry_revision": 2,
                "status": self.stage, "expires_at": isoformat_z(test.expires_at),
                "endpoints": [{"container_name": "app", "port": 8080, "protocol": "HTTP", "service_url": "https://test.example"}]}

    def start(self, request_id=None):
        return self.client.post(self.create_url, {"request_id": str(request_id or uuid.uuid4())}, format="json")

    def detail_url(self, test_id):
        return f"{self.list_url}/{test_id}"

    def resolve(self):
        container = self.release.containers.get()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer deployment-test-runtime-worker-token")
        return client.post("/internal/v1/runtime-secrets/resolve", {
            "secret_ref": str(container.pk), "container": container.name, "image": container.image_ref,
        }, format="json")

    def test_selected_version_uses_separate_identity_and_keeps_current_and_participant(self):
        participant = Instance.objects.create(user=self.admin, team=self.team, challenge=self.challenge,
                                             status=InstanceStatus.RUNNING, release_id=self.old_id)
        response = self.start()
        self.assertEqual(response.status_code, 202, response.data)
        body = self.calls[0][2]
        self.assertEqual(body["release_id"], str(self.release.pk))
        self.assertNotEqual(body["user_id"], str(self.admin.pk))
        self.assertNotEqual(body["team_id"], str(self.team.pk))
        self.assertEqual((body["ttl_minutes"], body["hard_timeout_minutes"]), (15, 15))
        self.assertEqual(body["containers"][0]["env"], {"APP_MODE": "preview"})
        self.assertEqual(body["containers"][0]["secret_ref"], str(self.release.containers.get().pk))
        self.assertEqual(self.calls[0][1], "/api/v2/instances")
        self.assertNotIn("MSG{flag}", json.dumps(body))
        self.challenge.refresh_from_db()
        self.release.refresh_from_db()
        participant.refresh_from_db()
        self.assertEqual(str(self.challenge.runtime_config.current_release_id), self.old_id)
        self.assertIsNone(self.release.approved_at)
        self.assertEqual(participant.status, InstanceStatus.RUNNING)
        self.assertEqual(Instance.objects.count(), 1)
        self.assertEqual(response.data["data"]["endpoints"], [])

    def test_retry_and_second_click_do_not_create_another_instance(self):
        key = uuid.uuid4()
        first, second = self.start(key), self.start(key)
        self.assertEqual(first.data["data"]["test_id"], second.data["data"]["test_id"])
        self.assertEqual(self.start().status_code, 409)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(DeploymentTest.objects.count(), 1)

    def test_request_cannot_be_reused_for_another_release(self):
        key = uuid.uuid4()
        self.start(key)
        response = self.client.post(f"{self.base_url}/{self.old_id}/deployment_tests", {"request_id": str(key)}, format="json")
        self.assertEqual(response.status_code, 409)

    def test_request_timeout_recovers_by_test_identity_without_resubmitting(self):
        with patch("apps.instances.deployment_tests.call_scheduler_create", side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "raw private data", 503)):
            response = self.start()
        self.assertIsNone(response.data["data"]["instance_id"])
        self.stage = InstanceStatus.RUNNING
        detail = self.client.get(self.detail_url(response.data["data"]["test_id"]))
        self.assertEqual(detail.data["data"]["status"], "RUNNING")
        self.assertEqual(len(detail.data["data"]["endpoints"]), 1)
        self.assertIsNone(detail.data["data"]["error_message"])
        self.assertEqual([call[0] for call in self.calls], ["GET"])

    def test_secret_access_is_limited_to_live_test_without_activating_release(self):
        self.assertEqual(self.resolve().status_code, 404)
        response = self.start()
        self.assertEqual(self.resolve().data["data"]["env"], {"FLAG": "MSG{flag}"})
        self.stage = InstanceStatus.RUNNING
        url = self.detail_url(response.data["data"]["test_id"])
        self.client.get(url)
        self.assertEqual(self.client.delete(url).status_code, 202)
        self.assertEqual(self.resolve().status_code, 404)
        self.stage = InstanceStatus.CLEANED
        detail = self.client.get(url)
        self.assertIsNotNone(detail.data["data"]["finished_at"])
        self.assertEqual(detail.data["data"]["endpoints"], [])
        self.assertIsNone(self.client.get(self.list_url).data["data"]["active_test"])
        self.instance_id = uuid.uuid4()
        self.stage = InstanceStatus.REQUESTED
        self.assertEqual(self.start().status_code, 202)

    def test_secret_access_expires_even_if_browser_is_closed(self):
        self.start()
        DeploymentTest.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.resolve().status_code, 404)

    def test_pending_instance_cannot_be_marked_stopped(self):
        response = self.start()
        self.assertEqual(self.client.delete(self.detail_url(response.data["data"]["test_id"])).status_code, 409)
        self.assertFalse(any(call[0] == "DELETE" for call in self.calls))

    def test_wrong_remote_release_does_not_expose_endpoint_or_mark_running(self):
        response = self.start()
        test = DeploymentTest.objects.get()
        self.stage = InstanceStatus.RUNNING
        bad = {**self.response(test), "release_id": self.old_id}
        with patch("apps.instances.deployment_tests.call_scheduler_detail", return_value=bad):
            result = self.client.get(self.detail_url(test.pk)).data["data"]
        self.assertEqual(result["status"], "REQUESTED")
        self.assertEqual(result["error_code"], "SCHEDULER_RESPONSE_INVALID")
        self.assertEqual(result["endpoints"], [])

    def test_failure_reasons_omit_raw_runtime_details_and_secrets(self):
        response = self.start()
        self.stage = InstanceStatus.FAILED
        with patch("apps.instances.deployment_tests.scheduler_request", return_value=[{
            "error_code": "RUNTIME_CREATE_FAILED", "admin_detail": "password=private MSG{flag}",
        }]):
            result = self.client.get(self.detail_url(response.data["data"]["test_id"]))
        self.assertEqual(result.data["data"]["error_code"], "RUNTIME_CREATE_FAILED")
        self.assertNotIn("private", json.dumps(result.data))
        self.assertNotIn("MSG{flag}", json.dumps(result.data))
        self.assertIn("no-store", result["Cache-Control"])

    def test_create_failure_reason_survives_cleanup(self):
        response = self.start()
        url = self.detail_url(response.data["data"]["test_id"])
        with patch("apps.instances.deployment_tests.scheduler_request", return_value=[
            {"error_code": "RUNTIME_CREATE_FAILED"}, {"error_code": "RUNTIME_DELETE_FAILED"},
        ]):
            for stage in [InstanceStatus.CLEANUP_PENDING, InstanceStatus.CLEANED]:
                self.stage = stage
                result = self.client.get(url).data["data"]
                self.assertEqual(result["error_code"], "RUNTIME_CREATE_FAILED")
                self.assertIsNone(result["running_at"])
                self.assertEqual(result["endpoints"], [])
        self.assertIsNotNone(result["finished_at"])

    def test_secret_of_another_unapproved_release_is_not_authorized_by_test(self):
        response = self.start()
        another = self.client.post(self.base_url, artifact_payload(revision=3), format="json").data["data"]
        container = ChallengeRelease.objects.get(pk=another["release_id"]).containers.get()
        container.secret_env = self.release.containers.get().secret_env
        container.save(update_fields=["secret_env"])
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer deployment-test-runtime-worker-token")
        result = client.post("/internal/v1/runtime-secrets/resolve", {"secret_ref": str(container.pk), "container": container.name, "image": container.image_ref}, format="json")
        self.assertEqual(result.status_code, 404)

    def test_read_failure_keeps_instance_recoverable(self):
        response = self.start()
        with patch("apps.instances.deployment_tests.call_scheduler_detail", side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "raw", 503)):
            result = self.client.get(self.detail_url(response.data["data"]["test_id"]))
        self.assertIsNone(result.data["data"]["finished_at"])
        self.assertEqual(result.data["data"]["error_code"], "SCHEDULER_UNAVAILABLE")
        self.assertEqual(self.start().status_code, 409)

    def test_unconfirmed_request_can_finish_only_after_bounded_execution_window(self):
        with patch("apps.instances.deployment_tests.call_scheduler_create", side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "raw", 503)):
            self.start()
        with patch("apps.instances.deployment_tests.call_scheduler_active", side_effect=SchedulerError("INSTANCE_NOT_FOUND", "raw", 404)):
            self.assertIsNotNone(self.client.get(self.list_url).data["data"]["active_test"])
            DeploymentTest.objects.update(expires_at=timezone.now() - timedelta(minutes=2))
            result = self.client.get(self.list_url)
        self.assertIsNone(result.data["data"]["active_test"])
        self.assertEqual(result.data["data"]["tests"][0]["error_code"], "RESULT_UNAVAILABLE")

    def test_other_administrators_cannot_read_or_stop_own_test(self):
        response = self.start()
        User.objects.create_user(login_id="other", password="pw1234", nickname="다른 관리자", role=Role.ADMIN)
        self.auth("other")
        url = self.detail_url(response.data["data"]["test_id"])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertEqual(self.client.get(self.list_url).data["data"]["tests"], [])

    def test_admin_permission_required_on_every_endpoint(self):
        response = self.start()
        url = self.detail_url(response.data["data"]["test_id"])
        self.auth("player")
        for result in [self.start(), self.client.get(self.list_url), self.client.get(url), self.client.delete(url)]:
            self.assertEqual(result.status_code, 403)
        self.client.credentials()
        self.assertEqual(self.client.get(self.list_url).status_code, 401)

    def test_rejects_extra_runtime_settings_and_unknown_release(self):
        response = self.client.post(self.create_url, {"request_id": str(uuid.uuid4()), "env": {"FLAG": "new"}}, format="json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post(f"{self.base_url}/{uuid.uuid4()}/deployment_tests", {"request_id": str(uuid.uuid4())}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(DeploymentTest.objects.count(), 0)

    def test_unsupported_runtime_and_missing_secret_are_rejected_before_scheduling(self):
        self.release.runtime_type = "VM"
        self.release.save(update_fields=["runtime_type"])
        self.assertEqual(self.start().status_code, 400)
        self.release.runtime_type = "KUBERNETES"
        self.release.save(update_fields=["runtime_type"])
        container = self.release.containers.get()
        container.secret_env = {"FLAG": str(uuid.uuid4())}
        container.save(update_fields=["secret_env"])
        self.assertEqual(self.start().status_code, 400)
        self.assertEqual(DeploymentTest.objects.count(), 0)
        self.assertEqual(self.calls, [])

    def test_docker_container_release_uses_existing_scheduler_placement(self):
        self.release.runtime_type = "DOCKER"
        self.release.save(update_fields=["runtime_type"])
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.calls[0][2]["release_id"], str(self.release.pk))
        self.assertEqual(self.calls[0][2]["containers"][0]["image"], self.release.containers.get().image_ref)

    def test_remote_wrong_scope_and_excessive_lifetime_are_rejected(self):
        self.start()
        record = DeploymentTest.objects.get()
        for fields in [{"team_id": str(self.team.pk)}, {"challenge_id": str(uuid.uuid4())},
                       {"expires_at": isoformat_z(timezone.now() + timedelta(hours=2))}]:
            with patch("apps.instances.deployment_tests.call_scheduler_detail", return_value={**self.response(record), **fields}):
                response = self.client.get(self.detail_url(record.pk))
            self.assertEqual(response.data["data"]["error_code"], "SCHEDULER_RESPONSE_INVALID")
            self.assertEqual(response.data["data"]["endpoints"], [])
