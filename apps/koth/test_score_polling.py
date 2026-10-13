import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase

from apps.accounts.models import Team

from .models import (
    KothChallenge,
    KothClub,
    KothScorePeriod,
    KothScorePeriodStatus,
    KothSolve,
)
from .services import ScoreFetchError, poll_challenge_period
from .test_http_clients import score_server

PERIOD = datetime(2026, 9, 10, 4, 45, tzinfo=timezone.utc)


def make_challenge(name):
    return KothChallenge.objects.create(
        club=KothClub.objects.create(name=name),
        title=name,
        open_group=1,
        status="ACTIVE",
        inbound_internal_token_hash=hashlib.sha256(name.encode()).hexdigest(),
        score_api_token_env="TEST_POLL_SCORE_TOKEN",
    )


@patch.dict(os.environ, {"TEST_POLL_SCORE_TOKEN": "test-only-score-token"})
class ScorePayloadFailureTests(TestCase):
    def test_missing_data_is_recorded_as_failure_and_valid_null_can_be_retried(self):
        challenge = make_challenge("missing-data")
        with score_server(body=b'{"code":"SUCCESS"}') as (url, requests):
            challenge.score_api_url = url
            with self.assertRaisesRegex(ScoreFetchError, "missing data"):
                poll_challenge_period(challenge, PERIOD)
        self.assertEqual(len(requests), 1)
        record = KothScorePeriod.objects.get(challenge=challenge, period_id=PERIOD)
        self.assertEqual(record.status, KothScorePeriodStatus.FAILED)
        self.assertEqual(record.attempts, 1)
        self.assertIn("missing data", record.last_error)
        self.assertFalse(KothSolve.objects.exists())

        with score_server() as (url, requests):
            challenge.score_api_url = url
            self.assertTrue(poll_challenge_period(challenge, PERIOD))
            self.assertFalse(poll_challenge_period(challenge, PERIOD))
        self.assertEqual(len(requests), 1)
        record.refresh_from_db()
        self.assertEqual(record.status, KothScorePeriodStatus.APPLIED)
        self.assertEqual(record.attempts, 2)
        self.assertEqual(record.last_error, "")
        self.assertEqual(record.response_payload, {"code": "SUCCESS", "data": None})
        self.assertFalse(KothSolve.objects.exists())

    def test_missing_data_retries_then_continues_to_next_challenge(self):
        malformed = make_challenge("a-malformed")
        healthy = make_challenge("b-healthy")
        with score_server(body=b'{"code":"SUCCESS"}') as (bad_url, bad_requests):
            with score_server() as (good_url, good_requests):
                KothChallenge.objects.filter(pk=malformed.pk).update(
                    score_api_url=bad_url
                )
                KothChallenge.objects.filter(pk=healthy.pk).update(
                    score_api_url=good_url
                )
                with patch(
                    "apps.koth.management.commands.poll_koth_scores.time.sleep"
                ) as sleep:
                    with self.assertRaises(CommandError):
                        call_command(
                            "poll_koth_scores",
                            period_id="2026-09-10T04:45:00Z",
                            max_retries=1,
                            stdout=StringIO(),
                            stderr=StringIO(),
                        )
        sleep.assert_called_once_with(60)
        self.assertEqual(len(bad_requests), 2)
        self.assertEqual(len(good_requests), 1)
        failed = KothScorePeriod.objects.get(challenge=malformed)
        self.assertEqual(failed.status, KothScorePeriodStatus.FAILED)
        self.assertEqual(failed.attempts, 2)
        self.assertIn("missing data", failed.last_error)
        applied = KothScorePeriod.objects.get(challenge=healthy)
        self.assertEqual(applied.status, KothScorePeriodStatus.APPLIED)
        self.assertEqual(applied.attempts, 1)


class ConcurrentScorePollTests(TransactionTestCase):
    def test_late_failure_preserves_applied_period_and_cannot_double_award(self):
        self.assert_overlapping_polls(first_fails=False, second_fails=True)

    def test_failure_before_success_still_allows_the_period_to_be_applied(self):
        self.assert_overlapping_polls(first_fails=True, second_fails=False)

    def test_two_successful_polls_apply_the_period_only_once(self):
        self.assert_overlapping_polls(first_fails=False, second_fails=False)

    def assert_overlapping_polls(self, first_fails, second_fails):
        challenge = make_challenge("concurrent-poll")
        team = Team.objects.create(team_name="concurrent-poll-team")
        payload = {
            "code": "SUCCESS",
            "data": {
                "koth_challenge_id": str(challenge.pk),
                "period_id": "2026-09-10T04:45:00Z",
                "results": [
                    {"team_id": str(team.pk), "period_rank": 1, "metric_score": 100}
                ],
            },
        }
        first_fetch = threading.Event()
        second_fetch = threading.Event()
        first_finished = threading.Event()
        worker = threading.local()
        first_snapshot = {}

        def fetch(*args):
            if worker.index == 0:
                first_fetch.set()
                if not second_fetch.wait(timeout=10):
                    raise AssertionError("Second poll did not reach score fetching")
                should_fail = first_fails
            else:
                second_fetch.set()
                if not first_finished.wait(timeout=10):
                    raise AssertionError(
                        "First poll did not finish its database transaction"
                    )
                should_fail = second_fails
            if should_fail:
                raise ScoreFetchError("controlled score fetch failure")
            return payload

        def invoke(index):
            worker.index = index
            close_old_connections()
            try:
                if connection.vendor == "postgresql":
                    with connection.cursor() as cursor:
                        cursor.execute("SET lock_timeout = '5s'")
                try:
                    result = poll_challenge_period(challenge, PERIOD)
                    if index == 0:
                        record = KothScorePeriod.objects.get(challenge=challenge)
                        first_snapshot.update(
                            applied_at=record.applied_at,
                            updated_at=record.updated_at,
                            response_payload=record.response_payload,
                        )
                    return result
                except ScoreFetchError:
                    return "fetch_failed"
            finally:
                if index == 0:
                    first_finished.set()
                connections.close_all()

        with patch("apps.koth.services._request_scores", side_effect=fetch):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(invoke, 0)
                self.assertTrue(first_fetch.wait(timeout=10))
                second = executor.submit(invoke, 1)
                outcomes = [first.result(timeout=15), second.result(timeout=15)]
        expected = ["fetch_failed", True] if first_fails else [True, False]
        self.assertEqual(outcomes, expected)

        record = KothScorePeriod.objects.get(challenge=challenge, period_id=PERIOD)
        self.assertEqual(record.status, KothScorePeriodStatus.APPLIED)
        self.assertEqual(record.attempts, 2)
        self.assertEqual(record.last_error, "")
        self.assertEqual(record.response_payload, payload)
        self.assertIsNotNone(record.applied_at)
        if not first_fails:
            for field, value in first_snapshot.items():
                self.assertEqual(getattr(record, field), value, field)
        self.assertEqual(KothSolve.objects.count(), 1)
        self.assertEqual(KothSolve.objects.get(team=team).earned_score, Decimal("40"))
        with patch("apps.koth.services._request_scores") as request:
            self.assertFalse(poll_challenge_period(challenge, PERIOD))
        request.assert_not_called()
        self.assertEqual(KothSolve.objects.get(team=team).earned_score, Decimal("40"))
