import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge
from apps.instances import tests as instance_tests
from apps.instances.models import DeleteReason, Instance, InstanceStatus
from apps.instances.services import SchedulerError


@override_settings(CACHES=instance_tests.LOCMEM, SECURE_SSL_REDIRECT=False, SCHEDULER_API_TOKEN="test-scheduler-token")
class TerminalInstanceStatusTests(TestCase):
    setUp = instance_tests.InstanceLockTests.setUp
    auth = instance_tests.InstanceLockTests.auth

    def instance(self, **overrides):
        values = dict(user=self.user, team=self.team, challenge=self.challenge,
                      status=InstanceStatus.FAILED, delete_reason=DeleteReason.TTL_EXPIRED,
                      host="https://old.example", ports=[8080],
                      endpoints=instance_tests.ENDPOINTS, expires_at=timezone.now())
        values.update(overrides)
        instance = Instance.objects.create(**values)
        self.history_created_at = getattr(
            self, "history_created_at", timezone.now() - timedelta(days=1)
        ) + timedelta(seconds=1)
        Instance.objects.filter(pk=instance.pk).update(created_at=self.history_created_at)
        instance.created_at = self.history_created_at
        return instance

    def request(self, challenge=None):
        return self.client.get("/api/v1/teams/me/instances", {"challenge_id": str(challenge or self.challenge.pk)})

    @patch("apps.instances.views.call_scheduler_active", side_effect=SchedulerError("INSTANCE_NOT_FOUND", "absent", 404))
    def test_failed_reset_remains_visible_after_refresh_without_exposing_endpoints(self, active):
        previous = self.instance(status=InstanceStatus.CLEANED, delete_reason=DeleteReason.REPLACED_BY_NEW_INSTANCE)
        replacement = self.instance(replaced_instance=previous)
        for _ in range(2):
            response = self.request()
            self.assertEqual(response.status_code, 200)
            data = response.data["data"]
            self.assertEqual(data["instance_id"], str(replacement.pk))
            self.assertEqual(data["replaced_instance_id"], str(previous.pk))
            self.assertEqual(data["status"], "FAILED")
            self.assertEqual(data["delete_reason"], "TTL_EXPIRED")
            self.assertIsNone(data["host"])
            self.assertEqual(data["ports"], [])
            self.assertEqual(data["endpoints"], [])
            self.assertTrue(all(key == key.lower() and "-" not in key for key in data))
            self.assertNotIn("deleteReason", data)
            self.assertNotIn("error_message", data)

    @patch("apps.instances.views.call_scheduler_active", side_effect=SchedulerError("INSTANCE_NOT_FOUND", "absent", 404))
    def test_unfiltered_lookup_keeps_active_only_contract(self, active):
        self.instance()
        response = self.client.get("/api/v1/teams/me/instances")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["data"])

    @patch("apps.instances.views.call_scheduler_active", side_effect=SchedulerError("INSTANCE_NOT_FOUND", "absent", 404))
    def test_latest_history_is_scoped_to_user_team_and_requested_challenge(self, active):
        expected = self.instance()
        other_user = User.objects.create_user(login_id="other-history-user", password="pw1234", team=self.team)
        self.instance(user=other_user)
        other_team = Team.objects.create(team_name="another-history-team")
        self.instance(team=other_team)
        other_challenge = Challenge.objects.create(title="Another", category="WEB", difficulty="EASY", score=100, flag_hash="x")
        self.instance(challenge=other_challenge)
        self.assertEqual(self.request().data["data"]["instance_id"], str(expected.pk))
        self.assertIsNone(self.request(uuid.uuid4()).data["data"])

    @patch("apps.instances.views.call_scheduler_active", side_effect=SchedulerError("INSTANCE_NOT_FOUND", "absent", 404))
    @patch("apps.instances.views.call_scheduler_detail")
    def test_newer_stopping_instance_hides_an_older_failure(self, detail, active):
        self.instance()
        stopping = self.instance(status=InstanceStatus.STOPPING, delete_reason=DeleteReason.USER_REQUESTED)
        detail.return_value = {"status": "STOPPING"}
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["instance_id"], str(stopping.pk))
        self.assertEqual(response.data["data"]["status"], "STOPPING")
        self.assertEqual(response.data["data"]["delete_reason"], "USER_REQUESTED")

    @patch("apps.instances.views.call_scheduler_active", side_effect=SchedulerError("INSTANCE_NOT_FOUND", "absent", 404))
    @patch("apps.instances.views.call_scheduler_detail")
    def test_cleanup_and_stopping_replace_stale_create_status(self, detail, active):
        for status, reason in (
            (InstanceStatus.CLEANUP_PENDING, "CREATE_FAILED_CLEANUP"),
            (InstanceStatus.STOPPING, "USER_REQUESTED"),
        ):
            with self.subTest(status=status):
                instance = self.instance(status=InstanceStatus.REQUESTED, delete_reason=None)
                detail.return_value = {
                    "instance_id": str(instance.pk), "user_id": str(self.user.pk),
                    "team_id": str(self.team.pk), "challenge_id": str(self.challenge.pk),
                    "release_id": str(instance.release_id) if instance.release_id else None,
                    "status": status, "delete_reason": reason,
                }
                response = self.request()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["data"]["status"], status)
                instance.refresh_from_db()
                self.assertEqual(instance.status, status)
                self.assertIsNone(response.data["data"]["host"])
                self.assertEqual(response.data["data"]["endpoints"], [])

    @patch("apps.instances.views.call_scheduler_active", side_effect=SchedulerError("INSTANCE_NOT_FOUND", "absent", 404))
    @patch("apps.instances.views.call_scheduler_detail")
    def test_cleanup_response_with_foreign_scope_cannot_change_pending_instance(self, detail, active):
        instance = self.instance(status=InstanceStatus.REQUESTED, delete_reason=None)
        detail.return_value = {
            "instance_id": str(instance.pk), "team_id": str(uuid.uuid4()),
            "challenge_id": str(self.challenge.pk), "status": "CLEANUP_PENDING",
            "delete_reason": "CREATE_FAILED_CLEANUP",
        }
        response = self.request()
        self.assertEqual(response.status_code, 503)
        instance.refresh_from_db()
        self.assertEqual(instance.status, InstanceStatus.REQUESTED)

    @patch("apps.instances.views.call_scheduler_active")
    def test_malformed_challenge_is_rejected_before_scheduler_access(self, active):
        response = self.client.get("/api/v1/teams/me/instances", {"challenge_id": "not-a-uuid"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "INVALID_REQUEST")
        active.assert_not_called()

    @patch("apps.instances.views.call_scheduler_active")
    def test_scheduler_failure_does_not_present_old_history_as_current_state(self, active):
        self.instance()
        active.side_effect = SchedulerError("SCHEDULER_UNAVAILABLE", "unavailable", 503)
        response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(response.data["data"])

    @patch("apps.instances.views.call_scheduler_active")
    def test_active_instance_of_another_challenge_still_takes_precedence(self, active):
        self.instance()
        other = Challenge.objects.create(title="Running elsewhere", category="WEB", difficulty="EASY", score=100, flag_hash="x")
        running = self.instance(challenge=other, status=InstanceStatus.RUNNING, delete_reason=None)
        active.return_value = {"instance_id": str(running.pk), "status": "RUNNING"}
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["instance_id"], str(running.pk))
        self.assertEqual(response.data["data"]["challenge_id"], str(other.pk))

    @patch("apps.instances.views.call_scheduler_active")
    def test_anonymous_request_cannot_read_history(self, active):
        self.instance()
        self.client.credentials()
        self.assertEqual(self.request().status_code, 401)
        active.assert_not_called()
