from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.adminpanel.models import AdminEvent
from apps.challenge.models import ExeSubmission
from apps.challenge.remote_run_config import remote_run_data
from apps.challenge.services import hash_flag
from apps.challenge.tests_exe_submissions import ExeFixtures
from apps.instances.models import RuntimeSecret


class RemoteRunPreflightTests(ExeFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.remote.enabled = False
        self.remote.save(update_fields=["enabled"])
        admin = User.objects.create_user(login_id="preflight-admin", role=Role.ADMIN)
        self.manager = APIClient()
        self.manager.force_authenticate(admin)
        self.settings_url = f"/api/v1/admin/challenges/{self.challenge.pk}/remote_run"
        network = patch(
            "apps.challenge.checker_dispatch.build_opener",
            side_effect=AssertionError("Live checker forbidden"),
        )
        network.start()
        self.addCleanup(network.stop)

    def assert_not_ready(self):
        self.challenge.refresh_from_db()
        data = remote_run_data(self.challenge)
        self.assertFalse(data["can_enable"])
        self.assertFalse(data["available"])
        before = self.remote.version
        response = self.manager.patch(
            self.settings_url,
            {"enabled": True, "environment": "Not saved"},
            format="json",
        )
        self.assertEqual(
            (response.status_code, response.data["code"]), (409, "RELEASE_NOT_READY")
        )
        self.remote.refresh_from_db()
        self.assertFalse(self.remote.enabled)
        self.assertEqual(self.remote.version, before)
        self.assertNotEqual(self.remote.environment, "Not saved")
        self.assertFalse(AdminEvent.objects.exists())
        self.assertFalse(ExeSubmission.objects.exists())
        self.assertFalse(list(self.root.rglob("payload.exe")))
        self.assertNotIn("local-exe-test", str(data) + str(response.data))
        self.assertNotIn(str(self.root), str(data) + str(response.data))

    def test_complete_configuration_enables_without_writing_payload_or_starting_jobs(
        self,
    ):
        secret = RuntimeSecret.objects.get()
        self.assertTrue(remote_run_data(self.challenge)["can_enable"])
        self.assertFalse(remote_run_data(self.challenge)["available"])
        response = self.manager.patch(
            self.settings_url, {"enabled": True}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["data"]["can_enable"])
        self.assertTrue(response.data["data"]["available"])
        self.assertEqual(AdminEvent.objects.count(), 1)
        self.assertFalse(ExeSubmission.objects.exists())
        self.assertFalse(list(self.root.iterdir()))
        for value in (
            "local-exe-test",
            secret.encrypted_value,
            str(self.root),
            "local-test",
        ):
            self.assertNotIn(value, str(response.data))

    def test_private_storage_must_be_absolute_and_outside_media(self):
        media = self.root / "public"
        for root in (
            "",
            "relative",
            str(media),
            str(media / "nested"),
            str(self.root) + "\x00bad",
        ):
            with self.subTest(root=root), override_settings(
                EXE_SUBMISSION_STORAGE_ROOT=root, MEDIA_ROOT=str(media)
            ):
                self.assert_not_ready()

    def test_missing_flag_secret_blocks_enabling(self):
        RuntimeSecret.objects.all().delete()
        self.assert_not_ready()

    def test_undecryptable_newest_secret_does_not_fall_back_to_older_version(self):
        secret = RuntimeSecret.objects.get()
        RuntimeSecret.objects.create(
            challenge=self.challenge,
            name="flag",
            version=secret.version + 1,
            encrypted_value="unreadable-ciphertext",
            created_by="test",
        )
        self.assert_not_ready()

    def test_missing_invalid_or_wrong_encryption_keys_block_enabling(self):
        for keys in ([], ["invalid-key"], [Fernet.generate_key().decode("ascii")]):
            with self.subTest(keys_present=bool(keys)), override_settings(
                RUNTIME_SECRET_ENCRYPTION_KEYS=keys
            ):
                self.assert_not_ready()

    def test_current_scoring_flag_must_match_injected_secret(self):
        self.challenge.flag_hash = hash_flag("different-scoring-flag")
        self.challenge.save(update_fields=["flag_hash"])
        self.assert_not_ready()

    def test_missing_or_malformed_scheduler_token_blocks_enabling(self):
        for token in ("", " ", "token\r\nheader", "non-ascii-한글", None):
            with self.subTest(token_type=type(token).__name__), override_settings(
                SCHEDULER_API_TOKEN=token
            ):
                self.assert_not_ready()

    def test_can_disable_when_required_settings_are_missing(self):
        self.remote.enabled = True
        self.remote.save(update_fields=["enabled"])
        with override_settings(
            EXE_SUBMISSION_STORAGE_ROOT="",
            SCHEDULER_API_TOKEN="",
            RUNTIME_SECRET_ENCRYPTION_KEYS=[],
        ):
            self.assertFalse(remote_run_data(self.challenge)["available"])
            response = self.manager.patch(
                self.settings_url, {"enabled": False}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        self.remote.refresh_from_db()
        self.assertFalse(self.remote.enabled)

    def test_degraded_settings_stop_new_acceptance_but_keep_same_key_replay(self):
        self.remote.enabled = True
        self.remote.save(update_fields=["enabled"])
        submission = self.accepted()
        with override_settings(
            EXE_SUBMISSION_STORAGE_ROOT="",
            SCHEDULER_API_TOKEN="",
            RUNTIME_SECRET_ENCRYPTION_KEYS=[],
        ):
            self.assertFalse(remote_run_data(self.challenge)["available"])
            self.assertEqual(self.post_exe().status_code, 409)
            response = self.post_exe(submission.idempotency_key)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["data"]["submission_id"], str(submission.pk))
            listing = self.client.get(self.url)
            self.assertEqual(listing.status_code, 200)
            self.assertFalse(listing.data["data"]["remote_run"]["can_enable"])
        self.assertEqual(ExeSubmission.objects.count(), 1)
        self.assertTrue((self.root / submission.payload_key).exists())
