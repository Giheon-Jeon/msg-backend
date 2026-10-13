import copy
import json
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import override_settings
from rest_framework.test import APIClient

from apps.challenge.models import Challenge
from apps.instances.models import ChallengeRelease, RuntimeSecret
from apps.instances.poller import poll_once, register_bundle
from apps.instances.models import PollerArtifact
from apps.instances.releases import ReleaseValidationError, create_release, validate_release_payload
from apps.instances.services import SchedulerError, build_scheduler_create_body, call_scheduler_create, create_instance_from_scheduler, update_instance_from_scheduler
from apps.instances.tests_releases import ReleaseTestBase, artifact_payload

TEST_KEY = Fernet.generate_key().decode()
TEST_TOKEN = "execution-contract-runtime-worker-token"


@override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[TEST_KEY], RUNTIME_SECRET_API_TOKEN=TEST_TOKEN)
class ExecutionSettingsTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.auth("root")
        self.secret_url = f"/api/v1/admin/challenges/{self.challenge.pk}/runtime-secrets"

    def secret(self, name="flag", value="MSG{flag}"):
        return self.client.post(self.secret_url, {"name": name, "value": value}, format="json")

    def release(self, revision=1, env=None, aliases=None):
        body = artifact_payload(revision=revision)
        body["artifact"]["schema_version"] = "2.1"
        body["artifact"]["workload"]["containers"][0].update({
            "env": env or {"APP_MODE": "ctf"}, "secret_env": aliases or {"FLAG": "flag"},
        })
        response = self.client.post(self.base_url, body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])

    def resolve(self, release_container, token=TEST_TOKEN, **overrides):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        body = {"secret_ref": str(release_container.id), "container": release_container.name, "image": release_container.image_ref}
        body.update(overrides)
        return client.post("/internal/v1/runtime-secrets/resolve", body, format="json")

    def test_encrypted_flag_is_bound_to_approved_release_and_not_exposed_in_metadata(self):
        response = self.secret()
        self.assertEqual(response.status_code, 200)
        stored = RuntimeSecret.objects.get(pk=response.data["data"]["secret_id"])
        self.assertNotIn("MSG{flag}", stored.encrypted_value)
        self.assertNotIn("value", response.data["data"])
        release = self.release()
        container = release.containers.get()
        self.assertEqual(container.secret_env, {"FLAG": str(stored.pk)})
        self.assertEqual(self.resolve(container).status_code, 404)
        self.assertEqual(self.activate(release.pk).status_code, 200)
        resolved = self.resolve(container)
        self.assertEqual(resolved.data["data"]["env"], {"FLAG": "MSG{flag}"})
        self.assertIn("no-store", resolved["Cache-Control"])
        self.assertNotIn("MSG{flag}", json.dumps(self.client.get(self.base_url).data))

    def test_general_env_and_reference_survive_database_and_scheduler_serialization(self):
        self.secret()
        release = self.release(env={"APP_MODE": "ctf", "LANG": "ko_KR.UTF-8"})
        self.activate(release.pk)
        self.challenge.refresh_from_db()
        config = self.challenge.runtime_config
        body = build_scheduler_create_body(self.player, self.team, self.challenge, config, release)
        self.assertEqual(body["containers"][0]["env"], {"APP_MODE": "ctf", "LANG": "ko_KR.UTF-8"})
        self.assertEqual(body["containers"][0]["secret_ref"], str(release.containers.get().id))
        self.assertNotIn("MSG{flag}", json.dumps(body))
        self.assertNotIn("secret_env", body["containers"][0])
        self.assertEqual(body["isolation_profile"], "WEB")

    def test_large_container_environment_preserves_all_values_at_the_boundary(self):
        from apps.instances.execution_settings import validate_env, validate_secret_env
        self.secret()
        env = {f"SETTING_{index}": str(index) for index in range(127)}
        release = self.release(env=env)
        self.activate(release.pk)
        self.challenge.refresh_from_db()
        body = build_scheduler_create_body(self.player, self.team, self.challenge, self.challenge.runtime_config, release)
        self.assertEqual(body["containers"][0]["env"], env)
        self.assertEqual(len(self.resolve(release.containers.get()).data["data"]["env"]), 1)
        self.assertEqual(len(validate_env({f"V{i}": "x" for i in range(128)})), 128)
        with self.assertRaises(ValueError):
            validate_env({f"V{i}": "x" for i in range(129)})
        with self.assertRaises(ValueError):
            validate_secret_env({"FLAG": "flag"}, {f"V{i}": "x" for i in range(128)})

    def test_secret_rotation_preserves_previous_release_binding(self):
        self.secret("internal_token", "first-test-value")
        self.secret()
        old = self.release(aliases={"FLAG": "flag", "INTERNAL_TOKEN": "internal_token"})
        self.activate(old.pk)
        self.secret("internal_token", "second-test-value")
        new = self.release(revision=2, aliases={"FLAG": "flag", "INTERNAL_TOKEN": "internal_token"})
        self.activate(new.pk)
        self.assertEqual(self.resolve(old.containers.get()).data["data"]["env"]["INTERNAL_TOKEN"], "first-test-value")
        self.assertEqual(self.resolve(new.containers.get()).data["data"]["env"]["INTERNAL_TOKEN"], "second-test-value")

    def test_admin_metadata_reports_saved_versions_and_immutable_bindings_without_decryption(self):
        self.secret()
        self.secret("internal_token", "first-test-value")
        old = self.release(aliases={"FLAG": "flag", "INTERNAL_TOKEN": "internal_token"})
        self.activate(old.pk)
        self.secret("internal_token", "second-test-value")
        with patch("apps.instances.runtime_secrets.decrypt_runtime_secret", side_effect=AssertionError("Metadata must not decrypt")):
            secrets = self.client.get(self.secret_url)
            releases = self.client.get(self.base_url)
        self.assertEqual(secrets.status_code, 200)
        self.assertEqual(releases.status_code, 200)
        tokens = [row for row in secrets.data["data"]["secrets"] if row["name"] == "internal_token"]
        self.assertEqual([(row["version"], row["is_latest"]) for row in tokens], [(2, True), (1, False)])
        bindings = {row["env_name"]: row for row in releases.data["data"]["releases"][0]["containers"][0]["secret_bindings"]}
        self.assertEqual(bindings["INTERNAL_TOKEN"], {"env_name": "INTERNAL_TOKEN", "name": "internal_token", "version": 1, "status": "registered", "is_latest": False})
        self.assertTrue(bindings["FLAG"]["is_latest"])
        for response in (secrets, releases):
            serialized = json.dumps(response.data)
            for forbidden in ("MSG{flag}", "first-test-value", "second-test-value", "encrypted_value", "flag_hash"):
                self.assertNotIn(forbidden, serialized)
            self.assertIn("no-store", response["Cache-Control"])

    def test_runtime_secret_metadata_is_admin_only_and_challenge_scoped(self):
        self.secret()
        self.auth("player")
        self.assertEqual(self.client.get(self.secret_url).status_code, 403)
        anonymous = APIClient()
        self.assertEqual(anonymous.get(self.secret_url).status_code, 401)
        self.auth("root")
        other = Challenge.objects.create(title="Other", category="WEB", difficulty="EASY", score=100, flag_hash="other")
        url = f"/api/v1/admin/challenges/{other.pk}/runtime-secrets"
        self.assertEqual(self.client.get(url).data["data"]["secrets"], [])
        self.assertEqual(self.client.get(f"/api/v1/admin/challenges/{uuid.uuid4()}/runtime-secrets").status_code, 404)

    def test_release_metadata_does_not_reveal_a_cross_challenge_secret_binding(self):
        self.secret()
        release = self.release()
        other = Challenge.objects.create(title="Other", category="WEB", difficulty="EASY", score=100, flag_hash="other")
        foreign = RuntimeSecret.objects.create(challenge=other, name="foreign_only", version=1, encrypted_value="not-read", created_by="root")
        container = release.containers.get()
        container.secret_env = {"FLAG": str(foreign.pk)}
        container.save(update_fields=["secret_env"])
        response = self.client.get(self.base_url)
        binding = response.data["data"]["releases"][0]["containers"][0]["secret_bindings"][0]
        self.assertEqual(binding, {"env_name": "FLAG", "name": None, "version": None, "status": "missing", "is_latest": False})
        self.assertNotIn("foreign_only", json.dumps(response.data))

    def test_execution_settings_require_schema_21_and_scheduler_v2(self):
        for field in ("env", "secret_env"):
            body = artifact_payload()
            body["artifact"]["workload"]["containers"][0][field] = {}
            self.assertEqual(self.client.post(self.base_url, body, format="json").status_code, 400)
        self.secret()
        release = self.release()
        self.activate(release.pk)
        self.challenge.refresh_from_db()
        with patch("apps.instances.services.scheduler_request", return_value={}) as request:
            call_scheduler_create(self.player, self.team, self.challenge, self.challenge.runtime_config, release)
        self.assertEqual(request.call_args.args[:2], ("POST", "/api/v2/instances"))

    def test_admin_can_derive_settings_from_existing_20_image_without_changing_source(self):
        base = self.client.post(self.base_url, artifact_payload(), format="json")
        self.assertEqual(base.status_code, 200, base.data)
        source = ChallengeRelease.objects.get(pk=base.data["data"]["release_id"])
        source_container = source.containers.get()
        self.secret()
        url = f"{self.base_url}/{source.pk}/derive"
        body = {"containers": [{"name": source_container.name, "env": {"APP_MODE": "ctf"}, "secret_env": {"FLAG": "flag"}}]}
        response = self.client.post(url, body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        derived = ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])
        self.assertEqual(derived.derived_from_id, source.pk)
        self.assertEqual(derived.registry_revision, source.registry_revision)
        self.assertEqual(derived.version, source.version + 1)
        self.assertEqual(derived.containers.get().image_ref, source_container.image_ref)
        self.assertEqual(derived.containers.get().ports, source_container.ports)
        source_container.refresh_from_db()
        self.assertEqual(source_container.env, {})
        self.assertEqual(source_container.secret_env, {})
        self.assertEqual(self.activate(derived.pk).status_code, 200)
        self.challenge.refresh_from_db()
        scheduler_body = build_scheduler_create_body(self.player, self.team, self.challenge, self.challenge.runtime_config, derived)
        self.assertEqual(scheduler_body["containers"][0]["env"], {"APP_MODE": "ctf"})
        self.assertEqual(scheduler_body["release_id"], str(derived.pk))
        self.assertEqual(scheduler_body["containers"][0]["secret_ref"], str(derived.containers.get().pk))
        self.assertEqual(self.resolve(derived.containers.get()).data["data"]["env"], {"FLAG": "MSG{flag}"})
        self.assertNotIn("MSG{flag}", json.dumps(response.data))

    def test_derived_settings_validate_scope_container_and_secret_alias(self):
        base = self.client.post(self.base_url, artifact_payload(), format="json")
        self.assertEqual(base.status_code, 200, base.data)
        source_id = base.data["data"]["release_id"]
        url = f"{self.base_url}/{source_id}/derive"
        valid = {"containers": [{"name": "web", "env": {}, "secret_env": {}}]}
        source_name = ChallengeRelease.objects.get(pk=source_id).containers.get().name
        valid["containers"][0]["name"] = source_name
        invalid = (
            {"containers": []},
            {"containers": [{"name": "other", "env": {}, "secret_env": {}}]},
            {"containers": [{"name": source_name, "env": {}, "secret_env": {}, "image": "other"}]},
            {"containers": [{"name": source_name, "env": {"FLAG": "raw"}, "secret_env": {}}]},
            {"containers": [{"name": source_name, "env": {}, "secret_env": {"FLAG": "flag"}}]},
            {**valid, "artifact": artifact_payload()["artifact"]},
        )
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(url, body, format="json").status_code, 400)
        self.assertEqual(ChallengeRelease.objects.count(), 1)
        self.auth("player")
        self.assertEqual(self.client.post(url, valid, format="json").status_code, 403)
        self.auth("root")
        other = Challenge.objects.create(title="Other", category="WEB", difficulty="EASY", score=100, flag_hash="other")
        self.assertEqual(self.client.post(f"/api/v1/admin/challenges/{other.pk}/releases/{source_id}/derive", valid, format="json").status_code, 404)

    def test_scheduler_recovery_uses_release_id_when_versions_share_revision(self):
        base = self.client.post(self.base_url, artifact_payload(), format="json")
        self.assertEqual(base.status_code, 200, base.data)
        source = ChallengeRelease.objects.get(pk=base.data["data"]["release_id"])
        derived = self.client.post(f"{self.base_url}/{source.pk}/derive", {
            "containers": [{"name": source.containers.get().name, "env": {"APP_MODE": "ctf"}, "secret_env": {}}],
        }, format="json")
        self.assertEqual(derived.status_code, 200, derived.data)
        scheduler_data = {
            "instance_id": str(uuid.uuid4()), "challenge_id": str(self.challenge.pk),
            "registry_revision": source.registry_revision, "status": "RUNNING",
            "service_url": "https://instance.example", "expires_at": "2026-11-08T10:00:00Z",
            "hard_expires_at": "2026-11-08T11:00:00Z",
        }
        with self.assertRaises(SchedulerError):
            create_instance_from_scheduler(scheduler_data, user=self.player, team=self.team, challenge=self.challenge)
        scheduler_data["release_id"] = derived.data["data"]["release_id"]
        restored = create_instance_from_scheduler(scheduler_data, user=self.player, team=self.team, challenge=self.challenge)
        self.assertEqual(str(restored.release_id), scheduler_data["release_id"])
        with self.assertRaises(SchedulerError):
            update_instance_from_scheduler(restored, {"release_id": str(source.pk), "status": "RUNNING"})
        scheduler_data["release_id"] = str(uuid.uuid4())
        with self.assertRaises(SchedulerError):
            create_instance_from_scheduler(scheduler_data, user=self.player, team=self.team, challenge=self.challenge)

    def test_participant_create_accepts_only_challenge_id(self):
        self.auth("player")
        for extra in ("env", "secret_env", "secret_ref", "containers", "isolation_profile"):
            body = {"challenge_id": str(self.challenge.pk), extra: "test-only"}
            response = self.client.post("/api/v1/instances", body, format="json")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["code"], "INVALID_REQUEST")
        for challenge_id in (None, {}, [], 1, "invalid", str(uuid.UUID(int=0))):
            response = self.client.post("/api/v1/instances", {"challenge_id": challenge_id}, format="json")
            self.assertEqual(response.status_code, 400)

    def test_invalid_schema_types_return_validation_error(self):
        for schema in (None, {}, [], 1, True):
            body = artifact_payload()
            body["artifact"]["schema_version"] = schema
            self.assertEqual(self.client.post(self.base_url, body, format="json").status_code, 400)

    def test_poller_retries_unavailable_encryption_configuration(self):
        self.secret()
        self.challenge.challenge_slug = "web-basic"
        self.challenge.save(update_fields=["challenge_slug"])
        body = artifact_payload()["artifact"]
        body["schema_version"] = "2.1"
        body["workload"]["containers"][0]["secret_env"] = {"FLAG": "flag"}
        artifact = {"id": 98123, "name": "fixture-publish-bundle"}
        PollerArtifact.objects.create(artifact_id=98123, kind=PollerArtifact.Kind.RELEASE, payload=artifact)
        with patch("apps.instances.poller.list_bundle_artifacts", return_value=[artifact]), \
             patch("apps.instances.poller.get_workflow_run", return_value={"status": "completed"}), \
             patch("apps.instances.poller.validate_workflow_run_source"), \
             patch("apps.instances.poller.download_bundle", return_value=body), \
             patch("apps.instances.poller.validate_bundle_source"):
            with override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[]):
                self.assertEqual(poll_once()["error"], 1)
                self.assertFalse(PollerArtifact.objects.filter(pk=98123, processed_at__isnull=False).exists())
            self.assertEqual(poll_once()["registered"], 1)
            self.assertIsNotNone(PollerArtifact.objects.get(pk=98123).processed_at)

    def test_resolver_requires_worker_token_not_admin_or_participant_jwt(self):
        self.secret()
        release = self.release()
        self.activate(release.pk)
        container = release.containers.get()
        self.assertEqual(self.resolve(container, token="wrong-test-token").status_code, 401)
        body = {"secret_ref": str(container.pk), "container": container.name, "image": container.image_ref}
        self.assertEqual(self.client.post("/internal/v1/runtime-secrets/resolve", body, format="json").status_code, 401)
        self.auth("player")
        self.assertEqual(self.secret().status_code, 403)

    def test_reference_is_bound_to_container_name_and_exact_image(self):
        self.secret()
        release = self.release()
        self.activate(release.pk)
        container = release.containers.get()
        for overrides in (
            {"container": "other"}, {"image": container.image_ref[:-1] + "b"},
            {"secret_ref": str(uuid.uuid4())},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.resolve(container, **overrides).status_code, 404)

    def test_invalid_or_null_ref_is_rejected_without_echo(self):
        self.secret()
        container = self.release().containers.get()
        for ref in (None, {}, [], "MSG{must-not-be-echoed}", str(uuid.UUID(int=0))):
            with self.subTest(ref=type(ref).__name__):
                response = self.resolve(container, secret_ref=ref)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("must-not-be-echoed", json.dumps(response.data))

    def test_flag_must_match_existing_judge_hash(self):
        response = self.secret(value="MSG{different-test-value}")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(RuntimeSecret.objects.count(), 0)
        self.assertNotIn("different-test-value", json.dumps(response.data))

    def test_missing_secret_aborts_manual_and_poller_registration(self):
        self.challenge.challenge_slug = "web-basic"
        self.challenge.save(update_fields=["challenge_slug"])
        body = artifact_payload()
        body["artifact"]["schema_version"] = "2.1"
        body["artifact"]["workload"]["containers"][0]["secret_env"] = {"FLAG": "flag"}
        self.assertEqual(self.client.post(self.base_url, body, format="json").status_code, 400)
        status, _ = register_bundle(body["artifact"])
        self.assertEqual(status, "invalid")
        self.assertEqual(ChallengeRelease.objects.count(), 0)

    def test_secret_of_other_challenge_cannot_satisfy_alias(self):
        self.secret()
        other = Challenge.objects.create(
            title="Other", challenge_slug="other", category="WEB", difficulty="EASY",
            score=500, description="fixture", flag_hash=self.challenge.flag_hash,
        )
        body = artifact_payload(slug="other")
        body["artifact"]["schema_version"] = "2.1"
        body["artifact"]["workload"]["containers"][0]["secret_env"] = {"FLAG": "flag"}
        with self.assertRaisesRegex(ReleaseValidationError, "등록되지 않은"):
            create_release(other, validate_release_payload(body), "root")

    def test_wrong_key_and_ciphertext_substitution_fail_closed(self):
        self.secret()
        self.secret("internal_token", "local-test-token-value")
        release = self.release(aliases={"FLAG": "flag", "INTERNAL_TOKEN": "internal_token"})
        self.activate(release.pk)
        container = release.containers.get()
        with override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()]):
            response = self.resolve(container)
            self.assertEqual(response.status_code, 503)
        flag = RuntimeSecret.objects.get(name="flag")
        other = RuntimeSecret.objects.get(name="internal_token")
        flag.encrypted_value = other.encrypted_value
        flag.save(update_fields=["encrypted_value"])
        self.assertEqual(self.resolve(container).status_code, 503)

    def test_previous_encryption_key_can_remain_for_rotation(self):
        self.secret()
        release = self.release()
        self.activate(release.pk)
        with override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode(), TEST_KEY]):
            self.assertEqual(self.resolve(release.containers.get()).status_code, 200)

    def test_missing_keys_and_worker_token_return_service_unavailable(self):
        with override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[]):
            self.assertEqual(self.secret().status_code, 503)
        self.secret()
        container = self.release().containers.get()
        with override_settings(RUNTIME_SECRET_API_TOKEN=""):
            self.assertEqual(self.resolve(container).status_code, 503)

    def test_changed_judge_flag_blocks_resolution(self):
        self.secret()
        release = self.release()
        self.activate(release.pk)
        self.challenge.flag_hash = "0" * 64
        self.challenge.save(update_fields=["flag_hash"])
        self.assertEqual(self.resolve(release.containers.get()).status_code, 503)

    def test_invalid_env_or_secret_alias_is_rejected_at_release_registration(self):
        examples = (
            {"env": {"FLAG": "raw-test-value"}},
            {"env": {"DB_PASSWORD": "raw-test-value"}},
            {"env": {"APP_MODE": 1}},
            {"env": {"lowercase": "value"}},
            {"env": {"APP_MODE": "\x00"}},
            {"env": {"APP_MODE": "x" * 4097}},
            {"env": None},
            {"secret_env": {"FLAG": "different_name"}},
            {"secret_env": {"FLAG": str(uuid.uuid4())}},
            {"secret_env": {"FLAG": "MSG{must-not-be-echoed}"}},
            {"env": {"APP_MODE": "plain"}, "secret_env": {"APP_MODE": "mode"}},
            {"environment": {"APP_MODE": "ctf"}},
        )
        for fields in examples:
            with self.subTest(field_names=list(fields)):
                body = copy.deepcopy(artifact_payload())
                body["artifact"]["schema_version"] = "2.1"
                body["artifact"]["workload"]["containers"][0].update(fields)
                response = self.client.post(self.base_url, body, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("raw-test-value", json.dumps(response.data))
                self.assertNotIn("must-not-be-echoed", json.dumps(response.data))
