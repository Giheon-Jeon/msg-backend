from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.common.utils import num
from apps.ranking.models import LineMonopoly

from .models import AdminEvent


@receiver(post_save, sender=LineMonopoly)
def record_line_monopolized(sender, instance, created, raw=False, **kwargs):
    # 독점 지급은 ranking 앱 소관이고 경로도 늘어난다(정답 제출, 보드 특수칸 소비).
    # 경로마다 기록을 넣지 않고 저장 시점에 받아, 지급과 같은 트랜잭션에서 남긴다.
    if not created or raw:
        return
    from .views import _record_event

    bonus = " (카테고리 보너스)" if instance.is_category_bonus else ""
    _record_event(
        AdminEvent.EventType.LINE_MONOPOLIZED,
        f"{instance.line_number}번 라인 독점: +{num(instance.earned_score)}점{bonus}",
        "system",
        team=instance.team,
    )
