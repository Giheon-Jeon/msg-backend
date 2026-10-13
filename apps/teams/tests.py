import datetime

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge, Solve
from apps.ranking.models import LineMonopoly

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class MyPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="우리팀", team_score=350, mileage=120)
        self.other = Team.objects.create(team_name="남의팀", team_score=9999, mileage=9999)
        self.user = User.objects.create_user(
            login_id="me",
            password="pw1234",
            nickname="나",
            team=self.team,
            is_leader=True,
        )
        User.objects.create_user(
            login_id="stranger", password="pw1234", nickname="남", team=self.other
        )
        self.auth("me")

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def test_team_me_returns_own_team_only(self):
        res = self.client.get("/api/v1/teams/me")
        self.assertEqual(res.data["data"]["team_name"], "우리팀")
        nicknames = [m["nickname"] for m in res.data["data"]["members"]]
        self.assertNotIn("남", nicknames)

    def test_other_team_data_isolated(self):
        self.auth("stranger")
        res = self.client.get("/api/v1/teams/me")
        self.assertEqual(res.data["data"]["team_name"], "남의팀")

    def test_team_me_includes_line_monopoly_score(self):
        Team.objects.filter(pk=self.team.pk).update(team_score=500)
        LineMonopoly.objects.create(
            team=self.team,
            line_number=1,
            earned_score=225,
            is_category_bonus=True,
        )

        data = self.client.get("/api/v1/teams/me").data["data"]

        self.assertEqual(data["jeopardy_score"], 500)
        self.assertEqual(data["line_score"], 225)
        self.assertEqual(data["team_score"], 725)

    def test_admin_without_team_gets_404(self):
        User.objects.create_user(
            login_id="noteam", password="pw1234", nickname="무소속", team=None
        )
        self.auth("noteam")
        res = self.client.get("/api/v1/teams/me")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "USER_HAS_NO_TEAM")

    def test_qr_token_key_name(self):
        res = self.client.post("/api/v1/teams/me/qr_token")
        self.assertIn("payment_token", res.data["data"])
        self.assertNotIn("token", res.data["data"])  # 금지 필드

    def test_qr_token_replaces_previous(self):
        from apps.teams.models import PaymentToken, PaymentTokenStatus

        self.client.post("/api/v1/teams/me/qr_token")
        self.client.post("/api/v1/teams/me/qr_token")
        active = PaymentToken.objects.filter(
            team=self.team, status=PaymentTokenStatus.ACTIVE
        ).count()
        self.assertEqual(active, 1)

    def test_banned_team_get_allowed_post_blocked(self):
        Team.objects.filter(pk=self.team.pk).update(is_banned=True, ban_reason="테스트")

        res = self.client.get("/api/v1/teams/me")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["data"]["is_banned"])

        res = self.client.post("/api/v1/teams/me/qr_token")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "TEAM_BANNED")

    def test_banned_team_can_still_login(self):
        Team.objects.filter(pk=self.team.pk).update(is_banned=True, ban_reason="테스트")
        self.client.credentials()
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": "me", "password": "pw1234"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)

    def _challenge(self, title="웹 상 문제", score=100):
        return Challenge.objects.create(
            title=title,
            category="WEB",
            difficulty="HARD",
            score=score,
            flag_hash="x",
            is_published=True,
        )

    def test_solves_empty(self):
        res = self.client.get("/api/v1/teams/me/solves")
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertEqual(res.data["data"]["solves"], [])

    def test_solves_does_not_count_line_monopoly_as_a_solve(self):
        LineMonopoly.objects.create(
            team=self.team,
            line_number=1,
            earned_score=225,
            is_category_bonus=True,
        )

        data = self.client.get("/api/v1/teams/me/solves").data["data"]

        self.assertEqual(data["solves"], [])
        self.assertEqual(data["total_count"], 0)

    def test_solves_returns_spec_fields(self):
        ch = self._challenge()
        Solve.objects.create(
            team=self.team,
            challenge=ch,
            solved_by_user=self.user,
            earned_score=100,
            earned_mileage=30,
            is_extra_dice_granted=True,
        )
        res = self.client.get("/api/v1/teams/me/solves")
        row = res.data["data"]["solves"][0]
        self.assertEqual(
            set(row),
            {
                "source_type",
                "challenge_id",
                "challenge_title",
                "earned_score",
                "earned_mileage",
                "is_extra_dice_granted",
                "solved_by",
                "solved_at",
            },
        )
        self.assertEqual(row["source_type"], "JEOPARDY")
        self.assertEqual(row["challenge_title"], "웹 상 문제")
        self.assertEqual(row["earned_score"], 100)
        self.assertEqual(row["earned_mileage"], 30)
        self.assertTrue(row["is_extra_dice_granted"])
        self.assertEqual(set(row["solved_by"]), {"user_id", "nickname"})
        self.assertEqual(row["solved_by"]["nickname"], "나")
        self.assertEqual(res.data["data"]["total_count"], 1)

    def test_solves_other_team_isolated(self):
        ch = self._challenge(title="남의 문제")
        Solve.objects.create(
            team=self.other,
            challenge=ch,
            solved_by_user=None,
            earned_score=500,
            earned_mileage=100,
        )
        res = self.client.get("/api/v1/teams/me/solves")
        self.assertEqual(res.data["data"]["solves"], [])

    def test_solves_newest_first(self):
        now = timezone.now()
        for i in range(3):
            solve = Solve.objects.create(
                team=self.team,
                challenge=self._challenge(title=f"문제{i}"),
                solved_by_user=self.user,
                earned_score=10 * i,
                earned_mileage=0,
            )
            Solve.objects.filter(pk=solve.pk).update(
                solved_at=now + datetime.timedelta(seconds=i)
            )
        res = self.client.get("/api/v1/teams/me/solves")
        titles = [s["challenge_title"] for s in res.data["data"]["solves"]]
        self.assertEqual(titles, ["문제2", "문제1", "문제0"])

    def test_solves_handles_deleted_user(self):
        ch = self._challenge()
        Solve.objects.create(
            team=self.team,
            challenge=ch,
            solved_by_user=None,
            earned_score=100,
            earned_mileage=30,
        )
        res = self.client.get("/api/v1/teams/me/solves")
        self.assertIsNone(res.data["data"]["solves"][0]["solved_by"])

    def _koth_solve(self, title="koth_web_1", score=800, when=None):
        from django.utils import timezone
        from apps.koth.models import KothClub, KothChallenge, KothSolve

        club = KothClub.objects.create(name=f"club-{title}")
        ch = KothChallenge.objects.create(
            club=club,
            title=title,
            open_group=1,
            inbound_internal_token_hash=f"h-{title}",
        )
        return KothSolve.objects.create(
            team=self.team,
            challenge=ch,
            earned_score=score,
            solved_at=when or timezone.now(),
        )

    def test_solves_includes_koth(self):
        self._koth_solve()
        res = self.client.get("/api/v1/teams/me/solves")
        row = res.data["data"]["solves"][0]
        self.assertEqual(
            set(row),
            {
                "source_type",
                "koth_challenge_id",
                "challenge_title",
                "earned_score",
                "earned_mileage",
                "is_extra_dice_granted",
                "solved_by",
                "solved_at",
            },
        )
        self.assertEqual(row["source_type"], "KOTH")
        self.assertEqual(row["earned_score"], 800)
        self.assertEqual(row["earned_mileage"], 0)
        self.assertFalse(row["is_extra_dice_granted"])
        self.assertIsNone(row["solved_by"])
        self.assertEqual(res.data["data"]["total_count"], 1)

    def test_solves_merges_jeopardy_and_koth_newest_first(self):
        import datetime
        from django.utils import timezone

        Solve.objects.create(
            team=self.team,
            challenge=self._challenge(title="제오"),
            solved_by_user=self.user,
            earned_score=100,
            earned_mileage=30,
        )
        self._koth_solve(
            title="koth_new", when=timezone.now() + datetime.timedelta(hours=1)
        )
        res = self.client.get("/api/v1/teams/me/solves")
        types = [s["source_type"] for s in res.data["data"]["solves"]]
        self.assertEqual(types, ["KOTH", "JEOPARDY"])  # KOTH가 더 최신
        self.assertEqual(res.data["data"]["total_count"], 2)
