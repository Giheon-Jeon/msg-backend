"""Keep CI participant archives tied to the source of the running KOTH image."""
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Subquery, Q
from django.http import FileResponse
from django.views.decorators.cache import never_cache
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from rest_framework.decorators import api_view, permission_classes

from apps.common.permissions import IsAdmin, IsAuthenticated
from apps.common.response import fail
from apps.instances.releases import ReleaseValidationError
from apps.instances.user_files import _same_bundle, validate_user_files_archive

from .models import KothChallenge, KothChallengeStatus, KothUserFileBundle


def register_koth_user_files(validated, archive_bytes, artifact):
    # The shared poller has already validated the manifest and trusted workflow.
    challenge = KothChallenge.objects.filter(
        repository_slug=validated["challenge_slug"]
    ).first()
    if challenge is None:
        return "unmatched", validated["challenge_slug"]
    existing = KothUserFileBundle.objects.filter(
        challenge=challenge, registry_revision=validated["registry_revision"]
    ).first()
    if existing is not None:
        if not _same_bundle(existing, validated):
            return "invalid", "같은 registry_revision의 참가자 파일 정보가 다릅니다"
        return "duplicate", existing
    if KothUserFileBundle.objects.filter(artifact_id=artifact["id"]).exists():
        return "invalid", "다른 배포 버전에 이미 등록된 artifact입니다"

    object_key = ""
    if validated["present"]:
        try:
            validate_user_files_archive(archive_bytes, validated)
        except ReleaseValidationError as error:
            return "invalid", error.message
        object_key = (
            f"koth-participant-files/{validated['challenge_slug']}/"
            f"{validated['registry_revision']}/{validated['source_sha']}/"
            f"{validated['sha256']}.zip"
        )
        if not default_storage.exists(object_key):
            object_key = default_storage.save(object_key, ContentFile(archive_bytes))
    elif archive_bytes is not None:
        return "invalid", "present가 false인 bundle에는 user-files.zip이 없어야 합니다"

    values = {
        name: validated[name]
        for name in (
            "registry_revision", "source_ref", "source_sha", "present", "sha256",
            "size_bytes", "file_count", "uncompressed_size_bytes",
        )
    }
    try:
        with transaction.atomic():
            bundle = KothUserFileBundle.objects.create(
                challenge=challenge,
                artifact_id=artifact["id"],
                artifact_name=artifact["name"],
                object_key=object_key,
                **values,
            )
    except IntegrityError:
        existing = KothUserFileBundle.objects.filter(
            challenge=challenge, registry_revision=validated["registry_revision"]
        ).first()
        if existing is None or not _same_bundle(existing, validated):
            return "invalid", "동시에 등록된 참가자 파일 정보가 다릅니다"
        return "duplicate", existing
    # Registering a newer file must never change the running image/source pointer.
    return "registered", bundle


def with_file_metadata(queryset):
    selected = KothUserFileBundle.objects.filter(
        challenge_id=OuterRef("pk"), source_sha=OuterRef("source_revision")
    ).filter(Q(registry_revision=OuterRef("active_publication__registry_revision")) | Q(challenge__active_publication__isnull=True)).order_by("-registry_revision")
    return queryset.annotate(**{
        "selected_file_" + field: Subquery(selected.values(field)[:1])
        for field in ("present", "object_key", "sha256", "size_bytes")
    })


def public_file_metadata(challenge):
    if (
        challenge.status != KothChallengeStatus.ACTIVE
        or challenge.files_mode == "NONE"
        or not challenge.selected_file_present
        or not challenge.selected_file_object_key
    ):
        return None
    return {
        "file_name": challenge.repository_slug + ".zip",
        "size_bytes": challenge.selected_file_size_bytes,
        "sha256": challenge.selected_file_sha256,
        "source_revision": challenge.source_revision,
    }


def _download(challenge_id, *, admin=False, publication_id=None):
    challenge = KothChallenge.objects.filter(pk=challenge_id).first()
    if challenge is None or challenge.files_mode == "NONE" or (not admin and challenge.status != KothChallengeStatus.ACTIVE):
        return fail("FILE_NOT_FOUND", "다운로드할 참가자 파일이 없습니다", 404)
    if admin and publication_id:
        from .environment import canonical_uuid, EnvironmentError
        try:
            publication = challenge.publications.filter(pk=canonical_uuid(publication_id)).first()
        except EnvironmentError:
            publication = None
        if publication is None:
            return fail("KOTH_PUBLICATION_NOT_FOUND", "이 문제의 발행본을 찾지 못했습니다", 404)
        bundle = challenge.user_file_bundles.filter(source_sha=publication.source_sha, registry_revision=publication.registry_revision).first()
    else:
        bundles = challenge.user_file_bundles.filter(source_sha=challenge.source_revision)
        if challenge.active_publication_id:
            bundles = bundles.filter(registry_revision=challenge.active_publication.registry_revision)
        bundle = bundles.order_by("-registry_revision").first()
    if bundle is None or not bundle.present or not bundle.object_key:
        return fail("FILE_NOT_FOUND", "다운로드할 참가자 파일이 없습니다", 404)
    try:
        archive = default_storage.open(bundle.object_key, "rb")
        response = FileResponse(
            archive,
            as_attachment=True,
            filename=challenge.repository_slug + ".zip",
            content_type="application/zip",
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Length"] = bundle.size_bytes
        return response
    except FileNotFoundError:
        return fail("FILE_NOT_FOUND", "저장소에서 ZIP 파일을 찾지 못했습니다", 404)
    except (OSError, GoogleAPIError, GoogleAuthError, ImproperlyConfigured):
        return fail("FILE_STORAGE_UNAVAILABLE", "파일 저장소에 연결하지 못했습니다", 503)


@never_cache
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def challenge_file(request, challenge_id):
    return _download(challenge_id)


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def admin_challenge_file(request, challenge_id):
    return _download(challenge_id, admin=True, publication_id=request.query_params.get("publication_id"))
