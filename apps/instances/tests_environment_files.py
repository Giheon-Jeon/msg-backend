import copy
import json
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import SimpleTestCase, override_settings

from apps.challenge.models import Challenge
from apps.instances.environment_files import parse_environment_file
from apps.instances.models import ChallengeRelease, RuntimeSecret
from apps.instances.releases import ReleaseValidationError, serialize_release
from apps.instances.runtime_secrets import create_runtime_secret, decrypt_runtime_secret
from apps.instances.tests_releases import ReleaseTestBase, artifact_payload


class EnvironmentFileParserTests(SimpleTestCase):
    def test_bom_crlf_quotes_and_literal_characters(self):
        content = '\ufeff# comment\r\nPORT=8080\r\nTEXT="  한글 # $VALUE ${OTHER}=yes \\n  "\r\nEMPTY=\r\n'
        self.assertEqual(parse_environment_file(content), {
            "PORT": "8080", "TEXT": "  한글 # $VALUE ${OTHER}=yes \\n  ", "EMPTY": "",
        })
        self.assertEqual(parse_environment_file("TEXT=a=b # literal"), {"TEXT": "a=b # literal"})

    def test_invalid_files_fail_without_echoing_line_contents(self):
        for content in ("A=one\nA=private-value", "export A=private-value", 'A="private-value',
                        "A=private-value\rB=two", "A=private\0value", "# comment", "A=\ud800"):
            with self.subTest(content_type=type(content).__name__):
                with self.assertRaises(ReleaseValidationError) as raised:
                    parse_environment_file(content)
                self.assertNotIn("private", str(raised.exception))

    def test_size_and_entry_limits_use_utf8_bytes(self):
        self.assertEqual(parse_environment_file("A=" + "한" * 1365)["A"], "한" * 1365)
        for content in ("A=" + "한" * 1366, "#" + "x" * 65536,
                        "\n".join(f"KEY_{i}=x" for i in range(129)),
                        "\n".join(f"KEY_{i}=" + "a" * 4096 for i in range(4))):
            with self.assertRaises(ReleaseValidationError):
                parse_environment_file(content)


@override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()])
class EnvironmentFileImportTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.auth("root")
        self.flag = create_runtime_secret(self.challenge, "flag", "MSG{flag}", "root")
        self.shared = create_runtime_secret(self.challenge, "shared", "first-local-value", "root")
        body = artifact_payload(containers=[
            {"name": "app", "image": "ghcr.io/msg-ctf/challenges/web-basic/app@sha256:" + "a" * 64,
             "ports": [{"port": 8080, "public": True}], "env": {"PORT": "8080", "KEEP": "original"},
             "secret_env": {"FLAG": "flag", "INTERNAL_TOKEN": "shared"}},
            {"name": "helper", "image": "ghcr.io/msg-ctf/challenges/web-basic/helper@sha256:" + "b" * 64,
             "ports": [{"port": 9090, "public": False}], "secret_env": {"INTERNAL_TOKEN": "shared"}},
        ])
        body["artifact"]["schema_version"] = "2.1"
        response = self.client.post(self.base_url, body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.source = ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])
        self.activate(self.source.pk)
        self.url = f"{self.base_url}/{self.source.pk}/environment_files"

    def preview(self, content, entries=None, **overrides):
        body = {"container": "app", "content": content, **overrides}
        if entries is not None:
            body["entries"] = entries
        return self.client.post(self.url + "/preview", body, format="json")

    def import_body(self, content, change=None):
        response = self.preview(content)
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]["entries"]
        entries = [{key: row[key] for key in ("name", "kind", "required", "secret_name", "use_existing", "secret_id") if key in row} for row in rows]
        if change:
            change(entries)
        return {"container": "app", "content": content, "entries": entries, "request_id": str(uuid.uuid4())}

    def save(self, body):
        return self.client.post(self.url + "/import", body, format="json")

    def test_preview_is_read_only_and_masks_new_or_known_secrets(self):
        before = (RuntimeSecret.objects.count(), ChallengeRelease.objects.count())
        with patch("apps.instances.runtime_secrets.decrypt_runtime_secret", side_effect=AssertionError("preview must not decrypt")):
            response = self.preview("PORT=8081\nDB_PASS=private-file-value\nNEW_VALUE=another-private-value")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        data = response.data["data"]
        self.assertEqual(data["entries"][0]["value"], "8081")
        self.assertTrue(all(row["kind"] == "secret" and "value" not in row for row in data["entries"][1:]))
        self.assertNotIn("private", json.dumps(response.data))
        self.assertEqual(before, (RuntimeSecret.objects.count(), ChallengeRelease.objects.count()))

    def test_known_secret_cannot_be_echoed_as_general_value(self):
        response = self.preview("DB_PASS=private-file-value", [{"name": "DB_PASS", "kind": "env", "required": True}])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["data"]["can_import"])
        self.assertNotIn("private-file-value", json.dumps(response.data))
        body = self.import_body("DB_PASS=private-file-value", lambda rows: rows[0].update(kind="env"))
        self.assertEqual(self.save(body).status_code, 400)

    def test_import_preserves_image_other_containers_and_pinned_versions(self):
        create_runtime_secret(self.challenge, "shared", "second-local-value", "root")
        content = 'PORT=8081\nDB_PASS="local-$literal # = \\value"\nFLAG=\n'
        response = self.save(self.import_body(content))
        self.assertEqual(response.status_code, 200, response.data)
        release = ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])
        self.assertEqual(release.derived_from_id, self.source.pk)
        self.assertEqual(release.registry_revision, self.source.registry_revision)
        self.assertFalse(response.data["data"]["is_current"])
        self.assertIsNone(release.approved_at)
        for old in self.source.containers.all():
            new = release.containers.get(name=old.name)
            self.assertEqual((new.image_ref, new.ports), (old.image_ref, old.ports))
            self.assertEqual(new.secret_env["INTERNAL_TOKEN"], str(self.shared.pk))
        app = release.containers.get(name="app")
        self.assertEqual(app.env, {"PORT": "8081", "KEEP": "original"})
        self.assertEqual(app.secret_env["FLAG"], str(self.flag.pk))
        secret = RuntimeSecret.objects.get(pk=app.secret_env["DB_PASS"])
        self.assertEqual(decrypt_runtime_secret(secret), "local-$literal # = \\value")
        self.assertNotIn("local-$literal", secret.encrypted_value)
        self.assertNotIn("local-$literal", json.dumps(response.data))
        self.assertNotIn("env_import_fingerprint", response.data["data"])
        self.assertEqual(app.env_rules["DB_PASS"], {"kind": "secret", "required": True})
        self.assertEqual(self.challenge.runtime_config.current_release_id, self.source.pk)

    def test_secret_update_does_not_rotate_other_container_binding(self):
        response = self.save(self.import_body("INTERNAL_TOKEN=new-local-value"))
        self.assertEqual(response.status_code, 200, response.data)
        release = ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])
        self.assertNotEqual(release.containers.get(name="app").secret_env["INTERNAL_TOKEN"], str(self.shared.pk))
        self.assertEqual(release.containers.get(name="helper").secret_env["INTERNAL_TOKEN"], str(self.shared.pk))

    def test_repeat_request_returns_same_release_and_secret_version(self):
        body = self.import_body("DB_PASS=new-local-value")
        first = self.save(body)
        counts = (RuntimeSecret.objects.count(), ChallengeRelease.objects.count())
        second = self.save(body)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.data["data"]["release_id"], second.data["data"]["release_id"])
        self.assertEqual(counts, (RuntimeSecret.objects.count(), ChallengeRelease.objects.count()))
        body["content"] = "DB_PASS=different-local-value"
        self.assertEqual(self.save(body).status_code, 409)
        self.assertEqual(counts, (RuntimeSecret.objects.count(), ChallengeRelease.objects.count()))

    def test_late_failure_rolls_back_new_secrets_and_release(self):
        body = self.import_body("DB_PASS=new-local-value")
        counts = (RuntimeSecret.objects.count(), ChallengeRelease.objects.count())
        with patch("apps.instances.environment_files.create_release", side_effect=ReleaseValidationError("등록 실패")):
            self.assertEqual(self.save(body).status_code, 400)
        self.assertEqual(counts, (RuntimeSecret.objects.count(), ChallengeRelease.objects.count()))

    def test_combined_limit_with_preserved_values_rolls_back(self):
        app = self.source.containers.get(name="app")
        app.env = {f"VALUE_{i}": "x" * 3500 for i in range(4)}
        app.save(update_fields=["env"])
        body = self.import_body("DB_PASS=" + "y" * 3000)
        counts = (RuntimeSecret.objects.count(), ChallengeRelease.objects.count())
        self.assertEqual(self.save(body).status_code, 400)
        self.assertEqual(counts, (RuntimeSecret.objects.count(), ChallengeRelease.objects.count()))

    def test_required_rules_survive_and_reject_empty_or_removed_values(self):
        self.assertFalse(self.preview("PORT=").data["data"]["can_import"])
        self.assertEqual(self.save(self.import_body("PORT=")).status_code, 400)
        saved = self.save(self.import_body("PORT=8082"))
        release_id = saved.data["data"]["release_id"]
        release = ChallengeRelease.objects.get(pk=release_id)
        containers = []
        metadata = serialize_release(release)["containers"]
        for container in metadata:
            containers.append({"name": container["name"], "env": container.get("env", {}),
                               "secret_env": {row["env_name"]: row["name"] for row in container["secret_bindings"]}})
        next(container for container in containers if container["name"] == "app")["env"].pop("PORT")
        response = self.client.post(f"{self.base_url}/{release_id}/derive", {"containers": containers}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("PORT", response.data["message"])

    def test_explicit_optional_empty_general_value_is_supported(self):
        body = self.import_body("PORT=", lambda rows: rows[0].update(required=False))
        response = self.save(body)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["containers"][0]["env"]["PORT"], "")

    def test_selected_reference_version_stays_fixed_after_new_secret(self):
        body = self.import_body("INTERNAL_TOKEN=")
        create_runtime_secret(self.challenge, "shared", "newer-local-value", "root")
        response = self.save(body)
        self.assertEqual(response.status_code, 200, response.data)
        release = ChallengeRelease.objects.get(pk=response.data["data"]["release_id"])
        self.assertEqual(release.containers.get(name="app").secret_env["INTERNAL_TOKEN"], str(self.shared.pk))

    def test_wrong_flag_and_missing_secret_reference_fail_without_writes(self):
        counts = (RuntimeSecret.objects.count(), ChallengeRelease.objects.count())
        for body in (self.import_body("FLAG=local-wrong-answer"), self.import_body("DB_PASS=")):
            self.assertEqual(self.save(body).status_code, 400)
        self.assertEqual(counts, (RuntimeSecret.objects.count(), ChallengeRelease.objects.count()))

    def test_invalid_input_cannot_change_scope_or_inject_unreviewed_fields(self):
        valid = self.import_body("PORT=8081")
        for patch_body in ({"container": "missing"}, {"runtime_type": "VM"}, {"requestId": str(uuid.uuid4())},
                           {"request_id": "invalid"}, {"entries": []}, {"entries": None},
                           {"entries": [{"name": "PORT", "kind": [], "required": True}]}):
            body = {**copy.deepcopy(valid), **patch_body}
            self.assertEqual(self.save(body).status_code, 400)
        other = Challenge.objects.create(title="Other", flag_hash=self.challenge.flag_hash, score=1)
        other_url = f"/api/v1/admin/challenges/{other.pk}/releases/{self.source.pk}/environment_files"
        self.assertEqual(self.client.post(other_url + "/preview", valid, format="json").status_code, 404)
        self.assertEqual(self.client.post(other_url + "/import", valid, format="json").status_code, 404)

    def test_player_cannot_preview_or_import(self):
        body = self.import_body("PORT=8081")
        self.auth("player")
        for suffix in ("/preview", "/import"):
            self.assertEqual(self.client.post(self.url + suffix, body, format="json").status_code, 403)
