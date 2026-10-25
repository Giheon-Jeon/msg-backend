from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from .models import KothChallenge, KothChallengeStatus, KothScorePeriod
from .test_http_clients import score_server
from .test_score_polling import make_challenge


BOUNDARY = datetime(2026, 11, 8, 5, 0, tzinfo=timezone.utc)


class CompletedPeriodCommandTests(TestCase):
    def run_poll(self, **kwargs):
        with patch.dict("os.environ", {"TEST_POLL_SCORE_TOKEN": "test-only"}):
            with patch(
                "apps.koth.management.commands.poll_koth_scores.current_period",
                return_value=BOUNDARY,
            ):
                call_command(
                    "poll_koth_scores", max_retries=0, stdout=StringIO(), **kwargs
                )

    def configure(self, url, **fields):
        challenge = make_challenge("completed-period")
        KothChallenge.objects.filter(pk=challenge.pk).update(
            score_api_url=url, **fields
        )
        return challenge

    def test_default_requests_finished_period_over_real_http(self):
        with score_server() as (url, requests):
            challenge = self.configure(url)
            self.run_poll()
        self.assertEqual(len(requests), 1)
        record = KothScorePeriod.objects.get(challenge=challenge)
        self.assertEqual(record.period_id, BOUNDARY - timedelta(minutes=15))
        self.assertEqual(record.status, "APPLIED")

    def test_current_or_future_period_never_contacts_score_server(self):
        with score_server() as (url, requests):
            self.configure(url)
            for period in [BOUNDARY, BOUNDARY + timedelta(minutes=15)]:
                with self.subTest(period=period):
                    with self.assertRaisesMessage(CommandError, "not ended"):
                        self.run_poll(period_id=period.isoformat())
        self.assertEqual(requests, [])
        self.assertFalse(KothScorePeriod.objects.exists())

    def test_closed_challenge_last_full_period_is_applied_once(self):
        with score_server() as (url, requests):
            challenge = self.configure(
                url,
                status=KothChallengeStatus.CLOSED,
                opened_at=BOUNDARY - timedelta(hours=4),
                closed_at=BOUNDARY,
            )
            self.run_poll()
            self.run_poll()
        self.assertEqual(len(requests), 1)
        self.assertEqual(KothScorePeriod.objects.filter(challenge=challenge).count(), 1)

    def test_period_before_opening_or_after_closing_is_skipped(self):
        with score_server() as (url, requests):
            challenge = self.configure(url, opened_at=BOUNDARY)
            self.run_poll()
            KothChallenge.objects.filter(pk=challenge.pk).update(
                opened_at=BOUNDARY - timedelta(hours=4),
                closed_at=BOUNDARY - timedelta(minutes=15),
            )
            self.run_poll()
        self.assertEqual(requests, [])
        self.assertFalse(KothScorePeriod.objects.exists())

    def test_scheduled_challenge_is_not_scored_even_with_past_dates(self):
        with score_server() as (url, requests):
            self.configure(
                url,
                status=KothChallengeStatus.SCHEDULED,
                opened_at=BOUNDARY - timedelta(hours=4),
                closed_at=BOUNDARY,
            )
            self.run_poll()
        self.assertEqual(requests, [])

    def test_explicit_ended_period_is_preserved_for_recovery(self):
        period = BOUNDARY - timedelta(minutes=30)
        with score_server() as (url, requests):
            challenge = self.configure(url)
            self.run_poll(period_id=period.isoformat())
        self.assertEqual(len(requests), 1)
        self.assertEqual(KothScorePeriod.objects.get(challenge=challenge).period_id, period)

    def test_invalid_retry_count_is_rejected_before_any_request(self):
        for retries in [-1, 4]:
            with self.subTest(retries=retries):
                with self.assertRaisesMessage(CommandError, "max_retries"):
                    call_command("poll_koth_scores", max_retries=retries)
