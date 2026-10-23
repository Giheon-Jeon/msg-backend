from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.koth.models import KothClub
from apps.signature.models import SignatureChallenge, SignatureSolve


class SignatureAdminListTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(login_id="list-admin", role=Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.items = []
        for club, title, score, published in [
            ("Zebra", "Alpha puzzle", 100, True),
            ("Alpha Club", "Beta puzzle", 300, False),
            ("Delta", "Gamma puzzle", 300, True),
            ("Beta Club", "Alpha sequel", 200, False),
        ]:
            self.items.append(SignatureChallenge.objects.create(
                club=KothClub.objects.create(name=club), title=title, score=score,
                is_published=published, description="List fixture", flag_hash="private-test-hash",
            ))
        for i in range(2):
            team = Team.objects.create(team_name=f"list-team-{i}")
            SignatureSolve.objects.create(team=team, challenge=self.items[0], earned_score=100)

    def get(self, **query):
        return self.client.get("/api/v1/admin/signatures", query)

    def test_default_preserves_club_order_and_safe_fields(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual((data["total_count"], data["page"], data["size"]), (4, 1, 50))
        self.assertEqual([row["club_name"] for row in data["signatures"]],
                         ["Alpha Club", "Beta Club", "Delta", "Zebra"])
        self.assertEqual({row["is_published"] for row in data["signatures"]}, {True, False})
        zebra = data["signatures"][-1]
        self.assertEqual(zebra["score"], 100)
        self.assertEqual(zebra["solved_team_count"], 2)
        for row in data["signatures"]:
            self.assertNotIn("flag", row)
            self.assertNotIn("flag_hash", row)
        self.assertNotIn("private-test-hash", str(data))

    def test_search_matches_title_or_club_case_insensitively(self):
        for term, expected in [(" alpha ", 3), ("zEbRa", 1), ("not found", 0), (" ", 4)]:
            with self.subTest(term=term):
                response = self.get(search=term)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["data"]["total_count"], expected)

    def test_search_precedes_pagination_and_counts_filtered_results(self):
        data = self.get(search="Alpha", sort="title", page=2, size=1).data["data"]
        self.assertEqual(data["total_count"], 3)
        self.assertEqual(len(data["signatures"]), 1)
        self.assertEqual(data["signatures"][0]["title"], "Alpha sequel")

    def test_sorting_has_stable_ties(self):
        for sort, expected in [
            ("club", [1, 3, 2, 0]), ("title", [0, 3, 1, 2]),
            ("score", [1, 2, 3, 0]), ("solved", [0, 1, 3, 2]),
        ]:
            with self.subTest(sort=sort):
                rows = []
                for page in range(1, 5):
                    response = self.get(sort=sort, page=page, size=1)
                    self.assertEqual(response.status_code, 200)
                    rows.extend(response.data["data"]["signatures"])
                self.assertEqual([row["signature_id"] for row in rows],
                                 [str(self.items[index].pk) for index in expected])

    def test_out_of_range_and_maximum_page_return_empty_list(self):
        for page in (5, 10000):
            data = self.get(page=page, size=1).data["data"]
            self.assertEqual(data["total_count"], 4)
            self.assertEqual(data["signatures"], [])

    def test_invalid_queries_return_400(self):
        for query in [dict(page=0), dict(page=-1), dict(page="x"), dict(page="1.5"),
                      dict(page=10001), dict(page="9" * 100), dict(size=0), dict(size=-1),
                      dict(size=101), dict(size="x"), dict(sort="bogus"), dict(sort="-flag_hash"),
                      dict(search="x" * 201)]:
            with self.subTest(query=query):
                response = self.get(**query)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "INVALID_REQUEST")

    def test_maximum_page_size_is_accepted(self):
        response = self.get(size=100)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["size"], 100)

    def test_unauthorized_queries_do_not_read_signature_data(self):
        self.client.force_authenticate(user=None)
        with self.assertNumQueries(0):
            response = self.get(search="Alpha")
        self.assertEqual(response.status_code, 401)
        participant = User.objects.create_user(login_id="list-participant")
        self.client.force_authenticate(participant)
        with self.assertNumQueries(0):
            response = self.get(search="Alpha", page=0)
        self.assertEqual(response.status_code, 403)

    def test_filtered_list_does_not_write_data(self):
        before = list(SignatureChallenge.objects.order_by("pk").values())
        self.get(search="Alpha", sort="score", size=1)
        self.assertEqual(list(SignatureChallenge.objects.order_by("pk").values()), before)
