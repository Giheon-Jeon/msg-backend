from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.db import DatabaseError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.adminpanel.models import AdminEvent
from apps.board.line_rules import CELL_LINE_NUMBERS
from apps.board.models import BoardChallenge, Cell, TeamChallengeAccess
from apps.challenge.models import Challenge, Solve
from apps.challenge.services import hash_flag


class ChallengeAuditTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(login_id="audit-admin", role=Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.challenge = Challenge.objects.create(
            title="Original", category="WEB", difficulty="EASY", description="Original text",
            flag_hash=hash_flag("qa-old-flag"), score=1000, initial_score=1000,
            minimum_score=100, current_score=1000, decay=70, is_published=True,
        )
        self.url = f"/api/v1/admin/challenges/{self.challenge.pk}"

    def update(self, **values):
        return self.client.patch(self.url, values, format="json")

    def visibility(self, published, reason="QA reason"):
        return self.client.patch(self.url + "/visibility", {
            "is_published": published, "reason": reason,
        }, format="json")

    def test_changed_fields_record_one_safe_event(self):
        old_hash = self.challenge.flag_hash
        response = self.update(
            title="private-title-content", description="private-description-content",
            category="PWN", difficulty="HARD", flag="qa-new-flag",
        )
        self.assertEqual(response.status_code, 200)
        event = AdminEvent.objects.get(type="CHALLENGE_UPDATED")
        self.assertEqual(event.actor, self.admin.login_id)
        self.assertEqual(event.challenge_id, self.challenge.pk)
        self.assertEqual(event.severity, AdminEvent.Severity.WARNING)
        for field in ["title", "description", "category", "difficulty", "flag"]:
            self.assertIn(field, event.message)
        self.assertIn("WEB -> PWN", event.message)
        self.assertIn("EASY -> HARD", event.message)
        for value in [old_hash, hash_flag("qa-new-flag"), "qa-old-flag", "qa-new-flag",
                      "private-title-content", "private-description-content"]:
            self.assertNotIn(value, event.message)
        self.assertEqual(AdminEvent.objects.count(), 1)
        events = self.client.get("/api/v1/admin/events", {"type": "CHALLENGE_UPDATED"})
        self.assertEqual(events.status_code, 200)

    def test_scoring_event_identifies_impact_and_current_score(self):
        team = Team.objects.create(team_name="Solved", team_score=1000)
        Solve.objects.create(team=team, challenge=self.challenge, earned_score=1000, earned_mileage=0)
        cell = Cell.objects.create(cell_index=2, type="CHALLENGE", difficulty="HARD",
                                   line_number=CELL_LINE_NUMBERS[2], name="QA")
        TeamChallengeAccess.objects.create(team=team, challenge=self.challenge, source_cell=cell)
        response = self.update(initial_score=2000)
        self.assertEqual(response.status_code, 200)
        event = AdminEvent.objects.get(type="CHALLENGE_UPDATED")
        self.assertIn("initial_score: 1000 -> 2000", event.message)
        self.assertIn("current_score:", event.message)
        self.assertIn("풀이 팀 1", event.message)
        self.assertIn("접근 팀 1", event.message)
        self.challenge.refresh_from_db()
        team.refresh_from_db()
        self.assertEqual(team.team_score, self.challenge.current_score)

    def test_noop_update_does_not_record_or_recalculate(self):
        with patch("apps.adminpanel.views.update_dynamic_score_and_team_scores") as recalculate:
            response = self.update(title="Original", initial_score=1000, flag="qa-old-flag")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AdminEvent.objects.count(), 0)
        recalculate.assert_not_called()

    def test_invalid_update_has_no_event(self):
        response = self.update(initial_score=1)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AdminEvent.objects.count(), 0)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.initial_score, 1000)

    def test_same_flag_preserves_legacy_password_hash_without_event(self):
        old_hash = make_password("qa-old-flag")
        self.challenge.flag_hash = old_hash
        self.challenge.save(update_fields=["flag_hash"])
        self.assertEqual(self.update(flag="qa-old-flag").status_code, 200)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.flag_hash, old_hash)
        self.assertEqual(AdminEvent.objects.count(), 0)

    def test_outer_rollback_removes_both_change_and_event(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.assertEqual(self.update(title="Temporary").status_code, 200)
                self.assertEqual(AdminEvent.objects.count(), 1)
                raise RuntimeError("QA rollback")
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.title, "Original")
        self.assertEqual(AdminEvent.objects.count(), 0)

    def test_event_failure_rolls_back_problem_and_recalculated_scores(self):
        team = Team.objects.create(team_name="Rollback", team_score=1000)
        Solve.objects.create(team=team, challenge=self.challenge, earned_score=1000, earned_mileage=0)
        with patch("apps.adminpanel.views._record_event", side_effect=DatabaseError("QA failure")):
            response = self.update(initial_score=2000, flag="qa-new-flag")
        self.assertEqual(response.status_code, 500)
        self.challenge.refresh_from_db()
        team.refresh_from_db()
        self.assertEqual(self.challenge.initial_score, 1000)
        self.assertEqual(self.challenge.current_score, 1000)
        self.assertEqual(self.challenge.flag_hash, hash_flag("qa-old-flag"))
        self.assertEqual(team.team_score, 1000)
        self.assertEqual(AdminEvent.objects.count(), 0)

    def test_visibility_event_contains_both_states_and_reason(self):
        for before, after in [(True, False), (False, True)]:
            response = self.visibility(after)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["data"]["previous_is_published"], before)
            event = AdminEvent.objects.latest("created_at")
            self.assertIn(f"is_published: {before} -> {after}", event.message)
            self.assertTrue(event.message.endswith("QA reason"))
            self.assertEqual(event.actor, self.admin.login_id)
        self.assertEqual(AdminEvent.objects.count(), 2)

    def test_noop_visibility_does_not_record_event(self):
        response = self.visibility(True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AdminEvent.objects.count(), 0)

    def test_visibility_event_failure_rolls_back_catalog_and_state(self):
        self.challenge.is_published = False
        self.challenge.save(update_fields=["is_published"])
        with patch("apps.adminpanel.views._record_event", side_effect=DatabaseError("QA failure")):
            response = self.visibility(True)
        self.assertEqual(response.status_code, 500)
        self.challenge.refresh_from_db()
        self.assertFalse(self.challenge.is_published)
        self.assertFalse(BoardChallenge.objects.filter(challenge=self.challenge).exists())
        self.assertEqual(AdminEvent.objects.count(), 0)

    def test_non_admin_cannot_change_problem_or_create_events(self):
        participant = User.objects.create_user(login_id="audit-participant")
        self.client.force_authenticate(participant)
        self.assertEqual(self.update(title="Forbidden").status_code, 403)
        self.assertEqual(self.visibility(False).status_code, 403)
        self.assertEqual(AdminEvent.objects.count(), 0)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.title, "Original")
        self.assertTrue(self.challenge.is_published)
