from decimal import Decimal
from unittest.mock import patch

from django.db import DatabaseError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.adminpanel.models import AdminEvent, ResourceAccountBilling
from apps.adminpanel.tests import LOCMEM, _target
from apps.common.jwt import issue_access_token
from apps.instances.services import SchedulerError

ACCOUNT_ID = _target()["account_id"]
BILLING = {
    "plan": "FREE_TRIAL",
    "currency": "USD",
    "credit_total": 300,
    "spent": 241.6,
    "hourly_cost": 9.4,
}


@override_settings(CACHES=LOCMEM)
class ResourceAccountBillingTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자", role=Role.ADMIN
        )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(admin)}")
        broker = patch(
            "apps.adminpanel.views.fetch_resource_targets",
            return_value=("2026-11-08T05:00:00Z", [_target()]),
        )
        self.broker = broker.start()
        self.addCleanup(broker.stop)

    def patch_account(self, body, account_id=ACCOUNT_ID):
        return self.client.patch(
            f"/api/v1/admin/resources/accounts/{account_id}", body, format="json"
        )

    def test_saves_billing_and_shows_it_in_resources(self):
        res = self.patch_account({"billing": BILLING, "reason": "크레딧 소진 예상"})

        self.assertEqual(res.status_code, 200, res.data)
        billing = res.data["data"]["billing"]
        self.assertEqual(
            {k: billing[k] for k in BILLING},
            BILLING,
        )
        self.assertIsNotNone(billing["observed_at"])
        [event] = AdminEvent.objects.filter(
            type=AdminEvent.EventType.RESOURCE_ACCOUNT_UPDATED
        )
        self.assertIn("크레딧 소진 예상", event.message)

        account = self.client.get("/api/v1/admin/resources").data["data"]["accounts"][0]
        self.assertEqual(account["billing"]["plan"], "FREE_TRIAL")
        self.assertEqual(account["billing"]["spent"], 241.6)

    def test_resources_show_null_billing_until_saved(self):
        account = self.client.get("/api/v1/admin/resources").data["data"]["accounts"][0]
        self.assertIsNone(account["billing"])

    def test_second_save_replaces_billing(self):
        self.patch_account({"billing": BILLING, "reason": "처음"})
        res = self.patch_account(
            {"billing": {**BILLING, "plan": "BUDGET", "spent": 0}, "reason": "다시"}
        )

        self.assertEqual(res.status_code, 200)
        row = ResourceAccountBilling.objects.get(pk=ACCOUNT_ID)
        self.assertEqual((row.plan, row.spent), ("BUDGET", 0))

    def test_scheduling_switch_is_not_supported_yet(self):
        res = self.patch_account(
            {"scheduling_enabled": False, "billing": BILLING, "reason": "중지"}
        )

        self.assertEqual(res.status_code, 400)
        self.assertIn("scheduling_enabled", res.data["message"])
        self.assertFalse(ResourceAccountBilling.objects.exists())

    def test_unknown_account_is_404(self):
        res = self.patch_account({"billing": BILLING, "reason": "x"}, account_id="nope")

        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "ACCOUNT_NOT_FOUND")

    def test_invalid_input_is_rejected_without_change(self):
        cases = [
            {"billing": BILLING},
            {"reason": "x"},
            {"billing": {**BILLING, "plan": "FREE"}, "reason": "x"},
            {"billing": {**BILLING, "currency": "EUR"}, "reason": "x"},
            {"billing": {**BILLING, "spent": -1}, "reason": "x"},
            {"billing": {**BILLING, "spent": "10"}, "reason": "x"},
            {"billing": {**BILLING, "spent": True}, "reason": "x"},
            {"billing": {**BILLING, "hourly_cost": 1e13}, "reason": "x"},
            {"billing": {"plan": "CREDIT"}, "reason": "x"},
            {"billing": BILLING, "reason": "x", "extra": 1},
        ]
        for body in cases:
            with self.subTest(body=body):
                res = self.patch_account(body)
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")
        self.assertFalse(ResourceAccountBilling.objects.exists())
        self.assertFalse(
            AdminEvent.objects.filter(
                type=AdminEvent.EventType.RESOURCE_ACCOUNT_UPDATED
            ).exists()
        )

    def test_oversized_finite_amounts_are_rejected_before_broker_or_writes(self):
        saved = self.patch_account({"billing": BILLING, "reason": "기존 요금"})
        self.assertEqual(saved.status_code, 200, saved.data)
        previous = ResourceAccountBilling.objects.values().get(pk=ACCOUNT_ID)
        previous_events = list(AdminEvent.objects.values())
        self.broker.reset_mock()

        for field in ("credit_total", "spent", "hourly_cost"):
            for value in (1e308, 10**26):
                with self.subTest(field=field, value=value):
                    with CaptureQueriesContext(connection) as queries:
                        response = self.patch_account(
                            {
                                "billing": {**BILLING, field: value},
                                "reason": "범위 밖 입력",
                            }
                        )
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertEqual(response.data["code"], "INVALID_REQUEST")
                    self.assertFalse(
                        any(
                            query["sql"].lstrip().upper().startswith(
                                ("INSERT", "UPDATE", "DELETE")
                            )
                            for query in queries.captured_queries
                        )
                    )
                    self.broker.assert_not_called()

        self.assertEqual(
            ResourceAccountBilling.objects.values().get(pk=ACCOUNT_ID), previous
        )
        self.assertEqual(list(AdminEvent.objects.values()), previous_events)

    def test_original_amount_range_is_checked_before_rounding(self):
        for field in ("credit_total", "spent", "hourly_cost"):
            for value in (-0.001, 999999999999.991):
                with self.subTest(field=field, value=value):
                    response = self.patch_account(
                        {
                            "billing": {**BILLING, field: value},
                            "reason": "반올림 전 범위 검사",
                        }
                    )
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertEqual(response.data["code"], "INVALID_REQUEST")

        self.broker.assert_not_called()
        self.assertFalse(ResourceAccountBilling.objects.exists())
        self.assertFalse(AdminEvent.objects.exists())

    def test_boundary_amounts_keep_two_decimal_half_up_rounding(self):
        for value, expected in (
            (0, Decimal("0.00")),
            (0.004, Decimal("0.00")),
            (0.005, Decimal("0.01")),
            (1.235, Decimal("1.24")),
            (999999999999.99, Decimal("999999999999.99")),
        ):
            with self.subTest(value=value):
                response = self.patch_account(
                    {
                        "billing": {
                            **BILLING,
                            "credit_total": value,
                            "spent": value,
                            "hourly_cost": value,
                        },
                        "reason": "허용 범위와 반올림",
                    }
                )
                self.assertEqual(response.status_code, 200, response.data)
                row = ResourceAccountBilling.objects.get(pk=ACCOUNT_ID)
                for field in ("credit_total", "spent", "hourly_cost"):
                    self.assertEqual(getattr(row, field), expected)

    def test_broker_failure_preserves_existing_billing_and_audit(self):
        saved = self.patch_account({"billing": BILLING, "reason": "기존 요금"})
        self.assertEqual(saved.status_code, 200, saved.data)
        previous = ResourceAccountBilling.objects.values().get(pk=ACCOUNT_ID)
        previous_events = list(AdminEvent.objects.values())
        self.broker.side_effect = SchedulerError(
            "SCHEDULER_UNAVAILABLE", "Local review fixture failure", 503
        )

        response = self.patch_account(
            {"billing": {**BILLING, "spent": 0}, "reason": "갱신 요청"}
        )

        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(
            ResourceAccountBilling.objects.values().get(pk=ACCOUNT_ID), previous
        )
        self.assertEqual(list(AdminEvent.objects.values()), previous_events)

    def test_audit_failure_rolls_back_billing_creation_and_update(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    saved = self.patch_account(
                        {"billing": BILLING, "reason": "기존 요금"}
                    )
                    self.assertEqual(saved.status_code, 200, saved.data)
                previous = list(ResourceAccountBilling.objects.values())
                previous_events = list(AdminEvent.objects.values())

                with patch(
                    "apps.adminpanel.views._record_event",
                    side_effect=DatabaseError("Local review fixture failure"),
                ):
                    response = self.patch_account(
                        {"billing": {**BILLING, "spent": 0}, "reason": "갱신 요청"}
                    )

                self.assertEqual(response.status_code, 500, response.data)
                self.assertEqual(list(ResourceAccountBilling.objects.values()), previous)
                self.assertEqual(list(AdminEvent.objects.values()), previous_events)
