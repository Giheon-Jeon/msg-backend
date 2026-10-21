import json

from django.core.management.base import BaseCommand

from apps.signature.drafts import ensure_signature_drafts


class Command(BaseCommand):
    help = "동아리별 시그니처 초안을 등록합니다 기존 문제는 유지합니다"

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(ensure_signature_drafts(), ensure_ascii=False))
