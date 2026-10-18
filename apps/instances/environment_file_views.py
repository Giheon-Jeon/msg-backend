from django.db import IntegrityError, transaction
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from rest_framework.views import APIView

from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances.admin_views import _get_challenge, _current_release_id
from apps.instances.environment_files import (
    environment_file_preview, import_identity, prepare_environment_file, save_environment_file,
)
from apps.instances.models import ChallengeRelease
from apps.instances.releases import ReleaseValidationError, is_deployable, serialize_release
from apps.instances.runtime_secrets import RuntimeSecretUnavailable


@method_decorator(never_cache, name="dispatch")
class EnvironmentFilePreviewView(APIView):
    permission_classes = [IsAdmin]

    @sensitive_variables()
    def post(self, request, challenge_id, release_id):
        source = ChallengeRelease.objects.select_related("challenge").prefetch_related("containers").filter(
            challenge_id=challenge_id, release_id=release_id,
        ).first()
        if source is None:
            return fail("RELEASE_NOT_FOUND", "존재하지 않는 릴리스입니다", 404)
        if not is_deployable(source):
            return fail("RELEASE_NOT_DEPLOYABLE", "실행할 수 있는 릴리스에서 가져와주세요", 400)
        try:
            plan = prepare_environment_file(source, request.data)
        except ReleaseValidationError as error:
            return fail("ENV_FILE_INVALID", error.message, 400)
        return ok(environment_file_preview(plan))


@method_decorator(never_cache, name="dispatch")
class EnvironmentFileImportView(APIView):
    permission_classes = [IsAdmin]

    @sensitive_variables()
    def post(self, request, challenge_id, release_id):
        # Catch outside atomic: a later validation failure must undo every secret write.
        try:
            with transaction.atomic():
                challenge = _get_challenge(challenge_id, for_update=True)
                if challenge is None:
                    return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제입니다", 404)
                source = ChallengeRelease.objects.select_related("challenge").prefetch_related("containers").filter(
                    challenge=challenge, release_id=release_id,
                ).first()
                if source is None:
                    return fail("RELEASE_NOT_FOUND", "존재하지 않는 릴리스입니다", 404)
                if not is_deployable(source):
                    return fail("RELEASE_NOT_DEPLOYABLE", "실행할 수 있는 릴리스에서 가져와주세요", 400)
                plan = prepare_environment_file(source, request.data)
                if request.data.get("entries") is None:
                    raise ReleaseValidationError("미리보기에서 각 항목을 확인한 뒤 저장해주세요")
                request_id, fingerprint = import_identity(request.data, source)
                existing = ChallengeRelease.objects.filter(env_import_id=request_id).first()
                if existing is not None:
                    if existing.challenge_id != challenge.pk or not constant_time_compare(existing.env_import_fingerprint, fingerprint):
                        return fail("ENV_FILE_REQUEST_CONFLICT", "다른 내용에 사용된 저장 요청입니다 새로 확인해주세요", 409)
                    release = existing
                else:
                    release = save_environment_file(source, plan, request.user.login_id, request_id, fingerprint)
                current_id = _current_release_id(challenge)
        except (ReleaseValidationError, ValueError) as error:
            return fail("ENV_FILE_INVALID", str(error), 400)
        except RuntimeSecretUnavailable:
            return fail("RUNTIME_SECRET_UNAVAILABLE", "비밀값 저장 설정을 확인해주세요", 503)
        except IntegrityError:
            return fail("ENV_FILE_REQUEST_CONFLICT", "저장 요청이 겹쳤습니다 같은 요청으로 다시 확인해주세요", 409)
        return ok(serialize_release(release, current_id), message="새 설정 버전을 저장했습니다 적용할 버전을 선택해주세요")
