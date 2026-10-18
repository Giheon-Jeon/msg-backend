import copy
import json
import time
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import SimpleTestCase, override_settings

from apps.instances.models import ChallengeRelease, RuntimeSecret
from apps.instances.releases import ReleaseValidationError
from apps.instances.repository_environment import discover_environment, _dotenv, _expand
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
    def test_canonical_file_is_the_only_runtime_source_after_migration(self):
        files = dict(FILES, **{"env/app.env": "PORT=8181\nDB_PASS='source$literal#value'\n"})
        snapshot = MemorySnapshot(files=files)
        with patch.object(snapshot, "read", wraps=snapshot.read) as read:
            data = discover_environment(snapshot, "app")
        self.assertEqual([call.args[0] for call in read.call_args_list], ["info.yaml", "env/app.env"])
        self.assertEqual(data["source_files"], ["env/app.env"])
        self.assertEqual({row["name"]: row["value"] for row in data["candidates"]},
                         {"PORT": "8181", "DB_PASS": "source$literal#value"})
        self.assertTrue(all(row["resolution"] == "configured" for row in data["candidates"]))

    def test_canonical_missing_values_do_not_use_host_or_old_defaults(self):
        files = dict(FILES, **{"env/app.env": "DB_PASS=${DB_PASS:?set DB_PASS}\nOPTIONAL_VALUE\n"})
        with patch.dict("os.environ", {"DB_PASS": "host-value", "OPTIONAL_VALUE": "host-value"}):
            data = discover_environment(MemorySnapshot(files=files), "app")
        self.assertEqual([row["value"] for row in data["candidates"]], [None, None])
        self.assertTrue(data["warnings"])

    def test_empty_canonical_file_does_not_reintroduce_image_or_code_values(self):
        files = dict(FILES, **{"env/app.env": "# No configured variables\n"})
        data = discover_environment(MemorySnapshot(files=files), "app")
        self.assertEqual(data["candidates"], [])
        self.assertEqual(data["source_files"], ["env/app.env"])

    def test_canonical_file_rejects_duplicates_and_invalid_names(self):
        for content in ("PORT=8080\nPORT=9090", "bad-name=value"):
            with self.assertRaises(ReleaseValidationError):
                discover_environment(MemorySnapshot(files=dict(FILES, **{"env/app.env": content})), "app")

    def test_author_values_are_read_without_generated_templates(self):
        snapshot = MemorySnapshot()
        with patch.object(snapshot, "read", wraps=snapshot.read) as read:
            result = discover_environment(snapshot, "app")
        paths = [call.args[0] for call in read.call_args_list]
        self.assertNotIn("env/app.env.example", paths)
        self.assertNotIn("app/tests/test_fake.py", paths)
        rows = {item["name"]: item for item in result["candidates"]}
        self.assertEqual(set(rows), {"PORT", "FLAG", "DB_PASS"})
        self.assertEqual(rows["PORT"]["value"], "9090")
        self.assertEqual(rows["PORT"]["kind"], "env")
        self.assertTrue(rows["FLAG"]["sensitive"])
        self.assertTrue(rows["DB_PASS"]["sensitive"])
        self.assertEqual(rows["DB_PASS"]["value"], "private-source-value")
        self.assertEqual(rows["FLAG"]["value"], "MSG{flag}")
        self.assertEqual(rows["PORT"]["resolution"], "configured")
        self.assertEqual(rows["DB_PASS"]["resolution"], "code_default")
        helper = discover_environment(MemorySnapshot(), "helper")["candidates"]
        self.assertEqual([item["name"] for item in helper], ["HELPER_TOKEN"])

    def test_no_template_is_required(self):
        files = dict(FILES)
        del files["env/app.env.example"]
        result = discover_environment(MemorySnapshot(files=files), "app")
        self.assertEqual(len(result["candidates"]), 3)

    def test_dotenv_preserves_literal_secret_characters_and_empty_values(self):
        rows = _dotenv("\ufeff# comment\r\nPORT=8080\r\nPASSWORD=\r\nTEXT=' a # $VALUE = b '\r\n", "app/.env")
        self.assertEqual(rows[1][1], "")
        self.assertEqual(rows[2][1], " a # $VALUE = b ")
        self.assertEqual(rows[2][2], 4)
        self.assertEqual(_dotenv("PASSWORD=literal$dollar=value", "app/.env")[0][1], "literal$dollar=value")

    def test_actual_env_file_and_compose_precedence_keep_values(self):
        files = dict(FILES)
        files[".env"] = "AUTHOR_PORT=9191\nAUTHOR_PASSWORD='original$secret=value'\nUNRELATED=do-not-inject\n"
        files["app/runtime.env"] = "PORT=8181\nDB_PASS='file$secret=value'\n"
        files["app/final.env"] = "DB_PASS='last$secret=value'\n"
        files["docker-compose.yml"] = "services:\n  app:\n    build: ./app\n    env_file: [app/runtime.env, app/final.env]\n    environment:\n      PORT: ${AUTHOR_PORT}\n      API_PASSWORD: ${AUTHOR_PASSWORD}\n"
        result = discover_environment(MemorySnapshot(files=files), "app")
        rows = {row["name"]: row for row in result["candidates"]}
        self.assertEqual(rows["PORT"]["value"], "9191")
        self.assertEqual(rows["DB_PASS"]["value"], "last$secret=value")
        self.assertEqual(rows["API_PASSWORD"]["value"], "original$secret=value")
        self.assertNotIn("UNRELATED", rows)
        self.assertEqual(rows["DB_PASS"]["sources"][0]["path"], "app/final.env")

    def test_local_dotenv_is_imported_without_any_value_redaction(self):
        files = dict(FILES)
        del files["docker-compose.yml"]
        files["app/.env"] = "PORT=8181\nDB_PASS='original$secret=value'\n"
        rows = {row["name"]: row for row in discover_environment(MemorySnapshot(files=files), "app")["candidates"]}
        self.assertEqual(rows["DB_PASS"]["value"], "original$secret=value")
        self.assertEqual(rows["PORT"]["value"], "8181")

    def test_unresolved_source_is_not_replaced_by_an_example_or_host_value(self):
        files = dict(FILES)
        files["docker-compose.yml"] = "services:\n  app:\n    build: ./app\n    environment:\n      DB_PASS: ${MISSING}\n"
        files[".env.example"] = "DB_PASS=example-only\n"
        with patch.dict("os.environ", {"MISSING": "server-value"}):
            rows = {row["name"]: row for row in discover_environment(MemorySnapshot(files=files), "app")["candidates"]}
        self.assertIsNone(rows["DB_PASS"]["value"])
        self.assertEqual(rows["DB_PASS"]["resolution"], "needs_value")

    def test_compose_defaults_nested_values_and_dollars(self):
        self.assertEqual(_expand("${PORT:-8080}", {}), "8080")
        self.assertEqual(_expand("${PORT-8080}", {"PORT": ""}), "")
        self.assertEqual(_expand("${A:-${B:-8080}}", {}), "8080")
        self.assertEqual(_expand("$$LITERAL", {}), "$LITERAL")
        self.assertEqual(_expand("${A:+yes}", {"A": "1"}), "yes")
        self.assertIsNone(_expand("${PASSWORD:?required}", {}))

    def test_more_than_128_declarations_warn_without_silently_dropping_entries(self):
        files = dict(FILES, **{"app/.env": "\n".join(f"PORT_{index}=1234" for index in range(129))})
        del files["docker-compose.yml"]
        result = discover_environment(MemorySnapshot(files=files), "app")
        self.assertEqual(len(result["candidates"]), 132)
        self.assertTrue(result["warnings"])

    def test_invalid_yaml_and_duplicate_entries_do_not_echo_source_values(self):
        files = dict(FILES, **{"info.yaml": "deployment: [private-source-value"})
        with self.assertRaises(ReleaseValidationError) as raised:
            discover_environment(MemorySnapshot(files=files), "app")
        self.assertNotIn("private-source", str(raised.exception))
        for text in ("PASSWORD=private-first\nPASSWORD=private-second", 'PASSWORD="private-first'):
            with self.assertRaises(ReleaseValidationError) as raised:
                _dotenv(text, "app/.env")
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
        self.assertFalse(flag["use_existing"])
        self.assertTrue(flag["has_value"])
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
        imported_flag = RuntimeSecret.objects.get(pk=app.secret_env["FLAG"])
        self.assertEqual(decrypt_runtime_secret(imported_flag), "MSG{flag}")
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

    def test_admin_can_explicitly_keep_an_existing_secret_version(self):
        body = self.body()
        next(row for row in body["entries"] if row["name"] == "FLAG")["use_existing"] = True
        response = self.client.post(self.url + "/import", body, format="json")
        self.assertEqual(response.status_code, 200)
        release = ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])
        self.assertEqual(release.containers.get(name="app").secret_env["FLAG"], str(self.flag.pk))

    def test_unknown_ordinary_value_is_not_returned_as_a_fake_empty_value(self):
        with patch.dict(FILES, {"docker-compose.yml": "services:\n  app:\n    build: ./app\n    environment:\n      PORT: ${MISSING}\n"}):
            loaded = self.loaded().data['data']
        port = next(row for row in loaded['entries'] if row['name'] == 'PORT')
        self.assertFalse(port['has_value'])
        self.assertFalse(port['selected'])
        self.assertNotIn('value', port)
        self.assertEqual(port['resolution'], 'needs_value')

    def test_repository_file_password_is_imported_without_retyping(self):
        with patch.dict(FILES, {"docker-compose.yml": "services:\n  app:\n    build: ./app\n    env_file: app/runtime.env\n",
                               "app/runtime.env": "DB_PASS='literal$author=value'\n"}):
            body = self.body()
            loaded = self.loaded()
            self.assertNotIn("literal$author=value", json.dumps(loaded.data))
            response = self.client.post(self.url + "/import", body, format="json")
        self.assertEqual(response.status_code, 200)
        release = ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])
        secret = RuntimeSecret.objects.get(pk=release.containers.get(name="app").secret_env["DB_PASS"])
        self.assertEqual(decrypt_runtime_secret(secret), "literal$author=value")

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
