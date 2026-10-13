import hashlib
import io
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.storage import default_storage
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import Cell, TeamChallengeAccess
from apps.challenge.models import Challenge
from apps.challenge.services import hash_flag
from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    ChallengeUserFileBundle,
    IsolationProfile,
    PollerArtifact,
)
from apps.instances.user_files import (
    current_user_file_bundle,
    poll_user_files_once,
    register_user_files_bundle,
)

SHA_A = "1" * 40
SHA_B = "2" * 40
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class GCSStorageSettingsTests(SimpleTestCase):
    def test_gcs_environment_configures_default_storage(self):
        environment = {
            **os.environ,
            "GCS_ENABLED": "True",
            "GCS_PROJECT_ID": "msg-broker",
            "GCS_BUCKET_NAME": "2026msg_gcs",
            "GCS_OBJECT_PREFIX": "dev",
        }
        script = """
from config import settings
storage = settings.STORAGES["default"]
assert storage["BACKEND"] == "storages.backends.gcloud.GoogleCloudStorage"
assert storage["OPTIONS"] == {
    "project_id": "msg-broker",
    "bucket_name": "2026msg_gcs",
    "location": "dev",
}
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


def user_archive(files=None):
    files = files or {"readme.txt": b"hello"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def user_manifest(archive_bytes=None, revision=1, slug="web-basic", source_sha=SHA_A):
    if archive_bytes is None:
        user_files = {"present": False}
    else:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        user_files = {
            "present": True,
            "archive": "user-files.zip",
            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "size_bytes": len(archive_bytes),
            "file_count": len(entries),
            "uncompressed_size_bytes": sum(entry.file_size for entry in entries),
        }
    return {
        "schema_version": "1.0",
        "challenge_slug": slug,
        "registry_revision": revision,
        "source_ref": "refs/heads/main",
        "source_sha": source_sha,
        "user_files": user_files,
    }


def user_artifact(artifact_id=1, slug="web-basic", sha=SHA_A):
    return {
        "id": artifact_id,
        "name": f"{slug}-main-user-files-bundle",
        "workflow_run": {
            "id": artifact_id + 1000,
            "head_branch": "main",
            "head_sha": sha,
        },
    }


def workflow_run(
    artifact_id=1,
    sha=SHA_A,
    status="completed",
    conclusion="success",
):
    return {
        "id": artifact_id + 1000,
        "status": status,
        "conclusion": conclusion,
        "head_branch": "main",
        "head_sha": sha,
    }


@override_settings(CACHES=LOCMEM)
class UserFilesTestBase(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.storage_settings = override_settings(MEDIA_ROOT=self.media.name)
        self.storage_settings.enable()
        self.challenge = Challenge.objects.create(
            challenge_slug="web-basic",
            title="Web Basic",
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=500,
            description="첨부파일 테스트",
            flag_hash=hash_flag("MSG{flag}"),
            is_published=True,
        )

    def tearDown(self):
        self.storage_settings.disable()
        self.media.cleanup()

    def create_release(self, revision):
        return ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=revision,
            registry_revision=revision,
            challenge_slug=self.challenge.challenge_slug,
            isolation_profile=IsolationProfile.WEB,
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
        )


class RegisterUserFilesBundleTests(UserFilesTestBase):
    def test_registers_verified_archive_and_links_release(self):
        release = self.create_release(1)
        archive_bytes = user_archive()

        status, bundle = register_user_files_bundle(
            user_manifest(archive_bytes), archive_bytes, user_artifact()
        )

        self.assertEqual(status, "registered")
        self.assertEqual(bundle.release, release)
        self.assertTrue(default_storage.exists(bundle.object_key))
        with default_storage.open(bundle.object_key, "rb") as stored:
            self.assertEqual(stored.read(), archive_bytes)

    def test_same_revision_is_idempotent_but_changed_content_is_rejected(self):
        archive_bytes = user_archive()
        manifest = user_manifest(archive_bytes)
        first_status, first = register_user_files_bundle(
            manifest, archive_bytes, user_artifact()
        )
        duplicate_status, duplicate = register_user_files_bundle(
            manifest, archive_bytes, user_artifact(artifact_id=2)
        )

        changed_archive = user_archive({"readme.txt": b"changed"})
        invalid_status, _ = register_user_files_bundle(
            user_manifest(changed_archive), changed_archive, user_artifact(artifact_id=3)
        )

        self.assertEqual(first_status, "registered")
        self.assertEqual(duplicate_status, "duplicate")
        self.assertEqual(duplicate.pk, first.pk)
        self.assertEqual(invalid_status, "invalid")
        self.assertEqual(ChallengeUserFileBundle.objects.count(), 1)

    def test_present_false_hides_previous_bundle_without_deleting_object(self):
        archive_bytes = user_archive()
        _, previous = register_user_files_bundle(
            user_manifest(archive_bytes), archive_bytes, user_artifact()
        )
        status, latest = register_user_files_bundle(
            user_manifest(revision=2, source_sha=SHA_B),
            None,
            user_artifact(artifact_id=2, sha=SHA_B),
        )

        self.assertEqual(status, "registered")
        self.assertFalse(latest.present)
        self.assertIsNone(current_user_file_bundle(self.challenge))
        self.assertTrue(default_storage.exists(previous.object_key))

    def test_rejects_checksum_mismatch_and_unsafe_zip_path(self):
        archive_bytes = user_archive()
        manifest = user_manifest(archive_bytes)
        manifest["user_files"]["sha256"] = "0" * 64
        checksum_status, _ = register_user_files_bundle(
            manifest, archive_bytes, user_artifact()
        )

        unsafe_archive = user_archive({"../flag.txt": b"no"})
        unsafe_status, _ = register_user_files_bundle(
            user_manifest(unsafe_archive), unsafe_archive, user_artifact(artifact_id=2)
        )

        self.assertEqual(checksum_status, "invalid")
        self.assertEqual(unsafe_status, "invalid")
        self.assertEqual(ChallengeUserFileBundle.objects.count(), 0)

    def test_active_release_selects_its_revision(self):
        release_one = self.create_release(1)
        self.create_release(2)
        archive_one = user_archive({"one.txt": b"one"})
        archive_two = user_archive({"two.txt": b"two"})
        _, bundle_one = register_user_files_bundle(
            user_manifest(archive_one), archive_one, user_artifact()
        )
        register_user_files_bundle(
            user_manifest(archive_two, revision=2, source_sha=SHA_B),
            archive_two,
            user_artifact(artifact_id=2, sha=SHA_B),
        )
        ChallengeRuntimeConfig.objects.create(
            challenge=self.challenge,
            current_release=release_one,
        )

        self.assertEqual(current_user_file_bundle(self.challenge), bundle_one)

    def test_dynamic_challenge_without_active_release_hides_files(self):
        archive_bytes = user_archive()
        register_user_files_bundle(
            user_manifest(archive_bytes), archive_bytes, user_artifact()
        )
        ChallengeRuntimeConfig.objects.create(challenge=self.challenge)

        self.assertIsNone(current_user_file_bundle(self.challenge))

    def test_present_false_only_hides_file_after_release_activation(self):
        release_one = self.create_release(1)
        release_two = self.create_release(2)
        archive_bytes = user_archive()
        _, bundle_one = register_user_files_bundle(
            user_manifest(archive_bytes), archive_bytes, user_artifact()
        )
        runtime_config = ChallengeRuntimeConfig.objects.create(
            challenge=self.challenge,
            current_release=release_one,
        )
        register_user_files_bundle(
            user_manifest(revision=2, source_sha=SHA_B),
            None,
            user_artifact(artifact_id=2, sha=SHA_B),
        )

        self.assertEqual(current_user_file_bundle(self.challenge), bundle_one)

        runtime_config.current_release = release_two
        runtime_config.save(update_fields=["current_release"])
        self.assertIsNone(current_user_file_bundle(self.challenge))


class UserFilesPollerTests(UserFilesTestBase):
    def test_poller_registers_bundle_and_rejects_source_sha_mismatch(self):
        archive_bytes = user_archive()
        artifact = user_artifact()
        with (
            patch("apps.instances.poller.list_artifacts_by_suffix", return_value=[artifact]),
            patch("apps.instances.user_files.get_workflow_run", return_value=workflow_run()),
            patch(
                "apps.instances.user_files.download_user_files_bundle",
                return_value=(user_manifest(archive_bytes), archive_bytes),
            ),
        ):
            registered = poll_user_files_once(token="token")

        bad_artifact = user_artifact(artifact_id=2, sha=SHA_B)
        with (
            patch("apps.instances.poller.list_artifacts_by_suffix", return_value=[bad_artifact]),
            patch(
                "apps.instances.user_files.get_workflow_run",
                return_value=workflow_run(artifact_id=2, sha=SHA_B),
            ),
            patch(
                "apps.instances.user_files.download_user_files_bundle",
                return_value=(user_manifest(archive_bytes), archive_bytes),
            ),
        ):
            rejected = poll_user_files_once(token="token")

        self.assertEqual(registered["registered"], 1)
        self.assertEqual(rejected["invalid"], 1)

    def test_processed_artifact_is_not_downloaded_again(self):
        archive_bytes = user_archive()
        artifact = user_artifact()
        with (
            patch(
                "apps.instances.poller.list_artifacts_by_suffix",
                side_effect=[[artifact], []],
            ),
            patch(
                "apps.instances.user_files.get_workflow_run",
                return_value=workflow_run(),
            ) as get_run,
            patch(
                "apps.instances.user_files.download_user_files_bundle",
                return_value=(user_manifest(archive_bytes), archive_bytes),
            ) as download,
        ):
            first = poll_user_files_once(token="token")
            second = poll_user_files_once(token="token")

        self.assertEqual(first["registered"], 1)
        self.assertEqual(
            second,
            {
                "registered": 0,
                "duplicate": 0,
                "unmatched": 0,
                "invalid": 0,
                "error": 0,
            },
        )
        self.assertEqual(get_run.call_count, 1)
        self.assertEqual(download.call_count, 1)
        self.assertIsNotNone(PollerArtifact.objects.get(pk=1).processed_at)

    def test_failed_artifact_is_retried_without_rediscovery(self):
        archive_bytes = user_archive()
        artifact = user_artifact()
        with (
            patch(
                "apps.instances.poller.list_artifacts_by_suffix",
                side_effect=[[artifact], []],
            ),
            patch(
                "apps.instances.user_files.get_workflow_run",
                side_effect=[TimeoutError("연결 실패"), workflow_run()],
            ),
            patch(
                "apps.instances.user_files.download_user_files_bundle",
                return_value=(user_manifest(archive_bytes), archive_bytes),
            ),
        ):
            failed = poll_user_files_once(token="token")
            retried = poll_user_files_once(token="token")

        self.assertEqual(failed["error"], 1)
        self.assertEqual(retried["registered"], 1)
        self.assertIsNotNone(PollerArtifact.objects.get(pk=1).processed_at)

    def test_running_workflow_is_retried_after_success(self):
        for offset, status in enumerate(("queued", "in_progress")):
            with self.subTest(status=status):
                artifact_id = offset + 1
                revision = offset + 1
                sha = SHA_A if offset == 0 else SHA_B
                archive_bytes = user_archive(
                    {"readme.txt": f"revision-{revision}".encode("utf-8")}
                )
                artifact = user_artifact(artifact_id=artifact_id, sha=sha)
                with (
                    patch(
                        "apps.instances.poller.list_artifacts_by_suffix",
                        side_effect=[[artifact], []],
                    ),
                    patch(
                        "apps.instances.user_files.get_workflow_run",
                        side_effect=[
                            workflow_run(
                                artifact_id=artifact_id,
                                sha=sha,
                                status=status,
                                conclusion=None,
                            ),
                            workflow_run(artifact_id=artifact_id, sha=sha),
                        ],
                    ),
                    patch(
                        "apps.instances.user_files.download_user_files_bundle",
                        return_value=(
                            user_manifest(
                                archive_bytes,
                                revision=revision,
                                source_sha=sha,
                            ),
                            archive_bytes,
                        ),
                    ) as download,
                ):
                    pending = poll_user_files_once(token="token")
                    record = PollerArtifact.objects.get(pk=artifact_id)
                    self.assertEqual(pending["registered"], 0)
                    self.assertEqual(pending["invalid"], 0)
                    self.assertEqual(download.call_count, 0)
                    self.assertIsNone(record.processed_at)

                    completed = poll_user_files_once(token="token")

                self.assertEqual(completed["registered"], 1)
                self.assertEqual(download.call_count, 1)
                record.refresh_from_db()
                self.assertIsNotNone(record.processed_at)

    def test_completed_failed_workflow_is_not_retried(self):
        artifact = user_artifact(artifact_id=10)
        with (
            patch(
                "apps.instances.poller.list_artifacts_by_suffix",
                return_value=[artifact],
            ),
            patch(
                "apps.instances.user_files.get_workflow_run",
                return_value=workflow_run(
                    artifact_id=10,
                    status="completed",
                    conclusion="failure",
                ),
            ),
            patch(
                "apps.instances.user_files.download_user_files_bundle"
            ) as download,
        ):
            summary = poll_user_files_once(token="token")

        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["registered"], 0)
        download.assert_not_called()
        self.assertIsNotNone(PollerArtifact.objects.get(pk=10).processed_at)

    @override_settings(RELEASE_POLL_GITHUB_TOKEN="token")
    def test_release_command_runs_user_files_poll(self):
        empty = {"registered": 0, "duplicate": 0, "unmatched": 0, "invalid": 0, "error": 0}
        with (
            patch("apps.instances.management.commands.poll_releases.poll_once", return_value=empty),
            patch(
                "apps.instances.management.commands.poll_releases.poll_user_files_once",
                return_value=empty,
            ) as user_files_poll,
        ):
            call_command("poll_releases", stdout=io.StringIO(), stderr=io.StringIO())

        user_files_poll.assert_called_once_with(token="token")


class ChallengeUserFilesApiTests(UserFilesTestBase):
    def setUp(self):
        super().setUp()
        self.team = Team.objects.create(team_name="file-team")
        self.user = User.objects.create_user(
            login_id="file-user",
            password="pw1234",
            nickname="file-user",
            team=self.team,
        )
        self.cell = Cell.objects.create(
            cell_index=1,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="file-cell",
        )
        self.access = TeamChallengeAccess.objects.create(
            team=self.team,
            challenge=self.challenge,
            source_cell=self.cell,
        )
        self.client = APIClient()
        login = self.client.post(
            "/api/v1/auth/login",
            {"login_id": "file-user", "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['data']['access_token']}"
        )

    def test_detail_lists_current_file_and_download_streams_it(self):
        archive_bytes = user_archive()
        _, bundle = register_user_files_bundle(
            user_manifest(archive_bytes), archive_bytes, user_artifact()
        )

        detail = self.client.get(f"/api/v1/challenges/{self.challenge.challenge_id}")
        file_data = detail.data["data"]["files"][0]
        download = self.client.get(file_data["download_url"])

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(file_data["file_id"], str(bundle.bundle_id))
        self.assertEqual(file_data["file_name"], "user-files.zip")
        self.assertEqual(file_data["file_size"], len(archive_bytes))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Cache-Control"], "private, no-store")
        self.assertEqual(b"".join(download.streaming_content), archive_bytes)

    def test_download_requires_access_and_rejects_hidden_revision(self):
        archive_bytes = user_archive()
        _, bundle = register_user_files_bundle(
            user_manifest(archive_bytes), archive_bytes, user_artifact()
        )
        path = (
            f"/api/v1/challenges/{self.challenge.challenge_id}/files/"
            f"{bundle.bundle_id}/download"
        )
        self.access.delete()
        locked = self.client.get(path)
        self.assertEqual(locked.status_code, 403)
        self.assertEqual(locked.data["code"], "CHALLENGE_LOCKED")

        TeamChallengeAccess.objects.create(
            team=self.team,
            challenge=self.challenge,
            source_cell=self.cell,
        )
        register_user_files_bundle(
            user_manifest(revision=2, source_sha=SHA_B),
            None,
            user_artifact(artifact_id=2, sha=SHA_B),
        )
        hidden = self.client.get(path)
        detail = self.client.get(f"/api/v1/challenges/{self.challenge.challenge_id}")

        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.data["code"], "CHALLENGE_FILE_NOT_FOUND")
        self.assertEqual(detail.data["data"]["files"], [])
