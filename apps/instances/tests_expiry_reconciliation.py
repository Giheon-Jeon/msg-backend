import uuid
from unittest.mock import patch
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from apps.instances.models import DeleteReason, Instance, InstanceStatus
from apps.instances.reconciliation import reconcile_instances_once
from apps.instances.services import SchedulerError, update_instance_from_scheduler
from apps.instances.tests_reconciliation import Fixtures, LOCMEM

@override_settings(CACHES=LOCMEM, SCHEDULER_API_TOKEN="test-history-token")
class ExpiryHistoryTests(Fixtures, TestCase):
    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_natural_expiry_updates_running_and_unpolled_creation_without_participant_get(self, detail):
        ttl = self.instance(status=InstanceStatus.RUNNING, delete_reason=None)
        hard = self.instance(status=InstanceStatus.REQUESTED, delete_reason=None)
        before = {row.pk: Instance.objects.values().get(pk=row.pk) for row in (ttl, hard)}
        detail.side_effect = lambda row, _: {
            **self.snapshot(row), "delete_reason": "TTL_EXPIRED" if row.pk == ttl.pk else "HARD_TIMEOUT_EXPIRED",
        }
        summary = reconcile_instances_once()
        self.assertEqual(summary["updated"], 2)
        for row, reason in ((ttl, DeleteReason.TTL_EXPIRED), (hard, DeleteReason.HARD_TIMEOUT)):
            row.refresh_from_db()
            self.assertEqual((row.status, row.delete_reason), (InstanceStatus.CLEANED, reason))
            for field in ("expires_at", "hard_expires_at", "release_id", "endpoints", "extend_count"):
                self.assertEqual(getattr(row, field), before[row.pk][field])

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_healthy_active_rows_are_observed_without_overwriting_runtime_snapshot(self, detail):
        for local in (InstanceStatus.REQUESTED, InstanceStatus.SCHEDULING, InstanceStatus.PROVISIONING,
                      InstanceStatus.RUNNING, InstanceStatus.RESTARTING, InstanceStatus.RESETTING):
            row = self.instance(status=local, delete_reason=None)
            before = Instance.objects.values().get(pk=row.pk)
            detail.side_effect = lambda item, _: {**self.snapshot(item, InstanceStatus.RUNNING),
                "expires_at": "2099-01-01T00:00:00Z", "endpoints": [{"service_url": "bad"}]}
            self.assertEqual(reconcile_instances_once()["error"], 0)
            self.assertEqual(Instance.objects.values().get(pk=row.pk), before)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_cleanup_pending_and_stopped_continue_until_final_cleanup(self, detail):
        row = self.instance(status=InstanceStatus.RUNNING, delete_reason=None)
        for state in (InstanceStatus.EXPIRED, InstanceStatus.CLEANUP_PENDING, InstanceStatus.CLEANED):
            detail.return_value = {**self.snapshot(row, state), "delete_reason": "TTL_EXPIRED"}
            self.assertEqual(reconcile_instances_once()["updated"], 1)
            row.refresh_from_db()
            self.assertEqual((row.status, row.delete_reason), (state, DeleteReason.TTL_EXPIRED))
        stopped = self.instance(status=InstanceStatus.STOPPED, delete_reason=DeleteReason.USER_REQUESTED)
        detail.return_value = self.snapshot(stopped)
        self.assertEqual(reconcile_instances_once()["updated"], 1)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_early_creation_hard_timeout_preserves_failed_outcome(self, detail):
        row = self.instance(status=InstanceStatus.SCHEDULING, delete_reason=None)
        detail.return_value = {**self.snapshot(row, InstanceStatus.FAILED), "delete_reason": "HARD_TIMEOUT_EXPIRED"}
        reconcile_instances_once()
        row.refresh_from_db()
        self.assertEqual((row.status, row.delete_reason), (InstanceStatus.FAILED, DeleteReason.HARD_TIMEOUT))

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_rejects_unknown_reason_or_foreign_scope_without_changing_active_history(self, detail):
        row = self.instance(status=InstanceStatus.RUNNING, delete_reason=None)
        before = Instance.objects.values().get(pk=row.pk)
        for extra in ({"delete_reason": "FUTURE_REASON"}, {"delete_reason": []},
                      {"delete_reason": "TTL_EXPIRED", "team_id": str(uuid.uuid4())},
                      {"delete_reason": "TTL_EXPIRED", "status": "RUNNING"}):
            detail.return_value = {**self.snapshot(row), **extra}
            self.assertEqual(reconcile_instances_once()["error"], 1)
            self.assertEqual(Instance.objects.values().get(pk=row.pk), before)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_does_not_infer_reason_from_local_expiry_or_legacy_response(self, detail):
        row = self.instance(status=InstanceStatus.RUNNING, delete_reason=None, expires_at=timezone.now())
        detail.return_value = self.snapshot(row)
        reconcile_instances_once()
        row.refresh_from_db()
        self.assertEqual((row.status, row.delete_reason), (InstanceStatus.CLEANED, None))

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_explicit_historical_scope_repairs_missing_reason_without_polling_all_final_rows(self, detail):
        row = self.instance(status=InstanceStatus.CLEANED, delete_reason=None)
        other = self.instance(status=InstanceStatus.FAILED, delete_reason=None)
        detail.side_effect = lambda item, _: {
            **self.snapshot(item, item.status),
            "delete_reason": "TTL_EXPIRED" if item.pk == row.pk else None,
        }
        self.assertEqual(reconcile_instances_once()["checked"], 1)
        self.assertEqual(reconcile_instances_once(instance_ids=[row.pk])["updated"], 1)
        row.refresh_from_db();other.refresh_from_db()
        self.assertEqual(row.delete_reason, DeleteReason.TTL_EXPIRED)
        self.assertIsNone(other.delete_reason)
        self.assertEqual(detail.call_count, 2)

    def test_participant_detail_path_also_copies_reason_without_overwriting_explicit_delete_reason(self):
        row = self.instance(status=InstanceStatus.RUNNING, delete_reason=None)
        update_instance_from_scheduler(row, {**self.snapshot(row), "delete_reason": "HARD_TIMEOUT_EXPIRED"})
        row.refresh_from_db()
        self.assertEqual((row.status, row.delete_reason), (InstanceStatus.CLEANED, DeleteReason.HARD_TIMEOUT))
        row.delete_reason=DeleteReason.REPLACED_BY_NEW_INSTANCE;row.save()
        update_instance_from_scheduler(row, {**self.snapshot(row), "delete_reason": "USER_REQUESTED"})
        row.refresh_from_db()
        self.assertEqual(row.delete_reason, DeleteReason.REPLACED_BY_NEW_INSTANCE)

    def test_participant_detail_rejects_unknown_reason_before_saving(self):
        row = self.instance(status=InstanceStatus.RUNNING, delete_reason=None)
        with self.assertRaises(SchedulerError):
            update_instance_from_scheduler(row, {**self.snapshot(row), "delete_reason": "private-invalid"})
        row.refresh_from_db()
        self.assertEqual((row.status, row.delete_reason), (InstanceStatus.RUNNING, None))
