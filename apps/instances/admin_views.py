from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.views import APIView

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
                config.current_release = release
                config.save(update_fields=["current_release", "updated_at"])

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
