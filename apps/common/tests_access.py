from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase
from rest_framework.test import APIClient


class AnonymousGameAccessTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()

    def test_game_and_admin_reads_require_a_login_before_loading_data(self):
        item_id = "11111111-1111-4111-8111-111111111111"
        paths = [
            "/api/v1/board/me",
            "/api/v1/board/dice/status",
            "/api/v1/board/cell/current",
            "/api/v1/board/opened_challenges",
            f"/api/v1/challenges/{item_id}",
            "/api/v1/leaderboard",
            "/api/v1/ranking",
            "/api/v1/ranking/me",
            "/api/v1/ranking/member",
            "/api/v1/timer",
            "/api/v1/koth/clubs",
            f"/api/v1/koth/clubs/{item_id}",
            "/api/v1/koth/me",
            "/api/v1/koth/team_token",
            "/api/v1/signatures",
            "/api/v1/teams/me",
            "/api/v1/admin/teams",
            "/api/v1/admin/challenges",
            "/api/v1/admin/settings",
            "/api/v1/admin/events",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.data["code"], "TOKEN_MISSING")
                self.assertIsNone(response.data["data"])

    def test_default_permission_is_authenticated(self):
        self.assertEqual(
            settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"],
            ["apps.common.permissions.IsAuthenticated"],
        )

    def test_login_and_refresh_remain_available_without_an_access_token(self):
        for path in ["/api/v1/auth/login", "/api/v1/auth/refresh"]:
            with self.subTest(path=path):
                response = self.client.post(path, {}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "INVALID_REQUEST")

    def test_health_check_is_not_hidden_behind_participant_login(self):
        with patch("apps.common.health._check_database", return_value="ok"), patch("apps.common.health._check_cache", return_value="ok"):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
