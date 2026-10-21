from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from time import monotonic, sleep
from unittest import skipUnless

from django.db import close_old_connections, connection, connections
from django.db.transaction import TransactionManagementError
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.challenge.services import get_team_total_score
from apps.signature import tests_concurrency as submit_tests

from .models import SignatureFlagSubmission, SignatureSolve, SignatureSubmissionLock
from .locks import lock_signature


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking")
@override_settings(SECURE_SSL_REDIRECT=False)
class ConcurrentSignatureAdminSubmitTests(TransactionTestCase):
    def setUp(self):
        submit_tests.ConcurrentSignatureSubmitTests.setUp(self)
        self.admin = User.objects.create_user(
            login_id="signature-race-admin", nickname="admin", role=Role.ADMIN,
        )
        SignatureSubmissionLock.objects.create(team=self.team, challenge=self.challenge)
        previous_team = Team.objects.create(team_name="signature-previous-solver")
        self.previous_solve = SignatureSolve.objects.create(
            team=previous_team, challenge=self.challenge, earned_score=300,
        )

    def overlap(self, payload, *, admin_first, late_submit=False):
        paused, proceed = Event(), Event()
        started = {"admin": Event(), "submit": Event()}
        pids = {}
        leader = "admin" if admin_first else "submit"
        follower = "submit" if admin_first else "admin"
        users = {"admin": self.admin, "submit": self.user}
        if late_submit:
            late_team = Team.objects.create(team_name="signature-late-solver")
            users["late"] = User.objects.create_user(
                login_id="signature-late-user", nickname="late", team=late_team,
            )
            started["late"] = Event()

        def request(kind):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '8s'")
                    cursor.execute("SET statement_timeout = '12s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[kind] = cursor.fetchone()[0]
                started[kind].set()
                client = APIClient()
                user = users[kind]
                client.force_authenticate(User.objects.get(pk=user.pk))
                held = False

                def hold_challenge_read(execute, sql, params, many, context):
                    nonlocal held
                    result = execute(sql, params, many, context)
                    if (
                        kind == leader and not held
                        and sql.lstrip().startswith("SELECT")
                        and 'FROM "signature_challenges"' in sql
                    ):
                        held = True
                        paused.set()
                        if not proceed.wait(10):
                            raise AssertionError("Challenge transaction was not released")
                    return result

                with connection.execute_wrapper(hold_challenge_read):
                    if kind == "admin":
                        url = f"/api/v1/admin/signatures/{self.challenge.pk}"
                        if "is_published" in payload:
                            url += "/publish"
                        response = client.patch(url, payload, format="json")
                    else:
                        response = client.post(
                            f"/api/v1/signatures/{self.challenge.pk}/submit",
                            {"flag": "MSG{signature_race}"}, format="json",
                        )
                return response.status_code, response.data
            finally:
                connections.close_all()

        def wait_for_blocker(kind, blocker, future):
            deadline = monotonic() + 4
            while monotonic() < deadline and not future.done():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_blocking_pids(%s)", [pids[kind]])
                    if pids[blocker] in cursor.fetchone()[0]:
                        return True
                sleep(0.01)
            return False

        with ThreadPoolExecutor(max_workers=3 if late_submit else 2) as pool:
            first = pool.submit(request, leader)
            try:
                self.assertTrue(paused.wait(10), "First challenge read was not reached")
                second = pool.submit(request, follower)
                self.assertTrue(started[follower].wait(10))
                self.assertTrue(
                    wait_for_blocker(follower, leader, second),
                    "Conflicting request did not wait for the challenge lock",
                )
                if late_submit:
                    third = pool.submit(request, "late")
                    self.assertTrue(started["late"].wait(10))
                    self.assertTrue(
                        wait_for_blocker("late", "admin", third),
                        "New submission bypassed a pending admin write",
                    )
            finally:
                proceed.set()
            results = {leader: first.result(timeout=15), follower: second.result(timeout=15)}
            if late_submit:
                status, body = third.result(timeout=15)
                if "is_published" in payload:
                    self.assertEqual(status, 404, body)
                    self.assertEqual(body["code"], "SIGNATURE_NOT_FOUND")
                    self.assertFalse(SignatureSolve.objects.filter(team=late_team).exists())
                else:
                    self.assertEqual(status, 200, body)
                    self.assertEqual(body["data"]["earned_score"], 600)
        self.assertEqual(results["admin"][0], 200, results["admin"])
        return results["submit"]

    def assert_final_score(self, response, submitted_score):
        status, body = response
        self.assertEqual(status, 200, body)
        self.assertEqual(body["code"], "SUCCESS")
        self.assertEqual(body["data"]["earned_score"], submitted_score)
        self.assertEqual(body["data"]["team_score"], submitted_score)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.score, Decimal("600"))
        self.assertEqual(SignatureSolve.objects.count(), 2)
        self.assertEqual(
            list(SignatureSolve.objects.values_list("earned_score", flat=True)),
            [Decimal("600"), Decimal("600")],
        )
        self.assertEqual(get_team_total_score(self.team.pk), Decimal("600"))
        self.assertEqual(
            SignatureFlagSubmission.objects.filter(
                team=self.team, result=SignatureFlagSubmission.SubmissionResult.CORRECT,
            ).count(),
            1,
        )

    def test_score_edit_waits_for_inflight_submit_and_updates_new_solve(self):
        response = self.overlap({"score": 600}, admin_first=False)
        self.assert_final_score(response, submitted_score=300)

    def test_submit_waits_for_score_edit_and_uses_latest_score(self):
        response = self.overlap({"score": 600}, admin_first=True)
        self.assert_final_score(response, submitted_score=600)

    def test_new_submit_waits_behind_pending_score_edit(self):
        self.overlap({"score": 600}, admin_first=False, late_submit=True)
        self.assertEqual(SignatureSolve.objects.count(), 3)
        self.assertFalse(SignatureSolve.objects.exclude(earned_score=600).exists())

    def test_new_submit_waits_behind_pending_unpublish(self):
        self.overlap({"is_published": False}, admin_first=False, late_submit=True)
        self.assertEqual(SignatureSolve.objects.count(), 2)

    def test_signature_lock_requires_transaction(self):
        with self.assertRaises(TransactionManagementError):
            lock_signature(self.challenge.pk)

    def test_unpublish_waits_for_inflight_submit(self):
        status, body = self.overlap({"is_published": False}, admin_first=False)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["code"], "SUCCESS")
        self.assertEqual(SignatureSolve.objects.filter(team=self.team).count(), 1)
        self.assertEqual(SignatureFlagSubmission.objects.filter(team=self.team).count(), 1)
        self.challenge.refresh_from_db()
        self.assertFalse(self.challenge.is_published)
        self.previous_solve.refresh_from_db()
        self.assertEqual(self.previous_solve.earned_score, Decimal("300"))

    def test_submit_waits_for_unpublish_and_creates_no_submission_records(self):
        status, body = self.overlap({"is_published": False}, admin_first=True)
        self.assertEqual(status, 404, body)
        self.assertEqual(body["code"], "SIGNATURE_NOT_FOUND")
        self.assertFalse(SignatureSolve.objects.filter(team=self.team).exists())
        self.assertFalse(SignatureFlagSubmission.objects.filter(team=self.team).exists())
        lock = SignatureSubmissionLock.objects.get(team=self.team, challenge=self.challenge)
        self.assertEqual(lock.failed_count, 0)
        self.assertIsNone(lock.locked_until)
        self.challenge.refresh_from_db()
        self.assertFalse(self.challenge.is_published)
        self.assertEqual(SignatureSolve.objects.count(), 1)
