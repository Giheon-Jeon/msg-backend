import time

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.instances.poller import poll_once
from apps.instances.user_files import poll_user_files_once


class Command(BaseCommand):
    help = (
        "공급망 publish bundle과 참가자 파일 bundle을 자동 수집한다. "
        "기본은 1회 실행이라 cron에 걸기 좋고, --interval을 주면 주기 실행한다. "
        "릴리스 전환은 하지 않으므로 배포 버전 선택은 관리자 몫으로 남는다."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=0,
            help="초 단위 반복 주기. 0이면 1회만 실행",
        )

    def handle(self, *args, **options):
        token = settings.RELEASE_POLL_GITHUB_TOKEN
        if not token:
            self.stderr.write(
                "RELEASE_POLL_GITHUB_TOKEN이 비어 있습니다. "
                "Actions artifact 다운로드에는 토큰이 필요합니다."
            )

        interval = options["interval"]
        while True:
            summary = poll_once(token=token)
            self.stdout.write(
                "poll 완료: 등록 {registered}, 중복 {duplicate}, "
                "매핑 실패 {unmatched}, 형식 오류 {invalid}, 통신 오류 {error}".format(**summary)
            )
            user_files_summary = poll_user_files_once(token=token)
            self.stdout.write(
                "참가자 파일 poll 완료: 등록 {registered}, 중복 {duplicate}, "
                "매핑 실패 {unmatched}, 형식 오류 {invalid}, 통신 오류 {error}".format(
                    **user_files_summary
                )
            )
            if interval <= 0:
                break
            time.sleep(interval)
