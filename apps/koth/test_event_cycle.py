import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.db import connection, connections
from django.test import TransactionTestCase

from apps.accounts.models import Team
from apps.adminpanel.models import AdminEvent
from apps.timer.models import Contest

from .management.commands.run_koth_cycle import CYCLE_LOCK
from .models import KothScorePeriod, KothSolve
from .test_http_clients import score_server
from .test_score_polling import make_challenge


START = datetime(2026, 11, 8, 1, tzinfo=timezone.utc)
COMMAND = "apps.koth.management.commands.run_koth_cycle"


class KothEventCycleTests(TransactionTestCase):
    def setUp(self):
        self.contest = Contest.objects.create(name="event", start_time=START, end_time=START + timedelta(hours=12), is_active=True)

    def challenge(self, name, url="https://score.example.test", **changes):
        challenge = make_challenge(name)
        challenge.status = "SCHEDULED"
        challenge.opened_at = START
        challenge.closed_at = START + timedelta(hours=4)
        challenge.challenge_url = "https://game.example.test"
        challenge.score_api_url = url
        for key, value in changes.items():
            setattr(challenge, key, value)
        challenge.save()
        return challenge

    def cycle(self, challenges, now, **options):
        with patch.dict("os.environ", {"TEST_POLL_SCORE_TOKEN": "isolated-test-only"}):
            with patch(COMMAND + ".timezone.now", return_value=now):
                call_command("run_koth_cycle", contest_id=str(self.contest.pk),
                             challenge_id=[str(challenge.pk) for challenge in challenges],
                             stdout=StringIO(), stderr=StringIO(), **options)

    def test_before_event_neither_opens_nor_fetches(self):
        with score_server() as (url, requests):
            challenge = self.challenge("before", url)
            self.cycle([challenge], START - timedelta(seconds=1))
        challenge.refresh_from_db()
        self.assertEqual(challenge.status, "SCHEDULED")
        self.assertEqual(requests, [])

    def test_only_explicitly_approved_challenge_opens_at_boundary(self):
        with score_server() as (url, requests):
            approved = self.challenge("approved", url)
            other = self.challenge("not-approved", url)
            self.cycle([approved], START)
        approved.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(approved.status, "ACTIVE")
        self.assertEqual(other.status, "SCHEDULED")
        self.assertEqual(requests, [])
        self.assertEqual(AdminEvent.objects.count(), 1)

    def test_missing_credential_keeps_game_unopened(self):
        challenge = self.challenge("missing", score_api_token_env="ABSENT_KOTH_TEST_TOKEN")
        with self.assertRaisesMessage(CommandError, "connection or provider"):
            self.cycle([challenge], START)
        challenge.refresh_from_db()
        self.assertEqual(challenge.status, "SCHEDULED")
        self.assertFalse(KothScorePeriod.objects.exists())

    def test_manual_closed_game_is_never_reopened(self):
        challenge = self.challenge("closed", status="CLOSED")
        self.cycle([challenge], START)
        challenge.refresh_from_db()
        self.assertEqual(challenge.status, "CLOSED")

    def test_missed_periods_are_caught_up_and_never_refetched(self):
        with score_server() as (url, requests):
            challenge = self.challenge("catch-up", url)
            self.cycle([challenge], START + timedelta(minutes=46))
            self.cycle([challenge], START + timedelta(minutes=46))
        self.assertEqual(len(requests), 3)
        self.assertEqual(list(KothScorePeriod.objects.order_by("period_id").values_list("period_id", flat=True)),
                         [START, START + timedelta(minutes=15), START + timedelta(minutes=30)])

    def test_closing_collects_last_ended_period_without_future_period(self):
        with score_server() as (url, requests):
            challenge = self.challenge("closing", url, status="ACTIVE", closed_at=START + timedelta(minutes=30))
            self.cycle([challenge], START + timedelta(minutes=30))
            self.cycle([challenge], START + timedelta(hours=1))
        challenge.refresh_from_db()
        self.assertEqual(challenge.status, "CLOSED")
        self.assertEqual(len(requests), 2)
        self.assertEqual(KothScorePeriod.objects.count(), 2)

    def test_bounded_batch_gives_other_provider_a_turn_after_failure(self):
        with score_server(status=503) as (bad_url, bad_requests), score_server() as (url, requests):
            bad = self.challenge("failed-provider", bad_url, status="ACTIVE")
            good = self.challenge("good-provider", url, status="ACTIVE")
            with self.assertRaises(CommandError):
                self.cycle([bad, good], START + timedelta(hours=1), max_periods=2)
        self.assertEqual(len(bad_requests), 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(KothScorePeriod.objects.filter(status="FAILED").count(), 1)
        self.assertEqual(KothScorePeriod.objects.filter(status="APPLIED").count(), 1)

    def test_dry_run_does_not_change_state_or_contact_provider(self):
        with score_server() as (url, requests):
            challenge = self.challenge("dry", url)
            self.cycle([challenge], START + timedelta(minutes=30), dry_run=True)
        challenge.refresh_from_db()
        self.assertEqual(challenge.status, "SCHEDULED")
        self.assertEqual(requests, [])
        self.assertFalse(AdminEvent.objects.exists())
        self.assertFalse(KothScorePeriod.objects.exists())

    def test_inactive_contest_cannot_open_game(self):
        self.contest.is_active = False
        self.contest.save()
        challenge = self.challenge("inactive")
        with self.assertRaisesMessage(CommandError, "not active"):
            self.cycle([challenge], START)
        challenge.refresh_from_db()
        self.assertEqual(challenge.status, "SCHEDULED")

    def test_invalid_window_rolls_back_all_transitions(self):
        good = self.challenge("valid-window")
        invalid = self.challenge("invalid-window", opened_at=START + timedelta(seconds=1))
        with self.assertRaisesMessage(CommandError, "15-minute"):
            self.cycle([good, invalid], START + timedelta(minutes=30))
        good.refresh_from_db()
        self.assertEqual(good.status, "SCHEDULED")
        self.assertFalse(AdminEvent.objects.exists())

    def test_another_postgres_session_skips_the_entire_duplicate_cycle(self):
        challenge = self.challenge("locked")
        other = connections["default"].copy(alias="koth_lock_test")
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [CYCLE_LOCK])
            self.cycle([challenge], START)
        finally:
            other.close()
        challenge.refresh_from_db()
        self.assertEqual(challenge.status, "SCHEDULED")
        self.cycle([challenge], START)
        challenge.refresh_from_db()
        self.assertEqual(challenge.status, "ACTIVE")

    def test_network_fetch_does_not_hold_a_database_transaction(self):
        challenge = self.challenge("outside-transaction", status="ACTIVE")
        states = []
        def poll(*args):
            states.append(connection.in_atomic_block)
            return True
        with patch(COMMAND + ".poll_challenge_period", side_effect=poll):
            self.cycle([challenge], START + timedelta(minutes=15))
        self.assertEqual(states, [False])

    def test_real_http_score_is_awarded_once_after_repeated_cycle(self):
        challenge = self.challenge("one-award")
        team = Team.objects.create(team_name="one-award")
        payload = {"code": "SUCCESS", "data": {"koth_challenge_id": str(challenge.pk),
                   "period_id": START.isoformat(), "results": [{"team_id": str(team.pk), "period_rank": 1, "metric_score": 10}]}}
        with score_server(body=json.dumps(payload).encode()) as (url, requests):
            challenge.score_api_url = url
            challenge.save()
            self.cycle([challenge], START + timedelta(minutes=15))
            self.cycle([challenge], START + timedelta(minutes=15))
        self.assertEqual(len(requests), 1)
        self.assertEqual(KothSolve.objects.get().earned_score, 40)
        self.assertEqual(KothScorePeriod.objects.count(), 1)

    def test_duplicate_approval_or_too_small_budget_is_rejected(self):
        first = self.challenge("budget-first")
        second = self.challenge("budget-second")
        for challenges, options in [([first, first], {}), ([first, second], {"max_periods": 1})]:
            with self.assertRaises(CommandError):
                self.cycle(challenges, START, **options)
