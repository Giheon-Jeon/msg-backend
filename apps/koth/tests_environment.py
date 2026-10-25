import copy
import json
import secrets
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.adminpanel.models import AdminEvent
from apps.instances.runtime_secrets import RuntimeSecretUnavailable
from .environment import agent_token_hash, create_version, decode_settings
from .models import KothChallenge, KothClub, KothEnvironmentApply, KothEnvironmentConfig, KothEnvironmentVersion

SHA = "d" * 40
IMAGE = "ghcr.io/msg-ctf/challenges/koth-test/service@sha256:" + "a" * 64


class Snapshot:
    files = {"info.yaml": "deployment:\n  runtime_type: KUBERNETES\n  containers:\n    - name: service\n      build: ./service\n",
             "env/service.env": "PORT=9090\nINTERNAL_TOKEN=author-placeholder\nOPTION\n"}

    def __init__(self, slug, revision=None):
        self.slug, self.revision = slug, revision or SHA

    def read(self, path):
        return self.files[path]


@override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()])
class EnvironmentTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(login_id="env-admin", nickname="Admin", role="ADMIN")
        self.participant = User.objects.create_user(login_id="env-participant", nickname="Participant")
        self.client = APIClient(); self.client.force_authenticate(self.admin)
        self.node = APIClient(); self.token = secrets.token_urlsafe(40)
        self.node.credentials(HTTP_AUTHORIZATION="Bearer " + self.token)
        self.challenge = KothChallenge.objects.create(club=KothClub.objects.create(name="Env club"), title="Game",
            open_group=1, inbound_internal_token_hash="a" * 64, repository_slug="koth-test", source_revision=SHA)
        self.settings = {"containers": [{"name": "service", "image": IMAGE, "entries": {
            "PORT": self.entry("8080", "env"),
            "INTERNAL_TOKEN": self.entry("test-runtime-internal-value", "secret", locked=True, operator_override=True),
            "OPTION": self.entry(None, "secret", required=False),
            "EMPTY": self.entry("", "env", required=False),
        }}]}
        self.source = create_version(self.challenge, self.settings, SHA, self.admin.login_id, uuid.uuid4(), "f" * 64)
        self.application = KothEnvironmentApply.objects.create(application_id=uuid.uuid4(), challenge=self.challenge,
            version=self.source, request_fingerprint="a" * 64, requested_by=self.admin.login_id)
        self.config = KothEnvironmentConfig.objects.create(challenge=self.challenge, target_id="test-vm", application=self.application,
            applied_version=self.source, state="READY", agent_token_hash=agent_token_hash(self.token))
        self.base = f"/api/v1/admin/koth/challenges/{self.challenge.pk}/environment"
        self.agent = f"/internal/v1/koth-environments/{self.challenge.pk}"

    @staticmethod
    def entry(value, kind, required=True, locked=False, operator_override=False):
        return {"value": value, "kind": kind, "required": required, "locked": locked, "operator_override": operator_override}

    def body(self, content="PORT=8081", **extra):
        return {"base_version_id": str(self.source.pk), "container": "service", "method": "file",
                "content": content, "request_id": str(uuid.uuid4()), **extra}

    def save(self, body=None):
        response = self.client.post(self.base + "/versions", body or self.body(), format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return KothEnvironmentVersion.objects.get(pk=response.data["data"]["version_id"])

    def apply(self, version, request_id=None):
        return self.client.post(f"{self.base}/versions/{version.pk}/apply", {"request_id": request_id or str(uuid.uuid4())}, format="json")

    def status(self, application_id, version, state="APPLIED", **extra):
        return self.node.post(self.agent + "/status", {"application_id": str(application_id), "version_id": str(version.pk), "state": state, **extra}, format="json")

    def test_storage_and_admin_responses_never_include_secret_plaintext(self):
        self.assertNotIn("test-runtime-internal-value", self.source.encrypted_settings)
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("test-runtime-internal-value", json.dumps(response.data))
        self.assertNotIn(self.token, json.dumps(response.data))
        rows = {r["name"]: r for r in response.data["data"]["selected"]["containers"][0]["entries"]}
        self.assertEqual(rows["PORT"]["value"], "8080")
        self.assertNotIn("value", rows["INTERNAL_TOKEN"])
        self.assertTrue(rows["INTERNAL_TOKEN"]["locked"])

    def test_only_admins_can_preview_save_or_apply(self):
        self.client.force_authenticate(self.participant)
        for method, path, body in (("get", self.base, None), ("post", self.base + "/preview", self.body()),
                                  ("post", self.base + "/versions", self.body()),
                                  ("post", f"{self.base}/versions/{self.source.pk}/apply", {"request_id": str(uuid.uuid4())})):
            response = getattr(self.client, method)(path, body, format="json")
            self.assertEqual(response.status_code, 403)

    def test_immutable_save_keeps_other_values_and_does_not_apply(self):
        version = self.save()
        self.assertEqual(decode_settings(version)["containers"][0]["entries"]["PORT"]["value"], "8081")
        self.assertEqual(decode_settings(self.source), self.settings)
        self.config.refresh_from_db()
        self.assertEqual(self.config.applied_version_id, self.source.pk)
        self.assertEqual(self.config.application_id, self.application.pk)

    def test_optional_unset_is_distinct_from_an_empty_string(self):
        version = self.save(self.body("OPTION\nEMPTY="))
        entries = decode_settings(version)["containers"][0]["entries"]
        self.assertIsNone(entries["OPTION"]["value"])
        self.assertEqual(entries["EMPTY"]["value"], "")

    def test_required_and_protected_values_cannot_be_unset(self):
        for content in ("PORT", "INTERNAL_TOKEN", "INTERNAL_TOKEN=changed", "KOTH_CHALLENGE_ID=other"):
            response = self.client.post(self.base + "/preview", self.body(content), format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(KothEnvironmentVersion.objects.count(), 1)

    def test_secret_cannot_be_downgraded_or_echoed(self):
        response = self.client.post(self.base + "/preview", self.body("OPTION=value", rules={"OPTION": {"kind": "env"}}), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("test-runtime-internal-value", json.dumps(response.data))

    def test_save_retry_is_durable_and_conflicting_key_is_rejected(self):
        body = self.body(); first = self.save(body); second = self.save(body)
        self.assertEqual(first.pk, second.pk)
        changed = {**body, "content": "PORT=8090"}
        response = self.client.post(self.base + "/versions", changed, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(KothEnvironmentVersion.objects.count(), 2)

    def test_stale_editor_cannot_overwrite_a_new_saved_version(self):
        self.save()
        response = self.client.post(self.base + "/versions", self.body("PORT=8090"), format="json")
        self.assertEqual(response.status_code, 409)

    def test_repository_uses_pinned_commit_and_keeps_operator_tokens(self):
        body = self.body(method="repository"); body.pop("content")
        with patch("apps.koth.environment.RepositorySnapshot", Snapshot):
            version = self.save(body)
        entries = decode_settings(version)["containers"][0]["entries"]
        self.assertEqual(entries["PORT"]["value"], "9090")
        self.assertEqual(entries["INTERNAL_TOKEN"]["value"], "test-runtime-internal-value")
        self.assertIsNone(entries["OPTION"]["value"])
        self.assertEqual(version.source_revision, SHA)

    def test_source_revision_change_blocks_saving_and_application(self):
        self.challenge.source_revision = "e" * 40; self.challenge.save(update_fields=["source_revision"])
        self.assertEqual(self.client.post(self.base + "/versions", self.body(), format="json").status_code, 409)
        self.assertEqual(self.apply(self.source).status_code, 409)

    def test_ciphertext_cannot_be_rebound_to_another_version(self):
        other = self.save()
        other.encrypted_settings = self.source.encrypted_settings
        with self.assertRaises(RuntimeSecretUnavailable):decode_settings(other)

    def test_apply_does_not_claim_success_until_the_agent_reports_it(self):
        version = self.save(); response = self.apply(version)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["state"]["state"], "QUEUED")
        self.config.refresh_from_db(); self.assertEqual(self.config.applied_version_id, self.source.pk)
        application_id = response.data["data"]["application_id"]
        response = self.status(application_id, version)
        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db(); self.assertEqual(self.config.applied_version_id, version.pk)
        self.assertEqual(self.config.state, "READY")

    def test_old_apply_retry_cannot_roll_back_the_next_request(self):
        version = self.save(); key = str(uuid.uuid4())
        first = self.apply(version, key); self.assertEqual(first.status_code, 200)
        newer = self.apply(self.source); self.assertEqual(newer.status_code, 200)
        replay = self.apply(version, key)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.data["data"]["superseded"])
        self.config.refresh_from_db(); self.assertEqual(str(self.config.application_id), newer.data["data"]["application_id"])

    def test_late_old_agent_ack_does_not_change_the_new_application(self):
        version = self.save(); first = self.apply(version).data["data"]
        newer = self.apply(self.source).data["data"]
        response = self.status(first["application_id"], version)
        self.assertEqual(response.status_code, 409)
        self.config.refresh_from_db(); self.assertEqual(str(self.config.application_id), newer["application_id"])
        self.assertEqual(self.config.applied_version_id, self.source.pk)

    def test_repeated_agent_ack_does_not_repeat_audit_or_regress_success(self):
        version = self.save(); app = self.apply(version).data["data"]["application_id"]
        self.assertEqual(self.status(app, version).status_code, 200)
        count = AdminEvent.objects.count()
        self.assertEqual(self.status(app, version).status_code, 200)
        self.assertEqual(self.status(app, version, "FAILED", error_code="ROLLOUT_FAILED").status_code, 200)
        self.config.refresh_from_db(); self.assertEqual(self.config.state, "READY")
        self.assertEqual(AdminEvent.objects.count(), count)

    def test_running_game_blocks_new_application(self):
        version = self.save()
        self.challenge.status = "ACTIVE"; self.challenge.save(update_fields=["status"])
        self.assertEqual(self.apply(version).status_code, 409)
        response = self.node.get(self.agent)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["data"]["can_apply"])

    def test_wrong_or_participant_tokens_cannot_read_agent_settings(self):
        self.node.credentials(HTTP_AUTHORIZATION="Bearer " + secrets.token_urlsafe(40))
        response = self.node.get(self.agent)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("test-runtime-internal-value", json.dumps(response.data))
        self.node.credentials(); self.assertEqual(self.node.get(self.agent).status_code, 401)

    def test_same_application_poll_does_not_decrypt_or_repeat_the_values(self):
        with patch("apps.koth.environment_views.decode_settings", side_effect=AssertionError("poll decrypted values")):
            response = self.node.get(self.agent, {"known_application_id": str(self.application.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["data"]["settings"])
        self.assertFalse(response.data["data"]["changed"])

    def test_agent_failure_codes_do_not_accept_arbitrary_log_text(self):
        version = self.save(); app = self.apply(version).data["data"]["application_id"]
        response = self.status(app, version, "FAILED", error_code="private log value")
        self.assertEqual(response.status_code, 400)
        self.config.refresh_from_db(); self.assertEqual(self.config.error_code, "")
