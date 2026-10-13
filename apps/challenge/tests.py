import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest import skipUnless
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection, connections, close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import Cell, TeamBoardState, TeamChallengeAccess
from apps.board.services import get_or_create_board_state
from apps.challenge.models import Challenge, FlagSubmissionLock, Solve
from apps.challenge.services import hash_flag
from apps.ranking.models import LineMonopoly
from apps.teams.models import MileageHistory, MileageType

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class ChallengeSubmitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="테스트팀")
        self.user = User.objects.create_user(
            login_id="challenger",
            password="pw1234",
            nickname="챌린저",
            team=self.team,
        )
        self.challenge = Challenge.objects.create(
            title="SQL Injection 기초",
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=1000,
            initial_score=1000,
            minimum_score=100,
            decay=20,
            current_score=1000,
            description="테스트 문제",
            flag_hash=hash_flag("MSG{correct_flag}"),
            is_published=True,
        )
        self.cell = Cell.objects.create(
            cell_index=1,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="challenge-cell-1",
        )
        TeamChallengeAccess.objects.create(
            team=self.team,
            challenge=self.challenge,
            source_cell=self.cell,
        )
        self.auth()

    def auth(self):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": "challenger", "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def submit(self, flag="MSG{wrong_flag}"):
        return self.client.post(
            f"/api/v1/challenges/{self.challenge.challenge_id}/submit",
            {"flag": flag},
            format="json",
        )

    def test_detail_and_submit_require_team_challenge_access(self):
        TeamChallengeAccess.objects.filter(
            team=self.team,
            challenge=self.challenge,
        ).delete()

        detail = self.client.get(f"/api/v1/challenges/{self.challenge.challenge_id}")
        submit = self.submit("MSG{correct_flag}")

        self.assertEqual(detail.status_code, 403)
        self.assertEqual(detail.data["code"], "CHALLENGE_LOCKED")
        self.assertEqual(submit.status_code, 403)
        self.assertEqual(submit.data["code"], "CHALLENGE_LOCKED")

    def test_submission_deadline_is_derived_from_access_opened_at(self):
        TeamChallengeAccess.objects.filter(
            team=self.team,
            challenge=self.challenge,
        ).update(opened_at=timezone.now() - datetime.timedelta(minutes=16))

        response = self.submit("MSG{correct_flag}")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["data"]["is_extra_dice_granted"])
        self.assertEqual(response.data["data"]["earned_mileage"], 30)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 30)

    def test_extra_dice_requires_current_cell_and_active_access(self):
        access = TeamChallengeAccess.objects.get(
            team=self.team, challenge=self.challenge
        )
        other_cell = Cell.objects.create(
            cell_index=2, type=Cell.CellType.START, name="other"
        )
        for case in ("current", "moved", "inactive", "cleared", "expired", "missing"):
            with self.subTest(case=case):
                Solve.objects.filter(team=self.team).delete()
                TeamBoardState.objects.filter(team=self.team).delete()
                access.status = (
                    TeamChallengeAccess.Status.CLEARED
                    if case == "cleared"
                    else TeamChallengeAccess.Status.OPENED
                )
                access.opened_at = timezone.now() - datetime.timedelta(
                    minutes=16 if case == "expired" else 1
                )
                access.save(update_fields=["status", "opened_at"])
                state = None
                if case != "missing":
                    state = TeamBoardState.objects.create(
                        team=self.team,
                        position=other_cell if case == "moved" else self.cell,
                        active_challenge_access=None if case == "inactive" else access,
                        dice_rolls_left=0,
                        next_dice_reset_at=timezone.now()
                        + datetime.timedelta(minutes=10),
                    )
                response = self.submit("MSG{correct_flag}")
                self.assertEqual(response.status_code, 200)
                granted = case == "current"
                self.assertEqual(
                    response.data["data"]["is_extra_dice_granted"], granted
                )
                solve = Solve.objects.get(team=self.team, challenge=self.challenge)
                self.assertEqual(solve.is_extra_dice_granted, granted)
                access.refresh_from_db()
                self.assertEqual(access.status, TeamChallengeAccess.Status.CLEARED)
                if state:
                    state.refresh_from_db()
                    self.assertEqual(state.dice_rolls_left, int(granted))
                    self.assertIsNotNone(state.next_dice_reset_at)
                if granted:
                    duplicate = self.submit("MSG{correct_flag}")
                    self.assertEqual(duplicate.status_code, 409)
                    state.refresh_from_db()
                    self.assertEqual(state.dice_rolls_left, 1)

    def test_solve_reward_preserves_recharge_until_full_and_reports_actual_grant(self):
        access = TeamChallengeAccess.objects.get(
            team=self.team, challenge=self.challenge
        )
        now = timezone.now().replace(microsecond=0)
        deadline = now + datetime.timedelta(minutes=5)
        for initial_rolls in (0, 1, 2, 3):
            with self.subTest(initial_rolls=initial_rolls):
                Solve.objects.filter(team=self.team).delete()
                access.status = TeamChallengeAccess.Status.OPENED
                access.cleared_at = None
                access.save(update_fields=["status", "cleared_at"])
                state, _ = TeamBoardState.objects.update_or_create(
                    team=self.team,
                    defaults={
                        "position": self.cell,
                        "active_challenge_access": access,
                        "dice_rolls_left": initial_rolls,
                        "next_dice_reset_at": deadline if initial_rolls < 3 else None,
                    },
                )
                with patch("apps.board.services.timezone.now", return_value=now):
                    response = self.submit("MSG{correct_flag}")
                    self.assertEqual(response.status_code, 200)
                    expected_rolls = min(3, initial_rolls + 1)
                    expected_deadline = deadline if expected_rolls < 3 else None
                    state.refresh_from_db()
                    self.assertEqual(state.dice_rolls_left, expected_rolls)
                    self.assertEqual(state.next_dice_reset_at, expected_deadline)
                    self.assertEqual(
                        response.data["data"]["is_extra_dice_granted"],
                        initial_rolls < 3,
                    )
                    self.assertEqual(
                        Solve.objects.get(
                            team=self.team, challenge=self.challenge
                        ).is_extra_dice_granted,
                        initial_rolls < 3,
                    )
                    for path in ("/api/v1/board/me", "/api/v1/board/dice/status"):
                        status = self.client.get(path)
                        self.assertEqual(status.status_code, 200)
                        self.assertEqual(
                            status.data["data"]["dice_rolls_left"], expected_rolls
                        )
                        self.assertEqual(
                            status.data["data"]["next_dice_reset_at"], expected_deadline
                        )

                with patch("apps.board.services.timezone.now", return_value=deadline):
                    status = self.client.get("/api/v1/board/dice/status")
                    charged_rolls = min(3, expected_rolls + 1)
                    self.assertEqual(
                        status.data["data"]["dice_rolls_left"], charged_rolls
                    )
                    self.assertEqual(
                        status.data["data"]["next_dice_reset_at"],
                        (
                            deadline + datetime.timedelta(minutes=15)
                            if charged_rolls < 3
                            else None
                        ),
                    )

    def test_three_wrong_flags_lock_submission(self):
        # 같은 팀이 같은 문제에 3회 연속 오답을 내면 제출 제한이 걸린다
        self.assertEqual(self.submit().data["code"], "INCORRECT_FLAG")
        self.assertEqual(self.submit().data["code"], "INCORRECT_FLAG")

        res = self.submit()

        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.data["code"], "TOO_MANY_ATTEMPTS")
        self.assertIn("retry_after_seconds", res.data["data"])

        flag_lock = FlagSubmissionLock.objects.get(
            team=self.team, challenge=self.challenge
        )
        self.assertEqual(flag_lock.failed_count, 3)
        self.assertIsNotNone(flag_lock.locked_until)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 0)
        self.assertFalse(MileageHistory.objects.filter(team=self.team).exists())

    def test_locked_submission_is_rejected_without_scoring(self):
        # 락 상태에서는 정답을 제출해도 채점하지 않고 429를 반환한다
        FlagSubmissionLock.objects.create(
            team=self.team,
            challenge=self.challenge,
            failed_count=3,
            locked_until=timezone.now() + datetime.timedelta(seconds=30),
        )

        res = self.submit("MSG{correct_flag}")

        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.data["code"], "TOO_MANY_ATTEMPTS")

    def test_repeated_correct_flag_does_not_create_duplicate_solve(self):
        # 같은 팀이 정답을 다시 제출해도 풀이 기록은 한 번만 생성된다
        first = self.submit("MSG{correct_flag}")
        second = self.submit("MSG{correct_flag}")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["code"], "SUCCESS")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.data["code"], "ALREADY_SOLVED")
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 30)
        self.assertEqual(
            list(
                MileageHistory.objects.filter(team=self.team).values_list(
                    "amount", flat=True
                )
            ),
            [30],
        )
        self.assertEqual(
            Solve.objects.filter(team=self.team, challenge=self.challenge).count(),
            1,
        )

    def test_duplicate_line_bonus_does_not_rollback_the_last_solve(self):
        Cell.objects.filter(pk=self.cell.pk).update(line_number=1)
        for index in range(2, 6):
            cell = Cell.objects.create(
                cell_index=index,
                type=Cell.CellType.CHALLENGE,
                difficulty=Cell.Difficulty.EASY,
                line_number=1,
                name=f"line-cell-{index}",
            )
            challenge = Challenge.objects.create(
                title=f"line-challenge-{index}",
                category=Challenge.CategoryType.WEB,
                difficulty=Challenge.DifficultyType.EASY,
                score=1000,
                current_score=1000,
                flag_hash=f"line-flag-{index}",
                is_published=True,
            )
            TeamChallengeAccess.objects.create(
                team=self.team,
                challenge=challenge,
                source_cell=cell,
                status=TeamChallengeAccess.Status.CLEARED,
            )
            Solve.objects.create(
                team=self.team,
                challenge=challenge,
                earned_score=1000,
                earned_mileage=30,
            )

        LineMonopoly.objects.create(
            team=self.team,
            line_number=1,
            earned_score=225,
            is_category_bonus=True,
        )

        response = self.submit("MSG{correct_flag}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Solve.objects.filter(team=self.team).count(), 5)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 30)
        self.assertEqual(
            MileageHistory.objects.filter(
                team=self.team,
                type=MileageType.CHALLENGE_SOLVE,
            ).count(),
            1,
        )

    def test_submit_updates_dynamic_score_and_matches_ranking_contract(self):
        response = self.submit("MSG{correct_flag}")

        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(
            set(data),
            {
                "challenge_id",
                "earned_score",
                "earned_mileage",
                "is_extra_dice_granted",
                "team_score",
                "mileage",
                "solved_at",
            },
        )
        self.assertEqual(data["earned_score"], 1000)
        self.assertEqual(data["earned_mileage"], 30)
        self.assertEqual(data["team_score"], 998)
        self.assertEqual(data["mileage"], 30)

        self.challenge.refresh_from_db()
        self.team.refresh_from_db()
        solve = Solve.objects.get(team=self.team, challenge=self.challenge)
        self.assertEqual(solve.earned_score, Decimal("1000"))
        self.assertEqual(self.challenge.current_score, Decimal("998"))
        self.assertEqual(self.team.team_score, Decimal("998"))

        ranking = self.client.get("/api/v1/ranking").data["data"]["rankings"][0]
        leaderboard = self.client.get("/api/v1/leaderboard").data["data"]["teams"][0]
        self.assertEqual(ranking["team_score"], 998)
        self.assertEqual(leaderboard["team_score"], 998)
        self.assertEqual(leaderboard["solves"][0]["points"], 998)
        detail = self.client.get(f"/api/v1/challenges/{self.challenge.challenge_id}")
        self.assertEqual(detail.data["data"]["score"], 998)

    def test_later_solve_updates_scores_of_all_teams_that_solved_challenge(self):
        self.submit("MSG{correct_flag}")

        other_team = Team.objects.create(team_name="second-team")
        User.objects.create_user(
            login_id="second-user",
            password="pw1234",
            nickname="second-user",
            team=other_team,
        )
        second_cell = Cell.objects.create(
            cell_index=2,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="challenge-cell-2",
        )
        TeamChallengeAccess.objects.create(
            team=other_team,
            challenge=self.challenge,
            source_cell=second_cell,
        )
        second_client = APIClient()
        login = second_client.post(
            "/api/v1/auth/login",
            {"login_id": "second-user", "password": "pw1234"},
            format="json",
        )
        second_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['data']['access_token']}"
        )

        response = second_client.post(
            f"/api/v1/challenges/{self.challenge.challenge_id}/submit",
            {"flag": "MSG{correct_flag}"},
            format="json",
        )

        self.challenge.refresh_from_db()
        self.team.refresh_from_db()
        other_team.refresh_from_db()
        self.assertEqual(response.data["data"]["earned_score"], 998)
        self.assertEqual(self.challenge.current_score, Decimal("991"))
        self.assertEqual(self.team.team_score, Decimal("991"))
        self.assertEqual(other_team.team_score, Decimal("991"))

    def test_submission_failure_rolls_back_solve_score_and_mileage(self):
        with patch(
            "apps.challenge.views.update_dynamic_score_and_team_scores",
            side_effect=RuntimeError("simulated scoring failure"),
        ):
            with self.assertLogs("apps.common.exceptions", level="ERROR"):
                response = self.submit("MSG{correct_flag}")

        self.assertEqual(response.status_code, 500)
        self.challenge.refresh_from_db()
        self.team.refresh_from_db()
        self.assertFalse(
            Solve.objects.filter(team=self.team, challenge=self.challenge).exists()
        )
        self.assertFalse(MileageHistory.objects.filter(team=self.team).exists())
        self.assertEqual(self.challenge.current_score, Decimal("1000"))
        self.assertEqual(self.team.team_score, Decimal("0"))
        self.assertEqual(self.team.mileage, 0)

    def test_successful_solve_awards_mileage_by_difficulty(self):
        extra_challenges = []
        for difficulty, reward in (
            (Challenge.DifficultyType.MEDIUM, 60),
            (Challenge.DifficultyType.HARD, 120),
        ):
            challenge = Challenge.objects.create(
                title=f"{difficulty} 문제",
                category=Challenge.CategoryType.WEB,
                difficulty=difficulty,
                score=500,
                description="난이도별 보상 테스트 문제",
                flag_hash=hash_flag(f"MSG{{{difficulty.lower()}_flag}}"),
                is_published=True,
            )
            cell = Cell.objects.create(
                cell_index=len(extra_challenges) + 2,
                type=Cell.CellType.CHALLENGE,
                difficulty=difficulty,
                name=f"challenge-cell-{len(extra_challenges) + 2}",
            )
            TeamChallengeAccess.objects.create(
                team=self.team,
                challenge=challenge,
                source_cell=cell,
            )
            extra_challenges.append(
                (challenge, reward, f"MSG{{{difficulty.lower()}_flag}}")
            )

        cases = [(self.challenge, 30, "MSG{correct_flag}"), *extra_challenges]
        total_mileage = 0
        expected_solves = {}
        for challenge, reward, flag in cases:
            with self.subTest(difficulty=challenge.difficulty):
                response = self.client.post(
                    f"/api/v1/challenges/{challenge.challenge_id}/submit",
                    {"flag": flag},
                    format="json",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["data"]["earned_mileage"], reward)
                total_mileage += reward
                expected_solves[str(challenge.challenge_id)] = reward
                self.assertEqual(response.data["data"]["mileage"], total_mileage)
                solve = Solve.objects.get(team=self.team, challenge=challenge)
                self.assertEqual(solve.earned_mileage, reward)

                profile = self.client.get("/api/v1/teams/me")
                self.assertEqual(profile.status_code, 200)
                self.assertEqual(profile.data["data"]["mileage"], total_mileage)
                solves = self.client.get("/api/v1/teams/me/solves")
                self.assertEqual(solves.status_code, 200)
                self.assertEqual(
                    {
                        row["challenge_id"]: row["earned_mileage"]
                        for row in solves.data["data"]["solves"]
                    },
                    expected_solves,
                )
                history = self.client.get("/api/v1/teams/me/mileage_history")
                self.assertEqual(history.status_code, 200)
                self.assertEqual(history.data["data"]["mileage"], total_mileage)
                rows = history.data["data"]["history"]
                self.assertEqual(len(rows), len(expected_solves))
                self.assertTrue(
                    all(row["type"] == MileageType.CHALLENGE_SOLVE for row in rows)
                )
                self.assertCountEqual(
                    [row["amount"] for row in rows], list(expected_solves.values())
                )
                self.assertEqual(sum(row["amount"] for row in rows), total_mileage)

        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 210)
        self.assertEqual(
            list(
                MileageHistory.objects.filter(
                    team=self.team, type=MileageType.CHALLENGE_SOLVE
                )
                .order_by("created_at")
                .values_list("amount", flat=True)
            ),
            [30, 60, 120],
        )


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking")
@override_settings(CACHES=LOCMEM, SECURE_SSL_REDIRECT=False)
class ConcurrentChallengeSubmitTests(TransactionTestCase):
    def test_due_recharge_and_solve_reward_are_applied_once_when_requests_overlap(self):
        team = Team.objects.create(team_name="recharge-and-solve")
        user = User.objects.create_user(
            login_id="recharge-user", nickname="recharge-user", team=team
        )
        cell = Cell.objects.create(
            cell_index=1, type=Cell.CellType.CHALLENGE, name="current"
        )
        challenge = Challenge.objects.create(
            title="recharge",
            category="WEB",
            difficulty="EASY",
            score=1000,
            flag_hash=hash_flag("MSG{recharge}"),
            is_published=True,
        )
        access = TeamChallengeAccess.objects.create(
            team=team, challenge=challenge, source_cell=cell
        )
        now = timezone.now()
        deadline = now - datetime.timedelta(seconds=1)
        state = TeamBoardState.objects.create(
            team=team,
            position=cell,
            active_challenge_access=access,
            dice_rolls_left=0,
            next_dice_reset_at=deadline,
        )
        status_locked = Event()
        submit_waiting = Event()

        def request(action):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                client = APIClient()
                client.force_authenticate(user=User.objects.get(pk=user.pk))
                synchronized = False

                def synchronize(execute, sql, params, many, context):
                    nonlocal synchronized
                    if (
                        synchronized
                        or 'FROM "team_board_states"' not in sql
                        or "FOR UPDATE" not in sql
                    ):
                        return execute(sql, params, many, context)
                    synchronized = True
                    if action == "status":
                        result = execute(sql, params, many, context)
                        status_locked.set()
                        if not submit_waiting.wait(timeout=10):
                            raise AssertionError(
                                "Submission never attempted the board lock"
                            )
                        return result
                    submit_waiting.set()
                    return execute(sql, params, many, context)

                if action == "submit" and not status_locked.wait(timeout=10):
                    raise AssertionError("Status read did not lock the recharge state")
                with connection.execute_wrapper(synchronize):
                    if action == "status":
                        response = client.get("/api/v1/board/dice/status")
                    else:
                        response = client.post(
                            f"/api/v1/challenges/{challenge.pk}/submit",
                            {"flag": "MSG{recharge}"},
                            format="json",
                        )
                return action, response.status_code, response.data
            finally:
                connections.close_all()

        with patch("apps.board.services.timezone.now", return_value=now):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(request, ("status", "submit")))
            for action, status, body in results:
                self.assertEqual(status, 200, body)
                if action == "submit":
                    self.assertTrue(body["data"]["is_extra_dice_granted"])
            # Repeating a status read at the same time must not charge again.
            get_or_create_board_state(team)
        state.refresh_from_db()
        self.assertEqual(state.dice_rolls_left, 2)
        self.assertEqual(
            state.next_dice_reset_at, deadline + datetime.timedelta(minutes=15)
        )
        self.assertTrue(
            Solve.objects.get(team=team, challenge=challenge).is_extra_dice_granted
        )

    def test_submit_and_roulette_finish_when_submit_locks_board_first(self):
        self._assert_submit_and_roulette_finish("submit")

    def test_submit_and_roulette_finish_when_roulette_locks_board_first(self):
        self._assert_submit_and_roulette_finish("roulette")

    def test_submit_and_cell_25_roulette_finish_when_submit_locks_board_first(self):
        self._assert_submit_and_roulette_finish("submit", cell_index=25)

    def test_submit_and_cell_25_roulette_finish_when_roulette_locks_board_first(self):
        self._assert_submit_and_roulette_finish("roulette", cell_index=25)

    def _assert_submit_and_roulette_finish(self, first_action, cell_index=16):
        team = Team.objects.create(team_name="submit-and-roulette")
        user = User.objects.create_user(
            login_id="roulette-leader",
            nickname="roulette-leader",
            team=team,
            is_leader=True,
        )
        challenge = Challenge.objects.create(
            title="previous-cell",
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=1000,
            initial_score=1000,
            minimum_score=100,
            decay=20,
            current_score=1000,
            flag_hash=hash_flag("MSG{concurrent}"),
            is_published=True,
        )
        challenge_cell = Cell.objects.create(
            cell_index=1, type=Cell.CellType.CHALLENGE, name="previous"
        )
        roulette_cell = Cell.objects.create(
            cell_index=cell_index, type=Cell.CellType.ROULETTE, name="roulette"
        )
        access = TeamChallengeAccess.objects.create(
            team=team, challenge=challenge, source_cell=challenge_cell
        )
        state = TeamBoardState.objects.create(
            team=team, position=roulette_cell, dice_rolls_left=1
        )
        first_locked = Event()
        second_waiting = Event()

        def request_action(action):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                client = APIClient()
                client.force_authenticate(user=User.objects.get(pk=user.pk))
                synchronized = False

                def synchronize(execute, sql, params, many, context):
                    nonlocal synchronized
                    is_board_lock = (
                        'FROM "team_board_states"' in sql and "FOR UPDATE" in sql
                    )
                    if not is_board_lock or synchronized:
                        return execute(sql, params, many, context)
                    synchronized = True
                    if action == first_action:
                        result = execute(sql, params, many, context)
                        first_locked.set()
                        if not second_waiting.wait(timeout=10):
                            raise AssertionError(
                                "Second request never attempted the board lock"
                            )
                        return result
                    second_waiting.set()
                    return execute(sql, params, many, context)

                if action != first_action and not first_locked.wait(timeout=10):
                    raise AssertionError("First request never acquired the board lock")
                with connection.execute_wrapper(synchronize):
                    if action == "submit":
                        response = client.post(
                            f"/api/v1/challenges/{challenge.pk}/submit",
                            {"flag": "MSG{concurrent}"},
                            format="json",
                        )
                    else:
                        response = client.post(
                            "/api/v1/board/roulette/spin",
                            {},
                            format="json",
                            HTTP_IDEMPOTENCY_KEY=f"concurrent-roulette-{first_action}",
                        )
                return action, response.status_code, response.data
            finally:
                connections.close_all()

        with patch("apps.board.services.random.choice", return_value=50):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(request_action, ["submit", "roulette"]))
        self.assertEqual(len(results), 2)
        bodies = {}
        for action, status, body in results:
            self.assertEqual(status, 200, body)
            bodies[action] = body["data"]
        self.assertFalse(bodies["submit"]["is_extra_dice_granted"])
        self.assertEqual(bodies["submit"]["team_score"], 998)
        self.assertEqual(bodies["roulette"]["mileage_gained"], 50)
        self.assertEqual(
            bodies["submit"]["mileage"], 30 if first_action == "submit" else 80
        )
        self.assertEqual(
            bodies["roulette"]["total_mileage"], 80 if first_action == "submit" else 50
        )
        team.refresh_from_db()
        state.refresh_from_db()
        access.refresh_from_db()
        self.assertEqual(team.mileage, 80)
        self.assertEqual(team.team_score, Decimal("998"))
        self.assertEqual(state.position_id, roulette_cell.pk)
        self.assertEqual(state.dice_rolls_left, 1)
        self.assertEqual(access.status, TeamChallengeAccess.Status.CLEARED)
        self.assertIsNotNone(access.cleared_at)
        solve = Solve.objects.get(team=team, challenge=challenge)
        self.assertFalse(solve.is_extra_dice_granted)
        self.assertEqual(solve.earned_mileage, 30)
        self.assertCountEqual(
            MileageHistory.objects.filter(team=team).values_list("type", "amount"),
            [(MileageType.CHALLENGE_SOLVE, 30), (MileageType.ROULETTE, 50)],
        )

    def test_cross_team_solves_on_different_challenges_finish_with_consistent_scores(
        self,
    ):
        teams = [Team.objects.create(team_name=f"concurrent-{i}") for i in range(3)]
        users = [
            User.objects.create_user(
                login_id=f"concurrent-{i}", nickname=f"user-{i}", team=team
            )
            for i, team in enumerate(teams)
        ]
        challenges = [
            Challenge.objects.create(
                title=f"concurrent-{i}",
                category=Challenge.CategoryType.WEB,
                difficulty=Challenge.DifficultyType.EASY,
                score=1000,
                initial_score=1000,
                minimum_score=100,
                decay=20,
                current_score=991,
                flag_hash=hash_flag("MSG{concurrent}"),
                is_published=True,
            )
            for i in range(3)
        ]
        # Every new solve affects all teams, with a different submitting team.
        for i, challenge in enumerate(challenges):
            cell = Cell.objects.create(
                cell_index=i + 1, type=Cell.CellType.CHALLENGE, name=str(i)
            )
            access = TeamChallengeAccess.objects.create(
                team=teams[i], challenge=challenge, source_cell=cell
            )
            TeamBoardState.objects.create(
                team=teams[i],
                position=cell,
                active_challenge_access=access,
                dice_rolls_left=0,
            )
            for j, team in enumerate(teams):
                if i != j:
                    Solve.objects.create(
                        team=team,
                        challenge=challenge,
                        solved_by_user=users[j],
                        earned_score=1000,
                        earned_mileage=30,
                    )
        barrier = Barrier(3)

        def submit(i):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                client = APIClient()
                client.force_authenticate(user=User.objects.get(pk=users[i].pk))

                # Rendezvous after each distinct challenge lock is acquired, before
                # any team lock: all three scoring transactions truly overlap.
                def synchronize(execute, sql, params, many, context):
                    result = execute(sql, params, many, context)
                    if 'FROM "challenges"' in sql and "FOR UPDATE" in sql:
                        barrier.wait(timeout=10)
                    return result

                with connection.execute_wrapper(synchronize):
                    response = client.post(
                        f"/api/v1/challenges/{challenges[i].pk}/submit",
                        {"flag": "MSG{concurrent}"},
                        format="json",
                    )
                return response.status_code, response.data
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(submit, range(3)))
        for status, body in results:
            self.assertEqual(status, 200, body)
            self.assertEqual(body["data"]["mileage"], 30)
            self.assertTrue(body["data"]["is_extra_dice_granted"])
        self.assertEqual(Solve.objects.count(), 9)
        self.assertEqual(MileageHistory.objects.count(), 3)
        for challenge in challenges:
            challenge.refresh_from_db()
            self.assertEqual(challenge.current_score, Decimal("980"))
        for team in teams:
            team.refresh_from_db()
            self.assertEqual(team.team_score, Decimal("2940"))
            self.assertEqual(team.mileage, 30)
            self.assertEqual(team.board_state.dice_rolls_left, 1)
