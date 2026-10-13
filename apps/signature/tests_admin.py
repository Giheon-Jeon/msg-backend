from decimal import Decimal

from django.contrib.auth.hashers import check_password
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.koth.models import KothClub

from .models import (
    SignatureChallenge,
    SignatureFlagSubmission,
    SignatureSolve,
    SignatureSubmissionLock,
)


class SignatureAdminApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            login_id="signature-admin",
            password="pw1234",
            nickname="관리자",
            role=Role.ADMIN,
        )
        self.participant = User.objects.create_user(
            login_id="signature-participant",
            password="pw1234",
            nickname="참가자",
        )
        self.club = KothClub.objects.create(name="CodeCure")

    def authenticate(self, user=None):
        self.client.force_authenticate(user=user or self.admin)

    def create_challenge(self, **overrides):
        values = {
            "club": self.club,
            "title": "CodeCure Signature",
            "description": "클럽 시그니처 문제",
            "flag_hash": "old-hash",
        }
        values.update(overrides)
        return SignatureChallenge.objects.create(**values)

    def test_admin_can_create_unpublished_challenge_with_default_score(self):
        self.authenticate()
        response = self.client.post(
            "/api/v1/admin/signatures",
            {
                "club_id": str(self.club.club_id),
                "title": "CodeCure Signature",
                "description": "클럽 시그니처 문제",
                "flag": "MSG{signature_flag}",
                "is_published": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["code"], "SUCCESS")
        challenge = SignatureChallenge.objects.get()
        self.assertEqual(challenge.score, Decimal("300.00"))
        self.assertFalse(challenge.is_published)
        self.assertTrue(check_password("MSG{signature_flag}", challenge.flag_hash))
        self.assertNotIn("flag", response.data["data"])
        self.assertNotIn("flag_hash", response.data["data"])

    def test_admin_list_includes_unpublished_challenges_without_flag(self):
        self.create_challenge()
        self.authenticate()
        response = self.client.get("/api/v1/admin/signatures")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["total_count"], 1)
        row = response.data["data"]["signatures"][0]
        self.assertFalse(row["is_published"])
        self.assertEqual(row["solved_team_count"], 0)
        self.assertNotIn("flag", row)
        self.assertNotIn("flag_hash", row)

    def test_create_rejects_duplicate_club_and_missing_club(self):
        self.create_challenge()
        self.authenticate()
        payload = {
            "club_id": str(self.club.club_id),
            "title": "Duplicate",
            "description": "중복 문제",
            "flag": "MSG{duplicate}",
        }
        duplicate = self.client.post("/api/v1/admin/signatures", payload, format="json")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.data["code"], "SIGNATURE_ALREADY_EXISTS")

        payload["club_id"] = "018f3f1e-0300-7a91-a30b-630000000999"
        missing = self.client.post("/api/v1/admin/signatures", payload, format="json")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.data["code"], "CLUB_NOT_FOUND")

    def test_create_validates_required_fields_and_positive_score(self):
        self.authenticate()
        missing = self.client.post(
            "/api/v1/admin/signatures",
            {"club_id": str(self.club.club_id)},
            format="json",
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.data["code"], "INVALID_REQUEST")

        invalid_score = self.client.post(
            "/api/v1/admin/signatures",
            {
                "club_id": str(self.club.club_id),
                "title": "Invalid",
                "description": "잘못된 점수",
                "flag": "MSG{invalid}",
                "score": 0,
            },
            format="json",
        )
        self.assertEqual(invalid_score.status_code, 400)
        self.assertEqual(invalid_score.data["code"], "INVALID_REQUEST")

    def test_admin_can_update_fields_and_existing_solve_score(self):
        challenge = self.create_challenge()
        team = Team.objects.create(team_name="해결팀")
        SignatureSolve.objects.create(
            team=team,
            challenge=challenge,
            earned_score=challenge.score,
        )
        self.authenticate()
        response = self.client.patch(
            f"/api/v1/admin/signatures/{challenge.signature_id}",
            {
                "title": "Updated Signature",
                "description": "수정된 설명",
                "flag": "MSG{updated_flag}",
                "score": 350,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        challenge.refresh_from_db()
        solve = SignatureSolve.objects.get(challenge=challenge)
        self.assertEqual(challenge.title, "Updated Signature")
        self.assertEqual(challenge.score, Decimal("350.00"))
        self.assertEqual(solve.earned_score, Decimal("350.00"))
        self.assertTrue(check_password("MSG{updated_flag}", challenge.flag_hash))
        self.assertNotIn("flag_hash", response.data["data"])

    def test_admin_can_publish_and_unpublish_challenge(self):
        challenge = self.create_challenge()
        self.authenticate()

        for is_published in (True, False):
            response = self.client.patch(
                f"/api/v1/admin/signatures/{challenge.signature_id}/publish",
                {"is_published": is_published},
                format="json",
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["data"]["is_published"], is_published)
            challenge.refresh_from_db()
            self.assertEqual(challenge.is_published, is_published)

    def test_admin_can_delete_unused_unpublished_challenge_and_recreate(self):
        challenge = self.create_challenge()
        signature_id = challenge.signature_id
        self.authenticate()

        response = self.client.delete(f"/api/v1/admin/signatures/{signature_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "SUCCESS")
        self.assertEqual(
            response.data["data"]["signature_id"],
            str(signature_id),
        )
        self.assertFalse(SignatureChallenge.objects.filter(pk=signature_id).exists())

        recreated = self.client.post(
            "/api/v1/admin/signatures",
            {
                "club_id": str(self.club.club_id),
                "title": "Recreated Signature",
                "description": "다시 등록한 문제",
                "flag": "MSG{recreated}",
            },
            format="json",
        )
        self.assertEqual(recreated.status_code, 201)

    def test_delete_rejects_published_challenge(self):
        challenge = self.create_challenge(is_published=True)
        self.authenticate()

        response = self.client.delete(
            f"/api/v1/admin/signatures/{challenge.signature_id}"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "SIGNATURE_IN_USE")
        self.assertTrue(
            SignatureChallenge.objects.filter(pk=challenge.signature_id).exists()
        )

    def test_delete_rejects_solve_submission_and_lock_records(self):
        self.authenticate()
        for index, record_type in enumerate(("solve", "submission", "lock"), start=1):
            with self.subTest(record_type=record_type):
                club = KothClub.objects.create(name=f"Delete Test {index}")
                challenge = self.create_challenge(club=club)
                team = Team.objects.create(team_name=f"Delete Team {index}")

                if record_type == "solve":
                    SignatureSolve.objects.create(
                        team=team,
                        challenge=challenge,
                        earned_score=challenge.score,
                    )
                elif record_type == "submission":
                    SignatureFlagSubmission.objects.create(
                        team=team,
                        challenge=challenge,
                        submitted_flag_hash="submitted-hash",
                        result=SignatureFlagSubmission.SubmissionResult.INCORRECT,
                    )
                else:
                    SignatureSubmissionLock.objects.create(
                        team=team,
                        challenge=challenge,
                    )

                response = self.client.delete(
                    f"/api/v1/admin/signatures/{challenge.signature_id}"
                )

                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data["code"], "SIGNATURE_IN_USE")
                self.assertTrue(
                    SignatureChallenge.objects.filter(
                        pk=challenge.signature_id
                    ).exists()
                )

    def test_update_rejects_empty_body(self):
        challenge = self.create_challenge()
        self.authenticate()
        response = self.client.patch(
            f"/api/v1/admin/signatures/{challenge.signature_id}",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "INVALID_REQUEST")

    def test_non_admin_is_forbidden(self):
        challenge = self.create_challenge()
        self.authenticate(self.participant)
        response = self.client.get("/api/v1/admin/signatures")
        deleted = self.client.delete(
            f"/api/v1/admin/signatures/{challenge.signature_id}"
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "FORBIDDEN")
        self.assertEqual(deleted.status_code, 403)
        self.assertEqual(deleted.data["code"], "FORBIDDEN")
        self.assertTrue(
            SignatureChallenge.objects.filter(pk=challenge.signature_id).exists()
        )

    def test_update_and_publish_return_not_found(self):
        self.authenticate()
        missing_id = "018f3f1e-0300-7a91-a30b-630000000999"

        update = self.client.patch(
            f"/api/v1/admin/signatures/{missing_id}",
            {"title": "Missing"},
            format="json",
        )
        publish = self.client.patch(
            f"/api/v1/admin/signatures/{missing_id}/publish",
            {"is_published": True},
            format="json",
        )
        deleted = self.client.delete(f"/api/v1/admin/signatures/{missing_id}")

        self.assertEqual(update.status_code, 404)
        self.assertEqual(update.data["code"], "SIGNATURE_NOT_FOUND")
        self.assertEqual(publish.status_code, 404)
        self.assertEqual(publish.data["code"], "SIGNATURE_NOT_FOUND")
        self.assertEqual(deleted.status_code, 404)
        self.assertEqual(deleted.data["code"], "SIGNATURE_NOT_FOUND")
