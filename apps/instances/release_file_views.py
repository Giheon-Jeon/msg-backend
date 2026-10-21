from uuid import UUID

from django.db import transaction
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.views import APIView

from apps.common.idempotency import run_idempotent
from apps.common.permissions import IsAdmin
from apps.common.response import fail
from apps.instances.admin_views import _get_challenge, _current_release_id
from apps.instances.models import ChallengeRelease, ChallengeUserFileBundle, ReleaseContainer, UserFilesMode
from apps.instances.releases import is_deployable, next_release_version, serialize_release


@method_decorator(never_cache, name="dispatch")
class ReleaseFilesConfigureView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, challenge_id, release_id):
        body = request.data
        if not isinstance(body, dict) or set(body) - {"mode", "file_id"}:
            return fail("RELEASE_FILES_INVALID", "파일 설정 형식을 확인해주세요", 400)
        mode = body.get("mode")
        if mode not in UserFilesMode.values or (mode != UserFilesMode.SELECTED and "file_id" in body):
            return fail("RELEASE_FILES_INVALID", "파일 연결 방식을 확인해주세요", 400)
        file_id = None
        if mode == UserFilesMode.SELECTED:
            try:
                file_id = UUID(body["file_id"]) if isinstance(body.get("file_id"), str) else None
            except ValueError:
                pass
            if file_id is None:
                return fail("RELEASE_FILES_INVALID", "연결할 ZIP을 선택해주세요", 400)
        with transaction.atomic():
            challenge = _get_challenge(challenge_id, for_update=True)
            if challenge is None:
                return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제입니다", 404)
            source = ChallengeRelease.objects.filter(challenge=challenge, pk=release_id).prefetch_related("containers").first()
            if source is None:
                return fail("RELEASE_NOT_FOUND", "존재하지 않는 배포 버전입니다", 404)
            if not is_deployable(source):
                return fail("RELEASE_NOT_DEPLOYABLE", "배포할 수 있는 버전에서 파일을 설정해주세요", 400)
            bundle = None
            if file_id:
                bundle = ChallengeUserFileBundle.objects.filter(challenge=challenge, pk=file_id, present=True).first()
                if bundle is None or not bundle.object_key:
                    return fail("FILE_NOT_FOUND", "이 문제에 등록된 ZIP을 선택해주세요", 404)

            def save():
                # 기존 이미지·환경변수·비밀값 UUID는 그대로 복제한다. 최신 비밀값으로 다시 연결하지 않는다
                fields = {name: getattr(source, name) for name in (
                    "registry_revision", "challenge_slug", "runtime_type", "architecture", "isolation_profile",
                    "cpu_millicores", "memory_mib", "ephemeral_storage_mib", "healthcheck", "source_ref",
                )}
                release = ChallengeRelease.objects.create(
                    challenge=challenge, version=next_release_version(challenge), derived_from=source,
                    user_files_mode=mode, user_file_bundle=bundle,
                    note="참가자 파일 연결 변경", created_by=request.user.login_id, **fields,
                )
                ReleaseContainer.objects.bulk_create([
                    ReleaseContainer(release=release, **{name: getattr(container, name) for name in (
                        "name", "image_ref", "ports", "env", "secret_env", "env_rules",
                    )}) for container in source.containers.all()
                ])
                return serialize_release(release, _current_release_id(challenge))

            return run_idempotent(request, body, save, message="파일 연결을 새 버전으로 저장했습니다 적용할 버전을 선택해주세요")
