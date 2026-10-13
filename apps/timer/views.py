from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Contest
from .serializers import ContestTimerSerializer


def format_duration(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@api_view(["GET"])
def contest_timer(request):
    contest = Contest.objects.filter(is_active=True).first()
    if contest is None:
        return Response(
            {
                "code": "SUCCESS",
                "message": "활성화된 대회가 없습니다.",
                "data": None,
            }
        )

    now = timezone.now()
    snapshot = contest.snapshot(now)

    payload = {
        "name": contest.name,
        "start_time": contest.start_time,
        "end_time": contest.end_time,
        "remaining_display": format_duration(snapshot["remaining_seconds"]),
        "server_time": now,
        **snapshot,
    }

    serializer = ContestTimerSerializer(payload)
    return Response(
        {
            "code": "SUCCESS",
            "message": "성공",
            "data": serializer.data,
        }
    )
