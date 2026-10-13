from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth.hashers import make_password
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge, Solve
from apps.challenge.services import get_team_total_score
from apps.koth.models import KothChallenge, KothClub, KothSolve

from .models import SignatureChallenge, SignatureFlagSubmission, SignatureSolve


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking")
class ConcurrentSignatureSubmitTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.team = Team.objects.create(team_name="signature-race-team")
        self.user = User.objects.create_user(
            login_id="signature-race-user",
            password="pw1234",
            nickname="signature-race-user",
            team=self.team,
        )
        club = KothClub.objects.create(name="signature-race-club")
        self.challenge = SignatureChallenge.objects.create(
            club=club,
            title="Concurrent Signature",
            description="Submit once",
            score=Decimal("300"),
            flag_hash=make_password("MSG{signature_race}"),
            is_published=True,
        )

    def test_concurrent_correct_submissions_award_score_once(self):
        start = Barrier(2)

        def submit():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")

                client = APIClient()
                client.force_authenticate(user=User.objects.get(pk=self.user.pk))
                synchronized = False

                def synchronize(execute, sql, params, many, context):
                    nonlocal synchronized
                    is_team_lock = (
                        'FROM "teams"' in sql
                        and "FOR UPDATE" in sql
                        and not synchronized
                    )
                    if is_team_lock:
                        synchronized = True
                        start.wait(timeout=10)
                    return execute(sql, params, many, context)

                with connection.execute_wrapper(synchronize):
                    response = client.post(
                        f"/api/v1/signatures/{self.challenge.pk}/submit",
                        {"flag": "MSG{signature_race}"},
                        format="json",
                    )
                return response.status_code, response.data
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))

        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        self.assertEqual(
            sorted(body["code"] for _, body in results),
            ["ALREADY_SOLVED", "SUCCESS"],
        )
        self.assertEqual(
            SignatureSolve.objects.filter(
                team=self.team,
                challenge=self.challenge,
            ).count(),
            1,
        )
        self.assertEqual(
            list(
                SignatureFlagSubmission.objects.order_by("result").values_list(
                    "result",
                    flat=True,
                )
            ),
            [
                SignatureFlagSubmission.SubmissionResult.ALREADY_SOLVED,
                SignatureFlagSubmission.SubmissionResult.CORRECT,
            ],
        )
        self.assertEqual(get_team_total_score(self.team.pk), Decimal("300"))

    def test_different_teams_submit_same_challenge_without_serializing(self):
        other_team = Team.objects.create(team_name="signature-race-other-team")
        other_user = User.objects.create_user(
            login_id="signature-race-other-user",
            password="pw1234",
            nickname="signature-race-other-user",
            team=other_team,
        )
        ready_to_insert = Barrier(2)

        def submit(user_id):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '2s'")
                    cursor.execute("SET statement_timeout = '10s'")

                client = APIClient()
                client.force_authenticate(user=User.objects.get(pk=user_id))
                synchronized = False

                def synchronize(execute, sql, params, many, context):
                    nonlocal synchronized
                    is_solve_insert = (
                        'INSERT INTO "signature_solves"' in sql and not synchronized
                    )
                    if is_solve_insert:
                        synchronized = True
                        ready_to_insert.wait(timeout=5)
                    return execute(sql, params, many, context)

                with connection.execute_wrapper(synchronize):
                    response = client.post(
                        f"/api/v1/signatures/{self.challenge.pk}/submit",
                        {"flag": "MSG{signature_race}"},
                        format="json",
                    )
                return response.status_code, response.data
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, [self.user.pk, other_user.pk]))

        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual([body["code"] for _, body in results], ["SUCCESS", "SUCCESS"])
        self.assertEqual(
            SignatureSolve.objects.filter(challenge=self.challenge).count(),
            2,
        )


class SignatureScoreAggregationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.team = Team.objects.create(
            team_name="signature-score-team",
            team_score=Decimal("900"),
        )
        self.user = User.objects.create_user(
            login_id="signature-score-user",
            password="pw1234",
            nickname="signature-score-user",
            team=self.team,
        )
        self.client.force_authenticate(user=self.user)

    def test_all_team_score_endpoints_include_signature_scores_once(self):
        jeopardy = Challenge.objects.create(
            title="Jeopardy",
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=Decimal("900"),
            current_score=Decimal("900"),
            flag_hash="unused",
            is_published=True,
        )
        Solve.objects.create(
            team=self.team,
            challenge=jeopardy,
            solved_by_user=self.user,
            earned_score=Decimal("900"),
            earned_mileage=0,
        )

        koth_club = KothClub.objects.create(name="score-koth-club")
        koth = KothChallenge.objects.create(
            club=koth_club,
            title="KOTH",
            open_group=1,
            inbound_internal_token_hash="unused",
        )
        KothSolve.objects.create(
            team=self.team,
            challenge=koth,
            earned_score=Decimal("50"),
            solved_at=timezone.now(),
        )

        for index, score in enumerate((Decimal("300"), Decimal("450"))):
            club = KothClub.objects.create(name=f"score-signature-club-{index}")
            signature = SignatureChallenge.objects.create(
                club=club,
                title=f"Signature {index}",
                description="Signature score",
                score=score,
                flag_hash="unused",
                is_published=True,
            )
            SignatureSolve.objects.create(
                team=self.team,
                challenge=signature,
                solved_by_user=self.user,
                earned_score=score,
            )

        expected_total = 1700
        self.assertEqual(get_team_total_score(self.team.pk), Decimal(expected_total))

        ranking = self.client.get("/api/v1/ranking").data["data"]["rankings"][0]
        self.assertEqual(ranking["team_score"], expected_total)

        leaderboard = self.client.get("/api/v1/leaderboard").data["data"]["teams"][0]
        self.assertEqual(leaderboard["team_score"], expected_total)
        self.assertEqual(
            [row["source_type"] for row in leaderboard["solves"]].count("SIGNATURE"),
            2,
        )
        self.assertEqual(
            sum(row["points"] for row in leaderboard["solves"]),
            expected_total,
        )

        team_me = self.client.get("/api/v1/teams/me").data["data"]
        self.assertEqual(team_me["signature_score"], 750)
        self.assertEqual(team_me["team_score"], expected_total)

        solves = self.client.get("/api/v1/teams/me/solves").data["data"]
        self.assertEqual(solves["total_count"], 4)
        self.assertEqual(
            [row["source_type"] for row in solves["solves"]].count("SIGNATURE"),
            2,
        )
