from django.core import signing
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
    default_secret_name, environment_file_preview, import_identity, prepare_environment_file, save_environment_file,
)
from apps.instances.models import ChallengeRelease, RuntimeSecret
from apps.instances.releases import ReleaseValidationError, is_deployable, serialize_release
from apps.instances.repository_environment import discover_environment
from apps.instances.repository_source import RepositorySnapshot, RepositorySourceUnavailable
from apps.instances.runtime_secrets import RuntimeSecretUnavailable
from apps.instances.execution_settings import MAX_ENV_COUNT

TOKEN_SALT = "repository.environment.v2"


def _source(challenge_id, release_id):
    source = ChallengeRelease.objects.select_related("challenge").prefetch_related("containers").filter(
        challenge_id=challenge_id, release_id=release_id,
    ).first()
    if source is None:
        raise ReleaseValidationError("존재하지 않는 릴리스입니다")
    if not is_deployable(source):
        raise ReleaseValidationError("사용할 수 있는 이미지 릴리스를 먼저 등록해주세요")
    return source


@sensitive_variables()
def _repository_request(source, body):
    if not isinstance(body, dict) or set(body) - {"source_token", "container", "entries", "overrides", "request_id"}:
        raise ReleaseValidationError("저장소 가져오기 요청 형식을 확인해주세요")
    token = body.get("source_token")
    if not isinstance(token, str) or len(token) > 2048:
        raise ReleaseValidationError("저장소 설정을 먼저 불러와주세요")
    try:
        evidence = signing.loads(token, salt=TOKEN_SALT, max_age=3600)
    except signing.BadSignature:
        raise ReleaseValidationError("저장소 확인 시간이 지났거나 내용이 바뀌었습니다 다시 불러와주세요") from None
    if (evidence.get("release_id") != str(source.pk) or evidence.get("slug") != source.challenge_slug
            or evidence.get("container") != body.get("container")):
        raise ReleaseValidationError("불러온 문제와 컨테이너를 다시 확인해주세요")
    snapshot = RepositorySnapshot(source.challenge_slug, evidence["revision"])
    if snapshot.repository != evidence.get("repository"):
        raise ReleaseValidationError("연결된 저장소가 바뀌었습니다 다시 불러와주세요")
    discovery = discover_environment(snapshot, body["container"])
    candidates = {item["name"]: item for item in discovery["candidates"]}
    entries = body.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENV_COUNT:
        raise ReleaseValidationError(f"가져올 항목을 1개 이상, {MAX_ENV_COUNT}개 이하로 선택해주세요")
    names = [item.get("name") for item in entries if isinstance(item, dict)]
    if len(names) != len(entries) or any(not isinstance(name, str) for name in names) or len(set(names)) != len(names) or not set(names) <= candidates.keys():
        raise ReleaseValidationError("저장소에서 확인한 항목만 선택해주세요")
    overrides = body.get("overrides", {})
    if not isinstance(overrides, dict) or not set(overrides) <= set(names):
        raise ReleaseValidationError("선택한 항목의 값만 수정해주세요")
    content = []
    for entry in entries:
        name = entry["name"]
        candidate = candidates[name]
        if candidate["sensitive"] and entry.get("kind") != "secret":
            raise ReleaseValidationError(f"{name}은 비밀값으로 등록해주세요")
        value = overrides.get(name, candidate["value"])
        if entry.get("use_existing"):
            if name in overrides and overrides[name] != "":
                raise ReleaseValidationError("새 값을 입력할 때는 기존 비밀값 연결을 해제해주세요")
            value = ""
        if value is None:
            raise ReleaseValidationError(f"{name}: 확인할 값이 없습니다 직접 입력하거나 가져오기에서 제외해주세요")
        if not isinstance(value, str) or any(char in value for char in "\r\n\0") or len(value.encode()) > 4096:
            raise ReleaseValidationError("한 줄에 4096바이트 이하의 값을 입력해주세요")
        # The existing .env parser strips exactly one pair of quotes, without evaluation.
        content.append(name + '="' + value + '"')
    return {"container": body["container"], "content": "\n".join(content), "entries": entries,
            "request_id": body.get("request_id")}, snapshot.revision


@method_decorator(never_cache, name="dispatch")
class RepositoryEnvironmentView(APIView):
    permission_classes = [IsAdmin]

    @sensitive_variables()
    def get(self, request, challenge_id, release_id):
        try:
            source = _source(challenge_id, release_id)
            container_name = request.query_params.get("container")
            target = next((item for item in source.containers.all() if item.name == container_name), None)
            if target is None:
                raise ReleaseValidationError("이 릴리스의 컨테이너를 선택해주세요")
            snapshot = RepositorySnapshot(source.challenge_slug)
            discovery = discover_environment(snapshot, container_name)
            records = {str(item.pk): item for item in RuntimeSecret.objects.filter(challenge_id=source.challenge_id).only("secret_id", "name", "version")}
            rows = []
            for item in discovery["candidates"]:
                name = item["name"]
                previous = records.get(target.secret_env.get(name))
                kind = "secret" if previous or item["sensitive"] else target.env_rules.get(name, {}).get("kind", item["kind"])
                row = {key: item[key] for key in ("name", "sources", "needs_value", "resolution")}
                has_value = item["value"] is not None and (kind == "env" or item["value"] != "")
                use_existing = previous is not None and not has_value
                row.update(kind=kind, required=target.env_rules.get(name, {}).get("required", item["value"] != ""),
                           selected=use_existing or (has_value and item["resolution"] != "author_example"),
                           has_value=has_value,
                           locked_secret=bool(previous or item["sensitive"] or target.env_rules.get(name, {}).get("kind") == "secret"),
                           secret_name=previous.name if previous else default_secret_name(container_name, name),
                           use_existing=use_existing)
                if previous:
                    row.update(secret_id=str(previous.pk), version=previous.version)
                if kind == "env" and item["value"] is not None:
                    row["value"] = item["value"]
                rows.append(row)
            evidence = {"release_id": str(source.pk), "slug": source.challenge_slug, "container": container_name,
                        "repository": snapshot.repository, "revision": snapshot.revision}
            return ok({"container": container_name, "source_revision": snapshot.revision,
                       "source_url": f"https://github.com/{snapshot.repository}/tree/{snapshot.revision}/{snapshot.slug}" if getattr(snapshot, "published", True) else None,
                       "source_token": signing.dumps(evidence, salt=TOKEN_SALT, compress=True), "entries": rows,
                       "warnings": discovery["warnings"], "build_context": discovery["build_context"],
                       "source_files": discovery["source_files"]})
        except ReleaseValidationError as error:
            return fail("REPOSITORY_ENV_INVALID", error.message, 400)
        except RepositorySourceUnavailable as error:
            return fail("REPOSITORY_SOURCE_UNAVAILABLE", str(error), 503)


@method_decorator(never_cache, name="dispatch")
class RepositoryEnvironmentPreviewView(APIView):
    permission_classes = [IsAdmin]

    @sensitive_variables()
    def post(self, request, challenge_id, release_id):
        try:
            source = _source(challenge_id, release_id)
            body, revision = _repository_request(source, request.data)
            plan = prepare_environment_file(source, body)
            return ok(environment_file_preview(plan) | {"source_revision": revision})
        except (ReleaseValidationError, ValueError) as error:
            return fail("REPOSITORY_ENV_INVALID", str(error), 400)
        except RepositorySourceUnavailable as error:
            return fail("REPOSITORY_SOURCE_UNAVAILABLE", str(error), 503)


@method_decorator(never_cache, name="dispatch")
class RepositoryEnvironmentImportView(APIView):
    permission_classes = [IsAdmin]

    @sensitive_variables()
    def post(self, request, challenge_id, release_id):
        try:
            # Fetch immutable source before acquiring database locks.
            source = _source(challenge_id, release_id)
            body, revision = _repository_request(source, request.data)
            with transaction.atomic():
                challenge = _get_challenge(challenge_id, for_update=True)
                if challenge is None:
                    return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제입니다", 404)
                source = _source(challenge_id, release_id)
                plan = prepare_environment_file(source, body)
                request_id, fingerprint = import_identity(body, source)
                existing = ChallengeRelease.objects.filter(env_import_id=request_id).first()
                if existing is not None:
                    if existing.challenge_id != challenge.pk or not constant_time_compare(existing.env_import_fingerprint, fingerprint):
                        return fail("ENV_FILE_REQUEST_CONFLICT", "다른 내용에 사용된 저장 요청입니다 새로 확인해주세요", 409)
                    release = existing
                else:
                    if plan["validated"]:
                        plan["validated"]["note"] = f"{body['container']} 저장소 설정 가져오기 · {revision[:12]}"
                    release = save_environment_file(source, plan, request.user.login_id, request_id, fingerprint)
                return ok(serialize_release(release, _current_release_id(challenge)), message="새 설정 버전을 저장했습니다 적용할 버전을 선택해주세요")
        except (ReleaseValidationError, ValueError) as error:
            return fail("REPOSITORY_ENV_INVALID", str(error), 400)
        except RepositorySourceUnavailable as error:
            return fail("REPOSITORY_SOURCE_UNAVAILABLE", str(error), 503)
        except RuntimeSecretUnavailable:
            return fail("RUNTIME_SECRET_UNAVAILABLE", "비밀값 저장 설정을 확인해주세요", 503)
        except IntegrityError:
            return fail("ENV_FILE_REQUEST_CONFLICT", "저장 요청이 겹쳤습니다 같은 요청으로 다시 확인해주세요", 409)
