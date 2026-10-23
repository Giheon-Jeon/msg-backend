import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import override_settings

from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig, RuntimeSecret, UserFilesMode
from apps.instances.poller import register_bundle
from apps.instances.published_challenges import _same_release
from apps.instances.releases import serialize_release, validate_release_payload
from apps.instances.repository_source import RepositorySourceUnavailable
from apps.instances.runtime_secrets import create_runtime_secret, decrypt_runtime_secret
from apps.instances.tests_releases import ReleaseTestBase, artifact_payload
from apps.instances.tests_repository_environment import MemorySnapshot

SHA = "c" * 40
INFO = "deployment:\n  runtime_type: KUBERNETES\n  containers:\n    - name: app\n      build: ./app\n"
ENV_FILE = "PORT=8080\nREDIS_URL=redis://cache:6379/0\nFLAG=MSG{flag}\nAPI_TOKEN=repo-token\n"


@override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()], RELEASE_POLL_GITHUB_TOKEN="test-token")
class BundleEnvironmentTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.files = {"info.yaml": INFO, "env/app.env": ENV_FILE}
        self.reads = []
        reader = patch("apps.instances.bundle_environment.RepositorySnapshot", side_effect=self.snapshot)
        reader.start()
        self.addCleanup(reader.stop)

    def snapshot(self, slug, revision=None):
        self.reads.append((slug, revision))
        return MemorySnapshot(slug, revision, self.files)

    def bundle(self, revision=1, **container):
        artifact = artifact_payload(revision=revision)["artifact"]
        artifact.update(challenge_id=str(self.challenge.pk), source_sha=SHA)
        artifact["workload"]["containers"][0].update(container)
        if container:
            artifact["schema_version"] = "2.1"
        return artifact

    def register(self, revision=1, **container):
        status, release = register_bundle(self.bundle(revision, **container))
        self.assertEqual(status, "registered", release)
        return release

    def derive(self, source, env, aliases):
        response = self.client.post(f"{self.base_url}/{source.pk}/derive", {
            "containers": [{"name": "app", "env": env, "secret_env": aliases}],
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])

    def test_github_version_gets_the_env_file_of_the_same_commit(self):
        release = self.register()
        self.assertEqual(self.reads, [("web-basic", SHA)])
        container = release.containers.get()
        self.assertEqual(container.env, {"PORT": "8080"})
        secrets = {name: RuntimeSecret.objects.get(pk=reference) for name, reference in container.secret_env.items()}
        self.assertEqual(set(secrets), {"FLAG", "REDIS_URL", "API_TOKEN"})
        self.assertEqual(secrets["FLAG"].name, "flag")
        self.assertEqual(decrypt_runtime_secret(secrets["API_TOKEN"]), "repo-token")
        self.assertEqual(container.env_rules["PORT"], {"kind": "env", "required": True})
        self.assertEqual(release.repository_env, {"revision": SHA, "files": ["env/app.env"], "missing": []})
        self.assertEqual(serialize_release(release)["repository_env"]["files"], ["env/app.env"])
        self.auth("root")
        self.assertEqual(self.activate(release.pk).status_code, 200)

    def test_next_version_reuses_stored_secrets_and_the_admin_kind(self):
        first = self.register()
        container = first.containers.get()
        # 관리자가 지금 버전에서 REDIS_URL 을 일반 값으로 바꿔 둔 경우
        container.env = {**container.env, "REDIS_URL": "redis://cache:6379/0"}
        container.secret_env = {name: value for name, value in container.secret_env.items() if name != "REDIS_URL"}
        container.env_rules = {**container.env_rules, "REDIS_URL": {"kind": "env", "required": True}}
        container.save()
        ChallengeRuntimeConfig.objects.update_or_create(challenge=self.challenge, defaults={"current_release": first})
        secrets = RuntimeSecret.objects.count()
        second = self.register(revision=2).containers.get()
        self.assertEqual(second.env, {"PORT": "8080", "REDIS_URL": "redis://cache:6379/0"})
        self.assertEqual(second.secret_env["FLAG"], container.secret_env["FLAG"])
        self.assertEqual(second.secret_env["API_TOKEN"], container.secret_env["API_TOKEN"])
        self.assertEqual(RuntimeSecret.objects.count(), secrets)

    def test_unfilled_values_keep_the_version_but_block_its_use(self):
        self.files["env/app.env"] = "PORT=8080\nFLAG=MSG{other}\nDB_PASS=\n"
        release = self.register()
        container = release.containers.get()
        self.assertEqual(container.env, {"PORT": "8080"})
        self.assertEqual(container.secret_env, {})
        self.assertCountEqual(release.repository_env["missing"], [
            {"container": "app", "name": "FLAG", "reason": "flag_mismatch"},
            {"container": "app", "name": "DB_PASS", "reason": "no_value"},
        ])
        self.auth("root")
        response = self.activate(release.pk)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "RELEASE_ENV_INCOMPLETE")

    def test_empty_values_use_secrets_already_stored_for_the_problem(self):
        self.files["env/app.env"] = "FLAG=\nDB_PASS=\n"
        flag = create_runtime_secret(self.challenge, "flag", "MSG{flag}", "root")
        stored = create_runtime_secret(self.challenge, "env_app_db_pass", "stored-pass", "root")
        release = self.register()
        self.assertEqual(release.containers.get().secret_env, {"FLAG": str(flag.pk), "DB_PASS": str(stored.pk)})
        self.assertEqual(release.repository_env["missing"], [])

    def test_bundle_env_missing_env_file_and_unreadable_setup_are_left_alone(self):
        own = self.register(env={"MODE": "bundle"})
        self.assertEqual((own.containers.get().env, own.repository_env, self.reads), ({"MODE": "bundle"}, {}, []))
        del self.files["env/app.env"]
        self.assertEqual(self.register(revision=2).repository_env, {})
        self.reads.clear()
        with override_settings(RELEASE_POLL_GITHUB_TOKEN="", REPOSITORY_ENV_GIT_DIR=""):
            self.assertEqual(self.register(revision=3).repository_env, {})
        self.assertEqual(self.reads, [])

    def test_unreadable_repository_is_retried_on_the_next_poll(self):
        with patch("apps.instances.bundle_environment.RepositorySnapshot", side_effect=RepositorySourceUnavailable("문제 저장소를 읽지 못했습니다")):
            status, detail = register_bundle(self.bundle())
        self.assertEqual(status, "error")
        self.assertFalse(self.challenge.releases.exists())
        self.register()
        status, _ = register_bundle(self.bundle())
        self.assertEqual(status, "duplicate")
        self.assertEqual(len(self.reads), 1)

    def test_manual_import_compares_only_the_bundle_part_of_a_filled_version(self):
        release = self.register()
        self.assertTrue(_same_release(release, validate_release_payload({"artifact": self.bundle()})))

    def test_unchanged_derived_versions_keep_missing_values_and_current_release(self):
        self.files["env/app.env"] = "PORT=8080\n"
        current = self.register()
        self.auth("root")
        self.assertEqual(self.activate(current.pk).status_code, 200)
        self.files["env/app.env"] = "PORT=8080\nFLAG=MSG{other}\nDB_PASS=\n"
        source = self.register(revision=2)
        expected = source.repository_env
        for _ in range(2):
            source = self.derive(source, {"PORT": "8080"}, {})
            self.assertEqual(source.repository_env, expected)
            self.assertEqual(source.containers.get().secret_env, {})
            response = self.activate(source.pk)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["code"], "RELEASE_ENV_INCOMPLETE")
            self.assertEqual(ChallengeRuntimeConfig.objects.get(challenge=self.challenge).current_release_id, current.pk)

    def test_only_filled_values_are_removed_from_derived_missing(self):
        self.files["env/app.env"] = "PORT=8080\nFLAG=MSG{other}\nDB_PASS=\n"
        source = self.register()
        expected = dict(source.repository_env)
        flag = create_runtime_secret(self.challenge, "flag", "MSG{flag}", "root")
        password = create_runtime_secret(self.challenge, "env_app_db_pass", "fixed-local-value", "root")
        self.auth("root")
        partial = self.derive(source, {"PORT": "8080"}, {"FLAG": "flag"})
        self.assertEqual(partial.repository_env["missing"], [
            {"container": "app", "name": "DB_PASS", "reason": "no_value"},
        ])
        self.assertEqual(self.activate(partial.pk).status_code, 400)
        complete = self.derive(partial, {"PORT": "8080"}, {"FLAG": "flag", "DB_PASS": password.name})
        self.assertEqual(complete.repository_env, {**expected, "missing": []})
        self.assertEqual(complete.containers.get().secret_env, {"FLAG": str(flag.pk), "DB_PASS": str(password.pk)})
        self.assertEqual(self.activate(complete.pk).status_code, 200)
        source.refresh_from_db()
        self.assertEqual(source.repository_env, expected)

    def test_env_file_import_keeps_missing_until_values_are_saved(self):
        self.files["env/app.env"] = "PORT=8080\nFLAG=MSG{other}\nDB_PASS=\n"
        source = self.register()
        self.auth("root")
        url = f"{self.base_url}/{source.pk}/environment_files/import"
        partial = self.client.post(url, {
            "container": "app", "content": "PORT=8081", "request_id": str(uuid.uuid4()),
            "entries": [{"name": "PORT", "kind": "env", "required": True}],
        }, format="json")
        self.assertEqual(partial.status_code, 200, partial.data)
        partial_release = ChallengeRelease.objects.get(pk=partial.data["data"]["release_id"])
        self.assertEqual(partial_release.repository_env, source.repository_env)
        self.assertEqual(self.activate(partial_release.pk).status_code, 400)
        complete = self.client.post(url, {
            "container": "app", "content": "FLAG=MSG{flag}\nDB_PASS=fixed-local-value",
            "request_id": str(uuid.uuid4()),
            "entries": [{"name": name, "kind": "secret", "required": True} for name in ("FLAG", "DB_PASS")],
        }, format="json")
        self.assertEqual(complete.status_code, 200, complete.data)
        complete_release = ChallengeRelease.objects.get(pk=complete.data["data"]["release_id"])
        self.assertEqual(complete_release.repository_env["missing"], [])
        self.assertEqual(self.activate(complete_release.pk).status_code, 200)
        self.assertEqual(complete_release.containers.get().env, {"PORT": "8080"})

    def test_empty_plain_value_does_not_resolve_missing(self):
        self.files["env/app.env"] = "PORT\n"
        source = self.register()
        self.auth("root")
        derived = self.derive(source, {"PORT": ""}, {})
        self.assertEqual(derived.repository_env, source.repository_env)
        self.assertEqual(self.activate(derived.pk).status_code, 400)
        filled = self.derive(derived, {"PORT": "8080"}, {})
        self.assertEqual(filled.repository_env["missing"], [])
        self.assertEqual(self.activate(filled.pk).status_code, 200)

    def test_container_parse_error_is_not_cleared_by_deriving_settings(self):
        self.files["env/app.env"] = "PORT=8080\nPORT=8081\n"
        source = self.register()
        self.assertIsNone(source.repository_env["missing"][0]["name"])
        self.auth("root")
        derived = self.derive(source, {"PORT": "8080"}, {})
        self.assertEqual(derived.repository_env, source.repository_env)
        self.assertEqual(self.activate(derived.pk).status_code, 400)

    def test_file_selection_copy_preserves_missing_values(self):
        self.files["env/app.env"] = "PORT=8080\nFLAG=MSG{other}\nDB_PASS=\n"
        source = self.register()
        self.auth("root")
        response = self.client.post(f"{self.base_url}/{source.pk}/files", {
            "mode": UserFilesMode.NONE,
        }, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        self.assertEqual(response.status_code, 200, response.data)
        copied = ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])
        self.assertEqual(copied.repository_env, source.repository_env)
        self.assertEqual(copied.containers.get().env, source.containers.get().env)
        self.assertEqual(copied.containers.get().secret_env, source.containers.get().secret_env)
        self.assertEqual(self.activate(copied.pk).status_code, 400)

    def test_current_required_plain_value_cannot_be_empty_or_removed(self):
        self.files["env/app.env"] = "PORT=8080\n"
        first = self.register()
        self.auth("root")
        self.assertEqual(self.activate(first.pk).status_code, 200)
        for revision, content in enumerate(("PORT=\n", "MODE=ctf\n"), 2):
            with self.subTest(content=content):
                self.files["env/app.env"] = content
                release = self.register(revision=revision)
                self.assertEqual(release.repository_env["missing"], [
                    {"container": "app", "name": "PORT", "reason": "no_value"},
                ])
                self.assertEqual(release.containers.get().env_rules["PORT"], {"kind": "env", "required": True})
                self.assertEqual(self.activate(release.pk).status_code, 400)
                self.assertEqual(ChallengeRuntimeConfig.objects.get(challenge=self.challenge).current_release_id, first.pk)

    def test_optional_plain_rule_is_preserved_for_empty_and_filled_values(self):
        self.files["env/app.env"] = "PORT=8080\n"
        first = self.register()
        container = first.containers.get()
        container.env_rules["PORT"]["required"] = False
        container.save(update_fields=["env_rules"])
        self.auth("root")
        self.assertEqual(self.activate(first.pk).status_code, 200)
        for revision, value in enumerate(("", "8081"), 2):
            with self.subTest(value=value):
                self.files["env/app.env"] = f"PORT={value}\n"
                release = self.register(revision=revision)
                self.assertEqual(release.containers.get().env, {"PORT": value})
                self.assertEqual(release.containers.get().env_rules["PORT"], {"kind": "env", "required": False})
                self.assertEqual(release.repository_env["missing"], [])
                self.assertEqual(self.activate(release.pk).status_code, 200)

    def test_preserved_missing_rules_cannot_exceed_environment_limit(self):
        self.files["env/app.env"] = "PORT=8080\n"
        first = self.register()
        self.auth("root")
        self.assertEqual(self.activate(first.pk).status_code, 200)
        self.files["env/app.env"] = "\n".join(f"VALUE_{index}=1" for index in range(128))
        release = self.register(revision=2)
        self.assertLessEqual(len(release.containers.get().env_rules), 128)
        self.assertEqual(self.activate(release.pk).status_code, 400)
        self.assertEqual(ChallengeRuntimeConfig.objects.get(challenge=self.challenge).current_release_id, first.pk)

    def test_other_container_secret_does_not_resolve_missing_value(self):
        self.files["info.yaml"] = INFO + "    - name: db\n      build: ./db\n"
        self.files["env/app.env"] = "PORT=8080\nDB_PASS=\n"
        self.files["env/db.env"] = "DB_PASS=local-db-value\n"
        artifact = self.bundle()
        artifact["workload"]["containers"].append({
            "name": "db", "image": "ghcr.io/msg-ctf/challenges/web-basic/db@sha256:" + "b" * 64,
            "ports": [{"port": 5432, "public": False}],
        })
        status, source = register_bundle(artifact)
        self.assertEqual(status, "registered", source)
        db = source.containers.get(name="db")
        secret = RuntimeSecret.objects.get(pk=db.secret_env["DB_PASS"])
        self.auth("root")
        response = self.client.post(f"{self.base_url}/{source.pk}/derive", {"containers": [
            {"name": "app", "env": {"PORT": "8080"}, "secret_env": {}},
            {"name": "db", "env": {}, "secret_env": {"DB_PASS": secret.name}},
        ]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        derived = ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])
        self.assertEqual(derived.repository_env["missing"], [
            {"container": "app", "name": "DB_PASS", "reason": "no_value"},
        ])
        self.assertEqual(self.activate(derived.pk).status_code, 400)
