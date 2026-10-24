import threading
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import Team
from apps.leaderboard import views

MID_READ_TEAM = "읽는 도중 생긴 팀"


class LeaderboardSnapshotTest(TransactionTestCase):
    def test_read_starts_with_repeatable_read_read_only(self):
        with CaptureQueriesContext(connection) as ctx:
            views.read_leaderboard_snapshot()
        # BEGIN 다음, 데이터를 읽는 첫 쿼리보다 먼저 격리 수준을 정해야 한다
        statements = [q["sql"] for q in ctx.captured_queries if q["sql"] != "BEGIN"]
        self.assertEqual(
            statements[0],
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        )

    def test_rows_committed_during_read_are_not_mixed_in(self):
        # 점수를 읽은 직후 다른 연결이 팀을 만들고 커밋해도 같은 읽기의 팀 목록에는 보이지 않아야 한다
        original = views.collect_solves_map

        def create_team_elsewhere():
            try:
                Team.objects.create(team_name=MID_READ_TEAM)
            finally:
                connection.close()

        def collect_then_commit_elsewhere():
            result = original()
            worker = threading.Thread(target=create_team_elsewhere)
            worker.start()
            worker.join()
            return result

        with patch.object(views, "collect_solves_map", side_effect=collect_then_commit_elsewhere):
            _, teams = views.read_leaderboard_snapshot()

        self.assertTrue(Team.objects.filter(team_name=MID_READ_TEAM).exists())
        self.assertNotIn(MID_READ_TEAM, [team.team_name for team in teams])
