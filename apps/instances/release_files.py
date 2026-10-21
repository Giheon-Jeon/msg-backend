"""릴리스별 참가자 파일 선택. 파일 저장소 접근은 다운로드 때만 수행한다."""
from apps.instances.models import ChallengeUserFileBundle, UserFilesMode

USER_FILES_ARCHIVE = "user-files.zip"


def file_bundle_for_release(release, bundles=None):
    if release.user_files_mode == UserFilesMode.NONE:
        return None
    if bundles is not None:
        return next((bundle for bundle in bundles if bundle.challenge_id == release.challenge_id and (
            bundle.pk == release.user_file_bundle_id if release.user_files_mode == UserFilesMode.SELECTED
            else bundle.registry_revision == release.registry_revision
        )), None)
    queryset = ChallengeUserFileBundle.objects.filter(challenge_id=release.challenge_id)
    if release.user_files_mode == UserFilesMode.SELECTED:
        return queryset.filter(pk=release.user_file_bundle_id).first()
    return queryset.filter(registry_revision=release.registry_revision).first()


def file_bundle_metadata(bundle):
    if bundle is None:
        return None
    return {
        "file_id": str(bundle.pk), "file_name": USER_FILES_ARCHIVE,
        "present": bundle.present, "download_available": bool(bundle.present and bundle.object_key),
        "file_size": bundle.size_bytes, "file_count": bundle.file_count,
        "uncompressed_size_bytes": bundle.uncompressed_size_bytes,
        "registry_revision": bundle.registry_revision, "source_sha": bundle.source_sha,
        "sha256": bundle.sha256, "created_at": bundle.created_at.isoformat().replace("+00:00", "Z"),
    }


def release_files_summary(release, bundles=None):
    bundle = file_bundle_for_release(release, bundles)
    if release.user_files_mode == UserFilesMode.NONE:
        status = "disabled"
    elif bundle is None:
        status = "unavailable" if release.user_files_mode == UserFilesMode.SELECTED else "pending"
    elif not bundle.present:
        status = "none"
    else:
        status = "available" if bundle.object_key else "unavailable"
    return {
        "mode": release.user_files_mode, "status": status,
        "file": file_bundle_metadata(bundle),
        "matches_image_revision": bundle.registry_revision == release.registry_revision if bundle else None,
    }
