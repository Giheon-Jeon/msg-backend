import uuid

from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.challenge.models import Challenge
from apps.challenge.services import is_correct_flag
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances.models import ReleaseContainer, RuntimeSecret
from apps.instances.runtime_secrets import (
    RuntimeSecretUnavailable, create_runtime_secret, decrypt_runtime_secret,
    service_token_matches,
)


@method_decorator(never_cache, name="dispatch")
class RuntimeSecretListCreateView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request, challenge_id):
        challenge = Challenge.objects.filter(challenge_id=challenge_id).first()
        if challenge is None:
            return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다", 404)
        # 조회에는 암호문과 복호화 값을 포함하지 않는다
        records = RuntimeSecret.objects.filter(challenge=challenge).values(
            "secret_id", "name", "version", "created_at", "created_by",
        ).order_by("name", "-version")
        rows = []
        latest = {}
        for record in records:
            latest.setdefault(record["name"], record["version"])
            rows.append({
                "secret_id": str(record["secret_id"]),
                "name": record["name"], "version": record["version"],
                "created_at": record["created_at"].isoformat(),
                "created_by": record["created_by"],
                "is_latest": record["version"] == latest[record["name"]],
            })
        return ok({"challenge_id": str(challenge.pk), "secrets": rows, "total_count": len(rows)})

    def post(self, request, challenge_id):
        if not isinstance(request.data, dict) or set(request.data) != {"name", "value"}:
            return fail("RUNTIME_SECRET_INVALID", "비밀값 이름과 값이 필요합니다", 400)
        with transaction.atomic():
            challenge = Challenge.objects.select_for_update().filter(challenge_id=challenge_id).first()
            if challenge is None:
                return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다", 404)
            try:
                secret = create_runtime_secret(challenge, request.data["name"], request.data["value"], request.user.login_id)
            except ValueError as error:
                return fail("RUNTIME_SECRET_INVALID", str(error), 400)
            except RuntimeSecretUnavailable:
                return fail("RUNTIME_SECRET_UNAVAILABLE", "비밀값 저장 설정을 확인하세요", 503)
        return ok({"secret_id": str(secret.secret_id), "name": secret.name, "version": secret.version})


@method_decorator(never_cache, name="dispatch")
class RuntimeSecretResolveView(APIView):
    # 이 토큰은 Runtime worker 전용이며 참가자·관리자 JWT로 대체할 수 없다
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            if not service_token_matches(request.headers.get("Authorization", "")):
                return fail("TOKEN_INVALID", "인증 정보가 올바르지 않습니다", 401)
        except RuntimeSecretUnavailable:
            return fail("RUNTIME_SECRET_UNAVAILABLE", "비밀값 조회 설정을 확인하세요", 503)
        body = request.data
        if not isinstance(body, dict) or set(body) != {"secret_ref", "container", "image"}:
            return fail("RUNTIME_SECRET_INVALID", "비밀값 참조 형식이 올바르지 않습니다", 400)
        if not all(isinstance(body[field], str) for field in body):
            return fail("RUNTIME_SECRET_INVALID", "비밀값 참조 형식이 올바르지 않습니다", 400)
        try:
            reference = uuid.UUID(body["secret_ref"])
            if reference.int == 0 or str(reference) != body["secret_ref"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            return fail("RUNTIME_SECRET_INVALID", "비밀값 참조 형식이 올바르지 않습니다", 400)
        container = ReleaseContainer.objects.select_related("release__challenge").filter(
            id=reference, name=body["container"], image_ref=body["image"],
        ).first()
        if container is None or not container.secret_env:
            return fail("RUNTIME_SECRET_NOT_FOUND", "사용할 수 없는 비밀값 참조입니다", 404)
        if container.release.approved_at is None and not container.release.deployment_tests.filter(
            finished_at__isnull=True, expires_at__gt=timezone.now(),
            status__in=["REQUESTED", "SCHEDULING", "PROVISIONING", "RUNNING"],
        ).exists():
            return fail("RUNTIME_SECRET_NOT_FOUND", "사용할 수 없는 비밀값 참조입니다", 404)
        records = RuntimeSecret.objects.filter(
            secret_id__in=container.secret_env.values(), challenge_id=container.release.challenge_id,
        )
        by_id = {str(record.secret_id): record for record in records}
        try:
            values = {}
            for name, secret_id in container.secret_env.items():
                record = by_id[secret_id]
                value = decrypt_runtime_secret(record)
                if name == "FLAG" and (record.name != "flag" or not is_correct_flag(value, container.release.challenge.flag_hash)):
                    raise RuntimeSecretUnavailable()
                values[name] = value
        except (KeyError, RuntimeSecretUnavailable):
            return fail("RUNTIME_SECRET_UNAVAILABLE", "비밀값을 조회할 수 없습니다", 503)
        return ok({"env": values})
