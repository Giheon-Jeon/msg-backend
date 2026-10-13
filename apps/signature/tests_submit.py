from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.koth.models import KothClub
from apps.ranking.views import collect_team_data

from .models import (
    SignatureChallenge,
    SignatureFlagSubmission,
    SignatureSolve,
    SignatureSubmissionLock,
)


BASE_TIME = datetime(2026, 9, 13, 0, 0, tzinfo=datetime_timezone.utc)


class SignatureSubmitApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.team = Team.objects.create(team_name="시그니처 참가 팀")
        self.user = User.objects.create_user(
            login_id="signature-submitter",
            password="pw1234",
            nickname="제출자",
            team=self.team,
        )
        self.club = KothClub.objects.create(name="CodeCure")
        self.challenge = SignatureChallenge.objects.create(
            club=self.club,
            title="CodeCure Signature",
            description="부스 플래그를 제출하세요.",
            score=Decimal("300"),
            flag_hash=make_password("MSG{correct_signature}"),
            is_published=True,
        )
        self.url = f"/api/v1/signatures/{self.challenge.signature_id}/submit"
        self.client.force_authenticate(user=self.user)

    def submit(self, flag):
        return self.client.post(self.url, {"flag": flag}, format="json")

    def test_correct_flag_creates_solve_and_updates_team_ranking_score(self):
        response = self.submit("MSG{correct_signature}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "SUCCESS")
        self.assertEqual(response.data["data"]["club_id"], str(self.club.club_id))
        self.assertEqual(response.data["data"]["earned_score"], 300)
        self.assertEqual(response.data["data"]["team_score"], 300)
        solve = SignatureSolve.objects.get(team=self.team, challenge=self.challenge)
        self.assertEqual(solve.solved_by_user, self.user)
        self.assertEqual(solve.earned_score, Decimal("300"))
        self.assertEqual(
            SignatureFlagSubmission.objects.get().result,
            SignatureFlagSubmission.SubmissionResult.CORRECT,
        )
        lock = SignatureSubmissionLock.objects.get(
            team=self.team,
            challenge=self.challenge,
        )
        self.assertEqual(lock.failed_count, 0)
        self.assertIsNone(lock.locked_until)

        ranking_row = next(
            row for row in collect_team_data() if row["team_id"] == str(self.team.pk)
        )
        self.assertEqual(ranking_row["signature_score"], Decimal("300"))
        ranking = self.client.get("/api/v1/ranking")
        self.assertEqual(ranking.data["data"]["rankings"][0]["team_score"], 300)

        leaderboard = self.client.get("/api/v1/leaderboard")
        leaderboard_team = leaderboard.data["data"]["teams"][0]
        self.assertEqual(leaderboard_team["team_score"], 300)
        self.assertEqual(leaderboard_team["solves"][0]["source_type"], "SIGNATURE")

        team_me = self.client.get("/api/v1/teams/me")
        self.assertEqual(team_me.data["data"]["team_score"], 300)
        self.assertEqual(team_me.data["data"]["signature_score"], 300)

        team_solves = self.client.get("/api/v1/teams/me/solves")
        self.assertEqual(team_solves.data["data"]["total_count"], 1)
        self.assertEqual(
            team_solves.data["data"]["solves"][0]["source_type"],
            "SIGNATURE",
        )

    def test_solved_team_cannot_receive_score_twice(self):
        self.assertEqual(self.submit("MSG{correct_signature}").status_code, 200)

        duplicate = self.submit("MSG{correct_signature}")

        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.data["code"], "ALREADY_SOLVED")
        self.assertEqual(SignatureSolve.objects.filter(team=self.team).count(), 1)
        self.assertEqual(
            SignatureFlagSubmission.objects.order_by("created_at").last().result,
            SignatureFlagSubmission.SubmissionResult.ALREADY_SOLVED,
        )

    def test_third_wrong_flag_locks_submissions_for_30_seconds(self):
        with patch("apps.signature.views.timezone.now", return_value=BASE_TIME):
            first = self.submit("wrong-1")
            second = self.submit("wrong-2")
            third = self.submit("wrong-3")
            blocked = self.submit("MSG{correct_signature}")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["code"], "INCORRECT_FLAG")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(third.status_code, 429)
        self.assertEqual(third.data["code"], "TOO_MANY_ATTEMPTS")
        self.assertEqual(third.data["data"]["retry_after_seconds"], 30)
        self.assertEqual(blocked.status_code, 429)
        self.assertFalse(SignatureSolve.objects.filter(team=self.team).exists())
        lock = SignatureSubmissionLock.objects.get(
            team=self.team, challenge=self.challenge
        )
        self.assertEqual(lock.failed_count, 3)
        self.assertEqual(lock.locked_until, BASE_TIME + timedelta(seconds=30))
        raw_flags = {
            "wrong-1",
            "wrong-2",
            "wrong-3",
            "MSG{correct_signature}",
        }
        self.assertFalse(
            SignatureFlagSubmission.objects.filter(
                submitted_flag_hash__in=raw_flags
            ).exists()
        )

    def test_correct_flag_succeeds_after_lock_expires(self):
        lock = SignatureSubmissionLock.objects.create(
            team=self.team,
            challenge=self.challenge,
            failed_count=3,
            locked_until=BASE_TIME + timedelta(seconds=30),
            last_failed_at=BASE_TIME,
        )

        with patch(
            "apps.signature.views.timezone.now",
            return_value=BASE_TIME + timedelta(seconds=31),
        ):
            response = self.submit("MSG{correct_signature}")

        self.assertEqual(response.status_code, 200)
        lock.refresh_from_db()
        self.assertEqual(lock.failed_count, 0)
        self.assertIsNone(lock.locked_until)
        self.assertIsNone(lock.last_failed_at)

    def test_invalid_body_unpublished_and_missing_challenge_are_rejected(self):
        invalid = self.client.post(self.url, {}, format="json")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.data["code"], "INVALID_REQUEST")

        self.challenge.is_published = False
        self.challenge.save(update_fields=["is_published"])
        hidden = self.submit("MSG{correct_signature}")
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.data["code"], "SIGNATURE_NOT_FOUND")

        missing = self.client.post(
            "/api/v1/signatures/018f3f1e-0300-7a91-a30b-630000000999/submit",
            {"flag": "MSG{correct_signature}"},
            format="json",
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.data["code"], "SIGNATURE_NOT_FOUND")

    def test_user_without_team_and_unauthenticated_user_are_rejected(self):
        no_team = User.objects.create_user(
            login_id="signature-no-team",
            password="pw1234",
            nickname="무소속",
        )
        self.client.force_authenticate(user=no_team)
        response = self.submit("MSG{correct_signature}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "USER_HAS_NO_TEAM")

        self.client.force_authenticate(user=None)
        response = self.submit("MSG{correct_signature}")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["code"], "TOKEN_MISSING")
