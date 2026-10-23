from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import override_settings

from apps.instances.models import ChallengeRuntimeConfig, RuntimeSecret
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
