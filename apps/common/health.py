import uuid

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_safe

# SLA 감시용 헬스체크. /api/v1 밖의 예외 경로라 공통 봉투를 쓰지 않고
# 인증 없이 상태만 내려준다 (contract.yaml base_url_exceptions 참고).
# 조회 전용이라 GET·HEAD만 허용한다. CSRF 미들웨어가 뷰보다 먼저 POST 등을 403으로
# 막아 메서드 제한이 가려지지 않도록 csrf_exempt를 함께 둔다 (상태를 바꾸는 경로가 없다).


def _check_database():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return "ok"
    except Exception:
        return "down"


def _check_cache():
    try:
        key = "healthz:" + uuid.uuid4().hex
        cache.set(key, "pong", 10)
        value = cache.get(key)
        cache.delete(key)
        return "ok" if value == "pong" else "down"
    except Exception:
        return "down"


@csrf_exempt
@require_safe
def healthz(request):
    database = _check_database()
    cache_status = _check_cache()
    healthy = database == "ok" and cache_status == "ok"

    return JsonResponse(
        {
            "status": "ok" if healthy else "down",
            "database": database,
            "cache": cache_status,
        },
        status=200 if healthy else 503,
    )
