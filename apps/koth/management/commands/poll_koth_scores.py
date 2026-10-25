import time
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.koth.models import KothChallenge, KothChallengeStatus
from apps.koth.services import (
    ScoreFetchError,
    current_period,
    parse_period,
    poll_challenge_period,
)


class Command(BaseCommand):
    help = "끝난 KOTH 15분 구간 점수를 수집합니다. 마감 직후에는 닫힌 문제의 마지막 구간도 반영합니다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--period-id", help="ISO-8601 UTC 구간 시작 시각. 생략 시 직전에 끝난 15분 구간"
        )
        parser.add_argument("--challenge-id", help="특정 KOTH 문제 ID만 수집")
        parser.add_argument(
            "--max-retries",
            type=int,
            default=3,
            help="실패한 동일 구간의 추가 재시도 횟수 (기본 3)",
        )

    def handle(self, *args, **options):
        try:
            period = (
                parse_period(options["period_id"])
                if options["period_id"]
                else current_period() - timedelta(minutes=15)
            )
        except ScoreFetchError as exc:
            raise CommandError(str(exc))
        if period >= current_period():
            raise CommandError("The score period has not ended yet.")
        if not 0 <= options["max_retries"] <= 3:
            raise CommandError("max_retries must be between 0 and 3.")
        end = period + timedelta(minutes=15)
        challenges = KothChallenge.objects.filter(
            Q(status=KothChallengeStatus.ACTIVE)
            | Q(
                status=KothChallengeStatus.CLOSED,
                opened_at__isnull=False,
                closed_at__isnull=False,
            )
        ).filter(
            Q(opened_at__isnull=True) | Q(opened_at__lte=period),
            Q(closed_at__isnull=True) | Q(closed_at__gte=end),
        )
        if options["challenge_id"]:
            challenges = challenges.filter(pk=options["challenge_id"])
        if not challenges.exists():
            self.stdout.write("No eligible KOTH challenges to poll.")
            return
        failed = False
        for challenge in challenges:
            delays = (60, 120, 240)
            for attempt in range(options["max_retries"] + 1):
                try:
                    applied = poll_challenge_period(challenge, period)
                    self.stdout.write(
                        f"{challenge.koth_challenge_id}: {'applied' if applied else 'already applied'}"
                    )
                    break
                except ScoreFetchError as exc:
                    if attempt == options["max_retries"]:
                        failed = True
                        self.stderr.write(
                            f"{challenge.koth_challenge_id}: failed: {exc}"
                        )
                        break
                    delay = delays[min(attempt, len(delays) - 1)]
                    self.stderr.write(
                        f"{challenge.koth_challenge_id}: attempt {attempt + 1} failed; retrying in {delay}s: {exc}"
                    )
                    time.sleep(delay)
        if failed:
            raise CommandError(
                "One or more KOTH score polls failed; rerun this same period up to the configured retry policy."
            )
