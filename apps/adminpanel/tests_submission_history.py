import uuid
from datetime import datetime, timezone

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.challenge.models import Challenge, FlagSubmission
from apps.koth.models import KothClub
from apps.signature.models import SignatureChallenge, SignatureFlagSubmission


@override_settings(SECURE_SSL_REDIRECT=False)
class SubmissionHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create(login_id="submission-admin", nickname="admin", role=Role.ADMIN)
        cls.team = Team.objects.create(team_name="history-team")
        cls.other_team = Team.objects.create(team_name="other-team")
        cls.user = User.objects.create(login_id="submission-user", nickname="player", team=cls.team)
        cls.leader = User.objects.create(login_id="submission-leader", nickname="leader", team=cls.team, is_leader=True)
        cls.general = Challenge.objects.create(title="history", score=500, category="WEB", difficulty="EASY", flag_hash="private-problem-hash")
        cls.signature = SignatureChallenge.objects.create(
            club=KothClub.objects.create(name="history-club"), title="history", flag_hash="private-problem-hash",
        )
        cls.cases = []
        for kind, model, problem in (("challenges", FlagSubmission, cls.general), ("signatures", SignatureFlagSubmission, cls.signature)):
            records = []
            for index, result in enumerate(("CORRECT", "INCORRECT", "TOO_MANY_ATTEMPTS", "ALREADY_SOLVED", "CORRECT")):
                row = model.objects.create(
                    challenge=problem, team=cls.team if index < 4 else cls.other_team, user=cls.user,
                    result=result, submitted_flag_hash="private-submitted-hash",
                )
                model.objects.filter(pk=row.pk).update(created_at=datetime(2026, 10, 24, index, tzinfo=timezone.utc))
                records.append(row)
            cls.cases.append((f"/api/v1/admin/{kind}/{problem.pk}/submissions", model, problem, records))

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def data(self, url, **params):
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()["data"]

    def test_both_endpoints_are_admin_only(self):
        for url, *_ in self.cases:
            for user, status in ((self.user, 403), (self.leader, 403), (None, 401), (self.admin, 200)):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.get(url).status_code, status)

    def test_all_results_summary_and_safe_response(self):
        for url, _, _, records in self.cases:
            response = self.client.get(url)
            self.assertIn("no-store", response["Cache-Control"])
            data = response.json()["data"]
            self.assertEqual(data["summary"], {"total": 5, "incorrect": 1, "locked": 1, "teams": 2, "correct": 2})
            self.assertEqual([row["submission_id"] for row in data["submissions"]], [str(row.pk) for row in reversed(records)])
            self.assertEqual(set(data["submissions"][0]), {"submission_id", "created_at", "team_id", "team_name", "user", "result"})
            self.assertEqual(data["submissions"][0]["user"], {"user_id": str(self.user.pk), "nickname": "player"})
            for forbidden in ("flag", "hash", "login_id", "password", "private-submitted-hash", "private-problem-hash"):
                self.assertNotIn(forbidden, response.content.decode())

    def test_combined_filters_and_global_summary(self):
        for url, *_ in self.cases:
            data = self.data(url, result="INCORRECT", team_id=str(self.team.pk),
                             start_at="2026-10-24T10:00:00+09:00", end_at="2026-10-24T11:00:00+09:00")
            self.assertEqual(data["total_count"], 1)
            self.assertEqual(data["submissions"][0]["result"], "INCORRECT")
            self.assertEqual(data["summary"]["total"], 5)
            self.assertEqual(self.data(url, team_id=str(uuid.uuid4()))["submissions"], [])
            self.assertEqual(self.data(url, result="")["total_count"], 5)

    def test_boundary_dates_and_deleted_user(self):
        self.user.delete()
        for url, _, _, records in self.cases:
            data = self.data(url, start_at="2026-10-24T01:00:00Z", end_at="2026-10-24T02:00:00Z")
            self.assertEqual(data["total_count"], 1)
            self.assertEqual(data["submissions"][0]["submission_id"], str(records[1].pk))
            self.assertIsNone(data["submissions"][0]["user"])

    def test_stable_pagination_with_tied_timestamps(self):
        for url, model, problem, records in self.cases:
            model.objects.filter(challenge=problem).update(created_at=datetime(2026, 10, 24, tzinfo=timezone.utc))
            ids = [self.data(url, page=page, size=1)["submissions"][0]["submission_id"] for page in range(1, 6)]
            self.assertEqual(ids, sorted((str(row.pk) for row in records), reverse=True))
            self.assertEqual(self.data(url, page=10)["submissions"], [])
            self.assertEqual(self.data(url, page=10**30)["submissions"], [])

    def test_invalid_queries(self):
        invalid = ({"result": "WRONG"}, {"team_id": "invalid"}, {"page": 0}, {"page": "bad"},
                   {"size": 0}, {"size": 101}, {"start_at": "2026-02-30T00:00:00Z"},
                   {"start_at": "2026-10-24T00:00:00"}, {"end_at": "bad"},
                   {"start_at": "2026-10-24T00:00:00Z", "end_at": "2026-10-24T00:00:00Z"})
        for url, *_ in self.cases:
            for params in invalid:
                with self.subTest(url=url, params=params):
                    response = self.client.get(url, params)
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertEqual(response.data["code"], "INVALID_REQUEST")

    def test_empty_history_and_unknown_problem(self):
        for url, model, problem, _ in self.cases:
            model.objects.filter(challenge=problem).delete()
            data = self.data(url)
            self.assertEqual(data["total_count"], 0)
            self.assertEqual(data["summary"], {"total": 0, "incorrect": 0, "locked": 0, "teams": 0, "correct": 0})
            response = self.client.get(url.replace(str(problem.pk), str(uuid.uuid4())))
            self.assertEqual(response.status_code, 404)

    def test_other_problem_records_are_excluded_and_writes_rejected(self):
        for url, model, problem, records in self.cases:
            if model is FlagSubmission:
                other = Challenge.objects.create(title="other", score=100, flag_hash="private")
            else:
                other = SignatureChallenge.objects.create(
                    club=KothClub.objects.create(name="other-history-club"), title="other", flag_hash="private",
                )
            model.objects.create(challenge=other, team=self.team, user=self.user, result="INCORRECT", submitted_flag_hash="private")
            expected = {str(row.pk) for row in records}
            self.assertEqual({row["submission_id"] for row in self.data(url)["submissions"]}, expected)
            for method in (self.client.post, self.client.patch, self.client.delete):
                self.assertEqual(method(url).status_code, 405)
            self.assertEqual(model.objects.filter(challenge=problem).count(), 5)

    def test_query_count_is_constant_and_correct_summary_counts_teams(self):
        for url, model, problem, _ in self.cases:
            model.objects.create(challenge=problem, team=self.team, user=self.user, result="CORRECT", submitted_flag_hash="private")
            with self.assertNumQueries(4):
                data = self.data(url, size=100)
            self.assertEqual(data["summary"]["correct"], 2)
            self.assertEqual(data["summary"]["total"], 6)
