"""Inspect immutable CI artifacts and import them without activating a release."""
import base64
import io
import json
import re
import zipfile
from urllib.parse import quote

import yaml
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_variables

from apps.challenge.models import Challenge
from apps.challenge.services import hash_flag, is_correct_flag
from apps.common.exceptions import APIError
from apps.instances import poller, user_files
from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig, ChallengeUserFileBundle, RuntimeSecret
from apps.instances.releases import ReleaseValidationError, create_release, validate_release_payload
from apps.instances.runtime_secrets import create_runtime_secret

NAME = re.compile(r"^(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<run>[1-9][0-9]*)-[1-9][0-9]*-[a-f0-9]+-(?P<kind>publish|user-files)-bundle$")
SHA = re.compile(r"^[a-f0-9]{40}$")
SALT = "admin-published-challenge-v1"
PREVIEW_SECONDS = 1200


class PublishedImportError(APIError):
    def __init__(self, code, message, status_code=400):
        self.code, self.status_code = code, status_code
        super().__init__(message)


def _json(path):
    return json.loads(poller.github_request(
        f"/repos/{settings.RELEASE_POLL_REPO}{path}",
        token=settings.RELEASE_POLL_GITHUB_TOKEN, timeout=10, max_bytes=2 * 1024 * 1024,
    ))


def artifact_identity(artifact):
    if not isinstance(artifact, dict) or type(artifact.get("id")) is not int or artifact["id"] <= 0:
        return None
    if not isinstance(artifact.get("name"), str):
        return None
    match = NAME.fullmatch(artifact["name"])
    run = artifact.get("workflow_run") or {}
    if (not isinstance(run, dict) or not isinstance(run.get("head_sha"), str)
            or match is None or artifact.get("expired") or len(match["slug"]) > 100
            or match["slug"] in settings.REPOSITORY_ENV_EXCLUDED_SLUGS
            or run.get("head_branch") != poller.branch_from_source_ref(poller.expected_source_ref())
            or int(match["run"]) != run.get("id") or not SHA.fullmatch(run.get("head_sha", ""))):
        return None
    return {"challenge_slug": match["slug"], "run_id": run["id"], "kind": match["kind"], "source_sha": run["head_sha"]}


def published_catalog(page):
    # Bound each browser request; older publications are loaded on demand.
    key = "published-catalog-v2:" + salted_hmac(SALT, f"{settings.RELEASE_POLL_REPO}:{poller.expected_source_ref()}:{page}").hexdigest()
    result = cache.get(key)
    if result is None:
        groups = {}
        next_page = None
        for current in range(page, page + 3):
            data = _json(f"/actions/artifacts?per_page=100&page={current}")
            artifacts = data.get("artifacts")
            if not isinstance(artifacts, list):
                raise ReleaseValidationError("발행 목록 형식을 확인하지 못했습니다")
            for artifact in artifacts:
                identity = artifact_identity(artifact)
                if identity is None:
                    continue
                group_key = (identity["challenge_slug"], identity["run_id"])
                row = groups.setdefault(group_key, {
                    "artifact_id": artifact["id"], "challenge_slug": identity["challenge_slug"],
                    "run_id": identity["run_id"], "source_sha": identity["source_sha"],
                    "published_at": artifact.get("created_at"), "has_release_artifact": False, "has_files_artifact": False,
                })
                row["has_release_artifact" if identity["kind"] == "publish" else "has_files_artifact"] = True
            next_page = current + 1 if len(artifacts) == 100 and current * 100 < data.get("total_count", 0) else None
            if next_page is None or len(groups) >= 12:
                break
        rows = list(groups.values())
        if rows:
            newest = max((row["published_at"] or "" for row in rows))
            branch = quote(poller.branch_from_source_ref(poller.expected_source_ref()), safe="")
            cutoff = f"&created=%3C%3D{quote(newest, safe='')}" if newest else ""
            runs = _json(f"/actions/runs?branch={branch}&per_page=100{cutoff}").get("workflow_runs", [])
            by_id = {run["id"]: run for run in runs if isinstance(run, dict) and "id" in run}
            for row in rows:
                run = by_id.get(row["run_id"])
                row["workflow_state"] = ("unknown" if run is None else "running" if run.get("status") != "completed"
                                         else "success" if run.get("conclusion") == "success" else "failed")
        result = {"items": rows, "next_page": next_page,
                  "repository": settings.RELEASE_POLL_REPO, "source_ref": poller.expected_source_ref()}
        cache.set(key, result, timeout=60)
    existing = {row.challenge_slug: row for row in Challenge.objects.filter(challenge_slug__in=[item["challenge_slug"] for item in result["items"]])}
    return {**result, "items": [{**item,
        "challenge_id": str(existing[item["challenge_slug"]].pk) if item["challenge_slug"] in existing else None,
        "title": existing[item["challenge_slug"]].title if item["challenge_slug"] in existing else None,
    } for item in result["items"]]}


@sensitive_variables()
def _metadata(slug, sha):
    data = _json(f"/contents/{quote(slug, safe='')}/info.yaml?ref={sha}")
    if (not isinstance(data, dict) or data.get("encoding") != "base64"
            or type(data.get("size")) is not int or data["size"] > 262144
            or not isinstance(data.get("content"), str)):
        raise ReleaseValidationError("info.yaml을 읽을 수 없습니다")
    raw = base64.b64decode(data["content"])
    if len(raw) > 262144:
        raise ReleaseValidationError("info.yaml이 너무 큽니다")
    value = yaml.safe_load(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ReleaseValidationError("info.yaml 형식을 확인해주세요")
    title, description, flag = value.get("name"), value.get("description", ""), value.get("flag")
    category = value.get("category", "").upper() if isinstance(value.get("category"), str) else ""
    if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 200
            or category not in Challenge.CategoryType.values
            or not isinstance(description, str) or len(description) > 100000
            or not isinstance(flag, str) or not flag or any(char in flag for char in "\0\r\n")
            or len(flag.encode("utf-8")) > 4096):
        raise ReleaseValidationError("info.yaml의 제목·분야·지문·플래그를 확인해주세요")
    return {"title": title.strip(), "description": description, "category": category,
            "flag": flag, "dynamic": bool(value.get("deployment"))}


def _release_artifact(artifact):
    raw = poller.github_request(f"/repos/{settings.RELEASE_POLL_REPO}/actions/artifacts/{artifact['id']}/zip",
        token=settings.RELEASE_POLL_GITHUB_TOKEN, timeout=30, max_bytes=2 * 1024 * 1024)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = [entry for entry in archive.infolist() if entry.filename.split("/")[-1] == poller.BUNDLE_FILE_NAME]
        if len(entries) != 1 or entries[0].file_size > 1024 * 1024:
            raise ReleaseValidationError("실행 발행 파일을 확인해주세요")
        return json.loads(archive.read(entries[0]))


@sensitive_variables()
def prepare_publication(artifact_id):
    artifact = _json(f"/actions/artifacts/{artifact_id}")
    identity = artifact_identity(artifact)
    if identity is None:
        raise PublishedImportError("PUBLICATION_UNAVAILABLE", "가져올 수 없는 발행본입니다 목록을 다시 확인해주세요", 409)
    run = poller.get_workflow_run(artifact, token=settings.RELEASE_POLL_GITHUB_TOKEN)
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise PublishedImportError("PUBLICATION_NOT_READY", "발행 작업이 완료되지 않았거나 실패했습니다 성공한 발행본을 선택해주세요", 409)
    poller.validate_workflow_run_source(artifact, run)
    slug, sha = identity["challenge_slug"], identity["source_sha"]
    matching = {}
    for page in range(1, 6):
        data = _json(f"/actions/runs/{identity['run_id']}/artifacts?per_page=100&page={page}")
        entries = data.get("artifacts")
        if not isinstance(entries, list):
            raise ReleaseValidationError("발행 파일 목록을 확인하지 못했습니다")
        for item in entries:
            if not isinstance(item, dict):
                raise ReleaseValidationError("발행 파일 형식을 확인해주세요")
            # The run-artifacts endpoint may omit workflow_run; it is supplied by this verified run.
            item = {**item, "workflow_run": {"id": run["id"], "head_branch": run["head_branch"], "head_sha": run["head_sha"]}}
            candidate = artifact_identity(item)
            if candidate and candidate["challenge_slug"] == slug:
                if candidate["kind"] in matching:
                    raise ReleaseValidationError("같은 실행에서 발행 파일이 중복되었습니다")
                matching[candidate["kind"]] = item
        if len(entries) < 100 or page * 100 >= data.get("total_count", 0):
            break
    else:
        raise ReleaseValidationError("발행 파일이 너무 많아 모두 확인하지 못했습니다")
    if not any(item["id"] == artifact_id for item in matching.values()):
        raise PublishedImportError("PUBLICATION_UNAVAILABLE", "선택한 발행 파일이 만료되었거나 삭제됐습니다", 409)
    metadata = _metadata(slug, sha)
    release_data = release_validated = manifest = file_validated = archive_bytes = None
    if "publish" in matching:
        release_data = _release_artifact(matching["publish"])
        poller.validate_bundle_source(matching["publish"], run, release_data)
        if release_data.get("challenge_slug") != slug or release_data.get("challenge_id"):
            raise ReleaseValidationError("실행 발행 파일의 문제 식별자가 일치하지 않습니다")
        release_validated = validate_release_payload({"artifact": release_data})
        if release_validated["runtime_type"] not in {"KUBERNETES", "DOCKER"}:
            raise ReleaseValidationError("VM 문제는 별도 배포 경로에서 관리해주세요")
    if metadata["dynamic"] != bool(release_data):
        raise ReleaseValidationError("문제 실행 설정과 이미지 발행 여부가 일치하지 않습니다")
    if "user-files" in matching:
        manifest, archive_bytes = user_files.download_user_files_bundle(matching["user-files"], token=settings.RELEASE_POLL_GITHUB_TOKEN)
        file_validated = user_files.validate_user_files_manifest(manifest)
        user_files.validate_user_files_source(matching["user-files"], run, file_validated)
        if file_validated["challenge_slug"] != slug or manifest.get("challenge_id"):
            raise ReleaseValidationError("참가자 파일의 문제 식별자가 일치하지 않습니다")
        if file_validated["present"]:
            user_files.validate_user_files_archive(archive_bytes, file_validated)
        elif archive_bytes is not None:
            raise ReleaseValidationError("파일 없음으로 발행한 기록에 ZIP이 들어 있습니다")
    if release_validated and file_validated and release_validated["registry_revision"] != file_validated["registry_revision"]:
        raise ReleaseValidationError("이미지와 참가자 파일의 발행번호가 일치하지 않습니다")
    if not release_validated and not file_validated:
        raise ReleaseValidationError("가져올 발행 파일이 없습니다")
    revision = (release_validated or file_validated)["registry_revision"]
    fingerprint = salted_hmac(SALT, json.dumps({"metadata": metadata, "release": release_data,
        "files": manifest, "artifact_ids": sorted(item["id"] for item in matching.values())}, sort_keys=True)).hexdigest()
    return {"artifact_id": artifact_id, "slug": slug, "source_sha": sha, "revision": revision,
            "run_id": run["id"], "metadata": metadata, "release": release_data,
            "file_manifest": manifest, "file_validated": file_validated, "archive": archive_bytes,
            "artifacts": matching, "fingerprint": fingerprint}


def existing_problem(prepared, lock=False):
    query = Challenge.objects.select_for_update() if lock else Challenge.objects
    return query.filter(challenge_slug=prepared["slug"]).first()


def conflict_reason(prepared, challenge):
    if challenge is None:
        return None
    if not is_correct_flag(prepared["metadata"]["flag"], challenge.flag_hash):
        return "저장된 채점 플래그와 발행본의 플래그가 다릅니다 기본 정보에서 먼저 확인해주세요"
    if not prepared["release"] and (ChallengeRuntimeConfig.objects.filter(challenge=challenge).exists()
                                    or ChallengeRelease.objects.filter(challenge=challenge).exists()):
        return "실행 문제로 등록된 항목에 정적 파일 발행본을 가져올 수 없습니다"
    return None


def preview_publication(prepared, user):
    challenge = existing_problem(prepared)
    blocked_reason = conflict_reason(prepared, challenge)
    release = prepared["release"]
    file_data = prepared["file_validated"]
    existing_release = ChallengeRelease.objects.filter(challenge=challenge, registry_revision=prepared["revision"], derived_from__isnull=True).first() if challenge else None
    if release and existing_release and not _same_release(existing_release, validate_release_payload({"artifact": release})):
        blocked_reason = blocked_reason or "같은 발행번호로 다른 실행 설정이 등록되어 있습니다 배포 이력을 확인해주세요"
    token = signing.dumps({"artifact_id": prepared["artifact_id"], "fingerprint": prepared["fingerprint"], "user_id": str(user.pk)}, salt=SALT)
    return {"preview_token": token, "challenge_slug": prepared["slug"], "registry_revision": prepared["revision"],
        "title": prepared["metadata"]["title"], "description": prepared["metadata"]["description"], "category": prepared["metadata"]["category"],
        "challenge_id": str(challenge.pk) if challenge else None, "existing_title": challenge.title if challenge else None,
        "existing_release_id": str(existing_release.pk) if existing_release else None,
        "can_import": blocked_reason is None, "blocked_reason": blocked_reason,
        "flag_status": "mismatch" if challenge and not is_correct_flag(prepared["metadata"]["flag"], challenge.flag_hash) else "match" if challenge else "from_repository",
        "runtime_type": release["runtime_type"] if release else None,
        "containers": [{"name": item["name"], "image_ref": item["image"], "ports": item["ports"],
                        "env_count": len(item.get("env", {})), "secret_count": len(item.get("secret_env", {}))} for item in release["workload"]["containers"]] if release else [],
        "files": {"status": "available" if file_data and file_data["present"] else "none" if file_data else "pending",
                  "file_count": file_data["file_count"] if file_data else 0, "size_bytes": file_data["size_bytes"] if file_data else 0},
        "source_sha": prepared["source_sha"],
        "run_url": f"https://github.com/{settings.RELEASE_POLL_REPO}/actions/runs/{prepared['run_id']}",
        "defaults": {"difficulty": "EASY", "initial_score": 1000, "minimum_score": 600, "decay": 70}}


def _same_release(release, validated):
    fields = ("challenge_slug", "runtime_type", "architecture", "isolation_profile", "cpu_millicores",
              "memory_mib", "ephemeral_storage_mib", "healthcheck", "source_ref")
    if any(getattr(release, field) != validated[field] for field in fields):
        return False
    stored = {item.name: item for item in release.containers.all()}
    if set(stored) != {item["name"] for item in validated["containers"]}:
        return False
    for item in validated["containers"]:
        container = stored[item["name"]]
        aliases = {str(secret.pk): secret.name for secret in RuntimeSecret.objects.filter(
            challenge=release.challenge, pk__in=container.secret_env.values())}
        if (container.image_ref != item["image_ref"] or container.ports != item["ports"]
                or container.env != item.get("env", {})
                or {key: aliases.get(value) for key, value in container.secret_env.items()} != item.get("secret_env", {})):
            return False
    return True


@sensitive_variables()
def save_publication(prepared, user, difficulty):
    meta = prepared["metadata"]
    with transaction.atomic():
        challenge, created = Challenge.objects.get_or_create(challenge_slug=prepared["slug"], defaults={
            "title": meta["title"], "description": meta["description"], "category": meta["category"],
            "difficulty": difficulty, "score": 1000, "flag_hash": hash_flag(meta["flag"]), "is_published": False,
        })
        challenge = Challenge.objects.select_for_update().get(pk=challenge.pk)
        reason = conflict_reason(prepared, challenge)
        if reason:
            raise PublishedImportError("PUBLICATION_CONFLICT", reason, 409)
        if not RuntimeSecret.objects.filter(challenge=challenge, name="flag").exists():
            create_runtime_secret(challenge, "flag", meta["flag"], user.login_id)
        release = None
        release_status = "not_applicable"
        if prepared["release"]:
            ChallengeRuntimeConfig.objects.get_or_create(challenge=challenge)
            validated = validate_release_payload({"artifact": prepared["release"],
                "note": f"GitHub 발행본 가져오기 · 실행 {prepared['run_id']}"})
            release = ChallengeRelease.objects.filter(challenge=challenge, registry_revision=prepared["revision"], derived_from__isnull=True).first()
            if release is not None:
                if not _same_release(release, validated):
                    raise PublishedImportError("PUBLICATION_CONFLICT", "같은 발행번호로 다른 실행 설정이 등록되어 있습니다 배포 이력을 확인해주세요", 409)
                release_status = "duplicate"
            elif ChallengeRelease.objects.filter(challenge=challenge, registry_revision=prepared["revision"]).exists():
                raise PublishedImportError("PUBLICATION_CONFLICT", "이 발행번호의 원본 배포 버전을 찾지 못했습니다 배포 이력을 확인해주세요", 409)
            else:
                release = create_release(challenge, validated, user.login_id)
                release_status = "registered"
        file_status = "pending"
        if prepared["file_manifest"]:
            file_status, result = user_files.register_user_files_bundle(prepared["file_manifest"], prepared["archive"], prepared["artifacts"]["user-files"])
            stored_file = ChallengeUserFileBundle.objects.filter(challenge=challenge, registry_revision=prepared["revision"]).first()
            file_fields = ("source_ref", "source_sha", "present", "sha256", "size_bytes", "file_count", "uncompressed_size_bytes")
            if (file_status not in {"registered", "duplicate"} or stored_file is None
                    or any(getattr(stored_file, field) != prepared["file_validated"][field] for field in file_fields)):
                raise PublishedImportError("PUBLICATION_CONFLICT", "같은 발행번호의 파일 기록이 다릅니다 기존 기록을 확인해주세요", 409)
        return {"challenge_id": str(challenge.pk), "challenge_slug": challenge.challenge_slug, "title": challenge.title, "category": challenge.category,
                "difficulty": challenge.difficulty, "is_published": challenge.is_published,
                "challenge_created": created, "release_id": str(release.pk) if release else None,
                "release_status": release_status, "files_status": file_status, "registry_revision": prepared["revision"]}
