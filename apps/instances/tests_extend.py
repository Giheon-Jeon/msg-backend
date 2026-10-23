from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as datetime_timezone
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import DatabaseError, close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import IdempotencyRequest
from apps.challenge.models import Challenge
from apps.instances import tests as instance_tests
from apps.instances.models import Instance, InstanceLock, InstanceStatus
from apps.instances.services import SchedulerError, isoformat_z


NOW = datetime(2026, 10, 23, 0, 0, tzinfo=datetime_timezone.utc)


@override_settings(
    CACHES=instance_tests.LOCMEM,
    SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="test-scheduler-token",
    INSTANCE_EXTEND_MINUTES=30,
)
class InstanceExtendCommitTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        instance_tests.InstanceLockTests.setUp(self)

    auth = instance_tests.InstanceLockTests.auth

    @patch("apps.instances.views.call_scheduler_extend")
    def test_commit_failure_does_not_cache_success(self, remote):
        instance = InstanceExtendTests.instance(self)
        remote.return_value = {"expires_at": isoformat_z(NOW + timedelta(hours=1))}
        key = "commit-failure"
        cache_key = f"idem:{self.user.pk}:POST:/api/v1/instances/{instance.pk}/extend:{key}"
        with patch.object(connection, "commit", side_effect=DatabaseError("injected commit failure")):
            response = InstanceExtendTests.extend(self, instance, key=key)
        self.assertEqual(response.status_code, 500)
        self.assertIsNone(cache.get(cache_key))
        self.assertFalse(IdempotencyRequest.objects.exists())
        instance.refresh_from_db()
        self.assertEqual(instance.extend_count, 0)
        retried = InstanceExtendTests.extend(self, instance, key=key)
        self.assertEqual(retried.status_code, 202, retried.data)
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(cache.get(cache_key)["status"], 202)

    @patch("apps.instances.views.call_scheduler_extend")
    def test_success_is_cached_only_after_outermost_commit(self, remote):
        instance = InstanceExtendTests.instance(self)
        remote.return_value = {"expires_at": isoformat_z(NOW + timedelta(hours=1))}
        key = "outer-commit"
        cache_key = f"idem:{self.user.pk}:POST:/api/v1/instances/{instance.pk}/extend:{key}"
        with transaction.atomic():
            response = InstanceExtendTests.extend(self, instance, key=key)
            self.assertEqual(response.status_code, 202, response.data)
            self.assertIsNone(cache.get(cache_key))
        self.assertEqual(cache.get(cache_key)["status"], 202)


@override_settings(
    CACHES=instance_tests.LOCMEM,
    SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="test-scheduler-token",
    INSTANCE_EXTEND_MINUTES=30,
)
class InstanceExtendTests(TestCase):
    setUp = instance_tests.InstanceLockTests.setUp
    auth = instance_tests.InstanceLockTests.auth

    def instance(self, **overrides):
        values = {
            "user": self.user, "team": self.team, "challenge": self.challenge,
            "status": InstanceStatus.RUNNING,
            "expires_at": NOW + timedelta(minutes=30),
            "hard_expires_at": NOW + timedelta(hours=2),
        }
        values.update(overrides)
        return Instance.objects.create(**values)

    def extend(self, instance, *, key=None, body=None, client=None):
        headers = {} if key is None else {"HTTP_IDEMPOTENCY_KEY": key}
        with patch("django.utils.timezone.now", return_value=NOW):
            return (client or self.client).post(
                f"/api/v1/instances/{instance.pk}/extend",
                {} if body is None else body, format="json", **headers,
            )

    def assert_unchanged(self, instance):
        fields = ("expires_at", "hard_expires_at", "extend_count", "status", "updated_at")
        before = [getattr(instance, field) for field in fields]
        instance.refresh_from_db()
        self.assertEqual([getattr(instance, field) for field in fields], before)

    @patch("apps.instances.views.call_scheduler_extend")
    def test_expired_ttl_is_rejected_before_scheduler_call(self, remote):
        remote.return_value = {}
        for expiry in (NOW - timedelta(seconds=1), NOW):
            with self.subTest(expiry=expiry):
                instance = self.instance(expires_at=expiry)
                response = self.extend(instance)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["code"], "INVALID_STATE_TRANSITION")
                self.assert_unchanged(instance)
        remote.assert_not_called()

    @patch("apps.instances.views.call_scheduler_extend")
    def test_hard_timeout_or_extension_over_limit_is_rejected_before_call(self, remote):
        for deadline in (NOW - timedelta(seconds=1), NOW, NOW + timedelta(minutes=59)):
            with self.subTest(deadline=deadline):
                instance = self.instance(hard_expires_at=deadline)
                response = self.extend(instance)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["code"], "HARD_TIMEOUT_EXCEEDED")
                self.assert_unchanged(instance)
        remote.assert_not_called()

    @patch("apps.instances.views.call_scheduler_extend")
    def test_missing_expiry_metadata_fails_closed(self, remote):
        for field in ("expires_at", "hard_expires_at"):
            with self.subTest(field=field):
                instance = self.instance(**{field: None})
                response = self.extend(instance)
                self.assertEqual(response.status_code, 400, response.data)
                self.assert_unchanged(instance)
        remote.assert_not_called()

    @patch("apps.instances.views.call_scheduler_extend")
    def test_extension_can_end_exactly_at_hard_limit(self, remote):
        instance = self.instance(hard_expires_at=NOW + timedelta(hours=1))
        remote.return_value = {"expires_at": isoformat_z(instance.hard_expires_at)}
        response = self.extend(instance)
        self.assertEqual(response.status_code, 202, response.data)
        instance.refresh_from_db()
        self.assertEqual(instance.expires_at, instance.hard_expires_at)
        self.assertEqual(instance.extend_count, 1)

    @patch("apps.instances.views.call_scheduler_extend")
    def test_invalid_remote_expiry_never_changes_instance(self, remote):
        instance = self.instance()
        payloads = [
            None, [], {}, {"expires_at": None}, {"expires_at": 123},
            {"expires_at": "not-a-date"}, {"expires_at": "2026-10-23T01:00:00"},
            {"expires_at": isoformat_z(instance.expires_at)},
            {"expires_at": isoformat_z(NOW)},
            {"expires_at": isoformat_z(instance.hard_expires_at + timedelta(seconds=1))},
            {"expires_at": isoformat_z(NOW + timedelta(hours=1)), "hard_expires_at": None},
            {"expires_at": isoformat_z(NOW + timedelta(hours=1)), "status": "STOPPED"},
            {
                "expires_at": isoformat_z(instance.hard_expires_at + timedelta(minutes=1)),
                "hard_expires_at": isoformat_z(instance.hard_expires_at + timedelta(hours=1)),
            },
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                remote.return_value = payload
                response = self.extend(instance)
                self.assertEqual(response.status_code, 503, response.data)
                self.assertEqual(response.data["code"], "SCHEDULER_UNAVAILABLE")
                self.assert_unchanged(instance)

    @patch("apps.instances.views.call_scheduler_extend")
    def test_identical_key_replays_202_even_without_cache(self, remote):
        instance = self.instance()
        remote.return_value = {"expires_at": isoformat_z(NOW + timedelta(hours=1))}
        first = self.extend(instance, key="same-extension")
        cache.clear()
        second = self.extend(instance, key="same-extension")
        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(second.status_code, 202, second.data)
        self.assertEqual(first.data, second.data)
        remote.assert_called_once()
        instance.refresh_from_db()
        self.assertEqual(instance.extend_count, 1)
        self.assertEqual(IdempotencyRequest.objects.get().response_status, 202)

    @patch("apps.instances.views.call_scheduler_extend")
    def test_success_replay_does_not_recheck_expiry_or_consume_another_extension(self, remote):
        instance = self.instance()
        remote.return_value = {"expires_at": isoformat_z(NOW + timedelta(hours=1))}
        first = self.extend(instance, key="completed-extension")
        Instance.objects.filter(pk=instance.pk).update(
            expires_at=NOW - timedelta(seconds=1), extend_count=3,
        )
        second = self.extend(instance, key="completed-extension")
        self.assertEqual(second.status_code, 202, second.data)
        self.assertEqual(first.data, second.data)
        remote.assert_called_once()

    @patch("apps.instances.views.call_scheduler_extend")
    def test_same_key_with_different_body_conflicts(self, remote):
        instance = self.instance()
        remote.return_value = {"expires_at": isoformat_z(NOW + timedelta(hours=1))}
        self.assertEqual(self.extend(instance, key="body-key").status_code, 202)
        response = self.extend(instance, key="body-key", body={"extend_minutes": 99})
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "IDEMPOTENCY_KEY_CONFLICT")
        remote.assert_called_once()

    @patch("apps.instances.views.call_scheduler_extend")
    def test_different_instances_do_not_share_replay(self, remote):
        first, second = self.instance(), self.instance()
        remote.return_value = {"expires_at": isoformat_z(NOW + timedelta(hours=1))}
        for instance in (first, second):
            response = self.extend(instance, key="per-instance-key")
            self.assertEqual(response.status_code, 202, response.data)
            self.assertEqual(response.data["data"]["instance_id"], str(instance.pk))
        self.assertEqual(remote.call_count, 2)

    @patch("apps.instances.views.call_scheduler_extend")
    def test_cached_response_is_not_returned_after_team_change(self, remote):
        instance = self.instance()
        remote.return_value = {"expires_at": isoformat_z(NOW + timedelta(hours=1))}
        self.assertEqual(self.extend(instance, key="owner-key").status_code, 202)
        self.user.team = Team.objects.create(team_name="new-extension-team")
        self.user.save(update_fields=["team"])
        response = self.extend(instance, key="owner-key")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertIsNone(response.data["data"])
        remote.assert_called_once()

    @patch("apps.instances.views.call_scheduler_extend")
    def test_calls_without_key_remain_supported(self, remote):
        instance = self.instance()
        remote.side_effect = [
            {"expires_at": isoformat_z(NOW + timedelta(minutes=minutes))}
            for minutes in (60, 90)
        ]
        self.assertEqual(self.extend(instance).status_code, 202)
        self.assertEqual(self.extend(instance).status_code, 202)
        self.assertEqual(remote.call_count, 2)
        instance.refresh_from_db()
        self.assertEqual(instance.extend_count, 2)

    @patch("apps.instances.views.call_scheduler_extend")
    def test_failed_scheduler_response_is_replayed_without_local_mutation(self, remote):
        instance = self.instance()
        remote.side_effect = SchedulerError("SCHEDULER_UNAVAILABLE", "unavailable", 503)
        first = self.extend(instance, key="failed-extension")
        second = self.extend(instance, key="failed-extension")
        self.assertEqual(first.status_code, 503, first.data)
        self.assertEqual(second.data, first.data)
        self.assertEqual(second.status_code, 503)
        remote.assert_called_once()
        self.assert_unchanged(instance)

    @patch("apps.instances.views.call_scheduler_extend")
    def test_invalid_keys_do_not_call_scheduler(self, remote):
        instance = self.instance()
        for key in ("", "k" * 256):
            with self.subTest(key_length=len(key)):
                response = self.extend(instance, key=key)
                self.assertEqual(response.status_code, 400, response.data)
                self.assert_unchanged(instance)
        remote.assert_not_called()

    def test_server_configuration_controls_extension_not_request_body(self):
        instance = self.instance(hard_expires_at=NOW + timedelta(minutes=50))
        with override_settings(INSTANCE_EXTEND_MINUTES=15):
            with patch(
                "apps.instances.services.scheduler_request",
                return_value={"expires_at": isoformat_z(NOW + timedelta(minutes=45))},
            ) as remote:
                response = self.extend(instance, key="server-duration", body={"extend_minutes": 999})
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(remote.call_args.kwargs["body"], {"extend_minutes": 15})
        instance.refresh_from_db()
        self.assertEqual(instance.expires_at, NOW + timedelta(minutes=45))

    @patch("apps.instances.views.call_scheduler_extend")
    def test_invalid_status_and_count_limit_are_rejected_without_call(self, remote):
        for status, count in (("STOPPING", 0), ("STOPPED", 0), ("RUNNING", 3)):
            with self.subTest(status=status, count=count):
                instance = self.instance(status=status, extend_count=count)
                response = self.extend(instance)
                self.assertEqual(response.status_code, 400, response.data)
                self.assert_unchanged(instance)
        remote.assert_not_called()


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking")
@override_settings(
    CACHES=instance_tests.LOCMEM, SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="test-scheduler-token", INSTANCE_EXTEND_MINUTES=30,
)
class ConcurrentInstanceExtendTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.team = Team.objects.create(team_name="concurrent-extension-team")
        self.user = User.objects.create_user(
            login_id="concurrent-extension-user", nickname="extend", team=self.team,
        )
        challenge = Challenge.objects.create(
            title="Extend", category="WEB", difficulty="EASY", score=500,
            flag_hash="unused",
        )
        self.instance = Instance.objects.create(
            user=self.user, team=self.team, challenge=challenge, status="RUNNING",
            expires_at=NOW + timedelta(minutes=30), hard_expires_at=NOW + timedelta(hours=2),
        )
        InstanceLock.objects.create(user=self.user)

    def extend_concurrently(self, keys):
        ready = Barrier(2)

        def extend(key):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=self.user.pk))
                synchronized = False

                def synchronize(execute, sql, params, many, context):
                    nonlocal synchronized
                    if not synchronized and 'FROM "instance_locks"' in sql and "FOR UPDATE" in sql:
                        synchronized = True
                        ready.wait(timeout=5)
                    return execute(sql, params, many, context)

                with connection.execute_wrapper(synchronize):
                    response = client.post(
                        f"/api/v1/instances/{self.instance.pk}/extend", {},
                        format="json", HTTP_IDEMPOTENCY_KEY=key,
                    )
                return response.status_code, response.data
            finally:
                connections.close_all()

        with patch("django.utils.timezone.now", return_value=NOW):
            with patch(
                "apps.instances.views.call_scheduler_extend",
                side_effect=lambda instance, auth: {
                    "expires_at": isoformat_z(instance.expires_at + timedelta(minutes=30)),
                },
            ) as remote:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(extend, keys))
        return results, remote.call_count

    def test_same_key_concurrent_requests_extend_once(self):
        results, calls = self.extend_concurrently(["concurrent-extension"] * 2)
        self.assertEqual([status for status, _ in results], [202, 202], results)
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(calls, 1)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.extend_count, 1)
        self.assertEqual(IdempotencyRequest.objects.count(), 1)

    def test_different_keys_preserve_extension_count(self):
        results, calls = self.extend_concurrently(["first-extension", "second-extension"])
        self.assertEqual([status for status, _ in results], [202, 202], results)
        self.assertEqual(calls, 2)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.extend_count, 2)
        self.assertEqual(self.instance.expires_at, NOW + timedelta(minutes=90))

    def test_different_keys_cannot_exceed_last_extension(self):
        self.instance.extend_count = 2
        self.instance.save(update_fields=["extend_count"])
        results, calls = self.extend_concurrently(["last-extension-a", "last-extension-b"])
        self.assertEqual(sorted(status for status, _ in results), [202, 400], results)
        self.assertEqual(calls, 1)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.extend_count, 3)
