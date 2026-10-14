import hashlib
import tempfile
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.db import IntegrityError, transaction
from django.db.models.deletion import RestrictedError
from django.test import override_settings
from rest_framework.test import APIClient

from apps.board.models import Cell, TeamChallengeAccess
from apps.challenge.models import Challenge
from apps.instances.models import ChallengeRelease, Instance, InstanceStatus, UserFilesMode
from apps.instances.runtime_secrets import create_runtime_secret
from apps.instances.services import SchedulerError
from apps.instances.tests_releases import ReleaseTestBase
from apps.instances.tests_user_files import user_archive, user_artifact, user_manifest
from apps.instances.user_files import current_user_file_bundle, register_user_files_bundle


@override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()], SCHEDULER_API_TOKEN="release-files-local-test")
class ReleaseFilesTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.auth("root")
        self.challenge.challenge_slug = "web-basic"
        self.challenge.save(update_fields=["challenge_slug"])
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = override_settings(MEDIA_ROOT=temporary.name, STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        })
        storage.enable()
        self.addCleanup(storage.disable)
        self.source = self.register_release(1)
        self.url = f"{self.base_url}/{self.source.pk}/files"
        self.files_url = f"/api/v1/admin/challenges/{self.challenge.pk}/files"

    def register_release(self, revision, **kwargs):
        response = self.register(revision=revision, **kwargs)
        self.assertEqual(response.status_code, 200, response.data)
        return ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])

    def bundle(self, revision, present=True):
        archive = user_archive({"README.txt": f"participant version {revision}".encode()}) if present else None
        status, bundle = register_user_files_bundle(user_manifest(archive, revision=revision), archive, user_artifact(revision))
        self.assertEqual(status, "registered")
        return bundle

    def configure(self, mode, bundle=None, key=None, url=None):
        body = {"mode": mode, **({"file_id": str(bundle.pk)} if bundle else {})}
        return self.client.post(url or self.url, body, format="json", HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))

    def current_file(self):
        return current_user_file_bundle(Challenge.objects.get(pk=self.challenge.pk))

    def test_auto_binding_follows_activation_and_rollback_without_using_newest_zip(self):
        first, second = self.bundle(1), self.bundle(2)
        latest = self.register_release(2)
        self.assertEqual(self.activate(self.source.pk).status_code, 200)
        self.assertEqual(self.current_file(), first)
        self.assertEqual(self.activate(latest.pk).status_code, 200)
        self.assertEqual(self.current_file(), second)
        self.assertEqual(self.activate(self.source.pk).status_code, 200)
        self.assertEqual(self.current_file(), first)

    def test_release_preview_can_inspect_inactive_version_without_changing_current(self):
        first, second = self.bundle(1), self.bundle(2)
        latest = self.register_release(2)
        self.activate(self.source.pk)
        data = self.client.get(self.files_url, {"release_id": latest.pk}).data["data"]
        self.assertEqual(data["release_id"], str(latest.pk))
        self.assertFalse(data["is_current"])
        self.assertEqual(data["current_file"]["file_id"], str(second.pk))
        self.assertEqual(data["matching_file"]["file_id"], str(second.pk))
        self.assertEqual(self.current_file(), first)
        self.assertEqual(self.client.get(self.files_url, {"release_id": "bad-id"}).status_code, 400)

    def test_manual_choice_creates_an_immutable_draft_and_preserves_pinned_secrets(self):
        old_secret = create_runtime_secret(self.challenge, "internal_token", "test-first", "root")
        original = self.source.containers.get()
        original.env = {"APP_MODE": "local"}
        original.secret_env = {"INTERNAL_TOKEN": str(old_secret.pk)}
        original.env_rules = {"APP_MODE": {"required": True}}
        original.save()
        create_runtime_secret(self.challenge, "internal_token", "test-second", "root")
        first, second = self.bundle(1), self.bundle(2)
        self.activate(self.source.pk)
        response = self.configure("SELECTED", second)
        self.assertEqual(response.status_code, 200, response.data)
        derived = ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])
        self.assertEqual(derived.derived_from_id, self.source.pk)
        self.assertEqual(derived.version, 2)
        self.assertIsNone(derived.approved_at)
        for field in ("registry_revision", "runtime_type", "architecture", "isolation_profile", "healthcheck", "source_ref",
                      "cpu_millicores", "memory_mib", "ephemeral_storage_mib"):
            self.assertEqual(getattr(derived, field), getattr(self.source, field))
        copied = derived.containers.get()
        for field in ("image_ref", "ports", "env", "secret_env", "env_rules"):
            self.assertEqual(getattr(copied, field), getattr(original, field))
        self.assertEqual(self.current_file(), first)
        self.source.refresh_from_db()
        self.assertEqual(self.source.user_files_mode, "MATCH_REVISION")
        self.assertEqual(response.data["data"]["user_files"]["file"]["file_id"], str(second.pk))
        self.assertFalse(response.data["data"]["user_files"]["matches_image_revision"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn("test-first", response.content.decode())
        self.activate(derived.pk)
        self.assertEqual(self.current_file(), second)
        self.activate(self.source.pk)
        self.assertEqual(self.current_file(), first)

    def test_retry_returns_same_version_and_different_body_reusing_key_conflicts(self):
        bundle = self.bundle(1)
        first = self.configure("SELECTED", bundle, key="same-request")
        again = self.configure("SELECTED", bundle, key="same-request")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), again.json())
        self.assertEqual(self.configure("NONE", key="same-request").status_code, 409)
        self.assertEqual(ChallengeRelease.objects.count(), 2)
        self.assertEqual(self.client.post(self.url, {"mode": "NONE"}, format="json").status_code, 400)
        self.assertEqual(ChallengeRelease.objects.count(), 2)

    def test_failed_container_copy_leaves_no_partial_version_and_can_retry(self):
        with patch("apps.instances.release_file_views.ReleaseContainer.objects.bulk_create", side_effect=RuntimeError("copy failed")):
            response = self.configure("NONE", key="retry-after-failure")
            self.assertEqual(response.status_code, 500)
        self.assertEqual(ChallengeRelease.objects.count(), 1)
        result = self.configure("NONE", key="retry-after-failure")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["data"]["version"], 2)

    def test_pending_none_and_explicitly_disabled_stay_distinct(self):
        self.activate(self.source.pk)
        self.assertEqual(self.client.get(self.files_url).data["data"]["status"], "pending")
        self.bundle(1, present=False)
        self.assertEqual(self.client.get(self.files_url).data["data"]["status"], "none")
        disabled = self.configure("NONE").data["data"]
        self.assertEqual(disabled["user_files"]["status"], "disabled")
        self.activate(disabled["release_id"])
        self.assertIsNone(self.current_file())
        self.assertEqual(self.client.get(self.files_url).data["data"]["status"], "disabled")

    def test_automatic_pending_selection_resolves_when_matching_bundle_arrives(self):
        selected = self.bundle(2)
        manual = self.configure("SELECTED", selected).data["data"]
        auto = self.configure("MATCH_REVISION", url=f"{self.base_url}/{manual['release_id']}/files").data["data"]
        self.assertEqual(auto["user_files"]["status"], "pending")
        self.activate(auto["release_id"])
        self.assertIsNone(self.current_file())
        matching = self.bundle(1)
        self.assertEqual(self.current_file(), matching)
        self.assertEqual(ChallengeRelease.objects.get(pk=manual["release_id"]).user_file_bundle_id, selected.pk)

    def test_file_choice_survives_environment_derivation_and_new_publish_uses_automatic(self):
        bundle = self.bundle(2)
        for mode in ("SELECTED", "NONE"):
            with self.subTest(mode=mode):
                source = self.configure(mode, bundle if mode == "SELECTED" else None).data["data"]
                derived = self.client.post(f"{self.base_url}/{source['release_id']}/derive", {
                    "containers": [{"name": "app", "env": {"APP_MODE": "changed"}, "secret_env": {}}],
                }, format="json")
                self.assertEqual(derived.status_code, 200, derived.data)
                self.assertEqual(derived.data["data"]["user_files"], source["user_files"])
                preview_url = f"{self.base_url}/{source['release_id']}/environment_files"
                preview = self.client.post(preview_url + "/preview", {"container": "app", "content": "PORT=8080"}, format="json")
                self.assertEqual(preview.status_code, 200, preview.data)
                entries = [{key: row[key] for key in ("name", "kind", "required", "secret_name", "use_existing", "secret_id") if key in row}
                           for row in preview.data["data"]["entries"]]
                imported = self.client.post(preview_url + "/import", {"container": "app", "content": "PORT=8080",
                    "entries": entries, "request_id": str(uuid.uuid4())}, format="json")
                self.assertEqual(imported.status_code, 200, imported.data)
                self.assertEqual(imported.data["data"]["user_files"], source["user_files"])
        published = self.register_release(3)
        self.assertEqual(published.user_files_mode, UserFilesMode.MATCH_REVISION)
        self.assertIsNone(published.user_file_bundle_id)

    def test_old_running_instance_keeps_its_zip_until_stopped(self):
        first, second = self.bundle(1), self.bundle(2)
        cell = Cell.objects.create(cell_index=1, type="CHALLENGE", difficulty="EASY", name="files")
        TeamChallengeAccess.objects.create(team=self.team, challenge=self.challenge, source_cell=cell)
        instance = Instance.objects.create(user=self.player, team=self.team, challenge=self.challenge,
                                          release=self.source, status=InstanceStatus.RUNNING)
        self.activate(self.register_release(2).pk)
        self.auth("player")
        detail_url = f"/api/v1/challenges/{self.challenge.pk}"
        with patch("apps.challenge.views.sync_instance_from_scheduler", return_value=instance):
            detail = self.client.get(detail_url)
            self.assertEqual(detail.status_code, 200, detail.data)
            self.assertEqual(detail.data["data"]["files"][0]["file_id"], str(first.pk))
            downloaded = self.client.get(detail.data["data"]["files"][0]["download_url"])
            self.assertEqual(self.client.get(f"{detail_url}/files/{second.pk}/download").status_code, 404)
        self.assertEqual(hashlib.sha256(b"".join(downloaded.streaming_content)).hexdigest(), first.sha256)
        instance.status = InstanceStatus.STOPPED
        instance.save(update_fields=["status"])
        self.assertEqual(self.client.get(detail_url).data["data"]["files"][0]["file_id"], str(second.pk))
        self.assertEqual(self.client.get(f"{detail_url}/files/{first.pk}/download").status_code, 404)

    def test_removed_or_ended_instance_uses_current_zip_in_detail_and_download(self):
        first, second = self.bundle(1), self.bundle(2)
        cell = Cell.objects.create(cell_index=1, type="CHALLENGE", difficulty="EASY", name="files")
        TeamChallengeAccess.objects.create(team=self.team, challenge=self.challenge, source_cell=cell)
        instance = Instance.objects.create(user=self.player, team=self.team, challenge=self.challenge,
                                          release=self.source, status=InstanceStatus.RUNNING)
        self.activate(self.register_release(2).pk)
        self.auth("player")
        url = f"/api/v1/challenges/{self.challenge.pk}"
        instance.status = InstanceStatus.EXPIRED
        for sync_result in ({"return_value": instance}, {"side_effect": SchedulerError("INSTANCE_NOT_FOUND", "gone", 404)}):
            with self.subTest(result=sync_result), patch("apps.challenge.views.sync_instance_from_scheduler", **sync_result):
                detail = self.client.get(url)
                self.assertEqual(detail.data["data"]["files"][0]["file_id"], str(second.pk))
                self.assertIsNone(detail.data["data"]["instance"])
                downloaded = self.client.get(detail.data["data"]["files"][0]["download_url"])
                self.assertEqual(downloaded.status_code, 200)
                self.assertEqual(hashlib.sha256(b"".join(downloaded.streaming_content)).hexdigest(), second.sha256)
                self.assertEqual(self.client.get(f"{url}/files/{first.pk}/download").status_code, 404)

    def test_files_and_release_are_scoped_to_the_challenge_and_admin(self):
        bundle = self.bundle(1)
        other = Challenge.objects.create(title="Other", category="WEB", difficulty="EASY", score=100, flag_hash="unused")
        foreign = ChallengeRelease.objects.create(challenge=other, version=1, registry_revision=1,
            cpu_millicores=500, memory_mib=512, ephemeral_storage_mib=1024, isolation_profile="WEB")
        self.assertEqual(self.client.get(self.files_url, {"release_id": foreign.pk}).status_code, 404)
        self.assertEqual(self.configure("NONE", url=f"{self.base_url}/{foreign.pk}/files").status_code, 404)
        bundle.challenge = other
        bundle.save(update_fields=["challenge"])
        self.assertEqual(self.configure("SELECTED", bundle).status_code, 404)
        self.auth("player")
        self.assertEqual(self.configure("NONE").status_code, 403)
        self.assertEqual(self.client.get(self.files_url, {"release_id": self.source.pk}).status_code, 403)
        self.assertEqual(APIClient().post(self.url, {"mode": "NONE"}, format="json").status_code, 401)

    def test_invalid_selection_creates_no_version(self):
        absent = self.bundle(1, present=False)
        self.assertEqual(self.configure("SELECTED", absent).status_code, 404)
        for body in ({}, [], {"mode": "unknown"}, {"mode": "SELECTED"}, {"mode": "SELECTED", "file_id": "not-an-id"},
                     {"mode": "NONE", "file_id": None}, {"mode": "NONE", "image": "new-image"},
                     {"mode": "SELECTED", "fileId": str(absent.pk)}):
            response = self.client.post(self.url, body, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ChallengeRelease.objects.count(), 1)

    def test_pagination_keeps_selected_and_matching_files_available(self):
        for revision in range(1, 24):
            self.bundle(revision)
        first = self.client.get(self.files_url, {"release_id": self.source.pk}).data["data"]
        second = self.client.get(self.files_url, {"release_id": self.source.pk, "page": first["history_next_page"]}).data["data"]
        self.assertEqual(len(first["history"]), 20)
        self.assertEqual(len(second["history"]), 2)
        ids = [row["file_id"] for row in first["history"] + second["history"]]
        self.assertEqual(len(set(ids)), 22)
        self.assertNotIn(first["current_file"]["file_id"], ids)
        self.assertEqual(first["matching_file"], second["matching_file"])
        self.assertIsNone(second["history_next_page"])
        self.assertEqual(self.client.get(self.files_url, {"page": "0"}).status_code, 400)

    def test_selection_constraint_and_bundle_deletion_preserve_historical_reference(self):
        bundle = self.bundle(1)
        self.configure("SELECTED", bundle)
        with self.assertRaises(RestrictedError):
            bundle.delete()
        for fields in ({"user_files_mode": "SELECTED"}, {"user_files_mode": "NONE", "user_file_bundle": bundle}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                ChallengeRelease.objects.filter(pk=self.source.pk).update(**fields)
        # 사용 인스턴스가 없는 문제 전체 삭제는 기존 동작을 유지한다
        self.challenge.delete()
        self.assertFalse(ChallengeRelease.objects.exists())
