"""관리자용 참가자 ZIP 조회. 원본 저장소 목록과 배포 파일을 구분한다."""
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import default_storage
from django.http import FileResponse
from django.views.decorators.cache import never_cache
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from rest_framework.decorators import api_view, permission_classes

from apps.challenge.models import Challenge
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances.models import ChallengeRelease, ChallengeUserFileBundle
from apps.instances.release_files import file_bundle_for_release, file_bundle_metadata, release_files_summary
from apps.instances.user_files import selected_user_file_bundle, USER_FILES_ARCHIVE


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def challenge_files(request, challenge_id):
    challenge = Challenge.objects.select_related("runtime_config__current_release").filter(pk=challenge_id).first()
    if challenge is None:
        return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다", 404)
    config = getattr(challenge, "runtime_config", None)
    release = config.current_release if config else None
    requested_release_id = request.query_params.get("release_id")
    if requested_release_id is not None:
        try:
            requested_release_id = UUID(requested_release_id)
        except ValueError:
            return fail("INVALID_REQUEST", "배포 버전 ID를 확인해주세요", 400)
        release = ChallengeRelease.objects.filter(challenge=challenge, pk=requested_release_id).first()
        if release is None:
            return fail("RELEASE_NOT_FOUND", "존재하지 않는 배포 버전입니다", 404)
    bundle = file_bundle_for_release(release) if release else selected_user_file_bundle(challenge)
    if release:
        status = release_files_summary(release)["status"]
    elif config:
        status = "no_release"
    elif bundle is None:
        status = "pending"
    elif not bundle.present:
        status = "none"
    elif not bundle.object_key:
        status = "unavailable"
    else:
        status = "available"
    try:
        page = int(request.query_params.get("page", "1"))
        if not 1 <= page <= 100000:
            raise ValueError
    except (ValueError, TypeError):
        return fail("INVALID_REQUEST", "파일 목록 페이지를 확인해주세요", 400)
    history_query = challenge.user_file_bundles.order_by("-registry_revision")
    if bundle:
        history_query = history_query.exclude(pk=bundle.pk)
    offset = (page - 1) * 20
    history = list(history_query[offset:offset + 21])
    matching = (challenge.user_file_bundles.filter(registry_revision=release.registry_revision).first()
                if release else None)
    storage = settings.STORAGES["default"]
    is_gcs = storage["BACKEND"] == "storages.backends.gcloud.GoogleCloudStorage"
    return ok({
        "status": status,
        "release_version": release.version if release else None,
        "registry_revision": release.registry_revision if release else (bundle.registry_revision if bundle else None),
        "release_id": str(release.pk) if release else None,
        "is_current": bool(release and config and config.current_release_id == release.pk),
        "mode": release.user_files_mode if release else "LATEST",
        "current_file": file_bundle_metadata(bundle),
        "matching_file": file_bundle_metadata(matching),
        "history": [file_bundle_metadata(item) for item in history[:20]],
        "history_has_more": len(history) > 20,
        "history_next_page": page + 1 if len(history) > 20 else None,
        "storage": {"provider": "gcs" if is_gcs else "local",
                    "bucket": storage.get("OPTIONS", {}).get("bucket_name", getattr(settings, "GS_BUCKET_NAME", "")) if is_gcs else None},
    })


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def challenge_file_download(request, challenge_id, file_id):
    bundle = ChallengeUserFileBundle.objects.filter(pk=file_id, challenge_id=challenge_id, present=True).first()
    if bundle is None or not bundle.object_key:
        return fail("FILE_NOT_FOUND", "등록된 참가자 파일이 없습니다", 404)
    try:
        if not default_storage.exists(bundle.object_key):
            return fail("FILE_NOT_FOUND", "저장소에서 ZIP 파일을 찾지 못했습니다", 404)
        archive = default_storage.open(bundle.object_key, "rb")
        return FileResponse(archive, as_attachment=True, filename=USER_FILES_ARCHIVE, content_type="application/zip")
    except FileNotFoundError:
        return fail("FILE_NOT_FOUND", "저장소에서 ZIP 파일을 찾지 못했습니다", 404)
    except (OSError, GoogleAPIError, GoogleAuthError, ImproperlyConfigured):
        return fail("FILE_STORAGE_UNAVAILABLE", "파일 저장소에 연결하지 못했습니다 인증과 읽기 권한을 확인해주세요", 503)
