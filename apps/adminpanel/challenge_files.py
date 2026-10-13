"""관리자용 참가자 ZIP 조회. 원본 저장소 목록과 배포 파일을 구분한다."""
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
from apps.instances.models import ChallengeUserFileBundle
from apps.instances.user_files import selected_user_file_bundle, USER_FILES_ARCHIVE


def _bundle_metadata(bundle):
    if bundle is None:
        return None
    return {
        "file_id": str(bundle.pk),
        "file_name": USER_FILES_ARCHIVE,
        "present": bundle.present,
        "download_available": bool(bundle.present and bundle.object_key),
        "file_size": bundle.size_bytes,
        "file_count": bundle.file_count,
        "uncompressed_size_bytes": bundle.uncompressed_size_bytes,
        "registry_revision": bundle.registry_revision,
        "source_sha": bundle.source_sha,
        "sha256": bundle.sha256,
        "created_at": bundle.created_at,
    }


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def challenge_files(request, challenge_id):
    challenge = Challenge.objects.select_related("runtime_config__current_release").filter(pk=challenge_id).first()
    if challenge is None:
        return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다", 404)
    config = getattr(challenge, "runtime_config", None)
    release = config.current_release if config else None
    bundle = selected_user_file_bundle(challenge)
    if config and release is None:
        status = "no_release"
    elif bundle is None:
        status = "pending"
    elif not bundle.present:
        status = "none"
    elif not bundle.object_key:
        status = "unavailable"
    else:
        status = "available"
    # 최신 20건과 현재 선택된 파일을 따로 보내 이전 revision도 정확히 표시한다
    history = list(challenge.user_file_bundles.order_by("-registry_revision")[:21])
    storage = settings.STORAGES["default"]
    is_gcs = storage["BACKEND"] == "storages.backends.gcloud.GoogleCloudStorage"
    return ok({
        "status": status,
        "release_version": release.version if release else None,
        "registry_revision": release.registry_revision if release else (bundle.registry_revision if bundle else None),
        "current_file": _bundle_metadata(bundle),
        "history": [_bundle_metadata(item) for item in history[:20] if bundle is None or item.pk != bundle.pk],
        "history_has_more": len(history) > 20,
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
