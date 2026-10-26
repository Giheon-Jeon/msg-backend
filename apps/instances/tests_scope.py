import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Team, User
from apps.instances import tests as instance_tests
from apps.instances.models import Instance, InstanceStatus
from apps.instances.services import SchedulerError


@override_settings(
    CACHES=instance_tests.LOCMEM,
    SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="test-scheduler-token",
)
class InstanceScopeTests(TestCase):
    setUp = instance_tests.InstanceLockTests.setUp
    auth = instance_tests.InstanceLockTests.auth

    def instance(self, **overrides):
        values = {
            "user": self.user, "team": self.team, "challenge": self.challenge,
            "release": self.release, "status": InstanceStatus.RUNNING,
            "host": "https://original.example", "endpoints": [],
            "expires_at": timezone.now() + timedelta(minutes=30),
            "hard_expires_at": timezone.now() + timedelta(hours=2),
        }
        values.update(overrides)
        return Instance.objects.create(**values)

    def scheduler_data(self, instance_id=None):
        return {
            "instance_id": str(instance_id or uuid.uuid4()),
            "user_id": str(self.user.pk), "team_id": str(self.team.pk),
            "challenge_id": str(self.challenge.pk), "release_id": str(self.release.pk),
            "registry_revision": self.release.registry_revision,
            "status": InstanceStatus.RUNNING,
            "service_url": "https://new.example",
            "endpoints": [{
                "container_name": "app", "port": 8080, "protocol": "HTTP",
                "service_url": "https://new.example",
            }],
            "expires_at": (timezone.now() + timedelta(hours=1)).isoformat(),
        }

    def assert_instance_unchanged(self, instance):
        before = Instance.objects.get(pk=instance.pk)
        self.assertEqual(before.user_id, instance.user_id)
        self.assertEqual(before.team_id, instance.team_id)
        self.assertEqual(before.challenge_id, instance.challenge_id)
        self.assertEqual(before.release_id, instance.release_id)
        self.assertEqual(before.status, instance.status)
        self.assertEqual(before.host, instance.host)
        self.assertEqual(before.endpoints, instance.endpoints)
        self.assertEqual(before.expires_at, instance.expires_at)
        self.assertEqual(before.updated_at, instance.updated_at)
        self.assertEqual(before.extend_count, 0)

    def assert_scope_error(self, response):
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(response.data["code"], "SCHEDULER_UNAVAILABLE")
        self.assertIsNone(response.data["data"])

    @patch("apps.instances.views.call_scheduler_active")
    def test_active_lookup_rejects_mismatched_or_null_scope(self, active):
        instance = self.instance()
        for field in ("user_id", "team_id", "challenge_id"):
            for value in (str(uuid.uuid4()), None):
                with self.subTest(field=field, value=value):
                    active.return_value = {**self.scheduler_data(instance.pk), field: value}
                    response = self.client.get("/api/v1/teams/me/instances")
                    self.assert_scope_error(response)
                    self.assert_instance_unchanged(instance)

    @patch("apps.instances.views.call_scheduler_active")
    def test_active_lookup_checks_scope_without_release(self, active):
        instance = self.instance(release=None)
        active.return_value = {
            **self.scheduler_data(instance.pk), "team_id": str(uuid.uuid4()),
        }
        self.assert_scope_error(self.client.get("/api/v1/teams/me/instances"))
        self.assert_instance_unchanged(instance)

    @patch("apps.instances.views.call_scheduler_extend")
    def test_extend_rejects_mismatched_response_identity_before_mutation(self, extend):
        instance = self.instance()
        for field in ("instance_id", "user_id", "team_id", "challenge_id"):
            with self.subTest(field=field):
                extend.return_value = {
                    **self.scheduler_data(instance.pk), field: str(uuid.uuid4()),
                }
                self.assert_scope_error(self.client.post(f"/api/v1/instances/{instance.pk}/extend"))
                self.assert_instance_unchanged(instance)

    @patch("apps.instances.views.call_scheduler_extend")
    def test_partial_extend_response_remains_supported(self, extend):
        instance = self.instance()
        after = instance.expires_at + timedelta(minutes=10)
        extend.return_value = {"expires_at": after.isoformat()}
        response = self.client.post(f"/api/v1/instances/{instance.pk}/extend")
        self.assertEqual(response.status_code, 202, response.data)
        instance.refresh_from_db()
        self.assertEqual(instance.expires_at, after)
        self.assertEqual(instance.extend_count, 1)

    @patch("apps.instances.views.call_scheduler_create")
    def test_create_rejects_mismatched_response_scope(self, create):
        for field in ("user_id", "team_id", "challenge_id"):
            with self.subTest(field=field):
                create.return_value = {**self.scheduler_data(), field: str(uuid.uuid4())}
                response = self.client.post(
                    "/api/v1/instances", {"challenge_id": str(self.challenge.pk)}, format="json",
                )
                self.assert_scope_error(response)
                self.assertFalse(Instance.objects.exists())

    @patch("apps.instances.views.call_scheduler_reset")
    def test_reset_rejects_mismatched_scope_without_replacing_instance(self, reset):
        instance = self.instance()
        for field in ("user_id", "team_id", "challenge_id"):
            with self.subTest(field=field):
                reset.return_value = {**self.scheduler_data(), field: str(uuid.uuid4())}
                response = self.client.post(f"/api/v1/instances/{instance.pk}/reset")
                self.assert_scope_error(response)
                self.assertEqual(Instance.objects.count(), 1)
                self.assert_instance_unchanged(instance)

    @patch("apps.instances.views.call_scheduler_active")
    def test_recovery_rejects_foreign_or_missing_owner_metadata(self, active):
        for field in ("user_id", "team_id", "challenge_id"):
            for missing in (False, True):
                if field == "user_id" and missing:
                    continue
                if field == "challenge_id" and not missing:
                    continue
                with self.subTest(field=field, missing=missing):
                    data = self.scheduler_data()
                    if missing:
                        data.pop(field)
                    else:
                        data[field] = str(uuid.uuid4())
                    active.return_value = data
                    self.assert_scope_error(self.client.get("/api/v1/teams/me/instances"))
                    self.assertFalse(Instance.objects.exists())

    @patch("apps.instances.views.call_scheduler_active")
    def test_recovery_accepts_matching_owner_metadata(self, active):
        active.return_value = self.scheduler_data()
        response = self.client.get("/api/v1/teams/me/instances")
        self.assertEqual(response.status_code, 200, response.data)
        instance = Instance.objects.get()
        self.assertEqual(instance.user_id, self.user.pk)
        self.assertEqual(instance.team_id, self.team.pk)
        self.assertEqual(instance.release_id, self.release.pk)
        self.assertEqual(instance.host, "https://new.example")

    @patch("apps.instances.views.call_scheduler_active")
    def test_documented_active_summary_without_user_id_supports_recovery_and_sync(self, active):
        data = self.scheduler_data()
        data.pop("user_id")
        active.return_value = data
        for recovery in (True, False):
            with self.subTest(recovery=recovery):
                response = self.client.get("/api/v1/teams/me/instances")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(Instance.objects.count(), 1)
                instance = Instance.objects.get()
                self.assertEqual(instance.user_id, self.user.pk)
                self.assertEqual(instance.team_id, self.team.pk)

    @patch("apps.instances.views.call_scheduler_active")
    def test_active_lookup_rejects_invalid_identity_shapes(self, active):
        responses = [None, [], {}, {"instance_id": "not-a-uuid"}]
        responses.extend(
            {**self.scheduler_data(), field: value}
            for field in ("instance_id", "challenge_id")
            for value in (None, "not-a-uuid", 123)
        )
        for data in responses:
            with self.subTest(data=data):
                active.return_value = data
                self.assert_scope_error(self.client.get("/api/v1/teams/me/instances"))
                self.assertFalse(Instance.objects.exists())

    @patch("apps.instances.views.update_instance_from_scheduler")
    @patch("apps.instances.views.call_scheduler_active")
    def test_team_move_hides_old_instance_without_sync(self, active, synchronize):
        instance = self.instance()
        active.return_value = self.scheduler_data(instance.pk)
        new_team = Team.objects.create(team_name="new-current-team")
        User.objects.filter(pk=self.user.pk).update(team=new_team)

        response = self.client.get("/api/v1/teams/me/instances")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["data"])
        synchronize.assert_not_called()
        self.assert_instance_unchanged(instance)

    @patch("apps.instances.views.call_scheduler_detail")
    @patch("apps.instances.views.call_scheduler_active")
    def test_missing_active_does_not_reconcile_previous_team_instance(self, active, detail):
        instance = self.instance(status=InstanceStatus.REQUESTED)
        active.side_effect = SchedulerError("INSTANCE_NOT_FOUND", "no active", 404)
        new_team = Team.objects.create(team_name="new-current-team")
        User.objects.filter(pk=self.user.pk).update(team=new_team)

        response = self.client.get("/api/v1/teams/me/instances")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["data"])
        detail.assert_not_called()
        self.assert_instance_unchanged(instance)

    @patch("apps.instances.views.call_scheduler_active")
    def test_user_without_team_is_rejected_before_remote_lookup(self, active):
        instance = self.instance()
        User.objects.filter(pk=self.user.pk).update(team=None)
        response = self.client.get("/api/v1/teams/me/instances")
        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(response.data["code"], "USER_HAS_NO_TEAM")
        active.assert_not_called()
        self.assert_instance_unchanged(instance)

    def test_mutations_reject_previous_team_before_remote_calls(self):
        instance = self.instance()
        new_team = Team.objects.create(team_name="new-current-team")
        for team in (new_team, None):
            User.objects.filter(pk=self.user.pk).update(team=team)
            for action in ("delete", "reset", "extend"):
                with self.subTest(team=team, action=action):
                    with patch(f"apps.instances.views.call_scheduler_{action}") as remote:
                        url = f"/api/v1/instances/{instance.pk}"
                        response = self.client.delete(url) if action == "delete" else self.client.post(url + f"/{action}")
                    self.assertEqual(response.status_code, 403, response.data)
                    remote.assert_not_called()
                    self.assert_instance_unchanged(instance)

    @patch("apps.instances.views.call_scheduler_detail")
    @patch("apps.instances.views.call_scheduler_active")
    def test_terminal_reconcile_rejects_mismatched_identity(self, active, detail):
        instance = self.instance(status=InstanceStatus.REQUESTED)
        active.side_effect = SchedulerError("INSTANCE_NOT_FOUND", "no active", 404)
        for field in ("instance_id", "user_id", "team_id", "challenge_id"):
            with self.subTest(field=field):
                detail.return_value = {
                    **self.scheduler_data(instance.pk), field: str(uuid.uuid4()),
                    "status": InstanceStatus.FAILED,
                }
                self.assert_scope_error(self.client.get("/api/v1/teams/me/instances"))
                self.assert_instance_unchanged(instance)

    @patch("apps.instances.services.scheduler_request")
    def test_detail_sync_rejects_foreign_identity(self, scheduler):
        instance = self.instance()
        scheduler.return_value = {
            **self.scheduler_data(instance.pk), "instance_id": str(uuid.uuid4()),
        }
        response = self.client.get(f"/api/v1/challenges/{self.challenge.pk}")
        self.assert_scope_error(response)
        self.assert_instance_unchanged(instance)
