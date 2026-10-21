import uuid
from datetime import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.challenge.models import Challenge, FlagSubmission, Solve


class ChallengeSolveHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create(login_id="history-admin", nickname="관리자", role=Role.ADMIN)
        cls.challenge = Challenge.objects.create(title="History", category="WEB", difficulty="EASY", score=500, flag_hash="not-in-response")
        cls.other = Challenge.objects.create(title="Other", category="PWN", difficulty="EASY", score=500, flag_hash="other-hash")
        cls.records = []
        for index, at in enumerate(("2026-10-13T14:59:59+00:00", "2026-10-13T15:00:00+00:00", "2026-10-13T15:30:00+00:00", "2026-10-13T16:00:00+00:00")):
            team = Team.objects.create(team_name=f"풀이팀 {index}")
            user = User.objects.create(login_id=f"solver-{index}", nickname=f"풀이자 {index}", team=team)
            solve = Solve.objects.create(team=team, solved_by_user=user, challenge=cls.challenge, earned_score=500, earned_mileage=10)
            Solve.objects.filter(pk=solve.pk).update(solved_at=datetime.fromisoformat(at))
            cls.records.append(solve)
        cls.participant = cls.records[0].solved_by_user
        Solve.objects.create(team=cls.records[0].team, challenge=cls.other, earned_score=400, earned_mileage=10)
        # Repeated submissions are not additional solves.
        FlagSubmission.objects.create(team=cls.records[0].team, user=cls.participant, challenge=cls.challenge,
                                      submitted_flag_hash="submission-hash", result="ALREADY_SOLVED")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = f"/api/v1/admin/challenges/{self.challenge.pk}/solves"

    def data(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()["data"]

    def test_admin_only_and_private_cache(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.client.force_authenticate(self.participant)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_only_successful_team_solves_and_minimal_solver_details(self):
        data = self.data()
        self.assertEqual(data["total_count"], 4)
        self.assertEqual(data["summary"]["total_solved_teams"], 4)
        self.assertEqual(data["solves"][0]["solve_id"], str(self.records[-1].pk))
        self.assertEqual(data["solves"][0]["solver"], {"user_id": str(self.records[-1].solved_by_user_id), "nickname": "풀이자 3"})
        self.assertNotIn("hash", str(data))
        self.assertNotIn("login_id", str(data))

    def test_kst_hour_buckets_cross_midnight(self):
        data = self.data()
        self.assertEqual([row["solved_team_count"] for row in data["hourly"]], [1, 2, 1])
        middle = data["hourly"][1]
        self.assertEqual(datetime.fromisoformat(middle["start_at"]), datetime.fromisoformat("2026-10-14T00:00:00+09:00"))
        selected = self.data(hour=middle["start_at"])
        self.assertEqual(selected["total_count"], 2)
        self.assertEqual(selected["summary"]["period_solved_teams"], 4)
        self.assertEqual(selected["hourly"], data["hourly"])

    def test_start_inclusive_end_exclusive_and_partial_hour_intersection(self):
        data = self.data(start_at="2026-10-14T00:00:00+09:00", end_at="2026-10-14T01:00:00+09:00")
        self.assertEqual(data["total_count"], 2)
        self.assertEqual(data["summary"]["total_solved_teams"], 4)
        self.assertEqual(data["summary"]["period_solved_teams"], 2)
        partial = self.data(start_at="2026-10-14T00:15:00+09:00", end_at="2026-10-14T01:00:00+09:00", hour="2026-10-14T00:00:00+09:00")
        self.assertEqual(partial["total_count"], 1)
        self.assertEqual(partial["solves"][0]["solve_id"], str(self.records[2].pk))

    def test_sort_and_pagination_keep_summary_and_buckets(self):
        first = self.data(sort="earliest", page=1, size=2)
        second = self.data(sort="earliest", page=2, size=2)
        self.assertEqual([row["solve_id"] for row in first["solves"] + second["solves"]], [str(row.pk) for row in self.records])
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(first["hourly"], second["hourly"])
        self.assertEqual(self.data(page=10)["solves"], [])

    def test_equal_timestamps_have_stable_pagination(self):
        Solve.objects.filter(challenge=self.challenge).update(solved_at=datetime.fromisoformat("2026-10-14T00:00:00+09:00"))
        ids = [self.data(sort="earliest", page=page, size=1)["solves"][0]["solve_id"] for page in range(1, 5)]
        self.assertEqual(ids, sorted(str(row.pk) for row in self.records))

    def test_deleted_solver_is_still_a_team_solve(self):
        self.records[0].solved_by_user.delete()
        data = self.data(sort="earliest")
        self.assertIsNone(data["solves"][0]["solver"])
        self.assertEqual(data["total_count"], 4)

    def test_empty_period_and_unknown_challenge(self):
        data = self.data(start_at="2026-11-01T00:00:00Z")
        self.assertEqual(data["solves"], [])
        self.assertEqual(data["hourly"], [])
        self.assertEqual(data["summary"]["period_solved_teams"], 0)
        empty = Challenge.objects.create(title="Empty", category="WEB", difficulty="EASY", score=500, flag_hash="unused")
        self.url = f"/api/v1/admin/challenges/{empty.pk}/solves"
        self.assertIsNone(self.data()["summary"]["first_solved_at"])
        self.assertEqual(self.client.get(f"/api/v1/admin/challenges/{uuid.uuid4()}/solves").status_code, 404)

    def test_invalid_query_does_not_silently_change_period(self):
        for params in ({"start_at": "2026-10-14T00:00:00"}, {"start_at": "2026-02-30T00:00:00Z"},
                       {"hour": "2026-10-14T00:30:00+09:00"}, {"sort": "score"}, {"page": 0}, {"size": 101},
                       {"start_at": "2026-10-14T01:00:00Z", "end_at": "2026-10-14T01:00:00Z"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.url, params).status_code, 400)
