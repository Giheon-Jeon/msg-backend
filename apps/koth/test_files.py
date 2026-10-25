import copy
import tempfile
import uuid
from unittest.mock import patch

from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from google.api_core.exceptions import Forbidden
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.common.jwt import hash_token, issue_access_token
from apps.instances.tests_user_files import (
    SHA_A, SHA_B, user_archive, user_artifact, user_manifest, workflow_run,
)
from apps.instances.user_files import poll_user_files_once, register_user_files_bundle

from .models import KothChallenge, KothClub, KothUserFileBundle


@override_settings(
    STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}},
    RELEASE_POLL_EXPECTED_SOURCE_REF="refs/heads/main",
)
class KothParticipantFileTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.slug = "koth-author-original"
        self.challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name="Author club"),
            title="Author original",
            description="Original participant description",
            repository_slug=self.slug,
            source_revision=SHA_A,
            open_group=1,
            status="ACTIVE",
            inbound_internal_token_hash=hash_token("fixture-only-inbound"),
        )
        self.user = User.objects.create_user(login_id="koth-files", password="test")
        self.admin = User.objects.create_user(
            login_id="koth-file-admin", password="test", role="ADMIN"
        )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.user))
        self.archive = user_archive({"starter.py": b"# unchanged author starter\n"})
        self.bundle = self.register()[1]

    def register(self, *, archive=None, revision=1, sha=SHA_A, artifact_id=None):
        archive = self.archive if archive is None else archive
        manifest = user_manifest(archive, revision=revision, slug=self.slug, source_sha=sha)
        return register_user_files_bundle(
            manifest, archive, user_artifact(artifact_id or revision, slug=self.slug, sha=sha)
        )

    def download(self, *, admin=False, challenge_id=None):
        prefix = "admin/koth" if admin else "koth"
        return self.client.get(
            f"/api/v1/{prefix}/challenges/{challenge_id or self.challenge.pk}/file"
        )

    def metadata(self):
        return self.client.get(f"/api/v1/koth/clubs/{self.challenge.club_id}").data["data"]["challenges"][0]

    def status(self, status):
        self.challenge.status = status
        self.challenge.save(update_fields=["status"])

    def test_registration_preserves_ci_zip_bytes(self):
        self.assertIsInstance(self.bundle, KothUserFileBundle)
        with default_storage.open(self.bundle.object_key, "rb") as source:
            self.assertEqual(source.read(), self.archive)

    def test_duplicate_is_idempotent_and_does_not_upload_again(self):
        with patch("apps.koth.files.default_storage.save") as upload:
            status, duplicate = self.register()
        self.assertEqual((status, duplicate.pk), ("duplicate", self.bundle.pk))
        upload.assert_not_called()
        self.assertEqual(KothUserFileBundle.objects.count(), 1)

    def test_same_revision_cannot_change_original_files(self):
        status, _ = self.register(archive=user_archive({"different.txt": b"changed"}))
        self.assertEqual(status, "invalid")

    def test_hash_mismatch_and_unsafe_paths_never_upload(self):
        for archive, wrong_bytes in (
            (self.archive, b"altered"),
            (user_archive({"../secret": b"bad"}), None),
        ):
            manifest = user_manifest(archive, revision=2, slug=self.slug)
            with patch("apps.koth.files.default_storage.save") as upload:
                status, _ = register_user_files_bundle(
                    manifest, wrong_bytes if wrong_bytes is not None else archive,
                    user_artifact(2, slug=self.slug),
                )
            self.assertEqual(status, "invalid")
            upload.assert_not_called()

    def test_absent_manifest_cannot_smuggle_zip(self):
        status, _ = register_user_files_bundle(
            user_manifest(None, revision=2, slug=self.slug),
            self.archive, user_artifact(2, slug=self.slug),
        )
        self.assertEqual(status, "invalid")

    def test_artifact_cannot_be_reused_for_different_revision(self):
        self.assertEqual(self.register(revision=2, artifact_id=1)[0], "invalid")

    def test_unmapped_repository_does_not_import(self):
        status, _ = register_user_files_bundle(
            user_manifest(self.archive, slug="unmapped"), self.archive,
            user_artifact(20, slug="unmapped"),
        )
        self.assertEqual(status, "unmatched")

    def test_importing_new_source_does_not_change_running_source(self):
        self.assertEqual(self.register(revision=2, sha=SHA_B)[0], "registered")
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.source_revision, SHA_A)
        self.assertEqual(self.metadata()["file"]["source_revision"], SHA_A)

    def test_public_download_matches_metadata_and_original_bytes(self):
        metadata = self.metadata()
        self.assertEqual(metadata["description"], self.challenge.description)
        self.assertEqual(metadata["file"]["sha256"], self.bundle.sha256)
        self.assertEqual(metadata["file"]["file_name"], self.slug + ".zip")
        self.assertNotIn("object_key", str(metadata))
        response = self.download()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(int(response["Content-Length"]), len(self.archive))
        self.assertEqual(b"".join(response.streaming_content), self.archive)
        response.close()

    def test_unauthenticated_download_is_rejected(self):
        self.client.credentials()
        self.assertEqual(self.download().status_code, 401)

    def test_scheduled_and_closed_hide_content_and_never_touch_storage(self):
        for status in ("SCHEDULED", "CLOSED"):
            with self.subTest(status=status):
                self.status(status)
                metadata = self.metadata()
                self.assertEqual(metadata["description"], "")
                self.assertIsNone(metadata["file"])
                with patch("apps.koth.files.default_storage.open") as source:
                    self.assertEqual(self.download().status_code, 404)
                source.assert_not_called()

    def test_admin_can_verify_scheduled_file_without_opening_problem(self):
        self.status("SCHEDULED")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.admin))
        response = self.download(admin=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), self.archive)
        response.close()
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.status, "SCHEDULED")

    def test_participant_cannot_use_admin_download(self):
        self.assertEqual(self.download(admin=True).status_code, 403)

    def test_invalid_and_unknown_ids_are_not_server_errors(self):
        for challenge_id in ("invalid-id", str(uuid.uuid4())):
            self.assertEqual(self.download(challenge_id=challenge_id).status_code, 404)

    def test_missing_storage_object_is_404(self):
        default_storage.delete(self.bundle.object_key)
        self.assertEqual(self.download().status_code, 404)

    def test_storage_errors_are_retryable_and_do_not_leak_credentials(self):
        for error in (OSError("secret-path"), Forbidden("private-bucket-and-key")):
            with patch("apps.koth.files.default_storage.open", side_effect=error):
                response = self.download()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.data["code"], "FILE_STORAGE_UNAVAILABLE")
            self.assertNotIn(str(error), str(response.data))

    def test_source_without_matching_file_does_not_use_old_or_latest_zip(self):
        self.challenge.source_revision = SHA_B
        self.challenge.save(update_fields=["source_revision"])
        self.assertIsNone(self.metadata()["file"])
        self.assertEqual(self.download().status_code, 404)

    def test_deployment_source_switch_selects_matching_zip(self):
        changed = user_archive({"starter.py": b"# next author version\n"})
        status, bundle = self.register(archive=changed, revision=2, sha=SHA_B)
        self.assertEqual(status, "registered")
        self.challenge.source_revision = SHA_B
        self.challenge.save(update_fields=["source_revision"])
        self.assertEqual(self.metadata()["file"]["sha256"], bundle.sha256)
        response = self.download()
        self.assertEqual(b"".join(response.streaming_content), changed)
        response.close()

    def test_explicit_file_removal_never_falls_back_to_older_zip(self):
        status, _ = register_user_files_bundle(
            user_manifest(None, revision=2, slug=self.slug), None,
            user_artifact(2, slug=self.slug),
        )
        self.assertEqual(status, "registered")
        self.assertIsNone(self.metadata()["file"])
        self.assertEqual(self.download().status_code, 404)

    def test_club_list_stays_constant_query_count_with_file_history(self):
        self.register(revision=2)
        for i in range(5):
            KothChallenge.objects.create(
                club=KothClub.objects.create(name=f"Club {i}"), title=f"Title {i}",
                open_group=2, inbound_internal_token_hash=hash_token(f"inbound-{i}"),
            )
        with self.assertNumQueries(3):
            response = self.client.get("/api/v1/koth/clubs")
        self.assertEqual(response.data["data"]["challenge_count"], 6)

    def test_mapped_slug_is_unique_but_unmapped_records_remain_allowed(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            KothChallenge.objects.create(
                club=KothClub.objects.create(name="Duplicate slug"), title="Duplicate",
                repository_slug=self.slug, open_group=2,
                inbound_internal_token_hash=hash_token("duplicate-inbound"),
            )

    def test_poller_imports_only_trusted_matching_source(self):
        for valid in (False, True):
            manifest = user_manifest(self.archive, revision=2, slug=self.slug)
            artifact = user_artifact(2, slug=self.slug)
            source = workflow_run(artifact_id=2, sha=SHA_A if valid else SHA_B)
            with (
                patch("apps.instances.user_files.pending_artifacts", return_value=[artifact]),
                patch("apps.instances.user_files.get_workflow_run", return_value=source),
                patch("apps.instances.user_files.download_user_files_bundle", return_value=(copy.deepcopy(manifest), self.archive)),
                patch("apps.instances.user_files.mark_artifact_processed"),
            ):
                summary = poll_user_files_once("fixture-token")
            self.assertEqual(summary["registered"], int(valid))
            self.assertEqual(summary["invalid"], int(not valid))

    def test_model_changes_have_migrations(self):
        call_command("makemigrations", "koth", check=True, dry_run=True, verbosity=0)
