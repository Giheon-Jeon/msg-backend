import copy
import json
import time
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import SimpleTestCase, override_settings

from apps.instances.models import ChallengeRelease, RuntimeSecret
from apps.instances.releases import ReleaseValidationError
from apps.instances.repository_environment import discover_environment, parse_environment_template
from apps.instances.repository_source import RepositorySnapshot, RepositorySourceUnavailable
from apps.instances.runtime_secrets import create_runtime_secret, decrypt_runtime_secret
from apps.instances.tests_releases import ReleaseTestBase, artifact_payload

REVISION = "a" * 40
FILES = {
    "info.yaml": "deployment:\n  runtime_type: KUBERNETES\n  containers:\n    - name: app\n      build: ./app\n    - name: helper\n      build: ./helper\n",
    "app/Dockerfile": 'FROM python:3\nENV PORT=8080\nENV FLAG="MSG{flag}"\n',
    "app/main.py": 'import os\nport = os.getenv("PORT", "7000")\npassword = os.getenv("DB_PASS", "private-source-value")\n',
    "app/tests/test_fake.py": 'import os\nos.getenv("TEST_ONLY", "ignored")\n',
    "helper/app.js": 'const value = process.env.HELPER_TOKEN || "private-helper-value";',
    "docker-compose.yml": "services:\n  app:\n    build: ./app\n    environment:\n      PORT: '9090'\n",
    "env/app.env.example": 'PORT=9090\nFLAG=MSG{flag}\nDB_PASS=private-source-value\n',
    "env/helper.env.example": 'HELPER_TOKEN=private-helper-value\n',
}


class MemorySnapshot:
    def __init__(self, slug="web-basic", revision=None, files=None):
        self.slug, self.revision, self.repository = slug, revision or REVISION, "MSG-CTF/2026_MSG_CTF"
        self.contents = dict(FILES if files is None else files)
        self.files = {name: {"size": len(text.encode())} for name, text in self.contents.items()}

    def read(self, path):
        if path not in self.contents:
            raise RepositorySourceUnavailable("설정 파일이 없습니다")
        return self.contents[path]


class RepositoryDiscoveryTests(SimpleTestCase):
    def test_only_the_declared_container_template_is_read(self):
        snapshot = MemorySnapshot()
        with patch.object(snapshot, "read", wraps=snapshot.read) as read:
            result = discover_environment(snapshot, "app")
        self.assertEqual([call.args[0] for call in read.call_args_list], ["info.yaml", "env/app.env.example"])
        rows = {item["name"]: item for item in result["candidates"]}
        self.assertEqual(set(rows), {"PORT", "FLAG", "DB_PASS"})
        self.assertEqual(rows["PORT"]["value"], "9090")
        self.assertEqual(rows["PORT"]["kind"], "env")
        self.assertTrue(rows["FLAG"]["sensitive"])
        self.assertTrue(rows["DB_PASS"]["sensitive"])
        helper = discover_environment(MemorySnapshot(), "helper")["candidates"]
        self.assertEqual([item["name"] for item in helper], ["HELPER_TOKEN"])

    def test_missing_template_does_not_fall_back_to_docker_or_code(self):
        files = dict(FILES)
        del files["env/app.env.example"]
        with self.assertRaises(ReleaseValidationError) as raised:
            discover_environment(MemorySnapshot(files=files), "app")
        self.assertIn("env/app.env.example", str(raised.exception))

    def test_dotenv_is_literal_and_preserves_line_numbers_and_blank_slots(self):
        rows = parse_environment_template('\ufeff# comment\r\nPORT=8080\r\nPASSWORD=\r\nTEXT=" a # $VALUE = b "\r\n', "env/app.env.example")
        self.assertEqual(rows[1]["value"], "")
        self.assertTrue(rows[1]["needs_value"])
        self.assertEqual(rows[2]["value"], " a # $VALUE = b ")
        self.assertEqual(rows[2]["sources"][0]["line"], 4)

    def test_more_than_32_declarations_warn_without_silently_dropping_entries(self):
        files = dict(FILES, **{"env/app.env.example": "\n".join(f"PORT_{index}=1234" for index in range(33))})
        result = discover_environment(MemorySnapshot(files=files), "app")
        self.assertEqual(len(result["candidates"]), 33)
        self.assertTrue(result["warnings"])

    def test_invalid_yaml_and_duplicate_entries_do_not_echo_source_values(self):
        files = dict(FILES, **{"info.yaml": "deployment: [private-source-value"})
        with self.assertRaises(ReleaseValidationError) as raised:
            discover_environment(MemorySnapshot(files=files), "app")
        self.assertNotIn("private-source", str(raised.exception))
        for text in ("PASSWORD=private-first\nPASSWORD=private-second", "export PASSWORD=private-first", 'PASSWORD="private-first'):
            with self.assertRaises(ReleaseValidationError) as raised:
                parse_environment_template(text, "env/app.env.example")
            self.assertNotIn("private", str(raised.exception))

    @override_settings(REPOSITORY_ENV_EXCLUDED_SLUGS={"pwn-msgdriver"})
    def test_separately_deployed_windows_problem_is_excluded(self):
        with self.assertRaises(ReleaseValidationError):
            RepositorySnapshot("pwn-msgdriver")

    @override_settings(REPOSITORY_ENV_GIT_DIR="", RELEASE_POLL_REPO="MSG-CTF/2026_MSG_CTF", REPOSITORY_ENV_REF="main")
    def test_github_reader_pins_commit_and_reads_only_regular_blobs(self):
        tree_sha, blob_sha = "b" * 40, "c" * 40
        answers = [
            {"sha": REVISION},
            {"tree": [{"path": "web-basic", "type": "tree", "sha": tree_sha}]},
            {"tree": [{"path": "info.yaml", "mode": "100644", "sha": blob_sha, "size": 20},
                      {"path": "linked.env", "mode": "120000", "sha": "d" * 40, "size": 10}]},
        ]
        with patch("apps.instances.repository_source._github", side_effect=answers) as request:
            snapshot = RepositorySnapshot("web-basic")
        self.assertEqual(snapshot.revision, REVISION)
        self.assertEqual(set(snapshot.files), {"info.yaml"})
        self.assertIn(REVISION, request.call_args_list[1].args[0])


@override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()])
class RepositoryEnvironmentApiTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.auth("root")
        self.flag = create_runtime_secret(self.challenge, "flag", "MSG{flag}", "root")
        body = artifact_payload(containers=[
            {"name": "app", "image": "ghcr.io/msg-ctf/challenges/web-basic/app@sha256:" + "a" * 64,
             "ports": [{"port": 8080, "public": True}], "env": {"PORT": "8080", "KEEP": "keep"}, "secret_env": {"FLAG": "flag"}},
            {"name": "helper", "image": "ghcr.io/msg-ctf/challenges/web-basic/helper@sha256:" + "b" * 64,
             "ports": [{"port": 9090, "public": False}]},
        ])
        body["artifact"]["schema_version"] = "2.1"
        response = self.client.post(self.base_url, body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.source = ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])
        self.activate(self.source.pk)
        self.url = f"{self.base_url}/{self.source.pk}/repository_environment"
        self.reader = patch("apps.instances.repository_environment_views.RepositorySnapshot", MemorySnapshot)
        self.reader.start()
        self.addCleanup(self.reader.stop)

    def loaded(self, container="app"):
        response = self.client.get(self.url, {"container": container})
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def body(self):
        data = self.loaded().data["data"]
        return {"container": "app", "source_token": data["source_token"], "overrides": {},
                "entries": [{key: row[key] for key in ("name", "kind", "required", "secret_name", "use_existing", "secret_id") if key in row}
                            for row in data["entries"]], "request_id": str(uuid.uuid4())}

    def test_load_is_read_only_and_redacts_source_secrets(self):
        before = (RuntimeSecret.objects.count(), ChallengeRelease.objects.count())
        response = self.loaded()
        self.assertIn("no-store", response["Cache-Control"])
        data = json.dumps(response.data)
        self.assertNotIn("private-source-value", data)
        self.assertNotIn("MSG{flag}", data)
        self.assertEqual(before, (RuntimeSecret.objects.count(), ChallengeRelease.objects.count()))
        flag = next(row for row in response.data["data"]["entries"] if row["name"] == "FLAG")
        self.assertTrue(flag["use_existing"])
        self.assertEqual(flag["secret_id"], str(self.flag.pk))

    def test_preview_and_import_encrypt_values_preserve_other_containers_and_are_idempotent(self):
        body = self.body()
        preview = self.client.post(self.url + "/preview", body, format="json")
        self.assertTrue(preview.data["data"]["can_import"], preview.data)
        self.assertNotIn("private-source-value", json.dumps(preview.data))
        saved = self.client.post(self.url + "/import", body, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        release = ChallengeRelease.objects.get(pk=saved.data["data"]["release_id"])
        app = release.containers.get(name="app")
        self.assertEqual(app.env, {"PORT": "9090", "KEEP": "keep"})
        self.assertEqual(app.secret_env["FLAG"], str(self.flag.pk))
        secret = RuntimeSecret.objects.get(pk=app.secret_env["DB_PASS"])
        self.assertEqual(decrypt_runtime_secret(secret), "private-source-value")
        self.assertNotIn("private-source-value", secret.encrypted_value)
        self.assertEqual(release.containers.get(name="helper").env, {})
        self.assertFalse(saved.data["data"]["is_current"])
        self.assertEqual(release.containers.get(name="app").image_ref, self.source.containers.get(name="app").image_ref)
        count = (RuntimeSecret.objects.count(), ChallengeRelease.objects.count())
        retried = self.client.post(self.url + "/import", body, format="json")
        self.assertEqual(retried.data["data"]["release_id"], str(release.pk))
        self.assertEqual(count, (RuntimeSecret.objects.count(), ChallengeRelease.objects.count()))
        changed = copy.deepcopy(body)
        changed["overrides"] = {"PORT": "9191"}
        self.assertEqual(self.client.post(self.url + "/import", changed, format="json").status_code, 409)

    def test_secret_downgrade_cross_container_and_unknown_fields_are_rejected(self):
        body = self.body()
        row = next(row for row in body["entries"] if row["name"] == "DB_PASS")
        row["kind"] = "env"
        response = self.client.post(self.url + "/preview", body, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("private-source-value", json.dumps(response.data))
        for patch_body in ({"container": "helper"}, {"sourceToken": "bad"}, {"source_token": "tampered"}):
            with self.subTest(fields=list(patch_body)):
                response = self.client.post(self.url + "/import", self.body() | patch_body, format="json")
                self.assertEqual(response.status_code, 400)

    def test_expired_token_requires_reload(self):
        with patch("django.core.signing.time.time", return_value=time.time() - 3700):
            body = self.body()
        response = self.client.post(self.url + "/preview", body, format="json")
        self.assertEqual(response.status_code, 400)

    def test_invalid_flag_rolls_back_all_writes(self):
        body = self.body()
        next(row for row in body["entries"] if row["name"] == "FLAG")["use_existing"] = False
        body["overrides"] = {"FLAG": "wrong-local-answer"}
        before = (RuntimeSecret.objects.count(), ChallengeRelease.objects.count())
        response = self.client.post(self.url + "/import", body, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(before, (RuntimeSecret.objects.count(), ChallengeRelease.objects.count()))

    def test_admin_permission_checked_before_repository_read(self):
        self.auth("player")
        with patch("apps.instances.repository_environment_views.RepositorySnapshot") as read:
            response = self.client.get(self.url, {"container": "app"})
        self.assertIn(response.status_code, (401, 403))
        read.assert_not_called()
