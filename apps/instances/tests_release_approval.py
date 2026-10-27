import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import patch

from django.db import close_old_connections, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.adminpanel.models import AdminEvent
from apps.challenge.models import Challenge
from apps.instances.image_preparations import process_preparation
from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig, ImagePreparationRequest, Instance, InstanceStatus
from apps.instances.tests_image_preparations import CONFIG, accepted, detail
from apps.instances.tests_releases import ReleaseTestBase


@override_settings(IMAGE_PREPARATION_ENABLED=False, IMAGE_PREPARATION_ACTIVATE_WHEN_READY=False)
class ReleaseApprovalTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.auth("root")
        self.first = self.register().data["data"]["release_id"]
        self.target = self.register(revision=2).data["data"]["release_id"]
        self.url = f"{self.base_url}/{self.target}/approve"

    def approve(self, body=None):
        return self.client.post(self.url, {} if body is None else body, format="json")

    def approval_events(self):
        return AdminEvent.objects.filter(type="CHALLENGE_RELEASE_APPROVED")

    def test_approval_without_current_release_does_not_activate_or_create_config(self):
        response = self.approve({"reason": "  review complete  "})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.data["data"]
        self.assertEqual(set(data), {
            "challenge_id", "release_id", "version", "registry_revision",
            "approved_at", "current_release_id", "is_current",
        })
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(data["release_id"], self.target)
        self.assertEqual(data["registry_revision"], 2)
        self.assertIsNone(data["current_release_id"])
        self.assertFalse(data["is_current"])
        self.assertIsNotNone(data["approved_at"])
        self.assertIsNotNone(ChallengeRelease.objects.get(pk=self.target).approved_at)
        self.assertFalse(ChallengeRuntimeConfig.objects.exists())
        self.assertFalse(ImagePreparationRequest.objects.exists())
        event = self.approval_events().get()
        self.assertEqual(event.actor, "root")
        self.assertEqual(event.challenge_id, self.challenge.pk)
        self.assertIn(self.target, event.message)
        self.assertIn("review complete", event.message)
        self.assertFalse(AdminEvent.objects.filter(type="CHALLENGE_RELEASE_ACTIVATED").exists())

    def test_approval_preserves_active_instances_and_all_other_release_fields(self):
        self.activate(self.first)
        instance = Instance.objects.create(user=self.player, team=self.team, challenge=self.challenge,
                                           release_id=self.first, status=InstanceStatus.RUNNING)
        before_instance = Instance.objects.values().get(pk=instance.pk)
        before_config = ChallengeRuntimeConfig.objects.values().get(challenge=self.challenge)
        before_release = ChallengeRelease.objects.values().get(pk=self.target)
        self.assertEqual(self.approve().status_code, 200)
        after_release = ChallengeRelease.objects.values().get(pk=self.target)
        self.assertIsNotNone(after_release.pop("approved_at"))
        before_release.pop("approved_at")
        self.assertEqual(after_release, before_release)
        self.assertEqual(Instance.objects.values().get(pk=instance.pk), before_instance)
        self.assertEqual(ChallengeRuntimeConfig.objects.values().get(challenge=self.challenge), before_config)

    def test_repeated_approval_preserves_timestamp_and_single_audit(self):
        first = self.approve({"reason": "first review"})
        second = self.approve({"reason": "second click"})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.data["data"]["approved_at"], second.data["data"]["approved_at"])
        self.assertEqual(self.approval_events().count(), 1)
        self.assertIn("first review", self.approval_events().get().message)

    def test_valid_unicode_null_and_empty_body_keep_activation_compatibility(self):
        reason = "\ud55c" * 499 + "\U0001f680"
        self.assertEqual(self.approve({"reason": reason}).status_code, 200)
        self.assertIn(reason, self.approval_events().get().message)
        self.assertEqual(self.approve({"reason": None}).status_code, 200)
        self.assertEqual(self.client.post(self.url).status_code, 200)
        self.assertEqual(self.approval_events().count(), 1)

    def test_already_active_approval_is_noop(self):
        self.activate(self.target)
        before = ChallengeRelease.objects.get(pk=self.target).approved_at
        config = ChallengeRuntimeConfig.objects.values().get(challenge=self.challenge)
        response = self.approve()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["data"]["is_current"])
        self.assertEqual(ChallengeRelease.objects.get(pk=self.target).approved_at, before)
        self.assertEqual(ChallengeRuntimeConfig.objects.values().get(challenge=self.challenge), config)
        self.assertFalse(self.approval_events().exists())

    def test_approval_timestamp_is_exposed_in_release_list(self):
        before = self.client.get(self.base_url).data["data"]["releases"]
        self.assertTrue(all(row["approved_at"] is None for row in before))
        response = self.approve()
        after = self.client.get(self.base_url).data["data"]["releases"]
        target = next(row for row in after if row["release_id"] == self.target)
        self.assertEqual(target["approved_at"], response.data["data"]["approved_at"])
        self.assertFalse(target["is_current"])

    def test_invalid_deployment_and_incomplete_environment_use_activation_checks(self):
        for values, code in (({"registry_revision": 0}, "RELEASE_NOT_DEPLOYABLE"),
                             ({"repository_env": {"missing": [{"name": "UNSET_KEY"}]}}, "RELEASE_ENV_INCOMPLETE")):
            with self.subTest(values=values):
                ChallengeRelease.objects.filter(pk=self.target).update(**values)
                for path in (self.url, f"{self.base_url}/{self.target}/activate"):
                    response = self.client.post(path, {}, format="json")
                    self.assertEqual((response.status_code, response.data["code"]), (400, code))
                self.assertIsNone(ChallengeRelease.objects.get(pk=self.target).approved_at)
                self.assertFalse(AdminEvent.objects.exists())
                self.assertFalse(ChallengeRuntimeConfig.objects.exists())
                ChallengeRelease.objects.filter(pk=self.target).update(registry_revision=2, repository_env={})

    def test_bad_image_digest_cannot_be_approved(self):
        ChallengeRelease.objects.get(pk=self.target).containers.update(image_ref="ghcr.io/msg-ctf/challenges/web-basic/app:latest")
        response = self.approve()
        self.assertEqual((response.status_code, response.data["code"]), (400, "RELEASE_NOT_DEPLOYABLE"))
        self.assertIsNone(ChallengeRelease.objects.get(pk=self.target).approved_at)

    def test_approval_has_no_preparation_or_network_side_effects_in_any_mode(self):
        revision = 3
        with patch("apps.instances.admin_views.enqueue_on_approval") as automatic, \
             patch("apps.instances.admin_views.queue_preparation") as queued, \
             patch("apps.instances.image_preparations._call_runtime") as runtime:
            for enabled in (False, True):
                for staged in (False, True):
                    with override_settings(IMAGE_PREPARATION_ENABLED=enabled,
                                           IMAGE_PREPARATION_ACTIVATE_WHEN_READY=staged,
                                           IMAGE_PREPARATION_RUNTIME_URL="", IMAGE_PREPARATION_API_TOKEN=""):
                        candidate = self.register(revision=revision).data["data"]["release_id"]
                        response = self.client.post(f"{self.base_url}/{candidate}/approve", {}, format="json")
                        self.assertEqual(response.status_code, 200)
                        revision += 1
            automatic.assert_not_called()
            queued.assert_not_called()
            runtime.assert_not_called()
        self.assertFalse(ImagePreparationRequest.objects.exists())
        self.assertFalse(ChallengeRuntimeConfig.objects.exists())

    @override_settings(**CONFIG, IMAGE_PREPARATION_ACTIVATE_WHEN_READY=True)
    def test_approval_does_not_replace_existing_pending_activation(self):
        with override_settings(IMAGE_PREPARATION_ENABLED=False, IMAGE_PREPARATION_ACTIVATE_WHEN_READY=False):
            self.activate(self.first)
        self.assertEqual(self.activate(self.target).status_code, 202)
        config = ChallengeRuntimeConfig.objects.values().get(challenge=self.challenge)
        pending = list(ImagePreparationRequest.objects.values())
        other = self.register(revision=3).data["data"]["release_id"]
        response = self.client.post(f"{self.base_url}/{other}/approve", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ChallengeRuntimeConfig.objects.values().get(challenge=self.challenge), config)
        self.assertEqual(list(ImagePreparationRequest.objects.values()), pending)

    def test_invalid_body_and_reason_are_rejected_before_approval(self):
        for body in ([], None, "text", {"activate": True}, {"reason": 4}, {"reason": " "},
                     {"reason": "x" * 501}, {"reason": "invalid\x00reason"}, {"reason": "\ud800"}):
            with self.subTest(body=ascii(body)):
                response = self.client.post(self.url, json.dumps(body), content_type="application/json")
                self.assertEqual(response.status_code, 400)
                self.assertIsNone(ChallengeRelease.objects.get(pk=self.target).approved_at)
        self.assertFalse(AdminEvent.objects.exists())
        self.assertFalse(ChallengeRuntimeConfig.objects.exists())

    def test_participants_and_anonymous_cannot_approve(self):
        self.auth("player")
        self.assertEqual(self.approve().status_code, 403)
        self.client.credentials()
        self.assertEqual(self.approve().status_code, 401)
        self.assertIsNone(ChallengeRelease.objects.get(pk=self.target).approved_at)
        self.assertFalse(AdminEvent.objects.exists())

    def test_missing_and_foreign_release_are_not_approved(self):
        other = Challenge.objects.create(title="Other", category="WEB", difficulty="EASY", score=100, flag_hash="unused")
        for path in (f"{self.base_url}/{uuid.uuid4()}/approve",
                     f"/api/v1/admin/challenges/{uuid.uuid4()}/releases/{self.target}/approve",
                     f"/api/v1/admin/challenges/{other.pk}/releases/{self.target}/approve"):
            self.assertEqual(self.client.post(path, {}, format="json").status_code, 404)
        self.assertIsNone(ChallengeRelease.objects.get(pk=self.target).approved_at)

    def test_audit_failure_rolls_back_approval(self):
        with patch("apps.instances.admin_views.AdminEvent.objects.create", side_effect=RuntimeError("audit unavailable")):
            response = self.approve()
        self.assertEqual(response.status_code, 500)
        self.assertIsNone(ChallengeRelease.objects.get(pk=self.target).approved_at)
        self.assertFalse(ChallengeRuntimeConfig.objects.exists())


@override_settings(IMAGE_PREPARATION_ENABLED=False, IMAGE_PREPARATION_ACTIVATE_WHEN_READY=False)
class ReleaseApprovalIntegrationTests(TransactionTestCase):
    setUp = ReleaseTestBase.setUp
    auth = ReleaseTestBase.auth
    register = ReleaseTestBase.register
    activate = ReleaseTestBase.activate

    @override_settings(**CONFIG, IMAGE_PREPARATION_ACTIVATE_WHEN_READY=True)
    def test_approve_then_explicit_prepare_completes_without_activation_intent(self):
        self.auth("root")
        first = self.register().data["data"]["release_id"]
        with override_settings(IMAGE_PREPARATION_ENABLED=False, IMAGE_PREPARATION_ACTIVATE_WHEN_READY=False):
            self.activate(first)
        target = self.register(revision=2).data["data"]["release_id"]
        preparation_url = f"{self.base_url}/{target}/image_preparations"
        rejected = self.client.post(preparation_url, {}, format="json", HTTP_IDEMPOTENCY_KEY="before-approval")
        self.assertEqual((rejected.status_code, rejected.data["code"]), (409, "RELEASE_NOT_APPROVED"))
        approved = self.client.post(f"{self.base_url}/{target}/approve", {}, format="json")
        self.assertEqual(approved.status_code, 200)
        response = self.client.post(preparation_url, {}, format="json", HTTP_IDEMPOTENCY_KEY="approved-preparation")
        self.assertEqual(response.status_code, 200, response.data)
        row = ImagePreparationRequest.objects.get(pk=response.data["data"]["request_id"])
        self.assertEqual(row.activation_status, "")
        with patch("apps.instances.image_preparations._call_runtime") as runtime:
            runtime.return_value = accepted(row)
            self.assertTrue(process_preparation(row.pk))
            row.refresh_from_db()
            ImagePreparationRequest.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
            runtime.return_value = detail(row)
            self.assertTrue(process_preparation(row.pk))
            self.assertEqual(runtime.call_count, 2)
        row.refresh_from_db()
        self.assertEqual(row.operation_status, "SUCCEEDED")
        self.assertEqual(row.activation_status, "")
        self.assertEqual(str(ChallengeRuntimeConfig.objects.get(challenge=self.challenge).current_release_id), first)

    def test_concurrent_approval_records_one_timestamp_and_audit(self):
        self.auth("root")
        release_id = self.register().data["data"]["release_id"]
        entered, finish = Event(), Event()
        original_create = AdminEvent.objects.create

        def audit(*args, **kwargs):
            entered.set()
            if not finish.wait(10):
                raise AssertionError("Test did not release approval transaction")
            return original_create(*args, **kwargs)

        def approve():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.admin)
                response = client.post(f"{self.base_url}/{release_id}/approve", {}, format="json")
                return response.status_code, response.data["data"]["approved_at"]
            finally:
                connections.close_all()

        with patch("apps.instances.admin_views.AdminEvent.objects.create", side_effect=audit):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(approve)
                try:
                    self.assertTrue(entered.wait(10))
                    second = pool.submit(approve)
                finally:
                    finish.set()
                results = [first.result(timeout=15), second.result(timeout=15)]
        self.assertEqual([result[0] for result in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(AdminEvent.objects.filter(type="CHALLENGE_RELEASE_APPROVED").count(), 1)
        self.assertFalse(ChallengeRuntimeConfig.objects.exists())
