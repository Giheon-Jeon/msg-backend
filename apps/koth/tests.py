import os
import time
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.forms import modelform_factory
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.common.jwt import hash_token

from .models import (
    KothChallenge,
    KothChallengeStatus,
    KothClub,
    KothScorePeriod,
    KothScorePeriodStatus,
    KothSolve,
    KothTeamToken,
    KothTokenVerificationAttempt,
)
from .services import ScoreFetchError, _request_scores, poll_challenge_period

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM, KOTH_TEAM_TOKEN_SECRET="test-koth-token-secret")
class KothApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="우리팀", team_score=100)
        self.other = Team.objects.create(team_name="다른팀")
        self.banned = Team.objects.create(team_name="차단팀", is_banned=True)
        self.user = User.objects.create_user(
            login_id="me", password="pw1234", nickname="나", team=self.team
        )
        self.other_user = User.objects.create_user(
            login_id="other", password="pw1234", nickname="상대", team=self.other
        )
        self.banned_user = User.objects.create_user(
            login_id="banned",
            password="pw1234",
            nickname="차단",
            team=self.banned,
        )
        self.no_team_user = User.objects.create_user(
            login_id="none", password="pw1234", nickname="무소속"
        )
        self.clubs = [
            KothClub.objects.create(name=name)
            for name in ("MJSEC", "ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO")
        ]
        self.club = self.clubs[0]
        self.challenge = KothChallenge.objects.create(
            club=self.club,
            title="KOTH A",
            status=KothChallengeStatus.ACTIVE,
            open_group=1,
            inbound_internal_token_hash=hash_token("inbound-secret"),
            score_api_url="https://koth.example/internal/koth/scores",
            score_api_token_env="KOTH_A_TOKEN",
        )
        for number, club in enumerate(self.clubs[1:], start=2):
            KothChallenge.objects.create(
                club=club,
                title=f"KOTH {number}",
                status=KothChallengeStatus.SCHEDULED,
                open_group=number,
                inbound_internal_token_hash=hash_token(f"secret-{number}"),
            )

    def auth(self, login_id="me"):
        response = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['data']['access_token']}"
        )

    def test_clubs_and_detail_expose_current_owner(self):
        solved_at = timezone.now()
        KothSolve.objects.create(
            team=self.team,
            challenge=self.challenge,
            earned_score=Decimal("40"),
            solved_at=solved_at,
        )
        KothSolve.objects.create(
            team=self.banned,
            challenge=self.challenge,
            earned_score=Decimal("100"),
            solved_at=solved_at,
        )
        response = self.client.get("/api/v1/koth/clubs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["total_count"], 6)
        self.assertEqual(response.data["data"]["challenge_count"], 6)
        club = next(
            club
            for club in response.data["data"]["clubs"]
            if club["club_id"] == str(self.club.club_id)
        )
        challenge = club["challenges"][0]
        self.assertEqual(challenge["current_owner_team_id"], str(self.team.team_id))
        self.assertEqual(challenge["current_score"], 40)

        detail = self.client.get(f"/api/v1/koth/clubs/{self.club.club_id}")
        self.assertEqual(detail.data["data"]["challenge_count"], 1)
        self.assertEqual(
            self.client.get("/api/v1/koth/clubs/not-a-uuid").data["code"],
            "INVALID_CLUB_ID",
        )
        self.assertEqual(
            self.client.get(f"/api/v1/koth/clubs/{KothClub().club_id}").data["code"],
            "CLUB_NOT_FOUND",
        )

    def test_each_club_accepts_only_one_challenge(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                KothChallenge.objects.create(
                    club=self.club,
                    title="Duplicate",
                    status=KothChallengeStatus.SCHEDULED,
                    open_group=7,
                    inbound_internal_token_hash=hash_token("duplicate-secret"),
                )

    def test_clubs_fetch_only_owner_fields_with_constant_query_count(self):
        teams = Team.objects.bulk_create(
            [Team(team_name=f"bulk-{i:03}") for i in range(100)]
        )
        KothSolve.objects.bulk_create(
            [
                KothSolve(
                    team=team,
                    challenge=self.challenge,
                    earned_score=i + 1,
                    solved_at=timezone.now(),
                )
                for i, team in enumerate(teams)
            ]
        )
        KothClub.objects.create(name="Empty club")
        with self.assertNumQueries(2), patch.object(
            KothSolve,
            "from_db",
            side_effect=AssertionError("Loading all solve objects is unnecessary"),
        ):
            response = self.client.get("/api/v1/koth/clubs")
        data = response.data["data"]
        self.assertEqual(data["total_count"], 7)
        self.assertEqual(data["challenge_count"], 6)
        self.assertEqual(data["active_count"], 1)
        challenge = next(c for c in data["clubs"] if c["club_id"] == str(self.club.pk))[
            "challenges"
        ][0]
        self.assertEqual(challenge["current_owner_team_id"], str(teams[-1].pk))
        self.assertEqual(challenge["current_score"], 100)
        unowned = next(
            c for c in data["clubs"] if c["club_id"] == str(self.clubs[1].pk)
        )["challenges"][0]
        self.assertIsNone(unowned["current_owner_team_id"])
        self.assertIsNone(unowned["current_owner_team_name"])
        self.assertEqual(unowned["current_score"], 0)

    def test_owner_ties_and_updates_remain_visible_immediately(self):
        first_at = datetime(2026, 9, 3, 7, 0, tzinfo=dt_timezone.utc)
        later_at = datetime(2026, 9, 3, 7, 15, tzinfo=dt_timezone.utc)
        self.team.team_name = "A"
        self.team.save(update_fields=["team_name"])
        self.other.team_name = "B"
        self.other.save(update_fields=["team_name"])
        own = KothSolve.objects.create(
            team=self.team,
            challenge=self.challenge,
            earned_score=100,
            solved_at=later_at,
        )
        KothSolve.objects.create(
            team=self.other,
            challenge=self.challenge,
            earned_score=100,
            solved_at=first_at,
        )
        url = f"/api/v1/koth/clubs/{self.club.pk}"
        with self.assertNumQueries(2):
            response = self.client.get(url)
        self.assertEqual(
            response.data["data"]["challenges"][0]["current_owner_team_id"],
            str(self.other.pk),
        )
        own.solved_at = first_at
        own.save(update_fields=["solved_at"])
        self.assertEqual(
            self.client.get(url).data["data"]["challenges"][0]["current_owner_team_id"],
            str(self.team.pk),
        )
        self.team.is_banned = True
        self.team.save(update_fields=["is_banned"])
        self.assertEqual(
            self.client.get(url).data["data"]["challenges"][0]["current_owner_team_id"],
            str(self.other.pk),
        )

    def test_me_ranks_multiple_challenges_without_per_challenge_queries(self):
        challenges = list(KothChallenge.objects.all())
        KothSolve.objects.bulk_create(
            [
                KothSolve(
                    team=self.team,
                    challenge=challenge,
                    earned_score=10,
                    solved_at=timezone.now(),
                )
                for challenge in challenges
            ]
        )
        KothSolve.objects.create(
            team=self.other,
            challenge=self.challenge,
            earned_score=20,
            solved_at=timezone.now(),
        )
        KothSolve.objects.create(
            team=self.banned,
            challenge=self.challenge,
            earned_score=30,
            solved_at=timezone.now(),
        )
        self.auth()
        with self.assertNumQueries(3):
            response = self.client.get("/api/v1/koth/me")
        data = response.data["data"]
        self.assertEqual(data["total_koth_score"], 60)
        self.assertEqual([c["rank"] for c in data["challenges"]], [2, 1, 1, 1, 1, 1])

    def test_challenge_url_is_available_on_all_challenge_responses_and_can_be_cleared(
        self,
    ):
        # The score endpoint is configured, but it must not be used as a public URL.
        self.assertEqual(self.challenge.challenge_url, "")
        for challenge_url in (
            "",
            "http://192.0.2.10:8080/",
            "https://challenge.example/play?mode=koth",
            "",
        ):
            with self.subTest(challenge_url=challenge_url):
                self.challenge.challenge_url = challenge_url
                self.challenge.save(update_fields=["challenge_url"])
                self.client.credentials()
                clubs = self.client.get("/api/v1/koth/clubs")
                detail = self.client.get(f"/api/v1/koth/clubs/{self.club.club_id}")
                self.auth()
                progress = self.client.get("/api/v1/koth/me")
                for response in (clubs, detail, progress):
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.data["code"], "SUCCESS")

                club = next(
                    row
                    for row in clubs.data["data"]["clubs"]
                    if row["club_id"] == str(self.club.club_id)
                )
                own_challenge = next(
                    row
                    for row in progress.data["data"]["challenges"]
                    if row["koth_challenge_id"] == str(self.challenge.pk)
                )
                for row in (
                    club["challenges"][0],
                    detail.data["data"]["challenges"][0],
                    own_challenge,
                ):
                    self.assertEqual(row["challenge_url"], challenge_url or None)
                    for internal_field in (
                        "score_api_url",
                        "score_api_token_env",
                        "inbound_internal_token_hash",
                    ):
                        self.assertNotIn(internal_field, row)

                for row in progress.data["data"]["challenges"]:
                    if row["koth_challenge_id"] != str(self.challenge.pk):
                        self.assertIsNone(row["challenge_url"])

    def test_challenge_url_form_accepts_blank_or_http_urls(self):
        form_class = modelform_factory(KothChallenge, fields=["challenge_url"])
        for challenge_url in (
            "",
            "http://192.0.2.10:8080/",
            "https://challenge.example/play",
        ):
            with self.subTest(challenge_url=challenge_url):
                form = form_class(
                    data={"challenge_url": challenge_url}, instance=self.challenge
                )
                self.assertTrue(form.is_valid(), form.errors)
                form.save()
                self.challenge.refresh_from_db()
                self.assertEqual(self.challenge.challenge_url, challenge_url)
        for challenge_url in (
            "ftp://challenge.example/",
            "javascript:alert(1)",
            "/play",
        ):
            with self.subTest(challenge_url=challenge_url):
                form = form_class(
                    data={"challenge_url": challenge_url}, instance=self.challenge
                )
                self.assertFalse(form.is_valid())
                self.assertIn("challenge_url", form.errors)

    def test_me_reports_all_challenges_and_team_token_is_shared(self):
        solved_at = timezone.now()
        KothSolve.objects.create(
            team=self.team,
            challenge=self.challenge,
            earned_score=Decimal("40"),
            solved_at=solved_at,
        )
        KothSolve.objects.create(
            team=self.other,
            challenge=self.challenge,
            earned_score=Decimal("50"),
            solved_at=solved_at,
        )
        KothSolve.objects.create(
            team=self.banned,
            challenge=self.challenge,
            earned_score=Decimal("60"),
            solved_at=solved_at,
        )
        self.auth()
        response = self.client.get("/api/v1/koth/me")
        self.assertEqual(response.data["data"]["total_koth_score"], 40)
        self.assertEqual(response.data["data"]["challenges"][0]["rank"], 2)
        self.assertEqual(response.data["data"]["total_count"], 6)

        first = self.client.get("/api/v1/koth/team_token").data["data"]["team_token"]
        User.objects.create_user(
            login_id="member", password="pw1234", nickname="팀원", team=self.team
        )
        self.auth("member")
        second = self.client.get("/api/v1/koth/team_token").data["data"]["team_token"]
        self.assertEqual(first, second)
        self.assertEqual(KothTeamToken.objects.count(), 1)
        self.assertEqual(KothTeamToken.objects.get().token_hash, hash_token(first))

    def test_me_does_not_assign_rank_to_banned_team(self):
        KothSolve.objects.create(
            team=self.banned,
            challenge=self.challenge,
            earned_score=Decimal("60"),
            solved_at=timezone.now(),
        )
        self.auth("banned")

        response = self.client.get("/api/v1/koth/me")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["challenges"][0]["earned_score"], 60)
        self.assertIsNone(response.data["data"]["challenges"][0]["rank"])

    def test_leaderboard_requires_auth_and_valid_challenge_id(self):
        url = "/api/v1/koth/leaderboard"
        self.assertEqual(self.client.get(url).status_code, 401)

        self.auth()
        required = self.client.get(url)
        self.assertEqual(required.status_code, 400)
        self.assertEqual(required.data["code"], "KOTH_CHALLENGE_ID_REQUIRED")

        invalid = self.client.get(f"{url}?koth_challenge_id=not-a-uuid")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.data["code"], "INVALID_KOTH_CHALLENGE_ID")

        missing = self.client.get(
            f"{url}?koth_challenge_id={KothChallenge().koth_challenge_id}"
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.data["code"], "KOTH_CHALLENGE_NOT_FOUND")

    def test_leaderboard_returns_score_order_and_shared_tie_rank(self):
        third = Team.objects.create(team_name="세번째팀")
        zero = Team.objects.create(team_name="0점팀")
        first_at = datetime(2026, 9, 3, 7, 0, tzinfo=dt_timezone.utc)
        second_at = datetime(2026, 9, 3, 7, 15, tzinfo=dt_timezone.utc)
        KothSolve.objects.create(
            team=self.team,
            challenge=self.challenge,
            earned_score=Decimal("100"),
            solved_at=second_at,
        )
        KothSolve.objects.create(
            team=self.other,
            challenge=self.challenge,
            earned_score=Decimal("100"),
            solved_at=first_at,
        )
        KothSolve.objects.create(
            team=third,
            challenge=self.challenge,
            earned_score=Decimal("40"),
            solved_at=first_at,
        )
        KothSolve.objects.create(
            team=self.banned,
            challenge=self.challenge,
            earned_score=Decimal("999"),
            solved_at=first_at,
        )
        KothSolve.objects.create(
            team=zero, challenge=self.challenge, earned_score=Decimal("0")
        )
        self.auth()

        response = self.client.get(
            f"/api/v1/koth/leaderboard?koth_challenge_id={self.challenge.koth_challenge_id}"
        )
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(
            data["koth_challenge_id"], str(self.challenge.koth_challenge_id)
        )
        self.assertEqual(data["title"], self.challenge.title)
        self.assertEqual(data["status"], KothChallengeStatus.ACTIVE)
        self.assertEqual(data["total_count"], 3)
        self.assertIsNotNone(data["updated_at"])
        self.assertEqual(
            [row["team_id"] for row in data["leaderboard"]],
            [str(self.other.team_id), str(self.team.team_id), str(third.team_id)],
        )
        self.assertEqual([row["rank"] for row in data["leaderboard"]], [1, 1, 3])
        self.assertEqual(
            [row["earned_score"] for row in data["leaderboard"]], [100, 100, 40]
        )
        self.assertTrue(
            all(row["solved_at"] is not None for row in data["leaderboard"])
        )

    @override_settings(KOTH_LEADERBOARD_CACHE_SECONDS=60)
    def test_leaderboard_cache_reuses_json_but_still_authenticates_and_separates_challenges(
        self,
    ):
        KothSolve.objects.create(
            team=self.team,
            challenge=self.challenge,
            earned_score=40,
            solved_at=timezone.now(),
        )
        self.auth()
        url = f"/api/v1/koth/leaderboard?koth_challenge_id={self.challenge.pk}"
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.auth("other")
        with self.assertNumQueries(
            1
        ):  # The requesting user is still authenticated against the DB.
            second = self.client.get(url)
        self.assertEqual(second.content, first.content)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(second["Content-Type"], first["Content-Type"])

        other_challenge = KothChallenge.objects.exclude(pk=self.challenge.pk).first()
        separate = self.client.get(
            f"/api/v1/koth/leaderboard?koth_challenge_id={other_challenge.pk}"
        )
        self.assertEqual(
            separate.data["data"]["koth_challenge_id"], str(other_challenge.pk)
        )
        self.assertEqual(separate.data["data"]["total_count"], 0)
        self.client.credentials()
        self.assertEqual(self.client.get(url).status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer invalid-token")
        self.assertEqual(self.client.get(url).status_code, 401)

    @override_settings(KOTH_LEADERBOARD_CACHE_SECONDS=1)
    def test_leaderboard_cache_expires_and_reflects_score_and_ban_changes(self):
        KothSolve.objects.create(
            team=self.team,
            challenge=self.challenge,
            earned_score=40,
            solved_at=timezone.now(),
        )
        KothSolve.objects.create(
            team=self.other,
            challenge=self.challenge,
            earned_score=10,
            solved_at=timezone.now(),
        )
        self.auth()
        url = f"/api/v1/koth/leaderboard?koth_challenge_id={self.challenge.pk}"
        now = time.time()
        with patch("django.core.cache.backends.locmem.time.time", return_value=now):
            first = self.client.get(url)
            KothSolve.objects.filter(team=self.team, challenge=self.challenge).update(
                earned_score=100
            )
            Team.objects.filter(pk=self.other.pk).update(is_banned=True)
            self.assertEqual(self.client.get(url).content, first.content)
        with patch(
            "django.core.cache.backends.locmem.time.time", return_value=now + 1.01
        ):
            refreshed = self.client.get(url)
        rows = refreshed.data["data"]["leaderboard"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["team_id"], str(self.team.pk))
        self.assertEqual(rows[0]["earned_score"], 100)
        self.assertEqual(rows[0]["rank"], 1)

    def test_leaderboard_cache_failure_falls_back_to_database(self):
        KothSolve.objects.create(
            team=self.team,
            challenge=self.challenge,
            earned_score=40,
            solved_at=timezone.now(),
        )
        self.auth()
        with patch("apps.koth.views.cache.get", side_effect=ConnectionError), patch(
            "apps.koth.views.cache.set", side_effect=ConnectionError
        ) as cache_set:
            response = self.client.get(
                f"/api/v1/koth/leaderboard?koth_challenge_id={self.challenge.pk}"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["leaderboard"][0]["earned_score"], 40)
        cache_set.assert_called_once()

    @override_settings(KOTH_LEADERBOARD_CACHE_SECONDS=0)
    def test_leaderboard_cache_can_be_disabled_for_immediate_updates(self):
        KothSolve.objects.create(
            team=self.team,
            challenge=self.challenge,
            earned_score=40,
            solved_at=timezone.now(),
        )
        self.auth()
        url = f"/api/v1/koth/leaderboard?koth_challenge_id={self.challenge.pk}"
        with patch("apps.koth.views.cache.get") as cache_get, patch(
            "apps.koth.views.cache.set"
        ) as cache_set:
            self.client.get(url)
            KothSolve.objects.filter(team=self.team, challenge=self.challenge).update(
                earned_score=100
            )
            response = self.client.get(url)
        self.assertEqual(response.data["data"]["leaderboard"][0]["earned_score"], 100)
        cache_get.assert_not_called()
        cache_set.assert_not_called()

    def test_leaderboard_browsable_html_does_not_use_shared_json_cache(self):
        self.auth()
        url = f"/api/v1/koth/leaderboard?koth_challenge_id={self.challenge.pk}"
        self.client.get(url)
        with patch("apps.koth.views.cache.get") as cache_get, patch(
            "apps.koth.views.cache.set"
        ) as cache_set:
            response = self.client.get(url, HTTP_ACCEPT="text/html")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/html"))
        cache_get.assert_not_called()
        cache_set.assert_not_called()

    def test_team_token_requires_team(self):
        self.auth("none")
        response = self.client.get("/api/v1/koth/team_token")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "USER_HAS_NO_TEAM")

    def test_internal_token_verification_and_team_listing(self):
        self.auth()
        raw_token = self.client.get("/api/v1/koth/team_token").data["data"][
            "team_token"
        ]
        headers = {"HTTP_X_INTERNAL_TOKEN": "inbound-secret"}
        response = self.client.post(
            "/internal/koth/team_tokens/verify",
            {
                "koth_challenge_id": str(self.challenge.koth_challenge_id),
                "team_token": raw_token,
            },
            format="json",
            **headers,
        )
        self.assertTrue(response.data["data"]["valid"])
        invalid = self.client.post(
            "/internal/koth/team_tokens/verify",
            {
                "koth_challenge_id": str(self.challenge.koth_challenge_id),
                "team_token": "bad",
            },
            format="json",
            **headers,
        )
        self.assertFalse(invalid.data["data"]["valid"])
        self.assertEqual(KothTokenVerificationAttempt.objects.count(), 1)
        unauthenticated = self.client.get(
            f"/internal/teams?koth_challenge_id={self.challenge.koth_challenge_id}"
        )
        self.assertEqual(unauthenticated.status_code, 401)
        teams = self.client.get(
            f"/internal/teams?koth_challenge_id={self.challenge.koth_challenge_id}",
            **headers,
        )
        returned_ids = {team["team_id"] for team in teams.data["data"]["teams"]}
        self.assertIn(str(self.team.team_id), returned_ids)
        self.assertNotIn(str(self.banned.team_id), returned_ids)


@override_settings(CACHES=LOCMEM)
class KothScorePollingTests(TestCase):
    def setUp(self):
        self.club = KothClub.objects.create(name="Club")
        self.challenge = KothChallenge.objects.create(
            club=self.club,
            title="KOTH",
            status=KothChallengeStatus.ACTIVE,
            open_group=1,
            inbound_internal_token_hash=hash_token("inbound"),
            score_api_url="https://example.test/scores",
            score_api_token_env="TEST_SCORE_TOKEN",
        )
        self.first = Team.objects.create(team_name="first")
        self.second = Team.objects.create(team_name="second")
        self.third = Team.objects.create(team_name="third")
        self.period = datetime(2026, 7, 31, 10, 15, tzinfo=dt_timezone.utc)

    @patch.dict(os.environ, {"TEST_SCORE_TOKEN": "test-token"})
    def test_score_api_url_must_use_http_or_https(self):
        self.challenge.score_api_url = "file:///tmp/scores"
        with self.assertRaises(ScoreFetchError):
            _request_scores(self.challenge, self.period)

    def payload(self):
        return {
            "code": "SUCCESS",
            "message": "성공",
            "data": {
                "koth_challenge_id": str(self.challenge.koth_challenge_id),
                "period_id": "2026-07-31T10:15:00Z",
                "results": [
                    {
                        "team_id": str(self.first.team_id),
                        "period_rank": 1,
                        "metric_score": 99.9,
                    },
                    {
                        "team_id": str(self.second.team_id),
                        "period_rank": 1,
                        "metric_score": 99.9,
                    },
                    {
                        "team_id": str(self.third.team_id),
                        "period_rank": 3,
                        "metric_score": 80,
                    },
                ],
            },
        }

    @patch("apps.koth.services._request_scores")
    def test_poll_applies_tie_scores_once_and_updates_team_summary(
        self, request_scores
    ):
        request_scores.return_value = self.payload()
        self.assertTrue(poll_challenge_period(self.challenge, self.period))
        self.assertEqual(KothSolve.objects.get(team=self.first).earned_score, 32)
        self.assertEqual(KothSolve.objects.get(team=self.second).earned_score, 32)
        self.assertEqual(KothSolve.objects.get(team=self.third).earned_score, 15)
        self.assertFalse(poll_challenge_period(self.challenge, self.period))
        self.assertEqual(
            KothScorePeriod.objects.get().status, KothScorePeriodStatus.APPLIED
        )
        self.assertEqual(KothSolve.objects.get(team=self.first).earned_score, 32)

        user = User.objects.create_user(
            login_id="first", password="pw1234", nickname="첫", team=self.first
        )
        client = APIClient()
        login = client.post(
            "/api/v1/auth/login",
            {"login_id": "first", "password": "pw1234"},
            format="json",
        )
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['data']['access_token']}"
        )
        summary = client.get("/api/v1/teams/me")
        self.assertEqual(summary.data["data"]["koth_score"], 32)
        self.assertEqual(summary.data["data"]["team_score"], 32)

    @patch("apps.koth.services._request_scores")
    def test_empty_period_is_applied_without_a_solve(self, request_scores):
        request_scores.return_value = {
            "code": "SUCCESS",
            "message": "성공",
            "data": None,
        }
        self.assertTrue(poll_challenge_period(self.challenge, self.period))
        self.assertEqual(KothSolve.objects.count(), 0)
        self.assertEqual(
            KothScorePeriod.objects.get().status, KothScorePeriodStatus.APPLIED
        )

    @patch("apps.koth.services._request_scores")
    def test_invalid_tie_rank_sequence_marks_period_failed(self, request_scores):
        payload = self.payload()
        payload["data"]["results"][2]["period_rank"] = 2
        request_scores.return_value = payload

        with self.assertRaises(ScoreFetchError):
            poll_challenge_period(self.challenge, self.period)
        self.assertEqual(
            KothScorePeriod.objects.get().status, KothScorePeriodStatus.FAILED
        )

    @patch("apps.koth.services._request_scores")
    def test_banned_team_in_rank_gap_does_not_fail_period(self, request_scores):
        self.second.is_banned = True
        self.second.save(update_fields=["is_banned"])
        payload = self.payload()
        payload["data"]["results"][1]["period_rank"] = 2
        request_scores.return_value = payload

        self.assertTrue(poll_challenge_period(self.challenge, self.period))
        self.assertEqual(
            KothScorePeriod.objects.get().status, KothScorePeriodStatus.APPLIED
        )
        self.assertEqual(KothSolve.objects.get(team=self.first).earned_score, 40)
        self.assertEqual(KothSolve.objects.get(team=self.third).earned_score, 15)
        self.assertFalse(KothSolve.objects.filter(team=self.second).exists())
