import hashlib
import io
import json
import logging
import re
import stat
import zipfile
from pathlib import PurePosixPath
from urllib.error import HTTPError, URLError

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError

from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    ChallengeUserFileBundle,
    PollerArtifact,
)
from apps.instances.poller import (
    expected_source_ref,
    get_workflow_run,
    github_request,
    mark_artifact_processed,
    match_challenge,
    pending_artifacts,
    validate_workflow_run_source,
    workflow_run_is_retryable,
)
from apps.instances.releases import ReleaseValidationError
from apps.instances.release_files import file_bundle_for_release, USER_FILES_ARCHIVE

logger = logging.getLogger(__name__)

USER_FILES_BUNDLE_SUFFIX = "-user-files-bundle"
USER_FILES_MANIFEST = "user-files.json"
MAX_FILE_COUNT = 1_000
MAX_UNCOMPRESSED_SIZE = 100 * 1024 * 1024
MAX_ARTIFACT_SIZE = 110 * 1024 * 1024
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _integer(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReleaseValidationError(f"{name} 값이 올바르지 않습니다")
    if maximum is not None and value > maximum:
        raise ReleaseValidationError(f"{name} 값이 허용 범위를 초과합니다")
    return value


def validate_user_files_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.0":
        raise ReleaseValidationError("user-files schema_version은 1.0이어야 합니다")

    slug = manifest.get("challenge_slug")
    if not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug):
        raise ReleaseValidationError("user-files challenge_slug 값이 올바르지 않습니다")

    source_ref = manifest.get("source_ref")
    if source_ref != expected_source_ref():
        raise ReleaseValidationError("user-files source_ref가 등록 허용 ref와 일치하지 않습니다")

    source_sha = manifest.get("source_sha")
    if not isinstance(source_sha, str) or not SHA_PATTERN.fullmatch(source_sha):
        raise ReleaseValidationError("user-files source_sha 값이 올바르지 않습니다")

    revision = _integer(manifest.get("registry_revision"), "registry_revision", minimum=1)
    user_files = manifest.get("user_files")
    if not isinstance(user_files, dict) or not isinstance(user_files.get("present"), bool):
        raise ReleaseValidationError("user_files.present 값이 올바르지 않습니다")

    validated = {
        "challenge_slug": slug,
        "registry_revision": revision,
        "source_ref": source_ref,
        "source_sha": source_sha,
        "present": user_files["present"],
        "sha256": "",
        "size_bytes": 0,
        "file_count": 0,
        "uncompressed_size_bytes": 0,
    }
    if not user_files["present"]:
        return validated

    if user_files.get("archive") != USER_FILES_ARCHIVE:
        raise ReleaseValidationError("user_files.archive 값이 올바르지 않습니다")
    checksum = user_files.get("sha256")
    if not isinstance(checksum, str) or not CHECKSUM_PATTERN.fullmatch(checksum):
        raise ReleaseValidationError("user_files.sha256 값이 올바르지 않습니다")

    validated.update(
        sha256=checksum,
        size_bytes=_integer(
            user_files.get("size_bytes"),
            "user_files.size_bytes",
            minimum=1,
            maximum=MAX_ARTIFACT_SIZE,
        ),
        file_count=_integer(
            user_files.get("file_count"),
            "user_files.file_count",
            minimum=1,
            maximum=MAX_FILE_COUNT,
        ),
        uncompressed_size_bytes=_integer(
            user_files.get("uncompressed_size_bytes"),
            "user_files.uncompressed_size_bytes",
            maximum=MAX_UNCOMPRESSED_SIZE,
        ),
    )
    return validated


def _validate_archive_path(name):
    path = PurePosixPath(name)
    if (
        "\\" in name
        or path.is_absolute()
        or ".." in path.parts
        or not name
        or (path.parts and ":" in path.parts[0])
    ):
        raise ReleaseValidationError("user-files.zip에 안전하지 않은 경로가 있습니다")


def validate_user_files_archive(archive_bytes, validated):
    if archive_bytes is None:
        raise ReleaseValidationError("user-files.zip 파일이 없습니다")
    if len(archive_bytes) != validated["size_bytes"]:
        raise ReleaseValidationError("user-files.zip 크기가 manifest와 일치하지 않습니다")
    if hashlib.sha256(archive_bytes).hexdigest() != validated["sha256"]:
        raise ReleaseValidationError("user-files.zip SHA-256이 manifest와 일치하지 않습니다")

    file_count = 0
    uncompressed_size = 0
    names = set()
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            for entry in archive.infolist():
                _validate_archive_path(entry.filename)
                if entry.filename in names:
                    raise ReleaseValidationError("user-files.zip에 중복 경로가 있습니다")
                names.add(entry.filename)

                mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ReleaseValidationError("user-files.zip에 허용되지 않은 파일 형식이 있습니다")
                if entry.is_dir():
                    continue

                file_count += 1
                uncompressed_size += entry.file_size
                if file_count > MAX_FILE_COUNT or uncompressed_size > MAX_UNCOMPRESSED_SIZE:
                    raise ReleaseValidationError("user-files.zip이 허용 범위를 초과합니다")
    except zipfile.BadZipFile as error:
        raise ReleaseValidationError("user-files.zip 형식이 올바르지 않습니다") from error

    if file_count != validated["file_count"]:
        raise ReleaseValidationError("user-files.zip 파일 수가 manifest와 일치하지 않습니다")
    if uncompressed_size != validated["uncompressed_size_bytes"]:
        raise ReleaseValidationError("user-files.zip 압축 해제 크기가 manifest와 일치하지 않습니다")


def download_user_files_bundle(artifact, token=None):
    raw = github_request(
        "/repos/" + settings.RELEASE_POLL_REPO
        + "/actions/artifacts/" + str(artifact["id"]) + "/zip",
        token=token,
        timeout=30,
        max_bytes=MAX_ARTIFACT_SIZE,
    )
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            names = bundle.namelist()
            if names.count(USER_FILES_MANIFEST) != 1:
                raise ReleaseValidationError("bundle 안에 user-files.json 파일이 없습니다")
            if bundle.getinfo(USER_FILES_MANIFEST).file_size > 1024 * 1024:
                raise ReleaseValidationError("user-files.json 파일이 너무 큽니다")
            manifest = json.loads(bundle.read(USER_FILES_MANIFEST).decode("utf-8"))
            if names.count(USER_FILES_ARCHIVE) > 1:
                raise ReleaseValidationError("bundle 안에 user-files.zip 파일이 중복되었습니다")
            if USER_FILES_ARCHIVE in names and bundle.getinfo(USER_FILES_ARCHIVE).file_size > MAX_ARTIFACT_SIZE:
                raise ReleaseValidationError("user-files.zip 파일이 너무 큽니다")
            archive_bytes = bundle.read(USER_FILES_ARCHIVE) if USER_FILES_ARCHIVE in names else None
    except zipfile.BadZipFile as error:
        raise ReleaseValidationError("user-files artifact 형식이 올바르지 않습니다") from error
    return manifest, archive_bytes


def validate_user_files_source(artifact, workflow_run, validated):
    validate_workflow_run_source(artifact, workflow_run)
    if validated["source_sha"] != workflow_run.get("head_sha"):
        raise ReleaseValidationError("user-files source_sha와 workflow_run sha가 일치하지 않습니다")


def _same_bundle(bundle, validated):
    return (
        bundle.source_ref == validated["source_ref"]
        and bundle.source_sha == validated["source_sha"]
        and bundle.present == validated["present"]
        and bundle.sha256 == validated["sha256"]
        and bundle.size_bytes == validated["size_bytes"]
        and bundle.file_count == validated["file_count"]
        and bundle.uncompressed_size_bytes == validated["uncompressed_size_bytes"]
    )


def register_user_files_bundle(manifest, archive_bytes, artifact):
    try:
        validated = validate_user_files_manifest(manifest)
        artifact_id = artifact.get("id")
        artifact_name = artifact.get("name")
        _integer(artifact_id, "artifact.id", minimum=1)
    except ReleaseValidationError as error:
        return "invalid", error.message
    if (
        not isinstance(artifact_name, str)
        or not artifact_name.endswith(USER_FILES_BUNDLE_SUFFIX)
        or not artifact_name.startswith(validated["challenge_slug"] + "-")
    ):
        return "invalid", "artifact 이름과 challenge_slug가 일치하지 않습니다"

    challenge = match_challenge(manifest)
    if challenge is None:
        return "unmatched", validated["challenge_slug"]

    release = ChallengeRelease.objects.filter(
        challenge=challenge,
        registry_revision=validated["registry_revision"],
    ).first()

    existing = ChallengeUserFileBundle.objects.filter(
        challenge=challenge,
        registry_revision=validated["registry_revision"],
    ).first()
    if existing is not None:
        if not _same_bundle(existing, validated):
            return "invalid", "같은 registry_revision의 참가자 파일 정보가 다릅니다"
        if existing.release_id is None and release is not None:
            existing.release = release
            existing.save(update_fields=["release"])
        return "duplicate", existing

    if ChallengeUserFileBundle.objects.filter(artifact_id=artifact_id).exists():
        return "duplicate", artifact_id

    object_key = ""
    if validated["present"]:
        try:
            validate_user_files_archive(archive_bytes, validated)
        except ReleaseValidationError as error:
            return "invalid", error.message
        object_key = (
            f"participant-files/{validated['challenge_slug']}/"
            f"{validated['registry_revision']}/{validated['source_sha']}/"
            f"{validated['sha256']}.zip"
        )
        if not default_storage.exists(object_key):
            object_key = default_storage.save(object_key, ContentFile(archive_bytes))
    elif archive_bytes is not None:
        return "invalid", "present가 false인 bundle에는 user-files.zip이 없어야 합니다"

    try:
        with transaction.atomic():
            bundle = ChallengeUserFileBundle.objects.create(
                challenge=challenge,
                release=release,
                artifact_id=artifact_id,
                artifact_name=artifact_name,
                object_key=object_key,
                registry_revision=validated["registry_revision"],
                source_ref=validated["source_ref"],
                source_sha=validated["source_sha"],
                present=validated["present"],
                sha256=validated["sha256"],
                size_bytes=validated["size_bytes"],
                file_count=validated["file_count"],
                uncompressed_size_bytes=validated["uncompressed_size_bytes"],
            )
    except IntegrityError:
        return "duplicate", validated["registry_revision"]
    return "registered", bundle


def poll_user_files_once(token=None):
    summary = {"registered": 0, "duplicate": 0, "unmatched": 0, "invalid": 0, "error": 0}
    try:
        artifacts = pending_artifacts(
            PollerArtifact.Kind.USER_FILES,
            USER_FILES_BUNDLE_SUFFIX,
            token=token,
        )
    except ReleaseValidationError as error:
        logger.warning("user-files poller 설정 오류: %s", error.message)
        summary["error"] += 1
        return summary
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        logger.warning("user-files poller artifact 목록 조회 실패: %s", error)
        summary["error"] += 1
        return summary

    for artifact in artifacts:
        try:
            workflow_run = get_workflow_run(artifact, token=token)
            if workflow_run_is_retryable(workflow_run):
                logger.info(
                    "user-files poller workflow 실행 중, 다음 poll에서 재시도: %s",
                    artifact.get("name"),
                )
                continue
            validate_workflow_run_source(artifact, workflow_run)
            manifest, archive_bytes = download_user_files_bundle(artifact, token=token)
            validated = validate_user_files_manifest(manifest)
            validate_user_files_source(artifact, workflow_run, validated)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            logger.warning("user-files poller bundle 다운로드 실패 %s: %s", artifact.get("name"), error)
            summary["error"] += 1
            continue
        except ReleaseValidationError as error:
            logger.warning("user-files poller bundle 형식 오류 %s: %s", artifact.get("name"), error.message)
            summary["invalid"] += 1
            mark_artifact_processed(artifact)
            continue

        try:
            status, detail = register_user_files_bundle(manifest, archive_bytes, artifact)
        except (OSError, GoogleAPIError, GoogleAuthError) as error:
            # GCS SDK 오류도 미처리 상태로 남겨 다음 poll에서 다시 시도한다
            logger.warning(
                "user-files poller 저장 실패 %s (%s)",
                artifact.get("name"), type(error).__name__,
            )
            summary["error"] += 1
            continue
        summary[status] += 1
        if status != "unmatched":
            mark_artifact_processed(artifact)
        if status == "registered":
            logger.info(
                "user-files poller 등록: challenge=%s revision=%s present=%s",
                detail.challenge_id,
                detail.registry_revision,
                detail.present,
            )
        elif status in ("unmatched", "invalid"):
            logger.warning("user-files poller 건너뜀 (%s): %s", status, detail)
    return summary


def selected_user_file_bundle(challenge):
    """현재 배포에 대응하는 수집 결과. 파일이 없는 revision도 그대로 반환한다."""
    try:
        runtime_config = challenge.runtime_config
    except ChallengeRuntimeConfig.DoesNotExist:
        runtime_config = None

    bundles = ChallengeUserFileBundle.objects.filter(challenge=challenge)
    if runtime_config is None:
        bundle = bundles.order_by("-registry_revision", "-created_at").first()
    elif runtime_config.current_release is None:
        return None
    else:
        bundle = file_bundle_for_release(runtime_config.current_release)
    return bundle


def current_user_file_bundle(challenge, *, release=None):
    if release is not None and release.challenge_id != challenge.pk:
        return None
    bundle = file_bundle_for_release(release) if release is not None else selected_user_file_bundle(challenge)
    if bundle is None or not bundle.present or not bundle.object_key:
        return None
    return bundle


def serialize_user_file(challenge, bundle):
    return {
        "file_id": str(bundle.bundle_id),
        "file_name": USER_FILES_ARCHIVE,
        "download_url": (
            f"/api/v1/challenges/{challenge.challenge_id}/files/"
            f"{bundle.bundle_id}/download"
        ),
        "file_size": bundle.size_bytes,
    }
