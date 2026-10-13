from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class EnvelopeTests(TestCase):
    """모든 응답이 공통 규약의 세 키를 가지는지 확인한다."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="봉투팀")
        User.objects.create_user(
            login_id="env", password="pw1234", nickname="봉투", team=self.team
        )

    def assertEnvelope(self, res):
        self.assertEqual(set(res.data), {"code", "message", "data"})
        self.assertIsInstance(res.data["code"], str)
        self.assertIsInstance(res.data["message"], str)

    def test_success_envelope(self):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": "env", "password": "pw1234"},
            format="json",
        )
        self.assertEnvelope(res)
        self.assertEqual(res.data["code"], "SUCCESS")

    def test_error_envelopes(self):
        cases = [
            self.client.post(
                "/api/v1/auth/login",
                {"login_id": "env", "password": "no"},
                format="json",
            ),
            self.client.get("/api/v1/teams/me"),
            self.client.get("/api/v1/teams/me", HTTP_AUTHORIZATION="Bearer bad"),
            self.client.post("/api/v1/auth/refresh", {}, format="json"),
        ]
        for res in cases:
            self.assertEnvelope(res)
            self.assertNotEqual(res.data["code"], "SUCCESS")

    def test_method_not_allowed_envelope(self):
        self.assertEnvelope(self.client.delete("/api/v1/auth/login"))
