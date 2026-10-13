import time

from django.core.management.base import BaseCommand, CommandError

from apps.koth.models import KothChallenge, KothChallengeStatus
from apps.koth.services import (
    ScoreFetchError,
    current_period,
    parse_period,
    poll_challenge_period,
)


class Command(BaseCommand):
    help = "활성 KOTH 문제 서버에서 15분 구간 점수를 수집해 반영합니다. 외부 스케줄러에서 15분마다 실행하세요."

    def add_arguments(self, parser):
        parser.add_argument(
            "--period-id", help="ISO-8601 UTC 구간 시작 시각. 생략 시 현재 15분 구간"
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
                else current_period()
            )
        except ScoreFetchError as exc:
            raise CommandError(str(exc))
        challenges = KothChallenge.objects.filter(status=KothChallengeStatus.ACTIVE)
        if options["challenge_id"]:
            challenges = challenges.filter(pk=options["challenge_id"])
        if not challenges.exists():
            self.stdout.write("No active KOTH challenges to poll.")
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
