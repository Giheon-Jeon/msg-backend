from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core import signing
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.common.jwt import issue_access_token
from apps.koth.admin_preview import COOKIE_NAME, MAX_AGE, SALT
from apps.koth.models import KothChallenge, KothClub, KothScorePeriod, KothSolve


@override_settings(KOTH_ADMIN_PREVIEW_ORIGINS=["https://game.example"])
class AdminPreviewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(login_id="preview-admin", nickname="admin", role="ADMIN")
        self.player = User.objects.create_user(login_id="preview-player", nickname="player")
        club = KothClub.objects.create(name="preview-club")
        self.challenge = KothChallenge.objects.create(club=club, title="preview-game", open_group=1,
            challenge_url="https://game.example", status="SCHEDULED", inbound_internal_token_hash="a"*64)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.admin))
        self.path = f"/api/v1/admin/koth/challenges/{self.challenge.pk}/preview_sessions"
        self.authorize = "/api/v1/koth/preview_sessions/authorize"

    def grant(self):
        response = self.client.post(self.path, {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["expires_in_seconds"], 300)
        self.assertIn("no-store", response["Cache-Control"])
        url = urlsplit(response.data["data"]["preview_url"])
        self.assertEqual(url.query, "")
        return parse_qs(url.fragment)["ticket"][0]

    def check(self, token=None, origin="https://game.example", challenge_id=None, client=None):
        client = client or APIClient()
        headers = dict(
            HTTP_X_KOTH_PREVIEW_CHALLENGE=str(challenge_id or self.challenge.pk),
            HTTP_X_KOTH_PREVIEW_ORIGIN=origin)
        if token is not None:
            return client.post(self.authorize, {"ticket": token}, format="json", **headers)
        return client.get(self.authorize, **headers)

    def test_admin_grant_and_cookie_preserve_scheduled_state_and_scores(self):
        token = self.grant()
        response = self.check(token)
        self.assertEqual(response.status_code, 204)
        cookie = response.cookies[COOKIE_NAME]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["samesite"], "Lax")
        client = APIClient();client.cookies[COOKIE_NAME] = cookie.value
        self.assertEqual(self.check(client=client).status_code, 204)
        self.assertFalse(self.check(client=client).cookies)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.status, "SCHEDULED")
        self.assertIsNone(self.challenge.opened_at)
        self.assertEqual(KothScorePeriod.objects.count(), 0)
        self.assertEqual(KothSolve.objects.count(), 0)

    def test_player_and_anonymous_cannot_issue(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.player))
        self.assertEqual(self.client.post(self.path, {}, format="json").status_code, 403)
        self.assertEqual(APIClient().post(self.path, {}, format="json").status_code, 401)

    def test_no_redirect_or_runtime_settings_can_be_supplied(self):
        for body in ({"url":"https://other.example"}, {"expires_in_seconds":3600}, []):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(self.path, body, format="json").status_code, 400)

    @override_settings(KOTH_ADMIN_PREVIEW_ORIGINS=[])
    def test_disabled_without_operator_allowlist(self):
        self.assertEqual(self.client.post(self.path, {}, format="json").status_code, 409)

    def test_only_scheduled_games_can_be_tested(self):
        for status in ("ACTIVE", "CLOSED"):
            self.challenge.status=status;self.challenge.save(update_fields=["status"])
            self.assertEqual(self.client.post(self.path, {}, format="json").status_code, 409)

    def test_unsafe_or_unconfigured_origins_cannot_be_signed(self):
        for value in ("http://game.example", "https://other.example", "https://game.example/a", "https://x:pw@game.example", "https://game.example?next=x", "https://game.example#x", "https://game.example:8443"):
            self.challenge.challenge_url=value;self.challenge.save(update_fields=["challenge_url"])
            self.assertEqual(self.client.post(self.path, {}, format="json").status_code, 409)

    def test_missing_bad_and_ordinary_access_tokens_are_rejected(self):
        for token in (None, "invalid", "x"*2049, issue_access_token(self.admin)):
            self.assertEqual(self.check(token).status_code, 401)

    def test_grant_is_bound_to_challenge_and_origin(self):
        token=self.grant()
        self.assertEqual(self.check(token, origin="https://other.example").status_code, 401)
        self.assertEqual(self.check(token, challenge_id=self.admin.pk).status_code, 401)

    def test_tampered_and_expired_tokens_are_rejected(self):
        token=self.grant()
        self.assertEqual(self.check(token+'x').status_code, 401)
        with patch("django.core.signing.time.time", return_value=__import__('time').time()+MAX_AGE+2):
            self.assertEqual(self.check(token).status_code, 401)

    def test_new_status_new_origin_removed_admin_and_disabled_feature_revoke_grants(self):
        token=self.grant()
        with override_settings(KOTH_ADMIN_PREVIEW_ORIGINS=[]):
            self.assertEqual(self.check(token).status_code, 401)
        self.challenge.status="ACTIVE";self.challenge.save(update_fields=["status"])
        self.assertEqual(self.check(token).status_code, 401)
        self.challenge.status="SCHEDULED";self.challenge.save(update_fields=["status"])
        self.challenge.challenge_url="https://other.example";self.challenge.save(update_fields=["challenge_url"])
        self.assertEqual(self.check(token).status_code, 401)
        self.challenge.challenge_url="https://game.example";self.challenge.save(update_fields=["challenge_url"])
        self.admin.role="PARTICIPANT";self.admin.save(update_fields=["role"])
        self.assertEqual(self.check(token).status_code, 401)
        self.admin.delete()
        self.assertEqual(self.check(token).status_code, 401)

    def test_other_signing_purpose_and_nonobject_payload_rejected(self):
        for token in (signing.dumps({"v":1},salt="other-purpose"),signing.dumps([],salt=SALT)):
            self.assertEqual(self.check(token).status_code, 401)

    @override_settings(CACHES={"default":{"BACKEND":"django.core.cache.backends.locmem.LocMemCache"}})
    def test_invalid_preview_attempts_are_rate_limited(self):
        from apps.koth.admin_preview import PreviewAuthorizeThrottle
        with patch.object(PreviewAuthorizeThrottle, "rate", "2/min"):
            client=APIClient()
            for expected in (401,401,429):
                response=client.get(self.authorize,REMOTE_ADDR="198.51.100.77")
                self.assertEqual(response.status_code,expected)
