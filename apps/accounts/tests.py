import datetime
import secrets
import threading
import time
from unittest import mock

import jwt as pyjwt
from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts import views
from apps.accounts.models import RefreshToken, Team, User
from apps.common.jwt import hash_token, issue_access_token, issue_refresh_token

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class AuthTests(TestCase):
    def setUp(self):
        cache.clear()  # 쓰로틀 카운터 초기화
        self.client = APIClient()
        self.team = Team.objects.create(team_name="테스트팀")
        self.user = User.objects.create_user(
            login_id="tester",
            password="pw1234",
            nickname="테스터",
            team=self.team,
            is_leader=True,
        )

    def login(self, login_id="tester", password="pw1234"):
        return self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": password},
            format="json",
        )

    # ---- 로그인 ----
    def test_login_success(self):
        res = self.login()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertIn("access_token", res.data["data"])

    def test_login_wrong_password(self):
        res = self.login(password="wrong")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.data["code"], "INVALID_CREDENTIALS")

    def test_login_unknown_id_same_error(self):
        """아이디가 없어도 비밀번호가 틀린 것과 같은 응답을 준다."""
        res = self.login(login_id="nobody")
        self.assertEqual(res.data["code"], "INVALID_CREDENTIALS")

    def test_login_twice_in_same_second(self):
        """같은 초에 두 번 로그인해도 둘 다 성공해야 한다 (jti)."""
        first, second = self.login(), self.login()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(
            first.data["data"]["refresh_token"], second.data["data"]["refresh_token"]
        )
        self.assertEqual(RefreshToken.objects.count(), 2)

    def test_unknown_field_rejected(self):
        """camelCase 오타를 조용히 무시하지 않는다."""
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": "tester", "loginId": "x", "password": "pw1234"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    # ---- 토큰 ----
    def test_token_missing(self):
        res = self.client.get("/api/v1/teams/me")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.data["code"], "TOKEN_MISSING")

    def test_token_malformed(self):
        res = self.client.get("/api/v1/teams/me", HTTP_AUTHORIZATION="Bearer garbage")
        self.assertEqual(res.data["code"], "TOKEN_INVALID")

    def test_token_expired(self):
        payload = {
            "typ": "access",
            "sub": str(self.user.user_id),
            "exp": datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=1),
        }
        expired = pyjwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")
        res = self.client.get(
            "/api/v1/teams/me", HTTP_AUTHORIZATION=f"Bearer {expired}"
        )
        self.assertEqual(res.data["code"], "TOKEN_EXPIRED")

    def test_token_signed_with_unknown_key_rejected(self):
        """현재 JWT 키와 다른 임시 키로 만든 토큰은 거절돼야 한다."""
        payload = {
            "typ": "access",
            "sub": str(self.user.user_id),
            "team_id": str(self.team.team_id),
            "role": "ADMIN",
            "is_leader": True,
            "exp": datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=1),
        }
        unknown_key = secrets.token_urlsafe(48)
        self.assertNotEqual(unknown_key, settings.JWT_SECRET)
        forged = pyjwt.encode(payload, unknown_key, algorithm="HS256")
        res = self.client.get("/api/v1/auth/me", HTTP_AUTHORIZATION=f"Bearer {forged}")
        self.assertEqual(res.data["code"], "TOKEN_INVALID")

    def test_refresh_token_cannot_call_api(self):
        """refresh 토큰으로 일반 API 를 호출할 수 없다 (typ 검증)."""
        refresh = self.login().data["data"]["refresh_token"]
        res = self.client.get(
            "/api/v1/teams/me", HTTP_AUTHORIZATION=f"Bearer {refresh}"
        )
        self.assertEqual(res.data["code"], "TOKEN_INVALID")

    # ---- refresh / logout ----
    def test_refresh_and_logout(self):
        refresh = self.login().data["data"]["refresh_token"]

        res = self.client.post(
            "/api/v1/auth/refresh", {"refresh_token": refresh}, format="json"
        )
        self.assertEqual(res.data["code"], "SUCCESS")

        access = res.data["data"]["access_token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/logout", {"refresh_token": refresh}, format="json"
            ).data["code"],
            "SUCCESS",
        )
        self.client.credentials()

        # 로그아웃한 토큰으로는 재발급 불가
        res = self.client.post(
            "/api/v1/auth/refresh", {"refresh_token": refresh}, format="json"
        )
        self.assertEqual(res.data["code"], "REFRESH_TOKEN_NOT_FOUND")

    # ---- 요청 제한 ----
    def test_login_throttled(self):
        for _ in range(10):
            self.login(password="wrong")
        res = self.login(password="wrong")
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.data["code"], "TOO_MANY_REQUESTS")

    def test_throttle_does_not_affect_other_account(self):
        User.objects.create_user(
            login_id="other", password="pw1234", nickname="다른사람", team=self.team
        )
        for _ in range(11):
            self.login(password="wrong")
        res = self.login(login_id="other", password="pw1234")
        self.assertEqual(res.status_code, 200)

    def test_banned_team_login_shows_ban_info(self):
        from apps.accounts.models import Team

        Team.objects.filter(pk=self.team.pk).update(is_banned=True, ban_reason="어뷰징")
        res = self.login()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["data"]["is_banned"])
        self.assertEqual(res.data["data"]["ban_reason"], "어뷰징")

    def test_login_response_has_exact_fields(self):

        res = self.login()
        expected = {
            "access_token",
            "refresh_token",
            "role",
            "is_leader",
            "nickname",
            "team_id",
            "team_name",
            "user_id",
            "is_banned",
            "ban_reason",
        }
        self.assertEqual(set(res.data["data"]), expected)


class RefreshLogoutRaceTest(TransactionTestCase):
    def setUp(self):
        self.team = Team.objects.create(team_name="레이스팀")
        self.user = User.objects.create_user(
            login_id="racer",
            password="pw1234",
            nickname="레이서",
            team=self.team,
            is_leader=True,
        )

    def _issue_token(self):
        raw, expires_at = issue_refresh_token(self.user)
        RefreshToken.objects.create(
            user=self.user,
            token_hash=hash_token(raw),
            expires_at=expires_at,
        )
        return raw

    def test_logout_then_refresh_rejected(self):
        raw = self._issue_token()
        auth = APIClient()
        auth.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.user)}")

        logout = auth.post("/api/v1/auth/logout", {"refresh_token": raw}, format="json")
        self.assertEqual(logout.status_code, 200)

        again = APIClient().post(
            "/api/v1/auth/refresh", {"refresh_token": raw}, format="json"
        )
        self.assertEqual(again.status_code, 401)
        self.assertEqual(again.data["code"], "REFRESH_TOKEN_NOT_FOUND")

    def test_refresh_before_logout_succeeds(self):
        raw = self._issue_token()

        refreshed = APIClient().post(
            "/api/v1/auth/refresh", {"refresh_token": raw}, format="json"
        )
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn("access_token", refreshed.data["data"])

        auth = APIClient()
        auth.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.user)}")
        logout = auth.post("/api/v1/auth/logout", {"refresh_token": raw}, format="json")
        self.assertEqual(logout.status_code, 200)

    def test_refresh_holds_lock_until_token_issued(self):
        raw = self._issue_token()
        logout_client = APIClient()
        logout_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.user)}"
        )

        state = {}

        def run_logout():
            res = logout_client.post(
                "/api/v1/auth/logout", {"refresh_token": raw}, format="json"
            )
            state["logout_status"] = res.status_code
            connections.close_all()

        real_issue = views.issue_access_token

        def issue_spy(user):
            worker = threading.Thread(target=run_logout)
            worker.start()
            state["worker"] = worker
            time.sleep(0.5)
            state["logout_finished_before_issue"] = "logout_status" in state
            return real_issue(user)

        with mock.patch.object(views, "issue_access_token", side_effect=issue_spy):
            res = APIClient().post(
                "/api/v1/auth/refresh", {"refresh_token": raw}, format="json"
            )

        state["worker"].join(timeout=5)
        connections.close_all()

        self.assertEqual(res.status_code, 200)
        self.assertFalse(state["logout_finished_before_issue"])
