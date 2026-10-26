import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.db import connection, connections, transaction
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.common.jwt import hash_token, issue_access_token
from apps.teams.models import MileageHistory, MileageType, PaymentToken, PaymentTokenStatus


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class PaymentMembershipFixture:
    def setUp(self):
        self.team = Team.objects.create(team_name="payment-A", mileage=100)
        self.other_team = Team.objects.create(team_name="payment-B", mileage=200)
        self.member = User.objects.create_user(login_id="member", password="qa-only", team=self.team)
        self.free = User.objects.create_user(login_id="free", password="qa-only")
        self.admin = User.objects.create_user(login_id="admin", password="qa-only", role=Role.ADMIN)
        self.member_access = issue_access_token(self.member)
        self.admin_access = issue_access_token(self.admin)
        self.admin_client = self.client_for(self.admin_access)
        for team in (self.team, self.other_team):
            MileageHistory.objects.create(team=team, type=MileageType.ADMIN_GRANT,
                amount=team.mileage, processed_by=self.admin.login_id)

    @staticmethod
    def client_for(access):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + access)
        return client

    def issue(self):
        response = self.client_for(self.member_access).post("/api/v1/teams/me/qr_token", {}, format="json")
        self.assertEqual(response.status_code, 200)
        return response.data["data"]["payment_token"]

    def update_members(self, team=None, **changes):
        return self.admin_client.patch(f"/api/v1/admin/teams/{(team or self.team).pk}",
            {**changes, "reason": "payment membership regression"}, format="json")

    def remove(self):
        response = self.update_members(remove_user_ids=[str(self.member.pk)])
        self.assertEqual(response.status_code, 200)

    def pay(self, raw, client=None):
        return (client or self.admin_client).post("/api/v1/admin/payment/checkout",
            {"payment_token": raw, "amount": 30, "item_name": "QA item"}, format="json")

    def assert_balances(self, expected_a=100, expected_b=200, purchases=0):
        for team, expected in ((self.team, expected_a), (self.other_team, expected_b)):
            team.refresh_from_db()
            self.assertEqual(team.mileage, expected)
            self.assertEqual(MileageHistory.objects.filter(team=team).aggregate(total=Sum("amount"))["total"], expected)
        self.assertEqual(MileageHistory.objects.filter(type=MileageType.PURCHASE).count(), purchases)

    def assert_invalid(self, response):
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "PAYMENT_TOKEN_INVALID")


@override_settings(CACHES=LOCMEM)
class PaymentMembershipTests(PaymentMembershipFixture, TestCase):
    def test_removed_member_old_qr_cannot_debit_previous_team(self):
        raw = self.issue()
        self.remove()
        self.assert_invalid(self.pay(raw))
        self.assertEqual(PaymentToken.objects.get(token_hash=hash_token(raw)).status, PaymentTokenStatus.INVALIDATED)
        self.assert_balances()

    def test_reassignment_does_not_transfer_or_keep_old_qr_authority(self):
        old = self.issue()
        self.remove()
        response = self.update_members(team=self.other_team, add_user_ids=[str(self.member.pk)])
        self.assertEqual(response.status_code, 200)
        self.assert_invalid(self.pay(old))
        fresh = self.issue()
        response = self.pay(fresh)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["team_id"], str(self.other_team.pk))
        self.assert_balances(expected_b=170, purchases=1)

    def test_returning_to_same_team_does_not_revive_old_qr(self):
        raw = self.issue()
        self.remove()
        response = self.update_members(add_user_ids=[str(self.member.pk)])
        self.assertEqual(response.status_code, 200)
        self.assert_invalid(self.pay(raw))
        self.assert_balances()

    def test_rejected_membership_change_keeps_qr_valid(self):
        raw = self.issue()
        response = self.update_members(remove_user_ids=[str(self.free.pk)])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pay(raw).status_code, 200)
        self.assert_balances(expected_a=70, purchases=1)

    def test_name_leader_and_member_addition_do_not_cancel_existing_qr(self):
        raw = self.issue()
        response = self.update_members(team_name="payment-renamed", leader_user_id=str(self.member.pk),
            add_user_ids=[str(self.free.pk)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pay(raw).status_code, 200)
        self.assert_balances(expected_a=70, purchases=1)

    def test_completed_purchase_and_used_token_survive_member_removal(self):
        raw = self.issue()
        self.assertEqual(self.pay(raw).status_code, 200)
        self.remove()
        self.assert_invalid(self.pay(raw))
        token = PaymentToken.objects.get(token_hash=hash_token(raw))
        self.assertEqual(token.status, PaymentTokenStatus.USED)
        self.assertEqual(token.history.amount, -30)
        self.assert_balances(expected_a=70, purchases=1)

    def test_authentication_snapshot_cannot_issue_after_member_removal(self):
        snapshot = User.objects.select_related("team").get(pk=self.member.pk)
        self.remove()
        client = APIClient()
        client.force_authenticate(user=snapshot)
        response = client.post("/api/v1/teams/me/qr_token", {}, format="json")
        self.assertIn(response.status_code, (400, 404))
        self.assertFalse(PaymentToken.objects.filter(team=self.team, status=PaymentTokenStatus.ACTIVE).exists())
        self.assert_balances()

    def test_qr_is_expired_at_exact_expiration(self):
        raw = self.issue()
        at = timezone.now().replace(microsecond=0)
        PaymentToken.objects.filter(token_hash=hash_token(raw)).update(expires_at=at)
        with patch("apps.adminpanel.views.timezone.now", return_value=at):
            response = self.pay(raw)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "PAYMENT_TOKEN_EXPIRED")
        self.assert_balances()


@override_settings(CACHES=LOCMEM)
class PaymentMembershipConcurrencyTests(PaymentMembershipFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(connection.vendor, "postgresql", "This regression requires real row locks")

    def worker(self, kind, name, raw=None):
        try:
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT set_config('application_name', %s, false)", [name])
            client = self.client_for(self.member_access if kind == "issue" else self.admin_access)
            if kind == "issue":
                return client.post("/api/v1/teams/me/qr_token", {}, format="json")
            if kind == "remove":
                return client.patch(f"/api/v1/admin/teams/{self.team.pk}",
                    {"remove_user_ids": [str(self.member.pk)], "reason": "queued membership regression"}, format="json")
            return self.pay(raw, client=client)
        finally:
            connections.close_all()

    def wait_for_lock(self, name):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_stat_clear_snapshot()")
                cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                    "AND application_name=%s AND wait_event_type='Lock'", [name])
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.01)
        self.fail("The intended request did not reach the owned team lock")

    def ordered(self, kinds, raw=None, hold_until=None):
        pending = []
        with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
            with transaction.atomic():
                Team.objects.select_for_update(no_key=True).get(pk=self.team.pk)
                for kind in kinds:
                    name = "payment-qa-" + uuid.uuid4().hex[:16]
                    pending.append(pool.submit(self.worker, kind, name, raw))
                    self.wait_for_lock(name)
                if hold_until is not None:
                    while timezone.now() <= hold_until:
                        time.sleep(0.01)
            return [future.result(timeout=10) for future in pending]

    def test_removal_commits_before_checkout_no_debit(self):
        raw = self.issue()
        remove, payment = self.ordered(["remove", "pay"], raw)
        self.assertEqual(remove.status_code, 200)
        self.assert_invalid(payment)
        self.assert_balances()

    def test_checkout_commits_before_removal_exactly_one_debit(self):
        raw = self.issue()
        first, remove, retry = self.ordered(["pay", "remove", "pay"], raw)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(remove.status_code, 200)
        self.assert_invalid(retry)
        self.assertEqual(PaymentToken.objects.get(token_hash=hash_token(raw)).status, PaymentTokenStatus.USED)
        self.assert_balances(expected_a=70, purchases=1)

    def test_qr_waiting_behind_removal_cannot_issue_for_old_team(self):
        remove, issue = self.ordered(["remove", "issue"])
        self.assertEqual(remove.status_code, 200)
        self.assertIn(issue.status_code, (400, 404))
        self.assertFalse(PaymentToken.objects.filter(team=self.team, status=PaymentTokenStatus.ACTIVE).exists())
        self.assert_balances()

    def test_qr_issued_before_removal_is_invalidated(self):
        issued, remove = self.ordered(["issue", "remove"])
        self.assertEqual(issued.status_code, 200)
        self.assertEqual(remove.status_code, 200)
        self.assert_invalid(self.pay(issued.data["data"]["payment_token"]))
        self.assert_balances()

    def test_expiration_is_checked_after_waiting_for_team_lock(self):
        raw = self.issue()
        expires = timezone.now() + timedelta(seconds=1)
        PaymentToken.objects.filter(token_hash=hash_token(raw)).update(expires_at=expires)
        response, = self.ordered(["pay"], raw, hold_until=expires + timedelta(milliseconds=100))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "PAYMENT_TOKEN_EXPIRED")
        self.assert_balances()
