import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.instances.models import DeleteReason, Instance, InstanceStatus
from apps.instances.reconciliation import reconcile_instances_once
from apps.instances.services import (
    ACTIVE_INSTANCE_STATUSES,
    SchedulerError,
    get_active_instance,
    sync_instance_from_scheduler,
    update_instance_from_scheduler,
)
from apps.instances.tests_reconciliation import Fixtures, LOCMEM


@override_settings(CACHES=LOCMEM, SCHEDULER_API_TOKEN="test-history-token")
class FailedCleanupHistoryTests(Fixtures, TestCase):
    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_failed_cleanup_can_resume_fail_again_and_finish(self, detail):
        row = self.instance(status=InstanceStatus.FAILED)
        before = Instance.objects.values().get(pk=row.pk)
        for state in (InstanceStatus.CLEANUP_PENDING, InstanceStatus.FAILED, InstanceStatus.CLEANED):
            detail.return_value = self.snapshot(row, state)
            self.assertEqual(reconcile_instances_once()["updated"], 1)
            row.refresh_from_db()
            self.assertEqual(row.status, state)
            for field in ("delete_reason", "release_id", "host", "endpoints", "expires_at", "extend_count"):
                self.assertEqual(getattr(row, field), before[field])
        self.assertEqual(reconcile_instances_once()["checked"], 0)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_missed_intermediate_state_and_missing_local_reason_do_not_block_cleanup(self, detail):
        for reason in (None, DeleteReason.USER_REQUESTED):
            row = self.instance(status=InstanceStatus.FAILED, delete_reason=reason)
            detail.return_value = {**self.snapshot(row), "delete_reason": "USER_REQUESTED"}
            self.assertEqual(reconcile_instances_once()["updated"], 1)
            row.refresh_from_db()
            self.assertEqual((row.status, row.delete_reason), (InstanceStatus.CLEANED, DeleteReason.USER_REQUESTED))

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_failed_cannot_regress_or_accept_foreign_identity(self, detail):
        row = self.instance(status=InstanceStatus.FAILED)
        before = Instance.objects.values().get(pk=row.pk)
        invalid = [self.snapshot(row, state) for state in (
            *ACTIVE_INSTANCE_STATUSES, InstanceStatus.STOPPING, InstanceStatus.STOPPED,
            InstanceStatus.EXPIRED, "UNKNOWN", None,
        )]
        invalid.extend({**self.snapshot(row), field: str(uuid.uuid4())}
                       for field in ("instance_id", "team_id", "challenge_id", "user_id"))
        for data in invalid:
            with self.subTest(data=data):
                detail.return_value = data
                self.assertEqual(reconcile_instances_once()["error"], 1)
                self.assertEqual(Instance.objects.values().get(pk=row.pk), before)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_failed_missing_or_unavailable_is_not_cleanup_evidence(self, detail):
        row = self.instance(status=InstanceStatus.FAILED)
        for code, status, counter in (("INSTANCE_NOT_FOUND", 404, "missing"),
                                      ("SCHEDULER_UNAVAILABLE", 503, "error")):
            detail.side_effect = SchedulerError(code, "unavailable", status)
            self.assertEqual(reconcile_instances_once()[counter], 1)
            row.refresh_from_db()
            self.assertEqual(row.status, InstanceStatus.FAILED)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_unchanged_failed_record_does_not_starve_following_cleanup(self, detail):
        first = self.instance(status=InstanceStatus.FAILED, created_at=timezone.now() - timedelta(seconds=1))
        second = self.instance()
        detail.side_effect = lambda row, _: self.snapshot(
            row, InstanceStatus.FAILED if row.pk == first.pk else InstanceStatus.CLEANED,
        )
        self.assertEqual(reconcile_instances_once(batch_size=1)["pending"], 1)
        self.assertEqual(reconcile_instances_once(batch_size=1)["updated"], 1)
        second.refresh_from_db()
        self.assertEqual(second.status, InstanceStatus.CLEANED)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_late_failed_snapshot_cannot_overwrite_completed_cleanup(self, detail):
        row = self.instance(status=InstanceStatus.FAILED)

        def response(snapshot, _):
            Instance.objects.filter(pk=row.pk).update(status=InstanceStatus.CLEANED)
            return self.snapshot(snapshot, InstanceStatus.CLEANUP_PENDING)

        detail.side_effect = response
        self.assertEqual(reconcile_instances_once()["skipped"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, InstanceStatus.CLEANED)

    @patch("apps.instances.services.call_scheduler_detail")
    def test_detail_sync_observes_failed_cleanup_but_never_revives_it(self, detail):
        row = self.instance(status=InstanceStatus.FAILED)
        for state in (InstanceStatus.RUNNING, InstanceStatus.STOPPING, InstanceStatus.EXPIRED):
            detail.return_value = self.snapshot(row, state)
            with self.assertRaises(SchedulerError):
                sync_instance_from_scheduler(row)
            row.refresh_from_db()
            self.assertEqual(row.status, InstanceStatus.FAILED)
        detail.return_value = self.snapshot(row, InstanceStatus.CLEANUP_PENDING)
        sync_instance_from_scheduler(row)
        row.refresh_from_db()
        self.assertEqual(row.status, InstanceStatus.CLEANUP_PENDING)
        detail.return_value = self.snapshot(row)
        sync_instance_from_scheduler(row)
        row.refresh_from_db()
        self.assertEqual(row.status, InstanceStatus.CLEANED)

    def test_direct_updates_reject_failed_regression_before_mutation(self):
        row = self.instance(status=InstanceStatus.FAILED, delete_reason=None)
        with self.assertRaises(SchedulerError):
            update_instance_from_scheduler(row, {"status": "STOPPED", "delete_reason": "USER_REQUESTED"})
        row.refresh_from_db()
        self.assertEqual((row.status, row.delete_reason), (InstanceStatus.FAILED, None))

    def test_failed_instances_remain_outside_active_limit(self):
        self.instance(status=InstanceStatus.FAILED)
        self.assertNotIn(InstanceStatus.FAILED, ACTIVE_INSTANCE_STATUSES)
        self.assertIsNone(get_active_instance(self.user))
