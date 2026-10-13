from rest_framework.response import Response


def ok(data=None, message="성공", status=200):
    """공통 규약 성공 응답. code 는 항상 SUCCESS 다."""
    return Response(
        {"code": "SUCCESS", "message": message, "data": data}, status=status
    )


def fail(code, message, status, data=None):
    """HTTP 200 이면서 실패인 경우 등, 직접 code 를 지정할 때 쓴다."""
    return Response({"code": code, "message": message, "data": data}, status=status)
