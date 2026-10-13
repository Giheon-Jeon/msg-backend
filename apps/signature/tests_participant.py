from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.koth.models import KothClub

from .models import SignatureChallenge, SignatureSolve


class SignatureParticipantApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.team = Team.objects.create(team_name="참가 팀")
        self.user = User.objects.create_user(
            login_id="signature-viewer",
            password="pw1234",
            nickname="참가자",
            team=self.team,
        )
        self.other_team = Team.objects.create(team_name="다른 팀")
        self.club = KothClub.objects.create(name="CodeCure")
        self.published = SignatureChallenge.objects.create(
            club=self.club,
            title="CodeCure Signature",
            description="부스에서 플래그를 받아 제출하세요.",
            flag_hash="secret-hash",
            is_published=True,
        )
        self.client.force_authenticate(user=self.user)

    def test_list_returns_only_published_challenges_and_team_solve_state(self):
        other_club = KothClub.objects.create(name="MJSEC")
        hidden = SignatureChallenge.objects.create(
            club=other_club,
            title="Hidden Signature",
            description="비공개 문제",
            flag_hash="hidden-hash",
            is_published=False,
        )
        solve = SignatureSolve.objects.create(
            team=self.team,
            challenge=self.published,
            solved_by_user=self.user,
            earned_score=self.published.score,
        )
        SignatureSolve.objects.create(
            team=self.other_team,
            challenge=self.published,
            earned_score=self.published.score,
        )

        response = self.client.get("/api/v1/signatures")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["total_count"], 1)
        row = response.data["data"]["signatures"][0]
        self.assertEqual(row["signature_id"], str(self.published.signature_id))
        self.assertTrue(row["is_solved"])
        self.assertEqual(
            row["solved_at"],
            solve.solved_at.isoformat().replace("+00:00", "Z"),
        )
        self.assertEqual(row["solved_team_count"], 2)
        self.assertNotEqual(row["signature_id"], str(hidden.signature_id))
        self.assertNotIn("description", row)
        self.assertNotIn("flag_hash", row)

    def test_list_returns_null_solved_at_when_unsolved(self):
        response = self.client.get("/api/v1/signatures")

        self.assertEqual(response.status_code, 200)
        row = response.data["data"]["signatures"][0]
        self.assertFalse(row["is_solved"])
        self.assertIsNone(row["solved_at"])

    def test_detail_returns_description_and_current_team_solved_at(self):
        solve = SignatureSolve.objects.create(
            team=self.team,
            challenge=self.published,
            solved_by_user=self.user,
            earned_score=self.published.score,
        )

        response = self.client.get(f"/api/v1/signatures/{self.published.signature_id}")

        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(data["description"], self.published.description)
        self.assertTrue(data["is_solved"])
        self.assertEqual(
            data["solved_at"], solve.solved_at.isoformat().replace("+00:00", "Z")
        )
        self.assertNotIn("flag_hash", data)

    def test_detail_returns_null_solved_at_when_unsolved(self):
        response = self.client.get(f"/api/v1/signatures/{self.published.signature_id}")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["data"]["is_solved"])
        self.assertIsNone(response.data["data"]["solved_at"])

    def test_unpublished_and_missing_challenges_return_not_found(self):
        other_club = KothClub.objects.create(name="MJU")
        hidden = SignatureChallenge.objects.create(
            club=other_club,
            title="Hidden Signature",
            description="비공개 문제",
            flag_hash="hidden-hash",
            is_published=False,
        )

        for signature_id in (
            hidden.signature_id,
            "018f3f1e-0300-7a91-a30b-630000000999",
        ):
            response = self.client.get(f"/api/v1/signatures/{signature_id}")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.data["code"], "SIGNATURE_NOT_FOUND")

    def test_user_without_team_is_rejected(self):
        user_without_team = User.objects.create_user(
            login_id="signature-no-team",
            password="pw1234",
            nickname="무소속",
        )
        self.client.force_authenticate(user=user_without_team)

        for url in (
            "/api/v1/signatures",
            f"/api/v1/signatures/{self.published.signature_id}",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.data["code"], "USER_HAS_NO_TEAM")

    def test_unauthenticated_request_is_rejected(self):
        self.client.force_authenticate(user=None)

        response = self.client.get("/api/v1/signatures")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["code"], "TOKEN_MISSING")
