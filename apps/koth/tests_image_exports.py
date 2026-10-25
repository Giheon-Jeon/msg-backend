import base64
import hashlib
import hmac
import json
import time
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings

from . import tests_environment as fixtures


@override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()],
    KOTH_IMAGE_EXPORT_SIGNING_KEY="test-only-image-export-signing-key-1234567890",
    KOTH_IMAGE_EXPORT_URL="https://exporter.example/internal/v1/koth_image_exports")
class ImageTicketTests(TestCase):
    setUp = fixtures.EnvironmentTests.setUp
    entry = staticmethod(fixtures.EnvironmentTests.entry)

    def ticket(self, **extra):
        body = {"application_id": str(self.application.pk), "version_id": str(self.source.pk), "image_ref": fixtures.IMAGE, **extra}
        return self.node.post(self.agent + "/image_export", body, format="json")

    def test_ticket_is_bound_to_current_game_application_version_and_exact_image(self):
        response = self.ticket()
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        encoded, signature = data["ticket"].split(".")
        expected = base64.urlsafe_b64encode(hmac.new(b"test-only-image-export-signing-key-1234567890", encoded.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        self.assertEqual(signature, expected)
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(claims["image_ref"], fixtures.IMAGE)
        self.assertEqual(claims["challenge_id"], str(self.challenge.pk))
        self.assertTrue(time.time() < claims["exp"] <= time.time() + 300)
        self.assertNotIn("test-runtime-internal-value", json.dumps(response.data))
        self.assertEqual(set(data), {"url", "ticket", "image_ref", "expires_at"})
        self.assertIn("no-store", response["Cache-Control"])

    def test_another_image_or_stale_application_cannot_get_a_ticket(self):
        self.assertEqual(self.ticket(image_ref=fixtures.IMAGE.replace("koth-test", "koth-other")).status_code, 409)
        self.assertEqual(self.ticket(application_id="12345678-1234-1234-1234-123456789abc").status_code, 409)
        self.assertEqual(self.ticket(version_id="12345678-1234-1234-1234-123456789abc").status_code, 409)

    def test_only_the_scoped_vm_token_can_request_an_image(self):
        self.assertEqual(self.client.post(self.agent + "/image_export", {}, format="json").status_code, 401)
        self.node.credentials(HTTP_AUTHORIZATION="Bearer " + "x" * 48)
        self.assertEqual(self.ticket().status_code, 401)

    def test_missing_signing_configuration_and_non_tls_exporter_fail_closed(self):
        with override_settings(KOTH_IMAGE_EXPORT_SIGNING_KEY=""):
            self.assertEqual(self.ticket().status_code, 503)
        with override_settings(KOTH_IMAGE_EXPORT_URL="http://exporter.example/internal/v1/koth_image_exports"):
            self.assertEqual(self.ticket().status_code, 503)

    def test_unknown_fields_are_rejected_without_registry_calls(self):
        self.assertEqual(self.ticket(repository="other").status_code, 400)
