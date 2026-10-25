import os
import uuid
from contextlib import contextmanager
from datetime import timedelta
from itertools import zip_longest

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.adminpanel.models import AdminEvent
from apps.koth.models import KothChallenge, KothChallengeStatus, KothScorePeriod, KothScorePeriodStatus
from apps.koth.services import ScoreFetchError, current_period, poll_challenge_period
from apps.timer.models import Contest


CYCLE_LOCK = 0x4B4F54484359434C
PERIOD = timedelta(minutes=15)


@contextmanager
def cycle_lock():
    # The lock belongs to this DB session and is released if the process dies.
    # HTTP calls run outside DB transactions so no row lock spans a network wait.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [CYCLE_LOCK])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [CYCLE_LOCK])


class Command(BaseCommand):
    help = "승인한 KOTH 문제만 정해진 시각에 공개·마감하고 누락된 종료 구간 점수를 수집합니다."

    def add_arguments(self, parser):
        parser.add_argument("--contest-id", required=True)
        parser.add_argument("--challenge-id", action="append", required=True)
        parser.add_argument("--max-periods", type=int, default=6)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        try:
            contest_id = uuid.UUID(options["contest_id"])
            ids = [uuid.UUID(value) for value in options["challenge_id"]]
        except (TypeError, ValueError, AttributeError) as exc:
            raise CommandError("contest-id and challenge-id must be UUIDs") from exc
        if not 1 <= len(ids) <= 6 or len(ids) != len(set(ids)):
            raise CommandError("Provide one to six distinct approved challenge IDs")
        if not len(ids) <= options["max_periods"] <= 24:
            raise CommandError("max-periods must be between the approved challenge count and 24")
        if connection.vendor != "postgresql":
            raise CommandError("KOTH cycles require PostgreSQL advisory locks")
        with cycle_lock() as acquired:
            if not acquired:
                self.stdout.write("Another KOTH cycle is running; skipped.")
                return
            self.run_cycle(contest_id, ids, options)

    def run_cycle(self, contest_id, ids, options):
        now = timezone.now()
        missing_connection = False
        try:
            contest = Contest.objects.get(pk=contest_id, is_active=True)
        except Contest.DoesNotExist as exc:
            raise CommandError("The selected contest is not active") from exc
        with transaction.atomic():
            challenges = list(KothChallenge.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
            if len(challenges) != len(ids):
                raise CommandError("An approved challenge does not exist")
            for challenge in challenges:
                self.validate_schedule(challenge, contest)
            for challenge in challenges:
                target = challenge.status
                if now >= challenge.closed_at:
                    target = KothChallengeStatus.CLOSED
                elif now >= challenge.opened_at and challenge.status == KothChallengeStatus.SCHEDULED:
                    # A manual early closure stays closed until an operator explicitly reopens it.
                    if not (challenge.challenge_url and challenge.score_api_url and
                            challenge.score_api_token_env and os.getenv(challenge.score_api_token_env)):
                        self.stderr.write(f"{challenge.pk}: not opened; connection or score credential missing")
                        missing_connection = True
                        continue
                    target = KothChallengeStatus.ACTIVE
                if target != challenge.status:
                    self.stdout.write(f"{challenge.pk}: {challenge.status} -> {target}")
                    challenge.status = target
                    if not options["dry_run"]:
                        challenge.save(update_fields=["status", "updated_at"])
                        AdminEvent.objects.create(
                            type=AdminEvent.EventType.SETTINGS_CHANGED,
                            actor="koth-scheduler",
                            message=f"KOTH {challenge.pk} scheduled status: {target}",
                        )

        applied = set(KothScorePeriod.objects.filter(challenge_id__in=ids, status=KothScorePeriodStatus.APPLIED)
                      .values_list("challenge_id", "period_id"))
        queues = []
        for challenge in challenges:
            queue = []
            boundary = min(current_period(now), challenge.closed_at)
            period = challenge.opened_at
            if challenge.status != KothChallengeStatus.SCHEDULED:
                while period + PERIOD <= boundary:
                    if (challenge.pk, period) not in applied:
                        queue.append((challenge, period))
                    period += PERIOD
            queues.append(queue)
        pending = [item for group in zip_longest(*queues) for item in group if item is not None]
        self.stdout.write(f"Missing ended periods: {len(pending)}")
        if options["dry_run"]:
            self.stdout.write("Dry run; no status or score changes applied.")
            return
        failed = missing_connection
        for challenge, period in pending[:options["max_periods"]]:
            try:
                changed = poll_challenge_period(challenge, period)
                self.stdout.write(f"{challenge.pk} {period.isoformat()}: {'applied' if changed else 'already applied'}")
            except ScoreFetchError as exc:
                failed = True
                self.stderr.write(f"{challenge.pk} {period.isoformat()}: {exc}")
        if failed:
            raise CommandError("A KOTH connection or provider failed; the next cycle will retry")

    def validate_schedule(self, challenge, contest):
        opened, closed = challenge.opened_at, challenge.closed_at
        if not opened or not closed or opened >= closed:
            raise CommandError(f"{challenge.pk}: opening and closing times are required")
        if closed - opened > timedelta(days=1):
            raise CommandError(f"{challenge.pk}: a challenge window cannot exceed 24 hours")
        if opened != current_period(opened) or closed != current_period(closed):
            raise CommandError(f"{challenge.pk}: times must align to 15-minute boundaries")
        if opened < contest.start_time or closed > contest.end_time:
            raise CommandError(f"{challenge.pk}: schedule must stay inside the selected contest")
