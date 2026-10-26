from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt


# Unknown routes never mutate state; keep their 404 instead of a CSRF 403.
@csrf_exempt
def api_not_found(request):
    return JsonResponse(
        {"code": "NOT_FOUND", "message": "대상을 찾을 수 없습니다", "data": None},
        status=404,
    )
