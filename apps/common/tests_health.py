from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from rest_framework.test import APIClient

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class HealthzTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_healthy_returns_200_with_component_status(self):
        # 정상이면 200과 함께 항목별 상태를 snake_case로 내려준다
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok", "database": "ok", "cache": "ok"})

    def test_no_auth_required(self):
        # 감시용이라 인증 없이 접근된다
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)

    def test_database_failure_returns_503(self):
        with patch("apps.common.health.connection") as fake_connection:
            fake_connection.cursor.side_effect = RuntimeError("db down")
            res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 503)
        body = res.json()
        self.assertEqual(body["status"], "down")
        self.assertEqual(body["database"], "down")
        self.assertEqual(body["cache"], "ok")

    def test_cache_failure_returns_503(self):
        with patch("apps.common.health.cache") as fake_cache:
            fake_cache.set.side_effect = RuntimeError("cache down")
            res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 503)
        body = res.json()
        self.assertEqual(body["status"], "down")
        self.assertEqual(body["database"], "ok")
        self.assertEqual(body["cache"], "down")

    def test_unsafe_methods_return_405_even_with_csrf_checks(self):
        # 실제 요청처럼 CSRF 검사를 켜도 GET·HEAD 외 메서드는 405로 거절하고 점검을 실행하지 않는다
        client = Client(enforce_csrf_checks=True)
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                with patch("apps.common.health._check_database") as check_db:
                    res = getattr(client, method)("/healthz")
                self.assertEqual(res.status_code, 405)
                self.assertEqual(res["Allow"], "GET, HEAD")
                check_db.assert_not_called()

    def test_head_is_allowed_for_monitoring(self):
        # 로드밸런서·모니터링 도구의 HEAD 요청은 허용한다
        res = self.client.head("/healthz")
        self.assertEqual(res.status_code, 200)
