from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.board.models import TeamChallengeAccess
from apps.challenge import tests as challenge_tests
from apps.challenge.models import Challenge, FlagSubmission, FlagSubmissionLock, Solve
from apps.challenge.services import hash_flag
from apps.teams.models import MileageHistory, MileageType


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking")
@override_settings(CACHES=challenge_tests.LOCMEM, SECURE_SSL_REDIRECT=False)
class ConcurrentChallengeDeleteTests(TransactionTestCase):
    auth = challenge_tests.ChallengeSubmitTests.auth

    def setUp(self):
        challenge_tests.ChallengeSubmitTests.setUp(self)
        self.challenge.is_published = False
        self.challenge.save(update_fields=["is_published"])
        self.admin = User.objects.create_user(
            login_id="delete-race-admin", nickname="admin", role=Role.ADMIN,
        )

    def test_delete_before_correct_submission_lock_returns_not_found(self):
        self._assert_delete_before_submission_lock("MSG{correct_flag}")

    def test_delete_before_wrong_submission_lock_returns_not_found(self):
        self._assert_delete_before_submission_lock("MSG{wrong_flag}")

    def _assert_delete_before_submission_lock(self, flag):
        validating, deleted = Event(), Event()

        def pause_hash(value):
            validating.set()
            if not deleted.wait(timeout=10):
                raise AssertionError("Deletion did not finish")
            return hash_flag(value)

        def submit():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '15s'")
                client = APIClient()
                client.force_authenticate(user=User.objects.get(pk=self.user.pk))
                return client.post(
                    f"/api/v1/challenges/{self.challenge.pk}/submit", {"flag": flag}, format="json",
                )
            finally:
                connections.close_all()

        with patch("apps.challenge.views.hash_flag", side_effect=pause_hash):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(submit)
                try:
                    self.assertTrue(validating.wait(timeout=10))
                    admin = APIClient()
                    admin.force_authenticate(user=self.admin)
                    # Removing the team removes the access record that protects deletion.
                    removed_team = admin.delete(
                        f"/api/v1/admin/teams/{self.team.pk}", {"reason": "delete race"}, format="json",
                    )
                    self.assertEqual(removed_team.status_code, 200, removed_team.data)
                    removed_challenge = admin.delete(f"/api/v1/admin/challenges/{self.challenge.pk}")
                    self.assertEqual(removed_challenge.status_code, 200, removed_challenge.data)
                finally:
                    deleted.set()
                response = pending.result(timeout=15)

        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(response.data["code"], "CHALLENGE_NOT_FOUND")
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIsNone(response.data["data"])
        self.assertFalse(Challenge.objects.filter(pk=self.challenge.pk).exists())
        self.assertFalse(Team.objects.filter(pk=self.team.pk).exists())
        self.assertFalse(Solve.objects.exists())
        self.assertFalse(FlagSubmission.objects.exists())
        self.assertFalse(FlagSubmissionLock.objects.exists())
        self.assertFalse(MileageHistory.objects.exists())

    def test_submission_lock_first_preserves_rewards_and_blocks_deletion(self):
        submit_locked, delete_waiting = Event(), Event()
        earned_score = self.challenge.current_score

        def request(action):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '15s'")
                client = APIClient()
                user = self.user if action == "submit" else self.admin
                client.force_authenticate(user=User.objects.get(pk=user.pk))
                synchronized = False

                def overlap(execute, sql, params, many, context):
                    nonlocal synchronized
                    if synchronized or 'FROM "challenges"' not in sql or "FOR UPDATE" not in sql:
                        return execute(sql, params, many, context)
                    synchronized = True
                    if action == "submit":
                        result = execute(sql, params, many, context)
                        submit_locked.set()
                        if not delete_waiting.wait(timeout=10):
                            raise AssertionError("Deletion never attempted the challenge lock")
                        return result
                    if not submit_locked.wait(timeout=10):
                        raise AssertionError("Submission did not acquire the challenge lock")
                    delete_waiting.set()
                    return execute(sql, params, many, context)

                with connection.execute_wrapper(overlap):
                    if action == "submit":
                        return client.post(
                            f"/api/v1/challenges/{self.challenge.pk}/submit",
                            {"flag": "MSG{correct_flag}"}, format="json",
                        )
                    return client.delete(f"/api/v1/admin/challenges/{self.challenge.pk}")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            submitting = pool.submit(request, "submit")
            deleting = pool.submit(request, "delete")
            submitted = submitting.result(timeout=20)
            removed = deleting.result(timeout=20)

        self.assertTrue(delete_waiting.is_set())
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self.assertEqual(removed.status_code, 409, removed.data)
        self.assertEqual(removed.data["code"], "CHALLENGE_IN_USE")
        self.challenge.refresh_from_db()
        self.team.refresh_from_db()
        solve = Solve.objects.get(challenge=self.challenge)
        self.assertEqual(solve.team_id, self.team.pk)
        self.assertEqual(solve.earned_score, earned_score)
        self.assertEqual(self.team.team_score, self.challenge.current_score)
        self.assertEqual(self.team.mileage, 30)
        self.assertEqual(
            list(MileageHistory.objects.values_list("type", "amount")),
            [(MileageType.CHALLENGE_SOLVE, 30)],
        )
        self.assertEqual(FlagSubmission.objects.get().result, FlagSubmission.SubmissionResult.CORRECT)
        self.assertEqual(
            TeamChallengeAccess.objects.get(team=self.team).status,
            TeamChallengeAccess.Status.CLEARED,
        )
