import uuid
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.hashers import check_password, make_password
from django.db import DatabaseError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.adminpanel.models import AdminEvent
from apps.common.jwt import issue_access_token
from apps.koth.models import KothClub

from .models import SignatureChallenge, SignatureSolve, SignatureSubmissionLock


class SignatureAdminAuditTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            login_id="signature-audit-admin", role=Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.club = KothClub.objects.create(name="Audit club")
        self.challenge = SignatureChallenge.objects.create(
            club=self.club,
            title="Original",
            description="Original description",
            flag_hash=make_password("qa-original-flag"),
            score=300,
        )
        self.url = f"/api/v1/admin/signatures/{self.challenge.pk}"

    def create(self, club=None, **overrides):
        values = {
            "club_id": str((club or self.club).pk),
            "title": "private-new-title",
            "description": "private-new-description",
            "flag": "qa-new-flag",
        }
        values.update(overrides)
        return self.client.post("/api/v1/admin/signatures", values, format="json")

    def update(self, **values):
        return self.client.patch(self.url, values, format="json")

    def event(self, event_type, signature_id=None, club_id=None):
        event = AdminEvent.objects.get()
        self.assertEqual(event.type, event_type)
        self.assertEqual(event.actor, self.admin.login_id)
        self.assertIn(
            f"signature_id={signature_id or self.challenge.pk}", event.message
        )
        self.assertIn(f"club_id={club_id or self.club.pk}", event.message)
        self.assertIsNone(event.challenge_id)
        self.assertIsNone(event.team_id)
        self.assertIsNotNone(event.created_at)
        response = self.client.get("/api/v1/admin/events", {"type": event_type})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["total_count"], 1)
        return event

    def test_create_records_one_safe_event_and_can_filter_it(self):
        club = KothClub.objects.create(name="Create club")
        response = self.create(club)
        self.assertEqual(response.status_code, 201)
        challenge = SignatureChallenge.objects.get(club=club)
        event = self.event("SIGNATURE_CREATED", challenge.pk, club.pk)
        self.assertEqual(event.severity, AdminEvent.Severity.INFO)
        for value in (
            "private-new-title",
            "private-new-description",
            "qa-new-flag",
            challenge.flag_hash,
        ):
            self.assertNotIn(value, event.message)
        response = self.client.get("/api/v1/admin/events", {"type": event.type})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["total_count"], 1)
        self.assertEqual(response.data["data"]["events"][0]["event_id"], str(event.pk))

    def test_update_records_changed_fields_without_content_or_flags(self):
        old_hash = self.challenge.flag_hash
        response = self.update(
            title="private-updated-title",
            description="private-updated-description",
            flag="qa-updated-flag",
        )
        self.assertEqual(response.status_code, 200)
        self.challenge.refresh_from_db()
        event = self.event("SIGNATURE_UPDATED")
        self.assertEqual(event.severity, AdminEvent.Severity.WARNING)
        for field in ("title", "description", "flag"):
            self.assertIn(field, event.message)
        for value in (
            "private-updated-title",
            "private-updated-description",
            "qa-original-flag",
            "qa-updated-flag",
            old_hash,
            self.challenge.flag_hash,
        ):
            self.assertNotIn(value, event.message)
        self.assertTrue(check_password("qa-updated-flag", self.challenge.flag_hash))

    def test_score_event_records_before_after_and_affected_solve_count(self):
        for index in range(2):
            team = Team.objects.create(team_name=f"Solved {index}")
            SignatureSolve.objects.create(
                team=team, challenge=self.challenge, earned_score=300
            )
        self.assertEqual(self.update(score=450).status_code, 200)
        event = self.event("SIGNATURE_UPDATED")
        self.assertIn("score: 300.00 -> 450.00", event.message)
        self.assertIn("풀이 팀 2", event.message)
        self.assertEqual(
            list(SignatureSolve.objects.values_list("earned_score", flat=True)),
            [Decimal("450.00")] * 2,
        )

    def test_noop_update_does_not_rehash_or_record(self):
        old_hash = self.challenge.flag_hash
        response = self.update(
            title="Original",
            description="Original description",
            score=300,
            flag="qa-original-flag",
        )
        self.assertEqual(response.status_code, 200)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.flag_hash, old_hash)
        self.assertFalse(AdminEvent.objects.exists())

    def test_delete_keeps_snapshot_and_repeated_delete_has_no_extra_event(self):
        signature_id = self.challenge.pk
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SignatureChallenge.objects.filter(pk=signature_id).exists())
        event = self.event("SIGNATURE_DELETED", signature_id)
        self.assertEqual(event.severity, AdminEvent.Severity.WARNING)
        self.club.delete()
        self.assertEqual(self.client.delete(self.url).status_code, 404)
        event.refresh_from_db()
        self.assertIn(str(signature_id), event.message)
        response = self.client.get(
            "/api/v1/admin/events", {"type": "SIGNATURE_DELETED"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["total_count"], 1)

    def test_invalid_duplicate_and_missing_requests_have_no_events(self):
        self.assertEqual(self.create().status_code, 409)
        self.assertEqual(self.create(club_id=str(uuid.uuid4())).status_code, 404)
        self.assertEqual(self.update(score=0).status_code, 400)
        self.assertEqual(self.update().status_code, 400)
        self.assertEqual(
            self.client.patch(
                f"/api/v1/admin/signatures/{uuid.uuid4()}",
                {"title": "Missing"},
                format="json",
            ).status_code,
            404,
        )
        self.assertFalse(AdminEvent.objects.exists())

    def test_in_use_deletion_has_no_event_or_changes(self):
        self.challenge.is_published = True
        self.challenge.save(update_fields=["is_published"])
        self.assertEqual(self.client.delete(self.url).status_code, 409)
        self.challenge.is_published = False
        self.challenge.save(update_fields=["is_published"])
        team = Team.objects.create(team_name="Locked")
        SignatureSubmissionLock.objects.create(
            team=team, challenge=self.challenge, failed_count=1
        )
        self.assertEqual(self.client.delete(self.url).status_code, 409)
        self.assertTrue(
            SignatureChallenge.objects.filter(pk=self.challenge.pk).exists()
        )
        self.assertFalse(AdminEvent.objects.exists())

    def test_non_admin_and_stale_admin_role_reject_before_changes(self):
        participant = User.objects.create_user(login_id="audit-player")
        self.client.force_authenticate(None)
        for user in (None, participant, self.admin):
            self.client.credentials()
            if user is not None:
                self.client.credentials(
                    HTTP_AUTHORIZATION=f"Bearer {issue_access_token(user)}"
                )
            if user == self.admin:
                User.objects.filter(pk=user.pk).update(role=Role.PARTICIPANT)
            for method, url, data in (
                ("post", "/api/v1/admin/signatures", {}),
                ("patch", self.url, {"title": "Forbidden"}),
                ("delete", self.url, {}),
            ):
                with self.subTest(user=user, method=method):
                    response = getattr(self.client, method)(url, data, format="json")
                    self.assertEqual(response.status_code, 401 if user is None else 403)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.title, "Original")
        self.assertFalse(AdminEvent.objects.exists())

    def test_event_write_failure_rolls_back_create(self):
        club = KothClub.objects.create(name="Rollback create")
        with patch(
            "apps.signature.admin_views._record_event",
            side_effect=DatabaseError("QA audit failure"),
        ):
            response = self.create(club)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(SignatureChallenge.objects.filter(club=club).exists())
        self.assertFalse(AdminEvent.objects.exists())

    def test_event_write_failure_rolls_back_update_and_existing_solve_scores(self):
        team = Team.objects.create(team_name="Rollback update")
        solve = SignatureSolve.objects.create(
            team=team, challenge=self.challenge, earned_score=300
        )
        old_hash = self.challenge.flag_hash
        with patch(
            "apps.signature.admin_views._record_event",
            side_effect=DatabaseError("QA audit failure"),
        ):
            response = self.update(score=450, title="Temporary", flag="qa-updated-flag")
        self.assertEqual(response.status_code, 500)
        self.challenge.refresh_from_db()
        solve.refresh_from_db()
        self.assertEqual(
            (self.challenge.title, self.challenge.score, self.challenge.flag_hash),
            ("Original", Decimal("300.00"), old_hash),
        )
        self.assertEqual(solve.earned_score, Decimal("300.00"))
        self.assertFalse(AdminEvent.objects.exists())

    def test_event_write_failure_rolls_back_delete(self):
        with patch(
            "apps.signature.admin_views._record_event",
            side_effect=DatabaseError("QA audit failure"),
        ):
            response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(
            SignatureChallenge.objects.filter(pk=self.challenge.pk).exists()
        )
        self.assertFalse(AdminEvent.objects.exists())

    def test_delete_failure_rolls_back_prepared_event(self):
        with patch.object(
            SignatureChallenge, "delete", side_effect=DatabaseError("QA delete failure")
        ):
            response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(
            SignatureChallenge.objects.filter(pk=self.challenge.pk).exists()
        )
        self.assertFalse(AdminEvent.objects.exists())

    def test_outer_rollback_removes_both_change_and_event(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.assertEqual(self.update(title="Temporary").status_code, 200)
                self.assertEqual(AdminEvent.objects.count(), 1)
                raise RuntimeError("QA rollback")
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.title, "Original")
        self.assertFalse(AdminEvent.objects.exists())
