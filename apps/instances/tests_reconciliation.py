import io
import json
import threading
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge
from apps.instances.models import ChallengeRelease, DeleteReason, Instance, InstanceStatus
from apps.instances.reconciliation import _cursor_key, reconcile_instances_once
from apps.instances.services import SchedulerError

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class Fixtures:
    def setUp(self):
        cache.clear()
        self.team = Team.objects.create(team_name="history-team")
        self.user = User.objects.create_user(login_id="history-user", nickname="history", team=self.team)
        self.challenge = Challenge.objects.create(
            title="History", category="PWN", difficulty="EASY", score=100, flag_hash="test",
        )

    def instance(self, **values):
        created_at = values.pop("created_at", None)
        defaults = dict(
            user=self.user, team=self.team, challenge=self.challenge,
            status=InstanceStatus.STOPPING, delete_reason=DeleteReason.REPLACED_BY_NEW_INSTANCE,
            host="tcp://original.example:31337", endpoints=[{"port": 31337}],
            expires_at=timezone.now(), extend_count=2,
        )
        defaults.update(values)
        instance = Instance.objects.create(**defaults)
        if created_at is not None:
            # auto_now_add ignores values passed to create(); persist explicit fixture order.
            instance.created_at = created_at
            instance.save(update_fields=["created_at"])
        return instance

    def snapshot(self, instance, status=InstanceStatus.CLEANED):
        return {
            "instance_id": str(instance.pk),
            "team_id": str(instance.team_id), "challenge_id": str(instance.challenge_id),
            "release_id": str(instance.release_id) if instance.release_id else None,
            "status": status,
        }


@override_settings(CACHES=LOCMEM, SCHEDULER_API_TOKEN="test-history-token")
class InstanceHistoryTests(Fixtures, TestCase):
    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_release_identity_must_match_and_terminal_rows_are_not_polled(self, detail):
        release = ChallengeRelease.objects.create(
            challenge=self.challenge, version=1, registry_revision=1,
            cpu_millicores=500, memory_mib=256, ephemeral_storage_mib=512,
        )
        row = self.instance(release=release)
        untouched = [
            self.instance(status=status)
            for status in (InstanceStatus.CLEANED, InstanceStatus.FAILED)
        ]
        for value in (None, str(uuid.uuid4())):
            detail.return_value = {**self.snapshot(row), "release_id": value}
            self.assertEqual(reconcile_instances_once()["error"], 1)
            row.refresh_from_db()
            self.assertEqual(row.status, InstanceStatus.STOPPING)
        self.assertEqual(detail.call_count, 2)
        self.assertEqual([Instance.objects.get(pk=i.pk).status for i in untouched], [i.status for i in untouched])

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_reconciles_replaced_history_while_new_instance_is_running(self, detail):
        old = self.instance(created_at=timezone.now() - timedelta(seconds=1))
        current = self.instance(status=InstanceStatus.RUNNING, replaced_instance=old)
        detail.side_effect = lambda row, _: self.snapshot(row, row.status if row.pk == current.pk else InstanceStatus.CLEANED)
        before = Instance.objects.values().get(pk=old.pk)

        summary = reconcile_instances_once()

        old.refresh_from_db()
        current.refresh_from_db()
        self.assertEqual((summary["checked"], summary["updated"], summary["pending"]), (2, 1, 1))
        self.assertEqual(old.status, InstanceStatus.CLEANED)
        self.assertEqual(current.status, InstanceStatus.RUNNING)
        for field in ("host", "endpoints", "expires_at", "extend_count", "delete_reason"):
            self.assertEqual(getattr(old, field), before[field])
        self.assertEqual(detail.call_count, 2)
        self.assertEqual(detail.call_args_list[0].args, (old, "Bearer test-history-token"))

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_reconciles_all_pending_history_without_active_instance_or_http_polling(self, detail):
        rows = [self.instance(), self.instance(delete_reason=DeleteReason.USER_REQUESTED)]
        other = User.objects.create_user(login_id="other-history-user", nickname="other", team=self.team)
        rows.append(self.instance(user=other, status=InstanceStatus.CLEANUP_PENDING))
        detail.side_effect = lambda row, _: self.snapshot(row)
        summary = reconcile_instances_once()
        self.assertEqual(summary["updated"], 3)
        self.assertFalse(Instance.objects.exclude(status=InstanceStatus.CLEANED).exists())

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_missing_scheduler_record_is_not_cleanup_evidence_and_does_not_starve_next_row(self, detail):
        missing = self.instance(created_at=timezone.now() - timedelta(seconds=1))
        healthy = self.instance()
        detail.side_effect = lambda row, _: (
            self.snapshot(row) if row.pk == healthy.pk
            else (_ for _ in ()).throw(SchedulerError("INSTANCE_NOT_FOUND", "missing", 404))
        )
        first = reconcile_instances_once(batch_size=1)
        second = reconcile_instances_once(batch_size=1)
        missing.refresh_from_db()
        healthy.refresh_from_db()
        self.assertEqual(first["missing"], 1)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(missing.status, InstanceStatus.STOPPING)
        self.assertEqual(healthy.status, InstanceStatus.CLEANED)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_timeout_keeps_state_and_next_round_continues_from_redis_cursor(self, detail):
        unavailable = self.instance(created_at=timezone.now() - timedelta(seconds=1))
        healthy = self.instance()
        detail.side_effect = SchedulerError("SCHEDULER_UNAVAILABLE", "timeout", 503)
        first = reconcile_instances_once()
        self.assertEqual((first["checked"], first["error"]), (1, 1))
        unavailable.refresh_from_db()
        self.assertEqual(unavailable.status, InstanceStatus.STOPPING)
        detail.side_effect = lambda row, _: self.snapshot(row)
        second = reconcile_instances_once()
        self.assertEqual(second["updated"], 1)
        healthy.refresh_from_db()
        self.assertEqual(healthy.status, InstanceStatus.CLEANED)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_rejects_missing_or_foreign_identity_without_mutating_history(self, detail):
        row = self.instance()
        before = row.updated_at
        for field in ("instance_id", "user_id", "team_id", "challenge_id"):
            for value in (None, str(uuid.uuid4())):
                with self.subTest(field=field, value=value):
                    detail.return_value = {**self.snapshot(row), field: value}
                    summary = reconcile_instances_once()
                    row.refresh_from_db()
                    self.assertEqual(summary["error"], 1)
                    self.assertEqual((row.status, row.updated_at), (InstanceStatus.STOPPING, before))

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_invalid_response_does_not_prevent_healthy_row_from_updating(self, detail):
        invalid = self.instance()
        healthy = self.instance()
        detail.side_effect = lambda row, _: (
            {**self.snapshot(row), "team_id": str(uuid.uuid4())}
            if row.pk == invalid.pk else self.snapshot(row)
        )
        summary = reconcile_instances_once()
        self.assertEqual((summary["error"], summary["updated"]), (1, 1))

    @override_settings(SCHEDULER_BASE_URL="https://scheduler.example")
    @patch("apps.instances.services.urlopen")
    def test_malformed_http_payload_does_not_starve_healthy_history(self, urlopen):
        for payload in (b"private-invalid-json", b"[]", b'{"data": []}', b"\xff"):
            with self.subTest(payload=payload):
                Instance.objects.all().delete()
                cache.clear()
                invalid = self.instance(created_at=timezone.now() - timedelta(seconds=1))
                healthy = self.instance()
                urlopen.side_effect = [
                    io.BytesIO(payload),
                    io.BytesIO(json.dumps({"data": self.snapshot(healthy)}).encode()),
                ]
                with self.assertLogs("apps.instances.reconciliation", level="WARNING") as logs:
                    summary = reconcile_instances_once()
                self.assertEqual((summary["error"], summary["updated"]), (1, 1))
                self.assertNotIn("private-invalid-json", "\n".join(logs.output))
                invalid.refresh_from_db()
                healthy.refresh_from_db()
                self.assertEqual(invalid.status, InstanceStatus.STOPPING)
                self.assertEqual(healthy.status, InstanceStatus.CLEANED)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_pending_state_advances_only_from_scheduler_and_failed_status_is_preserved(self, detail):
        row = self.instance()
        detail.return_value = self.snapshot(row, InstanceStatus.CLEANUP_PENDING)
        self.assertEqual(reconcile_instances_once()["updated"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, InstanceStatus.CLEANUP_PENDING)
        detail.return_value = self.snapshot(row, InstanceStatus.FAILED)
        self.assertEqual(reconcile_instances_once()["updated"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, InstanceStatus.FAILED)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_rejects_unknown_or_regressive_status(self, detail):
        row = self.instance()
        for status in ("RUNNING", "REQUESTED", "NEW_STATUS", None):
            with self.subTest(status=status):
                detail.return_value = self.snapshot(row, status)
                self.assertEqual(reconcile_instances_once()["error"], 1)
                row.refresh_from_db()
                self.assertEqual(row.status, InstanceStatus.STOPPING)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_skips_response_after_row_was_changed_or_deleted_during_lookup(self, detail):
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                row = self.instance()
                def response(snapshot, _):
                    if deleted:
                        Instance.objects.filter(pk=snapshot.pk).delete()
                    else:
                        Instance.objects.filter(pk=snapshot.pk).update(status=InstanceStatus.CLEANED, updated_at=timezone.now())
                    return self.snapshot(snapshot, InstanceStatus.FAILED)
                detail.side_effect = response
                self.assertEqual(reconcile_instances_once(instance_ids=[row.pk])["skipped"], 1)
                if not deleted:
                    row.refresh_from_db()
                    self.assertEqual(row.status, InstanceStatus.CLEANED)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_optional_response_fields_cannot_overwrite_runtime_settings_or_delete_reason(self, detail):
        row = self.instance()
        detail.return_value = {
            **self.snapshot(row), "host": "injected", "service_url": None,
            "expires_at": [], "delete_reason": "ADMIN_FORCED", "extend_count": 999,
        }
        self.assertEqual(reconcile_instances_once()["updated"], 1)
        row.refresh_from_db()
        self.assertEqual(row.delete_reason, DeleteReason.REPLACED_BY_NEW_INSTANCE)
        self.assertEqual(row.host, "tcp://original.example:31337")
        self.assertEqual(row.extend_count, 2)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_time_budget_leaves_unchecked_rows_for_next_round(self, detail):
        first = self.instance(created_at=timezone.now() - timedelta(seconds=1))
        second = self.instance()
        detail.side_effect = lambda row, _: self.snapshot(row)
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 21]))
        with patch("apps.instances.reconciliation.time", clock):
            summary = reconcile_instances_once(max_seconds=20)
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(reconcile_instances_once()["updated"], 1)
        second.refresh_from_db()
        self.assertEqual(second.status, InstanceStatus.CLEANED)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_equal_creation_times_use_uuid_order_without_skipping_history(self, detail):
        created_at = timezone.now()
        high = self.instance(instance_id=uuid.UUID(int=2), created_at=created_at)
        low = self.instance(instance_id=uuid.UUID(int=1), created_at=created_at)
        detail.side_effect = lambda row, _: self.snapshot(row)

        self.assertEqual(reconcile_instances_once(batch_size=1)["updated"], 1)
        high.refresh_from_db()
        low.refresh_from_db()
        self.assertEqual(high.status, InstanceStatus.STOPPING)
        self.assertEqual(low.status, InstanceStatus.CLEANED)
        self.assertEqual(reconcile_instances_once(batch_size=1)["updated"], 1)
        high.refresh_from_db()
        self.assertEqual(high.status, InstanceStatus.CLEANED)
        self.assertEqual([call.args[0].pk for call in detail.call_args_list], [low.pk, high.pk])

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_corrupt_cursor_recovers_and_scoped_runs_do_not_change_global_cursor(self, detail):
        row = self.instance()
        cache.set(_cursor_key(()), ["not-a-date", "bad-id"])
        detail.return_value = self.snapshot(row)
        self.assertEqual(reconcile_instances_once(instance_ids=[row.pk])["updated"], 1)
        self.assertEqual(cache.get(_cursor_key(())), ["not-a-date", "bad-id"])
        Instance.objects.filter(pk=row.pk).update(status=InstanceStatus.STOPPING)
        self.assertEqual(reconcile_instances_once()["updated"], 1)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_cache_failure_releases_global_lock_without_changing_history(self, detail):
        row = self.instance()
        with patch("apps.instances.reconciliation.cache.get", side_effect=RuntimeError("cache unavailable")):
            with self.assertRaises(RuntimeError):
                reconcile_instances_once()
        detail.assert_not_called()
        detail.return_value = self.snapshot(row)
        self.assertEqual(reconcile_instances_once()["updated"], 1)

    @override_settings(SCHEDULER_API_TOKEN="")
    def test_missing_service_token_keeps_history_unchanged(self):
        row = self.instance()
        with self.assertRaises(SchedulerError):
            reconcile_instances_once()
        row.refresh_from_db()
        self.assertEqual(row.status, InstanceStatus.STOPPING)


@override_settings(CACHES=LOCMEM, SCHEDULER_API_TOKEN="test-history-token")
class InstanceHistoryConcurrencyTests(Fixtures, TransactionTestCase):
    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_global_lock_excludes_second_worker_and_http_does_not_hold_row_lock(self, detail):
        row = self.instance()
        entered = threading.Event()
        release = threading.Event()
        errors = []
        result = []
        def response(snapshot, _):
            self.assertFalse(connection.in_atomic_block)
            entered.set()
            if not release.wait(10):
                raise AssertionError("lookup not released")
            return self.snapshot(snapshot, InstanceStatus.FAILED)
        detail.side_effect = response
        def run():
            close_old_connections()
            try: result.append(reconcile_instances_once())
            except Exception as error: errors.append(error)
            finally: connections.close_all()
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(reconcile_instances_once()["busy"])
            # 조회가 행 잠금을 유지하면 이 UPDATE는 release 신호를 보낼 수 없어 멈춘다
            Instance.objects.filter(pk=row.pk).update(status=InstanceStatus.CLEANED, updated_at=timezone.now())
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]["skipped"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, InstanceStatus.CLEANED)
        self.assertEqual(detail.call_count, 1)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_command_prints_safe_summary_and_retries_failed_record_on_later_run(self, detail):
        row = self.instance()
        detail.side_effect = SchedulerError("SCHEDULER_UNAVAILABLE", "private response must not be printed", 503)
        output = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("reconcile_instances", stdout=output)
        self.assertEqual(json.loads(output.getvalue())["error"], 1)
        self.assertNotIn("private response", output.getvalue())
        detail.side_effect = None
        detail.return_value = self.snapshot(row)
        call_command("reconcile_instances", stdout=io.StringIO())
        row.refresh_from_db()
        self.assertEqual(row.status, InstanceStatus.CLEANED)

    def test_command_rejects_unbounded_or_invalid_options(self):
        for options in (
            {"batch_size": 0}, {"batch_size": 1001}, {"max_seconds": 0},
            {"max_seconds": float("inf")}, {"max_seconds": float("nan")}, {"interval": -1},
        ):
            with self.subTest(options=options), self.assertRaises(CommandError):
                call_command("reconcile_instances", **options)
