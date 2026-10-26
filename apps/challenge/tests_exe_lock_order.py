import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, connections
from django.db.models.query import QuerySet
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.challenge.models import Challenge, ExeSubmission, FlagSubmission, Solve
from apps.challenge.tests_exe_submissions import ExeFixtures
from apps.teams.models import MileageHistory


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking")
class ExeLockOrderTests(ExeFixtures, TransactionTestCase):
    def flag(self):
        client = APIClient(raise_request_exception=False)
        client.force_authenticate(self.user)
        return client.post(
            f"/api/v1/challenges/{self.challenge.pk}/submit",
            {"flag": "local-exe-test"},
            format="json",
        )

    def exe(self):
        client = APIClient(raise_request_exception=False)
        client.force_authenticate(self.user)
        return self.post_exe(client=client)

    def admin_action(self, action):
        client = APIClient(raise_request_exception=False)
        client.force_authenticate(self.admin)
        if action == "remove":
            return client.patch(
                f"/api/v1/admin/teams/{self.team.pk}",
                {"remove_user_ids": [str(self.user.pk)], "reason": "동시성 검증"},
                format="json",
            )
        return client.post(
            f"/api/v1/admin/teams/{self.team.pk}/ban",
            {"ban_reason": "동시성 검증"},
            format="json",
        )

    def race(self, first_name, first, second_name, second, held_model, waiting_model):
        held = Event()
        waiting = Event()
        get = QuerySet.get

        def coordinate(queryset, *args, **kwargs):
            name = current_thread().name
            locking = queryset.query.select_for_update
            if locking and name.startswith(second_name) and queryset.model is waiting_model:
                waiting.set()
            value = get(queryset, *args, **kwargs)
            if locking and name.startswith(first_name) and queryset.model is held_model:
                held.set()
                if not waiting.wait(8):
                    raise AssertionError("Competing API did not reach its natural lock")
            return value

        def request(callback):
            close_old_connections()
            try:
                response = callback()
                return response.status_code, response.data
            finally:
                connections.close_all()

        with patch.object(QuerySet, "get", coordinate):
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix=first_name) as left:
                one = left.submit(request, first)
                self.assertTrue(held.wait(8), "First API did not acquire its natural lock")
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix=second_name) as right:
                    two = right.submit(request, second)
                    results = {first_name: one.result(timeout=25), second_name: two.result(timeout=25)}
        print("EXE_LOCK_ORDER " + json.dumps({"first": first_name, "results": results}, default=str))
        return results

    def assert_one_flag_score(self):
        self.assertEqual(Solve.objects.filter(team=self.team, challenge=self.challenge).count(), 1)
        self.assertEqual(FlagSubmission.objects.filter(result="CORRECT").count(), 1)
        self.assertEqual(MileageHistory.objects.filter(team=self.team).count(), 1)
        self.team.refresh_from_db()
        self.challenge.refresh_from_db()
        self.assertEqual(self.team.mileage, 30)
        self.assertEqual(self.team.team_score, self.challenge.current_score)

    def test_flag_first_serializes_exe_without_deadlock_or_payload(self):
        results = self.race("flag", self.flag, "exe", self.exe, Challenge, Challenge)
        self.assertEqual(results["flag"][0], 200, results)
        self.assertEqual((results["exe"][0], results["exe"][1]["code"]), (409, "ALREADY_SOLVED"), results)
        self.assert_one_flag_score()
        self.assertFalse(ExeSubmission.objects.exists())
        self.assertFalse(list(self.root.rglob("payload.exe")))

    def test_exe_first_allows_flag_to_finish_with_one_execution_and_one_score(self):
        results = self.race("exe", self.exe, "flag", self.flag, Challenge, Challenge)
        self.assertEqual(results["exe"][0], 202, results)
        self.assertEqual(results["flag"][0], 200, results)
        self.assert_one_flag_score()
        self.assertEqual(ExeSubmission.objects.count(), 1)
        self.assertEqual(len(list(self.root.rglob("payload.exe"))), 1)

    def test_member_removal_first_revalidates_membership_without_deadlock(self):
        self.admin = User.objects.create_user(login_id="lock-admin", role=Role.ADMIN)
        results = self.race("admin", lambda: self.admin_action("remove"), "exe", self.exe, Team, Team)
        self.assertEqual(results["admin"][0], 200, results)
        self.assertIn((results["exe"][0], results["exe"][1]["code"]), [(404, "USER_HAS_NO_TEAM"), (409, "TEAM_CHANGED")], results)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.team_id)
        self.assertFalse(ExeSubmission.objects.exists())
        self.assertFalse(list(self.root.rglob("payload.exe")))

    def test_ban_first_revalidates_ban_without_accepting_payload(self):
        self.admin = User.objects.create_user(login_id="lock-admin", role=Role.ADMIN)
        results = self.race("admin", lambda: self.admin_action("ban"), "exe", self.exe, Team, Team)
        self.assertEqual(results["admin"][0], 200, results)
        self.assertEqual((results["exe"][0], results["exe"][1]["code"]), (403, "TEAM_BANNED"), results)
        self.team.refresh_from_db()
        self.assertTrue(self.team.is_banned)
        self.assertFalse(ExeSubmission.objects.exists())
        self.assertFalse(list(self.root.rglob("payload.exe")))
