import threading
import time

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.adminpanel.models import AdminEvent
from apps.board.models import Cell, TeamBoardState, TeamChallengeAccess
from apps.challenge.models import Challenge, FlagSubmission, Solve
from apps.challenge.services import hash_flag
from apps.common.jwt import issue_access_token
from apps.ranking.models import LineMonopoly
from apps.teams.models import MileageHistory, MileageType

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class SubmissionDeleteSetup:
    """회수팀이 1번 라인 문제 2개를, 다른팀이 그중 하나를 실제 제출 API 로 푼 상태."""

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자", role=Role.ADMIN
        )
        self.cheater = Team.objects.create(team_name="회수팀")
        self.other = Team.objects.create(team_name="다른팀")
        self.cheater_user = User.objects.create_user(
            login_id="cheater", password="pw1234", nickname="회수", team=self.cheater
        )
        self.other_user = User.objects.create_user(
            login_id="other", password="pw1234", nickname="다른", team=self.other
        )
        line_cells = [
            Cell.objects.create(
                cell_index=index,
                type=Cell.CellType.CHALLENGE,
                line_number=1,
                name=f"문제 {index}",
            )
            for index in (1, 2)
        ]
        self.home = Cell.objects.create(
            cell_index=10, type=Cell.CellType.CHALLENGE, name="말 위치"
        )
        self.target, self.second = [
            Challenge.objects.create(
                title=title,
                category=Challenge.CategoryType.WEB,
                difficulty=Challenge.DifficultyType.EASY,
                score=500,
                current_score=500,
                initial_score=500,
                minimum_score=100,
                decay=10,
                flag_hash=hash_flag("MSG{ok}"),
                is_published=True,
            )
            for title in ("회수할 문제", "같은 라인 문제")
        ]
        for challenge, cell in zip((self.target, self.second), line_cells):
            TeamChallengeAccess.objects.create(
                team=self.cheater, challenge=challenge, source_cell=cell
            )
        TeamChallengeAccess.objects.create(
            team=self.other, challenge=self.target, source_cell=line_cells[0]
        )
        self.state = TeamBoardState.objects.create(
            team=self.cheater, position=self.home, dice_rolls_left=2
        )

        for user, challenge in (
            (self.cheater_user, self.target),
            (self.cheater_user, self.second),
            (self.other_user, self.target),
        ):
            res = self.as_user(user).post(
                f"/api/v1/challenges/{challenge.pk}/submit",
                {"flag": "MSG{ok}"},
                format="json",
            )
            self.assertEqual(res.status_code, 200, res.data)

    def as_user(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(user)}")
        return client

    def delete(self, challenge, submission, body=None, user=None):
        return self.as_user(user or self.admin).delete(
            f"/api/v1/admin/challenges/{challenge.pk}/submissions/{submission.pk}",
            {"reason": "부정 풀이 확인"} if body is None else body,
            format="json",
        )

    def correct_submission(self, team, challenge):
        return FlagSubmission.objects.get(
            team=team, challenge=challenge, result=FlagSubmission.SubmissionResult.CORRECT
        )

    def ledger_sum(self, team):
        return MileageHistory.objects.filter(team=team).aggregate(total=Sum("amount"))[
            "total"
        ]

    def set_cell_status(self, cell_index, status):
        return self.as_user(self.admin).patch(
            f"/api/v1/admin/teams/{self.cheater.pk}/board/cells/{cell_index}",
            {"status": status, "reason": "칸 정정"},
            format="json",
        )


@override_settings(CACHES=LOCMEM)
class SubmissionDeleteTests(SubmissionDeleteSetup, TestCase):
    def test_cell_reset_before_revoke_leaves_no_line_bonus(self):
        """칸 상태 수정으로 개방 기록이 지워져도 라인 보너스가 남지 않는다 (리뷰 P1)."""
        res = self.set_cell_status(1, "UNVISITED")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(LineMonopoly.objects.filter(team=self.cheater).exists())
        cell_event = AdminEvent.objects.get(type=AdminEvent.EventType.CELL_STATUS_CHANGED)
        self.assertIn("1번 라인 독점", cell_event.message)

        res = self.delete(self.target, self.correct_submission(self.cheater, self.target))

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(res.data["data"]["revoked"]["line_monopoly"])
        self.assertFalse(LineMonopoly.objects.filter(team=self.cheater).exists())

    def test_cell_reopened_by_admin_drops_line_bonus(self):
        res = self.set_cell_status(2, "OPENED")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(LineMonopoly.objects.filter(team=self.cheater).exists())

    def test_cell_cleared_again_keeps_line_bonus(self):
        """완성 상태가 그대로면 독점을 건드리지 않는다."""
        res = self.set_cell_status(2, "CLEARED")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(LineMonopoly.objects.filter(team=self.cheater, line_number=1).exists())

    def test_correct_submission_revokes_score_mileage_and_line_monopoly(self):
        self.assertTrue(LineMonopoly.objects.filter(team=self.cheater, line_number=1).exists())
        self.cheater.refresh_from_db()
        mileage_before = self.cheater.mileage
        self.other.refresh_from_db()
        other_score_before = self.other.team_score
        self.state.refresh_from_db()
        dice_before, position_before = self.state.dice_rolls_left, self.state.position_id

        res = self.delete(self.target, self.correct_submission(self.cheater, self.target))

        self.assertEqual(res.status_code, 200, res.data)
        revoked = res.data["data"]["revoked"]
        self.assertEqual(revoked["mileage"], 30)
        self.assertEqual(revoked["line_monopoly"]["line_number"], 1)
        self.assertFalse(Solve.objects.filter(team=self.cheater, challenge=self.target).exists())
        self.assertFalse(LineMonopoly.objects.filter(team=self.cheater, line_number=1).exists())

        self.cheater.refresh_from_db()
        self.assertEqual(self.cheater.mileage, mileage_before - 30)
        self.assertEqual(self.ledger_sum(self.cheater), self.cheater.mileage)
        self.assertTrue(
            MileageHistory.objects.filter(
                team=self.cheater, type=MileageType.ADMIN_DEDUCT, amount=-30
            ).exists()
        )

        # 푼 팀이 하나 줄어 동적 점수가 오르고, 남은 팀 점수도 따라 오른다.
        self.target.refresh_from_db()
        self.other.refresh_from_db()
        self.assertGreater(self.other.team_score, other_score_before)
        self.assertEqual(self.other.team_score, self.target.current_score)
        self.second.refresh_from_db()
        self.assertEqual(self.cheater.team_score, self.second.current_score)

        access = TeamChallengeAccess.objects.get(team=self.cheater, challenge=self.target)
        self.assertEqual(access.status, TeamChallengeAccess.Status.OPENED)
        self.state.refresh_from_db()
        self.assertEqual(
            (self.state.dice_rolls_left, self.state.position_id),
            (dice_before, position_before),
        )

        [event] = AdminEvent.objects.filter(type=AdminEvent.EventType.SUBMISSION_DELETED)
        self.assertEqual(event.severity, AdminEvent.Severity.WARNING)
        self.assertEqual(event.team_id, self.cheater.pk)
        self.assertIn("정답 회수", event.message)
        self.assertIn("1번 라인 독점", event.message)

    def test_revoked_challenge_can_be_solved_again(self):
        self.delete(self.target, self.correct_submission(self.cheater, self.target))
        self.state.refresh_from_db()
        dice_before = self.state.dice_rolls_left

        res = self.as_user(self.cheater_user).post(
            f"/api/v1/challenges/{self.target.pk}/submit", {"flag": "MSG{ok}"}, format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data["data"]["is_extra_dice_granted"])
        self.assertTrue(LineMonopoly.objects.filter(team=self.cheater, line_number=1).exists())
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, dice_before)

    def test_incorrect_submission_only_removes_the_row(self):
        # 이미 푼 문제의 오답은 409 로 끝나 기록되지 않으므로 풀기 전 오답을 직접 남긴다.
        wrong = FlagSubmission.objects.create(
            team=self.cheater,
            user=self.cheater_user,
            challenge=self.target,
            submitted_flag_hash=hash_flag("MSG{no}"),
            result=FlagSubmission.SubmissionResult.INCORRECT,
        )
        self.cheater.refresh_from_db()
        mileage_before, score_before = self.cheater.mileage, self.cheater.team_score

        res = self.delete(self.target, wrong)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(res.data["data"]["revoked"])
        self.assertFalse(FlagSubmission.objects.filter(pk=wrong.pk).exists())
        self.cheater.refresh_from_db()
        self.assertEqual((self.cheater.mileage, self.cheater.team_score), (mileage_before, score_before))
        self.assertTrue(Solve.objects.filter(team=self.cheater, challenge=self.target).exists())
        [event] = AdminEvent.objects.filter(type=AdminEvent.EventType.SUBMISSION_DELETED)
        self.assertEqual(event.severity, AdminEvent.Severity.INFO)

    def test_spent_mileage_goes_negative(self):
        Team.objects.filter(pk=self.cheater.pk).update(mileage=0)
        MileageHistory.objects.create(
            team=self.cheater, type=MileageType.PURCHASE, amount=-self.ledger_sum(self.cheater)
        )

        res = self.delete(self.target, self.correct_submission(self.cheater, self.target))

        self.assertEqual(res.status_code, 200, res.data)
        self.cheater.refresh_from_db()
        self.assertEqual(self.cheater.mileage, -30)
        self.assertEqual(self.ledger_sum(self.cheater), -30)

    def test_rejects_wrong_target_missing_reason_and_participant(self):
        submission = self.correct_submission(self.cheater, self.target)

        self.assertEqual(self.delete(self.second, submission).data["code"], "SUBMISSION_NOT_FOUND")
        self.assertEqual(self.delete(self.target, submission, body={}).status_code, 400)
        self.assertEqual(
            self.delete(self.target, submission, user=self.cheater_user).status_code, 403
        )
        self.assertTrue(FlagSubmission.objects.filter(pk=submission.pk).exists())
        self.assertTrue(Solve.objects.filter(team=self.cheater, challenge=self.target).exists())
        self.assertFalse(AdminEvent.objects.filter(type=AdminEvent.EventType.SUBMISSION_DELETED).exists())


@override_settings(CACHES=LOCMEM)
class SubmissionDeleteRaceTests(SubmissionDeleteSetup, TransactionTestCase):
    def test_revoked_amount_is_read_after_locking_monopoly(self):
        """독점 점수 수정이 먼저 행을 잡고 바꾸면, 회수 응답과 기록은 바뀐 금액을 남긴다 (리뷰 P2)."""
        holding = threading.Event()

        def edit_monopoly():
            with transaction.atomic():
                monopoly = LineMonopoly.objects.select_for_update().get(
                    team=self.cheater, line_number=1
                )
                holding.set()
                time.sleep(0.5)
                monopoly.earned_score = 777
                monopoly.save(update_fields=["earned_score"])
            connection.close()

        worker = threading.Thread(target=edit_monopoly)
        worker.start()
        holding.wait(5)
        res = self.delete(self.target, self.correct_submission(self.cheater, self.target))
        worker.join()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["revoked"]["line_monopoly"]["earned_score"], 777)
        event = AdminEvent.objects.get(type=AdminEvent.EventType.SUBMISSION_DELETED)
        self.assertIn("독점 777점", event.message)
        self.assertFalse(LineMonopoly.objects.filter(team=self.cheater).exists())
