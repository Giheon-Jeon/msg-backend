import base64
import copy
import io
import json
import tempfile
import zipfile
from urllib.error import HTTPError
from unittest.mock import patch

import yaml
from cryptography.fernet import Fernet
from django.core import signing
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from google.api_core.exceptions import ServiceUnavailable
from google.auth.exceptions import RefreshError

from apps.challenge.models import Challenge
from apps.challenge.services import hash_flag, is_correct_flag
from apps.common.models import IdempotencyRecord
from apps.instances import published_challenges as publications
from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig, ChallengeUserFileBundle, PollerArtifact, PublicationRequest, RuntimeSecret
from apps.instances.runtime_secrets import create_runtime_secret, decrypt_runtime_secret
from apps.instances.tests_releases import ReleaseTestBase, artifact_payload
from apps.instances.tests_repository_environment import MemorySnapshot
from apps.instances.tests_user_files import user_archive, user_manifest

BASE = "/api/v1/admin/published_challenges"
SLUG = "web-imported"
SHA = "c" * 40


def artifact(artifact_id, kind):
    return {"id": artifact_id, "name": f"{SLUG}-88-1-abcdef-{kind}-bundle", "expired": False,
            "created_at": "2026-10-14T01:00:00Z",
            "workflow_run": {"id": 88, "head_branch": "main", "head_sha": SHA}}


def packed(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


class PublishedChallengeTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.config = override_settings(
            MEDIA_ROOT=self.media.name, RELEASE_POLL_REPO="MSG-CTF/2026_MSG_CTF",
            RELEASE_POLL_SOURCE_REF="refs/heads/main", REPOSITORY_ENV_EXCLUDED_SLUGS={"pwn-msgdriver"},
            RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()],
            STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}},
        )
        self.config.enable()
        self.addCleanup(self.config.disable)
        self.artifacts = [artifact(11, "publish"), artifact(12, "user-files")]
        self.run = {"id": 88, "head_branch": "main", "head_sha": SHA, "status": "completed", "conclusion": "success"}
        self.metadata = {"name": "저장소 문제", "description": "출제자가 작성한 지문", "category": "web",
                         "flag": "MSG{source-flag}", "deployment": {"runtime_type": "KUBERNETES"}}
        self.release = artifact_payload(slug=SLUG, source_sha=SHA, schema_version="2.1")["artifact"]
        self.release["workload"]["containers"][0].update(env={"SETTING": "private-source-env"}, secret_env={"FLAG": "flag"})
        self.archive = user_archive()
        self.manifest = user_manifest(self.archive, slug=SLUG, source_sha=SHA)
        self.calls = []
        for target in ("apps.instances.poller.github_request", "apps.instances.user_files.github_request"):
            mocked = patch(target, side_effect=self.github)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.auth("root")

    def github(self, path, **kwargs):
        self.calls.append(path)
        if "/contents/" in path:
            self.assertIn(f"/{SLUG}/info.yaml?ref={SHA}", path)
            content = yaml.safe_dump(self.metadata).encode()
            value = {"size": len(content), "encoding": "base64", "content": base64.b64encode(content).decode()}
        elif any(path.endswith(f"/{item['id']}/zip") and item["name"].endswith("-publish-bundle") for item in self.artifacts):
            return packed({"artifact-v2.json": json.dumps(self.release)})
        elif any(path.endswith(f"/{item['id']}/zip") and item["name"].endswith("-user-files-bundle") for item in self.artifacts):
            files = {"user-files.json": json.dumps(self.manifest)}
            if self.archive is not None:
                files["user-files.zip"] = self.archive
            return packed(files)
        elif "artifacts?" in path:
            value = {"artifacts": self.artifacts, "total_count": len(self.artifacts)}
        elif "/actions/runs?" in path:
            value = {"workflow_runs": [self.run]}
        elif path.endswith("/runs/88"):
            value = self.run
        else:
            value = next(item for item in self.artifacts if path.endswith(f"/{item['id']}"))
        return json.dumps(value).encode()

    def preview(self, **extra):
        return self.client.post(BASE + "/preview", {"artifact_id": self.artifacts[0]["id"], **extra}, format="json")

    def body(self, **extra):
        response = self.preview(**extra)
        self.assertEqual(response.status_code, 200, response.data)
        return {"preview_token": response.data["data"]["preview_token"], "difficulty": "MEDIUM"}

    def import_body(self, body, key="publication-test"):
        return self.client.post(BASE + "/import", body, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def make_static(self):
        self.artifacts = self.artifacts[1:]
        del self.metadata["deployment"]

    def test_participant_and_anonymous_cannot_list_preview_or_import(self):
        for login, expected in (("player", 403), (None, 401)):
            if login:
                self.auth(login)
            else:
                self.client.credentials()
            self.assertEqual(self.client.get(BASE).status_code, expected)
            self.assertEqual(self.preview().status_code, expected)
            self.assertEqual(self.import_body({}).status_code, expected)
        self.assertEqual(self.calls, [])

    def test_catalog_groups_pairs_and_does_not_download_flags_or_zips(self):
        bad = copy.deepcopy(self.artifacts[0])
        bad["workflow_run"]["head_branch"] = "feature"
        expired = dict(self.artifacts[0], expired=True)
        windows = dict(self.artifacts[0], name="pwn-msgdriver-88-1-abcdef-publish-bundle")
        self.artifacts.extend([bad, expired, windows, {"id": 9, "name": None}, {"id": 8, "name": "a", "workflow_run": []}])
        response = self.client.get(BASE)
        self.assertEqual(response.status_code, 200)
        row, = response.data["data"]["items"]
        self.assertTrue(row["has_release_artifact"] and row["has_files_artifact"])
        self.assertEqual(row["challenge_slug"], SLUG)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(row["workflow_state"], "success")
        self.assertIn("no-store", response["Cache-Control"])

    def test_preview_hides_values_and_does_not_write_anything(self):
        response = self.preview()
        self.assertEqual(response.status_code, 200, response.data)
        encoded = json.dumps(response.data)
        self.assertNotIn(self.metadata["flag"], encoded)
        self.assertNotIn("private-source-env", encoded)
        self.assertEqual(response.data["data"]["files"]["status"], "available")
        self.assertFalse(Challenge.objects.filter(challenge_slug=SLUG).exists())
        self.assertEqual(ChallengeRelease.objects.count(), 0)

    def test_import_creates_private_challenge_release_secret_and_file_without_activating(self):
        response = self.import_body(self.body())
        self.assertEqual(response.status_code, 200, response.data)
        challenge = Challenge.objects.get(challenge_slug=SLUG)
        self.assertFalse(challenge.is_published)
        self.assertEqual(challenge.minimum_score, 100)
        self.assertEqual(challenge.initial_score, 1000)
        self.assertEqual(challenge.difficulty, "MEDIUM")
        self.assertEqual(challenge.title, self.metadata["name"])
        self.assertTrue(is_correct_flag(self.metadata["flag"], challenge.flag_hash))
        self.assertIsNone(challenge.runtime_config.current_release_id)
        release = challenge.releases.get()
        self.assertEqual(release.created_by, self.admin.login_id)
        self.assertEqual(challenge.user_file_bundles.get().release_id, release.pk)
        secret = RuntimeSecret.objects.get(challenge=challenge, name="flag")
        self.assertEqual(decrypt_runtime_secret(secret), self.metadata["flag"])
        self.assertNotIn(self.metadata["flag"], secret.encrypted_value)
        self.assertEqual(release.containers.get().secret_env, {"FLAG": str(secret.pk)})

    def test_first_import_requeues_only_this_problems_earlier_publications(self):
        for item, kind in zip(self.artifacts, (PollerArtifact.Kind.RELEASE, PollerArtifact.Kind.USER_FILES)):
            PollerArtifact.objects.create(artifact_id=item["id"], kind=kind, payload=item, processed_at=timezone.now())
        other = artifact(13, "publish")
        other["name"] = "web-imported-extra-88-1-abcdef-publish-bundle"
        PollerArtifact.objects.create(artifact_id=13, kind=PollerArtifact.Kind.RELEASE, payload=other, processed_at=timezone.now())

        response = self.import_body(self.body())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(PollerArtifact.objects.get(pk=11).processed_at)
        self.assertIsNone(PollerArtifact.objects.get(pk=12).processed_at)
        self.assertIsNotNone(PollerArtifact.objects.get(pk=13).processed_at)

    def test_existing_metadata_visibility_and_active_release_are_preserved(self):
        first = self.import_body(self.body()).data["data"]
        challenge = Challenge.objects.get(pk=first["challenge_id"])
        challenge.title, challenge.description, challenge.is_published = "운영 중인 제목", "관리자 지문", True
        challenge.save()
        config = challenge.runtime_config
        config.current_release_id = first["release_id"]
        config.save()
        self.release.update(revision=2, registry_revision=2)
        self.manifest["registry_revision"] = 2
        self.artifacts = [dict(item, id=item["id"] + 100) for item in self.artifacts]
        second = self.import_body(self.body(), "next")
        self.assertEqual(second.status_code, 200, second.data)
        challenge.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(challenge.title, "운영 중인 제목")
        self.assertEqual(challenge.description, "관리자 지문")
        self.assertTrue(challenge.is_published)
        self.assertEqual(str(config.current_release_id), first["release_id"])
        self.assertEqual(challenge.releases.count(), 2)
        self.assertEqual(challenge.user_file_bundles.count(), 2)

    def test_flag_conflict_is_visible_and_blocks_import(self):
        self.import_body(self.body())
        challenge = Challenge.objects.get(challenge_slug=SLUG)
        challenge.flag_hash = hash_flag("MSG{changed}")
        challenge.save(update_fields=["flag_hash"])
        preview = self.preview()
        self.assertEqual(preview.data["data"]["flag_status"], "mismatch")
        self.assertFalse(preview.data["data"]["can_import"])
        response = self.import_body(self.body(), "conflict")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(RuntimeSecret.objects.filter(challenge__challenge_slug=SLUG).count(), 1)

    def test_operator_flag_is_preserved_and_used_by_a_new_publication(self):
        first = self.import_body(self.body()).data["data"]
        challenge = Challenge.objects.get(pk=first["challenge_id"])
        previous = challenge.releases.get().containers.get().secret_env.copy()
        challenge.flag_hash = hash_flag("MSG{operator-selected}")
        challenge.save(update_fields=["flag_hash"])
        secret = create_runtime_secret(challenge, "flag", "MSG{operator-selected}", self.admin.login_id)
        self.release.update(revision=2, registry_revision=2)
        self.manifest["registry_revision"] = 2
        self.artifacts = [dict(item, id=item["id"] + 100) for item in self.artifacts]
        preview = self.preview().data["data"]
        self.assertEqual(preview["flag_status"], "mismatch")
        self.assertTrue(preview["can_import"])
        response = self.import_body(self.body(), "operator-flag")
        self.assertEqual(response.status_code, 200, response.data)
        latest = challenge.releases.get(registry_revision=2)
        self.assertEqual(latest.containers.get().secret_env, {"FLAG": str(secret.pk)})
        self.assertEqual(challenge.releases.get(registry_revision=1).containers.get().secret_env, previous)
        challenge.refresh_from_db()
        self.assertTrue(is_correct_flag("MSG{operator-selected}", challenge.flag_hash))
        self.assertNotIn("operator-selected", json.dumps(response.data))

    def test_metadata_refresh_is_explicit_and_preserves_operator_settings_and_history(self):
        first = self.import_body(self.body()).data["data"]
        challenge = Challenge.objects.get(pk=first["challenge_id"])
        challenge.title, challenge.description = "관리자 제목", "관리자 지문"
        challenge.minimum_score, challenge.difficulty, challenge.is_published = 250, "HARD", True
        challenge.save()
        config = challenge.runtime_config
        config.current_release_id = first["release_id"]
        config.save()
        previous_file = challenge.user_file_bundles.get().sha256
        preview = self.preview().data["data"]
        self.assertEqual(preview["metadata_changes"], ["title", "description"])
        body = {"preview_token": preview["preview_token"], "difficulty": "UNASSIGNED", "update_metadata": True}
        response = self.import_body(body, "refresh")
        self.assertEqual(response.status_code, 200, response.data)
        challenge.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual((challenge.title, challenge.description), (self.metadata["name"], self.metadata["description"]))
        self.assertEqual(challenge.minimum_score, 250)
        self.assertEqual(challenge.difficulty, "HARD")
        self.assertTrue(challenge.is_published)
        self.assertEqual(str(config.current_release_id), first["release_id"])
        self.assertEqual(challenge.releases.count(), 1)
        self.assertEqual(challenge.user_file_bundles.get().sha256, previous_file)
        self.assertEqual(response.data["data"]["metadata_updated"], ["title", "description"])
        self.assertEqual(self.import_body(body, "refresh").json(), response.json())

    def test_preview_cannot_overwrite_a_later_admin_edit(self):
        self.import_body(self.body())
        body = {**self.body(), "update_metadata": True}
        challenge = Challenge.objects.get(challenge_slug=SLUG)
        challenge.description = "미리보기 이후 다른 관리자가 저장한 지문"
        challenge.save(update_fields=["description"])
        response = self.import_body(body, "stale-metadata")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "PUBLICATION_CHALLENGE_CHANGED")
        challenge.refresh_from_db()
        self.assertEqual(challenge.description, "미리보기 이후 다른 관리자가 저장한 지문")

    def test_unassigned_challenge_is_a_private_draft_until_difficulty_is_selected(self):
        preview = self.preview().data["data"]
        self.assertEqual(preview["defaults"]["difficulty"], "UNASSIGNED")
        self.assertEqual(preview["defaults"]["minimum_score"], 100)
        response = self.import_body({"preview_token": preview["preview_token"], "difficulty": "UNASSIGNED"})
        self.assertEqual(response.status_code, 200, response.data)
        path = "/api/v1/admin/challenges/" + response.data["data"]["challenge_id"]
        rejected = self.client.patch(path + "/visibility", {"is_published": True, "reason": "공개 검증"}, format="json")
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.data["code"], "CHALLENGE_DIFFICULTY_REQUIRED")
        self.assertEqual(self.client.patch(path, {"difficulty": "EASY"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(path + "/visibility", {"is_published": True, "reason": "공개 검증"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(path, {"difficulty": "UNASSIGNED"}, format="json").status_code, 400)

    def test_metadata_option_requires_a_boolean_before_network_access(self):
        body = self.body()
        self.calls.clear()
        for value in ("false", 1, None):
            self.assertEqual(self.import_body({**body, "update_metadata": value}).status_code, 400)
        self.assertEqual(self.calls, [])

    def test_identical_retry_returns_same_response_without_github_even_after_preview_expiry(self):
        body = self.body()
        first = self.import_body(body)
        self.calls.clear()
        with patch("django.core.signing.TimestampSigner.unsign", side_effect=signing.SignatureExpired()):
            again = self.import_body(body)
        self.assertEqual(first.json(), again.json())
        self.assertEqual(self.calls, [])
        self.assertEqual(ChallengeRelease.objects.count(), 1)
        self.assertEqual(self.import_body(dict(body, difficulty="HARD")).status_code, 409)

    def test_another_key_still_does_not_duplicate_import(self):
        body = self.body()
        first = self.import_body(body)
        again = self.import_body(body, "another-key")
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(first.data["data"]["release_id"], again.data["data"]["release_id"])
        self.assertEqual(ChallengeUserFileBundle.objects.count(), 1)

    def test_same_revision_with_different_image_is_rejected(self):
        self.import_body(self.body())
        self.release["workload"]["containers"][0]["image"] = f"ghcr.io/msg-ctf/challenges/{SLUG}/app@sha256:{'b'*64}"
        response = self.import_body(self.body(), "different-image")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ChallengeRelease.objects.count(), 1)

    def test_static_file_and_explicit_no_file_imports(self):
        self.make_static()
        response = self.import_body(self.body())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["data"]["release_id"])
        self.assertFalse(ChallengeRuntimeConfig.objects.filter(challenge__challenge_slug=SLUG).exists())
        self.archive = None
        self.manifest = user_manifest(None, slug=SLUG, source_sha=SHA, revision=2)
        self.artifacts[0]["id"] += 100
        self.assertEqual(self.preview().data["data"]["files"]["status"], "none")
        self.assertEqual(self.import_body(self.body(), "no-file").status_code, 200)
        self.assertFalse(ChallengeUserFileBundle.objects.get(registry_revision=2).present)

    def test_artifact_already_assigned_to_another_revision_is_not_reported_as_success(self):
        self.make_static()
        self.import_body(self.body())
        self.manifest["registry_revision"] = 2
        self.assertEqual(self.import_body(self.body(), "reused-artifact").status_code, 409)
        self.assertEqual(ChallengeUserFileBundle.objects.count(), 1)

    def test_static_publication_cannot_replace_runtime_challenge(self):
        self.import_body(self.body())
        self.make_static()
        self.assertFalse(self.preview().data["data"]["can_import"])
        self.assertEqual(self.import_body(self.body(), "static-conflict").status_code, 409)

    def test_source_pair_and_revision_mismatches_fail_before_write(self):
        for field, bad in (("source_sha", "d" * 40), ("registry_revision", 2), ("challenge_id", str(self.challenge.pk))):
            with self.subTest(field=field):
                original = copy.deepcopy(self.manifest)
                self.manifest[field] = bad
                self.assertEqual(self.preview().status_code, 400)
                self.manifest = original
        for field, bad in (("source_sha", "e" * 40), ("source_ref", "refs/heads/feature"), ("challenge_id", str(self.challenge.pk))):
            with self.subTest(field=field):
                original = copy.deepcopy(self.release)
                self.release[field] = bad
                self.assertEqual(self.preview().status_code, 400)
                self.release = original
        self.assertFalse(Challenge.objects.filter(challenge_slug=SLUG).exists())

    def test_failed_workflow_and_incomplete_dynamic_publication_are_rejected(self):
        self.run["conclusion"] = "failure"
        self.assertEqual(self.preview().status_code, 409)
        self.assertEqual(self.client.get(BASE).data["data"]["items"][0]["workflow_state"], "failed")
        self.run["conclusion"] = "success"
        self.artifacts = self.artifacts[1:]
        self.assertEqual(self.preview().status_code, 400)

    def test_tampered_expired_or_other_user_preview_is_rejected(self):
        body = self.body()
        self.assertEqual(self.import_body(dict(body, preview_token=body["preview_token"] + "x")).status_code, 409)
        with patch("django.core.signing.TimestampSigner.unsign", side_effect=signing.SignatureExpired()):
            self.assertEqual(self.import_body(body).status_code, 409)
        payload = signing.loads(body["preview_token"], salt=publications.SALT)
        payload["user_id"] = "someone-else"
        body["preview_token"] = signing.dumps(payload, salt=publications.SALT)
        self.assertEqual(self.import_body(body).status_code, 409)

    def test_changed_artifact_after_preview_is_rejected(self):
        body = self.body()
        self.metadata["description"] = "수정된 발행 내용"
        self.assertEqual(self.import_body(body).status_code, 409)
        self.assertFalse(Challenge.objects.filter(challenge_slug=SLUG).exists())

    def test_storage_outage_rolls_back_and_same_key_can_retry(self):
        body = self.body()
        for error in (ServiceUnavailable, RefreshError, OSError):
            with self.subTest(error=error), patch("apps.instances.user_files.default_storage.save", side_effect=error("sensitive-test-detail")):
                response = self.import_body(body)
                self.assertEqual(response.status_code, 503, response.data)
                self.assertNotIn("sensitive-test-detail", json.dumps(response.data))
                self.assertFalse(Challenge.objects.filter(challenge_slug=SLUG).exists())
                self.assertFalse(IdempotencyRecord.objects.exists())
        self.assertEqual(self.import_body(body).status_code, 200)

    def test_missing_secret_encryption_rolls_back_then_recovers(self):
        body = self.body()
        with override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[]):
            self.assertEqual(self.import_body(body).status_code, 503)
        self.assertFalse(Challenge.objects.filter(challenge_slug=SLUG).exists())
        self.assertEqual(self.import_body(body).status_code, 200)

    def test_invalid_requests_never_reach_github(self):
        for body in ({"artifact_id": True}, {"artifact_id": "11"}, {"artifact_id": 11, "url": "http://private"}):
            self.assertEqual(self.client.post(BASE + "/preview", body, format="json").status_code, 400)
        self.assertEqual(self.client.get(BASE + "?page=-1").status_code, 400)
        self.assertEqual(self.calls, [])
        body = self.body()
        self.calls.clear()
        self.assertEqual(self.client.post(BASE + "/import", body, format="json").status_code, 400)
        self.assertEqual(self.calls, [])

    def test_github_error_is_retryable_and_does_not_expose_source_details(self):
        with patch("apps.instances.poller.github_request", side_effect=HTTPError("secret-url", 403, "secret-message", {}, None)):
            response = self.preview()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret-", json.dumps(response.data))

    def test_zip_traversal_is_rejected_before_saving(self):
        self.archive = user_archive({"../flag.txt": b"unsafe"})
        self.manifest = user_manifest(self.archive, slug=SLUG, source_sha=SHA)
        self.assertEqual(self.preview().status_code, 400)
        self.assertEqual(ChallengeUserFileBundle.objects.count(), 0)

    def test_oversized_nested_zip_is_rejected_before_read(self):
        with patch("apps.instances.user_files.MAX_ARTIFACT_SIZE", 4):
            self.assertEqual(self.preview().status_code, 400)

    def test_bundle_without_env_takes_the_env_file_of_the_same_commit(self):
        self.release = artifact_payload(slug=SLUG, source_sha=SHA)["artifact"]
        files = {"info.yaml": "deployment:\n  runtime_type: KUBERNETES\n  containers:\n    - name: app\n      build: ./app\n",
                 "env/app.env": "PORT=8080\nFLAG=" + self.metadata["flag"] + "\n"}
        reads = []

        def snapshot(slug, revision=None):
            reads.append((slug, revision))
            return MemorySnapshot(slug, revision, files)

        with override_settings(RELEASE_POLL_GITHUB_TOKEN="test-token"), \
                patch("apps.instances.bundle_environment.RepositorySnapshot", side_effect=snapshot):
            preview = self.preview()
            self.assertEqual(preview.status_code, 200, preview.data)
            self.assertEqual(preview.data["data"]["containers"][0]["env_file"], "env/app.env")
            self.assertEqual((preview.data["data"]["containers"][0]["env_count"], preview.data["data"]["containers"][0]["secret_count"]), (1, 1))
            response = self.import_body(self.body())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(reads), {(SLUG, SHA)})
        release = Challenge.objects.get(challenge_slug=SLUG).releases.get()
        container = release.containers.get()
        flag = RuntimeSecret.objects.get(challenge=release.challenge, name="flag")
        self.assertEqual((container.env, container.secret_env), ({"PORT": "8080"}, {"FLAG": str(flag.pk)}))
        self.assertEqual(release.repository_env, {"revision": SHA, "files": ["env/app.env"], "missing": []})
        self.assertEqual(RuntimeSecret.objects.filter(challenge=release.challenge).count(), 1)

    def linked_request(self):
        challenge = Challenge.objects.create(
            challenge_slug=SLUG, title="Linked publication", category="WEB", difficulty="EASY",
            score=500, flag_hash=hash_flag(self.metadata["flag"]), is_published=False,
        )
        row = PublicationRequest.objects.create(
            challenge=challenge, challenge_slug=SLUG, repository="MSG-CTF/2026_MSG_CTF",
            workflow="challenge-validation.yml", ref="main", requested_sha=SHA,
            dispatch_status="DISPATCHED", run_id=88, run_attempt=1, run_number=1,
            dispatch_started_at=timezone.now(), requested_by=self.admin, requested_by_login=self.admin.login_id,
        )
        self.run.update(
            run_attempt=1, run_number=1, repository={"full_name": row.repository},
            head_repository={"full_name": row.repository}, path=".github/workflows/challenge-validation.yml",
            event="workflow_dispatch", display_title=f"문제 요청 {SLUG} / {row.pk}",
        )
        return row

    def test_linked_import_commits_three_way_sha_proof_without_activating(self):
        row = self.linked_request()
        preview = self.preview(publication_request_id=str(row.pk))
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertTrue(preview.data["data"]["sha_verified"])
        row.refresh_from_db()
        self.assertEqual(row.collection, {})
        self.assertFalse(ChallengeRelease.objects.exists())
        body = {"preview_token": preview.data["data"]["preview_token"], "difficulty": "EASY"}
        response = self.import_body(body)
        self.assertEqual(response.status_code, 200, response.data)
        row.refresh_from_db()
        proof = response.data["data"]["collection"]
        self.assertEqual(row.collection, proof)
        self.assertEqual(proof["artifact_ids"], {"publish": 11, "user-files": 12})
        self.assertEqual((proof["run_id"], proof["run_attempt"], proof["registry_revision"], proof["source_sha"]), (88, 1, 1, SHA))
        self.assertEqual(proof["release_id"], response.data["data"]["release_id"])
        self.assertIsNone(ChallengeRuntimeConfig.objects.get(challenge=row.challenge).current_release_id)
        self.assertFalse(row.challenge.is_published)
        self.calls.clear()
        detail = self.client.get(f"/api/v1/admin/publication_requests/{row.pk}")
        self.assertTrue(detail.data["data"]["sha_verified"])
        self.assertEqual(detail.data["data"]["collection"], proof)
        self.assertEqual(self.import_body(body).json(), response.json())
        self.assertEqual(self.calls, [])
        encoded = json.dumps(detail.data)
        self.assertNotIn(self.metadata["flag"], encoded)
        self.assertNotIn("private-source-env", encoded)

    def test_linked_request_rejects_wrong_run_slug_sha_repository_and_state_before_download(self):
        row = self.linked_request()
        for field, bad in (("run_id", 89), ("challenge_slug", "web-other"), ("requested_sha", "d" * 40),
                           ("repository", "other/repo"), ("workflow", "other.yml"), ("ref", "dev"),
                           ("dispatch_status", "DISPATCH_UNKNOWN"), ("dispatch_started_at", None),
                           ("challenge_id", None)):
            original = getattr(row, field)
            PublicationRequest.objects.filter(pk=row.pk).update(**{field: bad})
            self.calls.clear()
            with self.subTest(field=field):
                response = self.preview(publication_request_id=str(row.pk))
                self.assertEqual(response.status_code, 409, response.data)
                self.assertFalse(any(path.endswith("/zip") for path in self.calls))
            PublicationRequest.objects.filter(pk=row.pk).update(**{field: original})
        self.assertFalse(ChallengeRelease.objects.exists())

    def test_linked_request_requires_exact_run_provenance_and_revision(self):
        row = self.linked_request()
        original = copy.deepcopy(self.run)
        for field, bad in (("path", ".github/workflows/other.yml"), ("event", "push"),
                           ("display_title", self.run["display_title"] + " extra"),
                           ("repository", {"full_name": "other/repo"}), ("head_repository", {"full_name": "other/repo"}),
                           ("run_number", 2), ("run_attempt", True)):
            self.run = {**original, field: bad}
            with self.subTest(field=field):
                response = self.preview(publication_request_id=str(row.pk))
                self.assertGreaterEqual(response.status_code, 400, response.data)
        self.run = original
        self.release.update(registry_revision=2, revision=2)
        self.manifest["registry_revision"] = 2
        self.assertEqual(self.preview(publication_request_id=str(row.pk)).status_code, 409)
        self.assertFalse(ChallengeRelease.objects.exists())

    def test_linked_bundle_sha_and_files_sha_are_rechecked_before_save(self):
        row = self.linked_request()
        for data in (self.release, self.manifest):
            original = data["source_sha"]
            data["source_sha"] = "e" * 40
            with self.subTest(bundle="release" if data is self.release else "files"):
                self.assertGreaterEqual(self.preview(publication_request_id=str(row.pk)).status_code, 400)
            data["source_sha"] = original
        del self.release["source_sha"]
        self.release["commit_sha"] = SHA
        self.assertEqual(self.preview(publication_request_id=str(row.pk)).status_code, 409)
        row.refresh_from_db()
        self.assertEqual(row.collection, {})
        self.assertFalse(ChallengeRelease.objects.exists())

    def test_linked_rerun_accepts_old_name_attempt_but_not_stale_preview_or_new_digest(self):
        row = self.linked_request()
        body = self.body(publication_request_id=str(row.pk))
        first = self.import_body(body)
        self.assertEqual(first.status_code, 200, first.data)
        self.run["run_attempt"] = 2
        PublicationRequest.objects.filter(pk=row.pk).update(run_attempt=2)
        detail = self.client.get(f"/api/v1/admin/publication_requests/{row.pk}").data["data"]
        self.assertFalse(detail["sha_verified"])
        self.assertEqual(detail["collection"]["run_attempt"], 1)
        self.assertEqual(self.import_body(body, "old-attempt").status_code, 409)
        second = self.import_body(self.body(publication_request_id=str(row.pk)), "rerun")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["data"]["release_status"], "duplicate")
        self.assertEqual(second.data["data"]["collection"]["run_attempt"], 2)
        self.assertEqual(ChallengeRelease.objects.count(), 1)
        self.release["workload"]["containers"][0]["image"] = f"ghcr.io/msg-ctf/challenges/{SLUG}/app@sha256:{'b' * 64}"
        self.assertEqual(self.import_body(self.body(publication_request_id=str(row.pk)), "changed-digest").status_code, 409)
        row.refresh_from_db()
        self.assertEqual(row.collection, second.data["data"]["collection"])

    def test_linked_duplicate_candidates_are_rejected_without_latest_selection(self):
        row = self.linked_request()
        self.artifacts.append({**self.artifacts[0], "id": 13, "name": f"{SLUG}-88-2-abcdef-publish-bundle"})
        self.assertEqual(self.preview(publication_request_id=str(row.pk)).status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.collection, {})

    def test_linked_alias_uses_canonical_proof_and_signed_request_cannot_be_swapped(self):
        row = self.linked_request()
        alias = PublicationRequest.objects.create(
            challenge=row.challenge, challenge_slug=SLUG, repository=row.repository, workflow=row.workflow,
            ref=row.ref, requested_sha=SHA, reused_request=row, requested_by=self.admin,
            requested_by_login=self.admin.login_id,
        )
        body = self.body(publication_request_id=str(alias.pk))
        self.assertEqual(self.import_body({**body, "publication_request_id": str(row.pk)}).status_code, 400)
        response = self.import_body(body)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["publication_request_id"], str(alias.pk))
        self.assertEqual(response.data["data"]["dispatch_request_id"], str(row.pk))
        alias.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(alias.collection, {})
        self.assertEqual(row.collection, response.data["data"]["collection"])
        detail = self.client.get(f"/api/v1/admin/publication_requests/{alias.pk}").data["data"]
        self.assertEqual(detail["collection"], row.collection)
        self.assertTrue(detail["sha_verified"])

    def test_linked_storage_failure_rolls_back_proof_and_retry_can_complete(self):
        row = self.linked_request()
        body = self.body(publication_request_id=str(row.pk))
        with patch("apps.instances.user_files.default_storage.save", side_effect=OSError("private-error")):
            response = self.import_body(body)
        self.assertEqual(response.status_code, 503, response.data)
        row.refresh_from_db()
        self.assertEqual(row.collection, {})
        self.assertFalse(ChallengeRelease.objects.exists())
        self.assertFalse(IdempotencyRecord.objects.exists())
        self.assertEqual(self.import_body(body).status_code, 200)

    def test_linked_request_is_rechecked_under_lock_before_any_import_writes(self):
        row = self.linked_request()
        body = self.body(publication_request_id=str(row.pk))
        save = publications.save_publication

        def newer_observation(prepared, *args):
            PublicationRequest.objects.filter(pk=row.pk).update(run_attempt=2)
            return save(prepared, *args)

        with patch("apps.instances.published_challenges.save_publication", side_effect=newer_observation):
            response = self.import_body(body)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(ChallengeRelease.objects.exists())
        row.refresh_from_db()
        self.assertEqual(row.collection, {})

    def test_linked_request_id_validation_and_admin_permission_precede_network(self):
        for value in (None, True, 1, "not-a-uuid"):
            with self.subTest(value=value):
                self.assertEqual(self.preview(publication_request_id=value).status_code, 400)
        self.assertEqual(self.calls, [])
        response = self.preview(publication_request_id="00000000-0000-0000-0000-000000000001")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.calls, [])
        row = self.linked_request()
        self.auth("player")
        self.assertEqual(self.preview(publication_request_id=str(row.pk)).status_code, 403)
        self.assertEqual(self.calls, [])

    def test_unlinked_collection_still_needs_no_request_uuid(self):
        response = self.import_body(self.body())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("publication_request_id", response.data["data"])
        self.assertFalse(PublicationRequest.objects.exists())

    def test_linked_static_no_file_publication_has_proof_without_a_runtime_release(self):
        self.make_static()
        self.archive = None
        self.manifest = user_manifest(None, slug=SLUG, source_sha=SHA)
        row = self.linked_request()
        response = self.import_body(self.body(publication_request_id=str(row.pk)))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["data"]["collection"]["release_id"])
        self.assertEqual(response.data["data"]["collection"]["artifact_ids"], {"user-files": 12})
        self.assertFalse(ChallengeRelease.objects.exists())
        self.assertFalse(ChallengeRuntimeConfig.objects.filter(challenge=row.challenge).exists())
        self.assertFalse(ChallengeUserFileBundle.objects.get(challenge=row.challenge).present)

    def test_linked_changed_file_content_does_not_replace_revision_or_successful_proof(self):
        row = self.linked_request()
        first = self.import_body(self.body(publication_request_id=str(row.pk)))
        self.assertEqual(first.status_code, 200, first.data)
        stored = ChallengeUserFileBundle.objects.get(challenge=row.challenge)
        self.archive = user_archive({"changed.txt": b"new content"})
        self.manifest = user_manifest(self.archive, slug=SLUG, source_sha=SHA)
        response = self.import_body(self.body(publication_request_id=str(row.pk)), "changed-files")
        self.assertEqual(response.status_code, 409, response.data)
        stored.refresh_from_db()
        row.refresh_from_db()
        self.assertNotEqual(stored.sha256, self.manifest["user_files"]["sha256"])
        self.assertEqual(row.collection, first.data["data"]["collection"])

    def test_linked_pair_provenance_and_artifact_selection_are_rechecked_after_preview(self):
        row = self.linked_request()
        body = self.body(publication_request_id=str(row.pk))
        self.artifacts[1]["workflow_run"]["head_sha"] = "e" * 40
        self.assertEqual(self.import_body(body).status_code, 400)
        self.artifacts[1]["workflow_run"]["head_sha"] = SHA
        self.artifacts[1]["id"] = 99
        self.assertEqual(self.import_body(body).status_code, 409)
        row.refresh_from_db()
        self.assertEqual(row.collection, {})
        self.assertFalse(ChallengeRelease.objects.exists())
