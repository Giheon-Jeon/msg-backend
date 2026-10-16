import io
import json
import logging
import uuid
import zipfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.challenge.models import Challenge
from apps.instances.models import ChallengeRelease, PollerArtifact
from apps.instances.releases import (
    ReleaseValidationError,
    check_slug_consistency,
    create_release,
    validate_release_payload,
)

logger = logging.getLogger(__name__)

BUNDLE_NAME_SUFFIX = "-publish-bundle"
BUNDLE_FILE_NAME = "artifact-v2.json"
POLLER_CREATED_BY = "release-poller"
RETRYABLE_WORKFLOW_STATUSES = {"queued", "in_progress"}


def github_request(path, token=None, timeout=10, max_bytes=None):
    # 공급망 artifact 조회용 GitHub API 호출. 응답 본문 bytes를 돌려준다
    base = settings.RELEASE_POLL_API_BASE.rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ReleaseValidationError("RELEASE_POLL_API_BASE는 http 또는 https 주소여야 합니다")

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "msg-backend-release-poller",
    }
    request = Request(base + path, headers=headers)
    if token:
        # Artifact downloads redirect to signed external storage URLs.
        request.add_unredirected_header("Authorization", "Bearer " + token)
    # 위에서 scheme을 http와 https로 제한한 운영 설정만 사용한다
    with urlopen(request, timeout=timeout) as response:  # nosec B310
        body = response.read() if max_bytes is None else response.read(max_bytes + 1)
    if max_bytes is not None and len(body) > max_bytes:
        raise ReleaseValidationError("GitHub artifact 크기가 허용 범위를 초과합니다")
    return body


def list_artifacts_by_suffix(suffix, token=None, stop_at_id=None):
    # 문제 저장소의 Actions artifact 전체에서 지정한 bundle만 최신순으로 돌려준다
    page_size = min(max(settings.RELEASE_POLL_LIMIT, 1), 100)
    page = 1
    bundles = []

    while True:
        raw = github_request(
            "/repos/" + settings.RELEASE_POLL_REPO
            + "/actions/artifacts?per_page=" + str(page_size)
            + "&page=" + str(page),
            token=token,
        )
        payload = json.loads(raw.decode("utf-8"))
        artifacts = payload.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise ReleaseValidationError(
                "GitHub artifact 목록 형식이 올바르지 않습니다"
            )

        reached_boundary = False
        for entry in artifacts:
            artifact_id = entry.get("id")
            if (
                stop_at_id is not None
                and isinstance(artifact_id, int)
                and not isinstance(artifact_id, bool)
                and artifact_id <= stop_at_id
            ):
                reached_boundary = True
                break
            if entry.get("name", "").endswith(suffix) and not entry.get("expired"):
                bundles.append(entry)

        if reached_boundary:
            break

        total_count = payload.get("total_count")
        if isinstance(total_count, int):
            if page * page_size >= total_count:
                break
        elif len(artifacts) < page_size:
            break

        if not artifacts:
            break
        page += 1

    return bundles


def pending_artifacts(kind, suffix, token=None):
    last_seen_id = PollerArtifact.objects.filter(kind=kind).aggregate(
        value=Max("artifact_id")
    )["value"]
    discovered = list_artifacts_by_suffix(
        suffix,
        token=token,
        stop_at_id=last_seen_id,
    )
    rows = []
    for artifact in discovered:
        artifact_id = artifact.get("id")
        if (
            isinstance(artifact_id, bool)
            or not isinstance(artifact_id, int)
            or artifact_id <= 0
        ):
            raise ReleaseValidationError("artifact id 값이 올바르지 않습니다")
        rows.append(
            PollerArtifact(
                artifact_id=artifact_id,
                kind=kind,
                payload=artifact,
            )
        )
    PollerArtifact.objects.bulk_create(rows, ignore_conflicts=True)
    return list(
        PollerArtifact.objects.filter(kind=kind, processed_at__isnull=True)
        .order_by("artifact_id")
        .values_list("payload", flat=True)
    )


def mark_artifact_processed(artifact):
    PollerArtifact.objects.filter(artifact_id=artifact["id"]).update(
        processed_at=timezone.now()
    )


def list_bundle_artifacts(token=None):
    return pending_artifacts(
        PollerArtifact.Kind.RELEASE,
        BUNDLE_NAME_SUFFIX,
        token=token,
    )


def workflow_run_id(artifact):
    workflow_run = artifact.get("workflow_run")
    if not isinstance(workflow_run, dict):
        raise ReleaseValidationError("artifact workflow_run 값이 올바르지 않습니다")

    run_id = workflow_run.get("id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise ReleaseValidationError("artifact workflow_run.id 값이 올바르지 않습니다")
    return run_id


def get_workflow_run(artifact, token=None):
    raw = github_request(
        "/repos/" + settings.RELEASE_POLL_REPO
        + "/actions/runs/" + str(workflow_run_id(artifact)),
        token=token,
    )
    workflow_run = json.loads(raw.decode("utf-8"))
    if not isinstance(workflow_run, dict):
        raise ReleaseValidationError("workflow_run 응답 값이 올바르지 않습니다")
    return workflow_run


def workflow_run_is_retryable(workflow_run):
    return workflow_run.get("status") in RETRYABLE_WORKFLOW_STATUSES


def download_bundle(artifact, token=None):
    # bundle zip을 내려받아 artifact-v2.json 내용을 dict로 돌려준다
    raw = github_request(
        "/repos/" + settings.RELEASE_POLL_REPO
        + "/actions/artifacts/" + str(artifact["id"]) + "/zip",
        token=token,
        timeout=30,
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for name in archive.namelist():
            if name.split("/")[-1] == BUNDLE_FILE_NAME:
                return json.loads(archive.read(name).decode("utf-8"))
    raise ReleaseValidationError("bundle 안에 " + BUNDLE_FILE_NAME + " 파일이 없습니다")


def expected_source_ref():
    value = getattr(settings, "RELEASE_POLL_SOURCE_REF", "refs/heads/main")
    return str(value or "").strip()


def branch_from_source_ref(source_ref):
    prefix = "refs/heads/"
    if not source_ref.startswith(prefix):
        raise ReleaseValidationError("RELEASE_POLL_SOURCE_REF는 refs/heads/* 형식이어야 합니다")
    return source_ref[len(prefix):]


def source_sha(artifact_data):
    value = artifact_data.get("source_sha") or artifact_data.get("commit_sha")
    if not isinstance(value, str) or not value.strip():
        raise ReleaseValidationError("bundle source_sha 값이 올바르지 않습니다")
    return value.strip()


def validate_workflow_run_source(artifact, workflow_run):
    expected_ref = expected_source_ref()
    expected_branch = branch_from_source_ref(expected_ref)
    artifact_run = artifact.get("workflow_run") or {}

    if workflow_run.get("id") != workflow_run_id(artifact):
        raise ReleaseValidationError("artifact와 workflow_run id가 일치하지 않습니다")
    if workflow_run.get("status") != "completed" or workflow_run.get("conclusion") != "success":
        raise ReleaseValidationError("성공한 workflow_run의 bundle만 등록할 수 있습니다")
    if workflow_run.get("head_branch") != expected_branch:
        raise ReleaseValidationError("workflow_run branch가 등록 허용 branch와 일치하지 않습니다")
    if artifact_run.get("head_branch") and artifact_run.get("head_branch") != workflow_run.get("head_branch"):
        raise ReleaseValidationError("artifact와 workflow_run branch가 일치하지 않습니다")
    if artifact_run.get("head_sha") and artifact_run.get("head_sha") != workflow_run.get("head_sha"):
        raise ReleaseValidationError("artifact와 workflow_run sha가 일치하지 않습니다")


def validate_bundle_source(artifact, workflow_run, artifact_data):
    validate_workflow_run_source(artifact, workflow_run)
    expected_ref = expected_source_ref()
    if artifact_data.get("source_ref") != expected_ref:
        raise ReleaseValidationError("bundle source_ref가 등록 허용 ref와 일치하지 않습니다")
    if source_sha(artifact_data) != workflow_run.get("head_sha"):
        raise ReleaseValidationError("bundle source_sha와 workflow_run sha가 일치하지 않습니다")


def match_challenge(artifact_data):
    challenge_id = artifact_data.get("challenge_id")
    if challenge_id:
        try:
            uuid.UUID(str(challenge_id))
        except (TypeError, ValueError):
            return None

        return Challenge.objects.filter(challenge_id=challenge_id).first()

    slug = artifact_data.get("challenge_slug", "")
    challenge = Challenge.objects.filter(challenge_slug=slug).first()
    if challenge is not None:
        return challenge

    # 기존 데이터 중 Challenge에 slug가 없는 문제는 릴리스 이력으로 호환한다.
    release = (
        ChallengeRelease.objects
        .filter(challenge_slug=slug)
        .select_related("challenge")
        .first()
    )
    if release is not None:
        return release.challenge

    return None


def register_bundle(artifact_data, note=None):
    # bundle 한 벌을 검증해 릴리스로 등록한다. 결과는 (상태, 상세) 튜플이다
    try:
        validated = validate_release_payload({"artifact": artifact_data, "note": note})
    except ReleaseValidationError as error:
        return "invalid", error.message

    challenge = match_challenge(artifact_data)
    if challenge is None:
        return "unmatched", artifact_data.get("challenge_slug", "")

    with transaction.atomic():
        challenge = Challenge.objects.select_for_update().get(pk=challenge.pk)

        duplicated = ChallengeRelease.objects.filter(
            challenge=challenge,
            registry_revision=validated["registry_revision"],
        ).exists()
        if duplicated:
            return "duplicate", validated["registry_revision"]

        try:
            check_slug_consistency(challenge, validated["challenge_slug"])
        except ReleaseValidationError as error:
            return "invalid", error.message

        release = create_release(challenge, validated, POLLER_CREATED_BY)

    return "registered", release


def poll_once(token=None):
    # 공급망 bundle을 한 바퀴 수집해 새 릴리스만 등록한다. 전환은 하지 않는다
    summary = {"registered": 0, "duplicate": 0, "unmatched": 0, "invalid": 0, "error": 0}
    try:
        artifacts = list_bundle_artifacts(token=token)
    except ReleaseValidationError as error:
        logger.warning("release poller 설정 오류: %s", error.message)
        summary["error"] += 1
        return summary
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        logger.warning("release poller artifact 목록 조회 실패: %s", error)
        summary["error"] += 1
        return summary

    for artifact in artifacts:
        try:
            workflow_run = get_workflow_run(artifact, token=token)
            if workflow_run_is_retryable(workflow_run):
                logger.info(
                    "release poller workflow 실행 중, 다음 poll에서 재시도: %s",
                    artifact.get("name"),
                )
                continue
            validate_workflow_run_source(artifact, workflow_run)
            artifact_data = download_bundle(artifact, token=token)
        except (HTTPError, URLError, TimeoutError, ValueError, zipfile.BadZipFile) as error:
            logger.warning("release poller bundle 다운로드 실패 %s: %s", artifact.get("name"), error)
            summary["error"] += 1
            continue
        except ReleaseValidationError as error:
            logger.warning("release poller bundle 형식 오류 %s: %s", artifact.get("name"), error.message)
            summary["invalid"] += 1
            mark_artifact_processed(artifact)
            continue

        try:
            validate_bundle_source(artifact, workflow_run, artifact_data)
        except ReleaseValidationError as error:
            logger.warning("release poller bundle 출처 오류 %s: %s", artifact.get("name"), error.message)
            summary["invalid"] += 1
            mark_artifact_processed(artifact)
            continue

        status, detail = register_bundle(
            artifact_data,
            note="공급망 자동 등록: " + str(artifact.get("name", "")),
        )
        summary[status] += 1
        if status != "unmatched":
            mark_artifact_processed(artifact)
        if status == "registered":
            logger.info(
                "release poller 등록: challenge=%s version=%s revision=%s",
                detail.challenge_id,
                detail.version,
                detail.registry_revision,
            )
        elif status in ("unmatched", "invalid"):
            logger.warning("release poller 건너뜀 (%s): %s", status, detail)

    return summary
