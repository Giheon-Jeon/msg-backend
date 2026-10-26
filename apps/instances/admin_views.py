from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.views import APIView

from apps.adminpanel.models import AdminEvent
from apps.challenge.models import Challenge
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig
from apps.instances.releases import (
    ReleaseValidationError,
    check_slug_consistency,
    create_release,
    is_deployable,
    serialize_release,
    validate_derived_settings,
    validate_release_payload,
)
from apps.instances.services import isoformat_z
from apps.instances.runtime_secrets import RuntimeSecretUnavailable


def _get_challenge(challenge_id, for_update=False):
    queryset = Challenge.objects.all()
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.filter(challenge_id=challenge_id).first()


def _record_release_switch(challenge, previous, release, reason, actor):
    # 누가 어느 버전으로 바꿨는지 남긴다. 실행 중인 인스턴스는 그대로이고 새 인스턴스부터 적용된다.
    rollback = previous is not None and release.version < previous.version
    before = f"v{previous.version}" if previous else "없음"
    message = f"{'릴리스 롤백' if rollback else '릴리스 전환'}: {before} → v{release.version}"
    if reason:
        message += f": {reason}"
    AdminEvent.objects.create(
        type=AdminEvent.EventType.CHALLENGE_RELEASE_ACTIVATED,
        severity=AdminEvent.Severity.WARNING if rollback else AdminEvent.Severity.INFO,
        message=message,
        challenge=challenge,
        actor=actor,
    )


def _current_release_id(challenge):
    config = ChallengeRuntimeConfig.objects.filter(challenge=challenge).first()
    if config is None:
        return None
    return config.current_release_id


@method_decorator(never_cache, name="dispatch")
class ReleaseListCreateView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request, challenge_id):
        # 문제의 릴리스 이력을 최신 버전부터 반환한다
        challenge = _get_challenge(challenge_id)
        if challenge is None:
            return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

        current_id = _current_release_id(challenge)
        releases = (
            ChallengeRelease.objects
            .filter(challenge=challenge)
            .prefetch_related("containers")
            .order_by("-version")
        )
        bundles = list(challenge.user_file_bundles.all())
        rows = [serialize_release(release, current_id, bundles) for release in releases]

        return ok(
            {
                "challenge_id": str(challenge.challenge_id),
                "current_release_id": str(current_id) if current_id else None,
                "releases": rows,
                "total_count": len(rows),
            }
        )

    def post(self, request, challenge_id):
        # 공급망 publish bundle 한 벌을 릴리스로 등록한다
        try:
            validated = validate_release_payload(request.data)
        except ReleaseValidationError as error:
            return fail("RELEASE_INVALID", error.message, 400)

        with transaction.atomic():
            challenge = _get_challenge(challenge_id, for_update=True)
            if challenge is None:
                return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

            try:
                check_slug_consistency(challenge, validated["challenge_slug"])
            except ReleaseValidationError as error:
                return fail("RELEASE_INVALID", error.message, 400)

            duplicated = ChallengeRelease.objects.filter(
                challenge=challenge,
                registry_revision=validated["registry_revision"],
            ).exists()
            if duplicated:
                return fail(
                    "RELEASE_DUPLICATED",
                    "같은 registry_revision의 릴리스가 이미 등록되어 있습니다.",
                    409,
                )

            try:
                release = create_release(challenge, validated, request.user.login_id)
            except ReleaseValidationError as error:
                return fail("RELEASE_INVALID", error.message, 400)
            except RuntimeSecretUnavailable:
                return fail("RUNTIME_SECRET_UNAVAILABLE", "비밀값 저장 설정을 확인하세요", 503)

        return ok(
            serialize_release(release, _current_release_id(challenge)),
            message="릴리스가 등록되었습니다.",
        )


class ReleaseActivateView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, challenge_id, release_id):
        # 지정한 릴리스를 현재 배포 버전으로 전환한다. 옛 버전 지정이 곧 롤백이다
        body = request.data
        if not isinstance(body, dict):
            return fail("INVALID_REQUEST", "요청 본문은 JSON 객체여야 합니다.", 400)
        # 사유는 선택이다. 기존 화면이 빈 본문으로 전환하므로 필수로 바꾸면 깨진다.
        reason = body.get("reason")
        if reason is not None:
            if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
                return fail("INVALID_REQUEST", "reason 은 1~500자 문자열이어야 합니다.", 400)
            reason = reason.strip()
            if "\x00" in reason or any("\ud800" <= char <= "\udfff" for char in reason):
                return fail("INVALID_REQUEST", "reason 에 유효하지 않은 문자가 있습니다.", 400)

        with transaction.atomic():
            challenge = _get_challenge(challenge_id, for_update=True)
            if challenge is None:
                return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

            release = (
                ChallengeRelease.objects
                .prefetch_related("containers")
                .filter(challenge=challenge, release_id=release_id)
                .first()
            )
            if release is None:
                return fail("RELEASE_NOT_FOUND", "존재하지 않는 릴리스 ID입니다.", 404)

            if not is_deployable(release):
                return fail(
                    "RELEASE_NOT_DEPLOYABLE",
                    "현재 Scheduler 계약으로 배포할 수 없는 릴리스입니다.",
                    400,
                )

            if release.repository_env.get("missing"):
                return fail(
                    "RELEASE_ENV_INCOMPLETE",
                    "저장소 환경변수 중 채우지 못한 값이 있습니다 환경변수에서 값을 넣어 새 버전을 만든 뒤 사용해주세요",
                    400,
                )

            if release.approved_at is None:
                release.approved_at = timezone.now()
                release.save(update_fields=["approved_at"])

            config, _ = ChallengeRuntimeConfig.objects.select_for_update().get_or_create(
                challenge=challenge
            )

            previous_release_id = config.current_release_id
            if previous_release_id == release.release_id:
                previous_release_id = None
            else:
                previous = config.current_release
                config.current_release = release
                config.save(update_fields=["current_release", "updated_at"])
                _record_release_switch(
                    challenge, previous, release, reason, request.user.login_id
                )

        return ok(
            {
                "challenge_id": str(challenge.challenge_id),
                "release_id": str(release.release_id),
                "version": release.version,
                "registry_revision": release.registry_revision,
                "previous_release_id": (
                    str(previous_release_id) if previous_release_id else None
                ),
                "activated_at": isoformat_z(timezone.now().replace(microsecond=0)),
            },
            message="현재 릴리스가 전환되었습니다.",
        )


@method_decorator(never_cache, name="dispatch")
class ReleaseDeriveView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, challenge_id, release_id):
        # 원본 이미지·포트·격리는 고정하고 주입 설정만 새 버전으로 복제한다
        with transaction.atomic():
            challenge = _get_challenge(challenge_id, for_update=True)
            if challenge is None:
                return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)
            source = (
                ChallengeRelease.objects.prefetch_related("containers")
                .filter(challenge=challenge, release_id=release_id).first()
            )
            if source is None:
                return fail("RELEASE_NOT_FOUND", "존재하지 않는 릴리스 ID입니다.", 404)
            if not is_deployable(source):
                return fail("RELEASE_NOT_DEPLOYABLE", "실행할 수 없는 릴리스에서는 설정을 만들 수 없습니다.", 400)
            try:
                validated = validate_derived_settings(request.data, source)
                release = create_release(challenge, validated, request.user.login_id, derived_from=source)
            except ReleaseValidationError as error:
                return fail("RELEASE_INVALID", error.message, 400)
            except RuntimeSecretUnavailable:
                return fail("RUNTIME_SECRET_UNAVAILABLE", "비밀값 저장 설정을 확인하세요", 503)
        return ok(
            serialize_release(release, _current_release_id(challenge)),
            message="실행 설정 버전이 등록되었습니다.",
        )
