from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from apps.accounts.models import Team
from apps.challenge.models import Challenge, Solve
from apps.koth.models import KothChallenge, KothClub, KothSolve
from apps.ranking.models import LineMonopoly
from apps.leaderboard import views


class LeaderboardAPITest(TestCase):
    def setUp(self):
        self.challenge = Challenge.objects.create(
            title="테스트문제",
            category="WEB",
            difficulty="EASY",
            score=Decimal("1000"),
            flag_hash="dummy",
            is_published=True,
        )

    def make_koth_solve(self, team, name, score, at):
        club = KothClub.objects.create(name=f"동아리_{name}")
        kc = KothChallenge.objects.create(
            club=club,
            title=name,
            open_group=1,
            inbound_internal_token_hash=f"hash_{name}",
        )
        return KothSolve.objects.create(
            team=team,
            challenge=kc,
            earned_score=Decimal(score),
            solved_at=at,
        )

    def make_team_with_solve(self, name, score, solved_at=None):
        team = Team.objects.create(team_name=name, team_score=Decimal(score))
        challenge = Challenge.objects.create(
            title=f"문제_{name}",
            category="WEB",
            difficulty="EASY",
            score=Decimal("1000"),
            flag_hash=f"hash_{name}",
            is_published=True,
        )
        solve = Solve.objects.create(
            team=team,
            challenge=challenge,
            earned_score=Decimal("1000"),
            earned_mileage=100,
        )
        if solved_at is not None:
            Solve.objects.filter(pk=solve.pk).update(solved_at=solved_at)
        return team

    def make_team_with_score(self, name, stored, actual):
        # 저장된 team_score와 실제 문제 점수를 따로 지정한다
        team = Team.objects.create(team_name=name, team_score=Decimal(stored))
        challenge = Challenge.objects.create(
            title=f"문제_{name}",
            category="WEB",
            difficulty="EASY",
            score=Decimal(actual),
            current_score=Decimal(actual),
            flag_hash=f"hash_{name}",
            is_published=True,
        )
        Solve.objects.create(
            team=team,
            challenge=challenge,
            earned_score=Decimal(actual),
            earned_mileage=100,
        )
        return team

    def test_team_without_solve_is_excluded(self):
        self.make_team_with_solve("푼팀", 100)
        Team.objects.create(team_name="안푼팀", team_score=Decimal("9999"))

        data = self.client.get("/api/v1/leaderboard").data["data"]

        names = [t["team_name"] for t in data["teams"]]
        self.assertIn("푼팀", names)
        self.assertNotIn("안푼팀", names)

    def test_banned_team_is_excluded(self):
        self.make_team_with_solve("정상팀", 100)
        banned = self.make_team_with_solve("밴팀", 9999)
        Team.objects.filter(pk=banned.pk).update(is_banned=True)

        data = self.client.get("/api/v1/leaderboard").data["data"]

        names = [t["team_name"] for t in data["teams"]]
        self.assertNotIn("밴팀", names)

    def test_is_top3_marks_only_first_three(self):
        for i in range(5):
            self.make_team_with_solve(f"팀{i}", 500 - i * 10)

        data = self.client.get("/api/v1/leaderboard").data["data"]

        flags = [t["is_top3"] for t in data["teams"]]
        self.assertEqual(flags, [True, True, True, False, False])

    def test_returns_at_most_eight_teams(self):
        for i in range(12):
            self.make_team_with_solve(f"팀{i}", 500 - i)

        data = self.client.get("/api/v1/leaderboard").data["data"]

        self.assertEqual(len(data["teams"]), 8)
        self.assertEqual(data["total_count"], 8)

    def test_solves_are_sorted_by_time(self):
        now = timezone.now()
        team = self.make_team_with_solve("팀", 100, solved_at=now)
        later = Solve.objects.create(
            team=team,
            challenge=self.challenge,
            earned_score=Decimal("1000"),
            earned_mileage=100,
        )
        Solve.objects.filter(pk=later.pk).update(solved_at=now + timedelta(hours=1))

        data = self.client.get("/api/v1/leaderboard").data["data"]

        times = [s["solved_at"] for s in data["teams"][0]["solves"]]
        self.assertEqual(times, sorted(times))

    def test_empty_when_no_team(self):
        data = self.client.get("/api/v1/leaderboard").data["data"]

        self.assertEqual(data["teams"], [])
        self.assertEqual(data["total_count"], 0)

    def test_koth_only_team_is_included(self):
        team = Team.objects.create(team_name="koth팀")
        self.make_koth_solve(team, "koth1", "40", timezone.now())

        data = self.client.get("/api/v1/leaderboard").data["data"]

        names = [t["team_name"] for t in data["teams"]]
        self.assertIn("koth팀", names)

    def test_header_score_matches_graph_sum(self):
        self.make_team_with_solve("팀", 0)

        data = self.client.get("/api/v1/leaderboard").data["data"]
        team = data["teams"][0]

        graph_sum = sum(s["points"] for s in team["solves"])
        self.assertEqual(team["team_score"], graph_sum)

    def test_total_score_sums_jeopardy_and_koth(self):
        team = self.make_team_with_solve("팀", 0)
        self.make_koth_solve(team, "koth1", "40", timezone.now())

        data = self.client.get("/api/v1/leaderboard").data["data"]

        self.assertEqual(data["teams"][0]["team_score"], 1040)

    def test_graph_includes_koth_solve(self):
        team = self.make_team_with_solve("팀", 0)
        self.make_koth_solve(team, "koth1", "40", timezone.now())

        data = self.client.get("/api/v1/leaderboard").data["data"]

        types = {s["source_type"] for s in data["teams"][0]["solves"]}
        self.assertEqual(types, {"JEOPARDY", "KOTH"})

    def test_line_bonus_increases_score_without_increasing_solved_count(self):
        team = self.make_team_with_solve("팀", 0)
        LineMonopoly.objects.create(
            team=team,
            line_number=1,
            earned_score=Decimal("225"),
            is_category_bonus=True,
        )

        row = self.client.get("/api/v1/leaderboard").data["data"]["teams"][0]

        self.assertEqual(row["team_score"], 1225)
        self.assertEqual(row["solved_count"], 1)
        line_event = next(
            event for event in row["solves"] if event["source_type"] == "LINE"
        )
        self.assertFalse(line_event["counts_as_solve"])

    def test_stored_team_score_is_ignored(self):
        self.make_team_with_score("저장9999", stored="9999", actual="100")
        self.make_team_with_score("저장0", stored="0", actual="200")

        data = self.client.get("/api/v1/leaderboard").data["data"]

        self.assertEqual(data["teams"][0]["team_name"], "저장0")
        self.assertEqual(data["teams"][0]["team_score"], 200)
        self.assertEqual(data["teams"][1]["team_name"], "저장9999")
        self.assertEqual(data["teams"][1]["team_score"], 100)

    def test_header_and_graph_stay_consistent_under_update(self):
        team = self.make_team_with_solve("팀", 0)
        koth = self.make_koth_solve(team, "koth1", "40", timezone.now())

        original = views.collect_solves_map

        def bump_koth_after_read():
            data = original()
            KothSolve.objects.filter(pk=koth.pk).update(earned_score=Decimal("80"))
            return data

        with patch.object(views, "collect_solves_map", bump_koth_after_read):
            data = self.client.get("/api/v1/leaderboard").data["data"]

        row = data["teams"][0]
        graph_sum = sum(s["points"] for s in row["solves"])
        self.assertEqual(row["team_score"], graph_sum)
