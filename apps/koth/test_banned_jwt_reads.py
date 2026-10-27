from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    KOTH_TEAM_TOKEN_SECRET="test-koth-token-secret",
)
class BannedTeamJwtReadTests(TestCase):
    def test_banned_team_existing_jwt_cannot_read_koth_profile_or_token(self):
        cache.clear()
        team = Team.objects.create(team_name="qa-banned-jwt")
        User.objects.create_user(
            login_id="qa-banned-leader",
            password="local-test-password",
            nickname="qa-leader",
            team=team,
        )
        client = APIClient()
        login = client.post(
            "/api/v1/auth/login",
            {"login_id": "qa-banned-leader", "password": "local-test-password"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['data']['access_token']}")

        for path in ("/api/v1/koth/me", "/api/v1/koth/team_token"):
            with self.subTest(path=path, banned=False):
                self.assertEqual(client.get(path).status_code, 200)

        team.is_banned = True
        team.save(update_fields=["is_banned"])
        for path in ("/api/v1/koth/me", "/api/v1/koth/team_token"):
            with self.subTest(path=path, banned=True):
                denied = client.get(path)
                self.assertEqual(denied.status_code, 403)
                self.assertEqual(denied.data["code"], "TEAM_BANNED")
                self.assertIsNone(denied.data.get("data"))
