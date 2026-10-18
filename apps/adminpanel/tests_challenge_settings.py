import json
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from django.test import override_settings
from rest_framework.test import APIClient

from apps.challenge.models import Challenge
from apps.challenge.services import hash_flag
from apps.instances.models import ChallengeRuntimeConfig
from apps.instances.repository_source import RepositorySourceUnavailable
from apps.instances.runtime_secrets import create_runtime_secret
from apps.instances.tests_releases import ReleaseTestBase


@override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()])
class ChallengeSettingsTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.auth("root")
        self.url = f"/api/v1/admin/challenges/{self.challenge.pk}"
        self.challenge.challenge_slug = "web-basic"
        self.challenge.save(update_fields=["challenge_slug"])
        self.snapshot = MagicMock()
        self.snapshot.repository = "MSG-CTF/problems"
        self.snapshot.slug = "web-basic"
        self.snapshot.revision = "a" * 40
        self.snapshot.published = False
        self.snapshot.read.return_value = 'name: Example\nflag: "MSG{flag}"\n'
        self.snapshot.files = {
            "info.yaml": {"size": 50}, "env/app.env": {"size": 30},
            "compose.runtime.yaml": {"size": 60},
            "prob/for_user/자료.zip": {"size": 4096},
            "prob/for_organizer/flag.txt": {"size": 10},
            "prob/for_organizer/app.py": {"size": 100},
        }
        self.repository = patch("apps.adminpanel.challenge_settings.RepositorySnapshot", return_value=self.snapshot).start()
        self.addCleanup(patch.stopall)

    def bind(self, secret):
        release = self.create_legacy_release()
        container = release.containers.get()
        container.secret_env = {"FLAG": str(secret.pk)}
        container.save(update_fields=["secret_env"])
        ChallengeRuntimeConfig.objects.create(challenge=self.challenge, current_release=release)
        return release

    def test_detail_and_patch_preserve_grading_and_runtime(self):
        self.repository.side_effect = RepositorySourceUnavailable("offline")
        secret = create_runtime_secret(self.challenge, "flag", "MSG{flag}", "root")
        release = self.bind(secret)
        initial_hash = self.challenge.flag_hash
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["description"], self.challenge.description)
        self.assertNotIn("flag_hash", response.data["data"])
        self.assertIn("no-store", response["Cache-Control"])
        self.repository.assert_not_called()
        saved = self.client.patch(self.url, {"title": "수정한 제목", "description": "첫 문단\n\n둘째 문단"}, format="json")
        self.assertEqual(saved.status_code, 200)
        loaded = self.client.get(self.url).data["data"]
        self.assertEqual(loaded["title"], "수정한 제목")
        self.assertEqual(loaded["description"], "첫 문단\n\n둘째 문단")
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.flag_hash, initial_hash)
        self.assertEqual(self.challenge.runtime_config.current_release_id, release.pk)

    def test_flag_status_is_masked_and_explicit_reveal_is_not_cached(self):
        secret = create_runtime_secret(self.challenge, "flag", "MSG{flag}", "root")
        self.bind(secret)
        response = self.client.get(self.url + "/flag")
        self.assertTrue(response.data["data"]["reveal_available"])
        self.assertEqual(response.data["data"]["bindings"][0]["status"], "match")
        self.assertNotIn("MSG{flag}", json.dumps(response.data))
        self.assertNotIn(self.challenge.flag_hash, json.dumps(response.data))
        self.repository.assert_not_called()
        with self.assertLogs("apps.adminpanel.challenge_settings", level="INFO") as logs:
            revealed = self.client.post(self.url + "/flag/reveal", {}, format="json")
        self.assertEqual(revealed.data["data"]["flag"], "MSG{flag}")
        self.assertIn("no-store", revealed["Cache-Control"])
        self.assertNotIn("MSG{flag}", " ".join(logs.output))
        self.assertEqual(logs.records[0].challenge_id, str(self.challenge.pk))
        self.assertEqual(self.client.get(self.url + "/flag/reveal").status_code, 405)

    def test_repository_flag_requires_current_grading_match(self):
        response = self.client.post(self.url + "/flag/reveal", {}, format="json")
        self.assertEqual(response.data["data"]["source"], "repository")
        self.assertEqual(response.data["data"]["source_revision"], self.snapshot.revision)
        self.snapshot.read.return_value = 'flag: "MSG{different}"'
        status = self.client.get(self.url + "/flag").data["data"]
        self.assertFalse(status["reveal_available"])
        self.assertEqual(status["source_status"], "mismatch")
        refused = self.client.post(self.url + "/flag/reveal", {}, format="json")
        self.assertEqual(refused.status_code, 409)
        self.assertNotIn("MSG{different}", json.dumps(refused.data))

    def test_grading_change_keeps_old_binding_visible_as_mismatch(self):
        secret = create_runtime_secret(self.challenge, "flag", "MSG{flag}", "root")
        self.bind(secret)
        self.challenge.flag_hash = hash_flag("MSG{new}")
        self.challenge.save(update_fields=["flag_hash"])
        self.snapshot.read.return_value = 'flag: "MSG{new}"'
        status = self.client.get(self.url + "/flag").data["data"]
        self.assertEqual(status["bindings"][0]["status"], "mismatch")
        self.assertTrue(status["reveal_available"])
        self.assertEqual(self.client.post(self.url + "/flag/reveal", {}, format="json").data["data"]["flag"], "MSG{new}")

    def test_other_challenge_secret_is_not_resolved(self):
        other = Challenge.objects.create(title="Other", category="WEB", difficulty="EASY", score=100, flag_hash=hash_flag("MSG{other}"))
        secret = create_runtime_secret(other, "flag", "MSG{other}", "root")
        self.bind(secret)
        self.snapshot.read.return_value = "name: Example"
        response = self.client.get(self.url + "/flag")
        self.assertEqual(response.data["data"]["bindings"][0]["status"], "missing")
        self.assertFalse(response.data["data"]["reveal_available"])
        self.assertEqual(self.client.post(self.url + "/flag/reveal", {}, format="json").status_code, 409)

    def test_unreadable_secrets_and_repository_do_not_expose_values(self):
        secret = create_runtime_secret(self.challenge, "flag", "MSG{flag}", "root")
        self.bind(secret)
        self.repository.side_effect = RepositorySourceUnavailable("offline")
        with override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[]):
            response = self.client.get(self.url + "/flag")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["bindings"][0]["status"], "unavailable")
        self.assertFalse(response.data["data"]["reveal_available"])
        self.assertNotIn(secret.encrypted_value, json.dumps(response.data))

    def test_files_list_real_metadata_without_contents_or_fake_published_links(self):
        response = self.client.get(self.url + "/repository-files")
        self.assertEqual(response.status_code, 200)
        files = {item["path"]: item for item in response.data["data"]["files"]}
        self.assertEqual(files["prob/for_user/자료.zip"]["kind"], "participant")
        self.assertEqual(files["prob/for_organizer/flag.txt"]["kind"], "source")
        self.assertEqual(files["env/app.env"]["kind"], "configuration")
        self.assertEqual(files["compose.runtime.yaml"]["kind"], "configuration")
        self.assertTrue(all(item["source_url"] is None for item in files.values()))
        self.snapshot.read.assert_not_called()
        self.assertIn("no-store", response["Cache-Control"])

    def test_published_file_links_are_pinned_to_the_listed_revision(self):
        self.snapshot.published = True
        files = self.client.get(self.url + "/repository-files").data["data"]["files"]
        self.assertTrue(all(f"/blob/{self.snapshot.revision}/web-basic/" in item["source_url"] for item in files))
        self.assertTrue(any("%EC%9E%90%EB%A3%8C.zip" in item["source_url"] for item in files))

    def test_missing_source_is_reported_separately_from_basic_information(self):
        self.repository.side_effect = RepositorySourceUnavailable("저장소 연결을 확인해주세요")
        self.assertEqual(self.client.get(self.url + "/repository-files").status_code, 503)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_settings_require_admin_for_read_and_reveal(self):
        targets = [("get", self.url), ("get", self.url + "/flag"), ("get", self.url + "/repository-files"), ("post", self.url + "/flag/reveal")]
        self.auth("player")
        for method, url in targets:
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url).status_code, 403)
                self.assertEqual(getattr(APIClient(), method)(url).status_code, 401)
        self.repository.assert_not_called()
