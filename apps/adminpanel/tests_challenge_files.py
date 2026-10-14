import hashlib
import json
import tempfile
from unittest.mock import patch

from django.test import override_settings
from google.api_core.exceptions import Forbidden
from rest_framework.test import APIClient

from apps.adminpanel.challenge_deployment import deployment_summaries
from apps.challenge.models import Challenge
from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig
from apps.instances.tests_releases import ReleaseTestBase
from apps.instances.tests_user_files import user_archive, user_artifact, user_manifest
from apps.instances.user_files import register_user_files_bundle


class AdminChallengeFilesTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.auth("root")
        self.challenge.challenge_slug = "web-basic"
        self.challenge.save(update_fields=["challenge_slug"])
        self.url = f"/api/v1/admin/challenges/{self.challenge.pk}/files"
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage = override_settings(MEDIA_ROOT=self.temporary.name, STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        })
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        self.archive = user_archive({"README.txt": b"participant attachment"})

    def bundle(self, revision=1, present=True):
        status, bundle = register_user_files_bundle(
            user_manifest(self.archive if present else None, revision=revision),
            self.archive if present else None, user_artifact(artifact_id=revision),
        )
        self.assertEqual(status, "registered")
        return bundle

    def select(self, revision):
        release = self.create_legacy_release()
        release.registry_revision = revision
        release.save(update_fields=["registry_revision"])
        return ChallengeRuntimeConfig.objects.create(challenge=self.challenge, current_release=release)

    def test_static_file_metadata_and_admin_without_team_can_download(self):
        bundle = self.bundle()
        response = self.client.get(self.url)
        self.assertEqual(response.data["data"]["status"], "available")
        self.assertEqual(response.data["data"]["current_file"]["file_id"], str(bundle.pk))
        self.assertEqual(response.data["data"]["current_file"]["file_count"], 1)
        self.assertEqual(response.data["data"]["storage"]["provider"], "local")
        self.assertNotIn("object_key", response.content.decode())
        self.assertIn("no-store", response["Cache-Control"])
        downloaded = self.client.get(f"{self.url}/{bundle.pk}/download")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded["Content-Type"], "application/zip")
        self.assertIn("attachment", downloaded["Content-Disposition"])
        self.assertIn("no-store", downloaded["Cache-Control"])
        self.assertEqual(hashlib.sha256(b"".join(downloaded.streaming_content)).hexdigest(), bundle.sha256)
        self.assertTrue(downloaded.closed)

    def test_current_release_and_later_history_are_distinct(self):
        old, latest = self.bundle(1), self.bundle(2)
        self.select(1)
        result = self.client.get(self.url).data["data"]
        self.assertEqual(result["registry_revision"], 1)
        self.assertEqual(result["release_version"], 1)
        self.assertEqual(result["current_file"]["file_id"], str(old.pk))
        self.assertEqual([file["file_id"] for file in result["history"]], [str(latest.pk)])
        # 운영자는 아직 사용하지 않는 파일도 검수할 수 있다
        downloaded = self.client.get(f"{self.url}/{latest.pk}/download")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(hashlib.sha256(b"".join(downloaded.streaming_content)).hexdigest(), latest.sha256)
        self.assertTrue(downloaded.closed)

    def test_no_attachment_is_distinct_from_pending_collection_or_no_selected_release(self):
        config = self.select(1)
        self.assertEqual(self.client.get(self.url).data["data"]["status"], "pending")
        self.bundle(1, present=False)
        self.assertEqual(self.client.get(self.url).data["data"]["status"], "none")
        config.current_release = None
        config.save(update_fields=["current_release"])
        result = self.client.get(self.url).data["data"]
        self.assertEqual(result["status"], "no_release")
        self.assertIsNone(result["current_file"])

    def test_other_challenge_file_is_not_returned(self):
        bundle = self.bundle()
        other = Challenge.objects.create(title="Other", category="WEB", difficulty="EASY", score=100, flag_hash="unused")
        response = self.client.get(f"/api/v1/admin/challenges/{other.pk}/files/{bundle.pk}/download")
        self.assertEqual(response.status_code, 404)

    def test_read_permissions_and_storage_errors(self):
        bundle = self.bundle()
        url = f"{self.url}/{bundle.pk}/download"
        with patch("apps.adminpanel.challenge_files.default_storage.exists", side_effect=Forbidden("private credential detail")):
            failure = self.client.get(url)
        self.assertEqual(failure.status_code, 503)
        self.assertNotIn("credential", json.dumps(failure.data))
        with patch("apps.adminpanel.challenge_files.default_storage.exists", return_value=False):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.auth("player")
        for route in (self.url, url):
            self.assertEqual(self.client.get(route).status_code, 403)
            self.assertEqual(APIClient().get(route).status_code, 401)

    def test_gcs_configuration_is_reported_without_network_request(self):
        self.bundle()
        with override_settings(STORAGES={"default": {"BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
                                                     "OPTIONS": {"bucket_name": "test-bucket", "location": "dev"}}}):
            with patch("apps.adminpanel.challenge_files.default_storage.exists") as exists:
                response = self.client.get(self.url)
        exists.assert_not_called()
        self.assertEqual(response.data["data"]["storage"], {"provider": "gcs", "bucket": "test-bucket"})


class AdminDeploymentSummaryTests(ReleaseTestBase):
    def test_current_version_takes_precedence_and_values_stay_out_of_summary(self):
        self.auth("root")
        current = self.create_legacy_release()
        container = current.containers.get()
        container.env = {"GREETING": "private-setting-value"}
        container.secret_env = {"FLAG": "private-secret-reference"}
        container.save(update_fields=["env", "secret_env"])
        ChallengeRuntimeConfig.objects.create(challenge=self.challenge, current_release=current)
        ChallengeRelease.objects.create(challenge=self.challenge, version=2, registry_revision=2,
                                        cpu_millicores=999, memory_mib=999, ephemeral_storage_mib=999,
                                        isolation_profile="WEB")
        response = self.client.get("/api/v1/admin/challenges")
        summary = response.data["data"]["challenges"][0]["deployment"]
        self.assertEqual(summary["version"], 1)
        self.assertTrue(summary["is_current"])
        self.assertEqual(summary["resource_profile"]["cpu_millicores"], 500)
        self.assertEqual(summary["containers"][0]["env_count"], 1)
        self.assertEqual(summary["containers"][0]["secret_count"], 1)
        self.assertNotIn("private-", json.dumps(response.data))
        self.assertIn("no-store", response["Cache-Control"])

    def test_draft_and_empty_summary_and_query_count(self):
        self.create_legacy_release()
        other = Challenge.objects.create(title="Other", category="MISC", difficulty="EASY", score=100, flag_hash="unused")
        with self.assertNumQueries(3):
            summaries = deployment_summaries([self.challenge, other])
        self.assertFalse(summaries[self.challenge.pk]["is_current"])
        self.assertIsNone(summaries[other.pk])
