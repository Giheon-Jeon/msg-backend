from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.common.throttling import ReadApiThrottle

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
READ_APIS = [
    "/api/v1/timer",
    "/api/v1/ranking",
    "/api/v1/ranking/me",
    "/api/v1/ranking/member",
    "/api/v1/leaderboard",
]


@override_settings(CACHES=LOCMEM)
class ReadApiThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        team = Team.objects.create(team_name="스로틀팀")
        self.user = User.objects.create_user("throttle_me", "pw123456", nickname="나", team=team)
        self.other = User.objects.create_user("throttle_other", "pw123456", nickname="남", team=team)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_default_rate_is_sixty_per_minute(self):
        # 프론트가 30초마다 다시 불러오므로 기본 한도는 정상 사용을 막지 않는다
        self.assertEqual(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["read_api"], "60/min")

    def test_exceeding_limit_returns_429_envelope_on_each_read_api(self):
        with patch.object(ReadApiThrottle, "THROTTLE_RATES", {"read_api": "2/min"}):
            for url in READ_APIS:
                with self.subTest(url=url):
                    cache.clear()
                    client = self.client_for(self.user)
                    for _ in range(2):
                        self.assertNotEqual(client.get(url).status_code, 429)
                    res = client.get(url)
                    self.assertEqual(res.status_code, 429)
                    self.assertEqual(res.json()["code"], "TOO_MANY_REQUESTS")

    def test_limit_is_counted_per_user(self):
        with patch.object(ReadApiThrottle, "THROTTLE_RATES", {"read_api": "2/min"}):
            mine = self.client_for(self.user)
            for _ in range(3):
                mine.get("/api/v1/ranking")
            self.assertEqual(mine.get("/api/v1/ranking").status_code, 429)
            self.assertEqual(self.client_for(self.other).get("/api/v1/ranking").status_code, 200)

    def test_cache_failure_lets_the_request_through(self):
        # 횟수를 셀 수 없어도 순위 조회 자체는 실패하지 않아야 한다
        broken = MagicMock()
        broken.get.side_effect = ConnectionError("cache down")
        with patch.object(ReadApiThrottle, "cache", broken):
            res = self.client_for(self.user).get("/api/v1/ranking")
        self.assertEqual(res.status_code, 200)
