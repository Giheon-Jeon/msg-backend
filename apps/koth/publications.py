"""Same-source KOTH metadata, participant archive and encrypted execution settings."""
import copy
import re
import uuid

import yaml
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.utils.crypto import constant_time_compare
from django.views.decorators.debug import sensitive_variables

from apps.instances import published_challenges as shared
from apps.instances.models import PollerArtifact
from apps.instances.execution_settings import SENSITIVE_NAME
from apps.instances.releases import ReleaseValidationError, validate_release_payload
from apps.instances.repository_environment import discover_environment
from apps.instances.repository_source import RepositorySnapshot
from .environment import EnvironmentError, PROTECTED_NAMES, create_version, decode_settings
from .files import register_koth_user_files
from .models import KothChallenge, KothEnvironmentConfig, KothEnvironmentVersion, KothPublication, KothUserFileBundle

SALT = "koth-publication-v1"
SLUG = re.compile(r"^koth-[a-z0-9]+(?:-[a-z0-9]+)*$")


def catalog(page, slug=None):
    if slug and (not isinstance(slug, str) or not SLUG.fullmatch(slug)):
        raise EnvironmentError("KOTH 문제 폴더를 확인해주세요")
    # Normal and KOTH jobs share the same Actions list. Do not make an operator page
    # through hundreds of unrelated normal publications to reach an existing game.
    key = "koth-publication-catalog-v1:" + settings.RELEASE_POLL_REPO
    groups = cache.get(key)
    if groups is None:
        known = list(PollerArtifact.objects.filter(payload__name__startswith="koth-").order_by("-artifact_id").values_list("payload", flat=True)[:1000])
        recent = shared._json("/actions/artifacts?per_page=100&page=1").get("artifacts")
        if not isinstance(recent, list):
            raise ReleaseValidationError("GitHub 발행 목록 형식을 확인해주세요")
        rows = {}
        for artifact in [*known, *recent]:
            identity = shared.artifact_identity(artifact)
            if not identity or not SLUG.fullmatch(identity["challenge_slug"]):
                continue
            group_key = (identity["challenge_slug"], identity["run_id"])
            row = rows.setdefault(group_key, {"artifact_id": artifact["id"], "challenge_slug": identity["challenge_slug"],
                "run_id": identity["run_id"], "source_sha": identity["source_sha"], "published_at": artifact.get("created_at"),
                "has_release_artifact": False, "has_files_artifact": False})
            if identity["kind"] == "publish":
                row["artifact_id"] = artifact["id"]
                row["has_release_artifact"] = True
            else:
                row["has_files_artifact"] = True
        groups = sorted(rows.values(), key=lambda row: row["artifact_id"], reverse=True)
        cache.set(key, groups, timeout=60)
    rows = [row for row in groups if not slug or row["challenge_slug"] == slug]
    selected = []
    linked = {c.repository_slug: c for c in KothChallenge.objects.exclude(repository_slug="")}
    for row in rows[(page - 1) * 10:page * 10]:
        run_key = f"koth-publication-run-v1:{settings.RELEASE_POLL_REPO}:{row['run_id']}"
        state = cache.get(run_key)
        if state is None:
            run = shared._json(f"/actions/runs/{row['run_id']}")
            state = "running" if run.get("status") != "completed" else "success" if run.get("conclusion") == "success" else "failed"
            cache.set(run_key, state, timeout=60)
        challenge = linked.get(row["challenge_slug"])
        existing = challenge.publications.filter(run_id=row["run_id"]).first() if challenge else None
        selected.append({**row, "workflow_state": state, "registered": bool(existing),
                         "publication_id": str(existing.pk) if existing else None,
                         "challenge_id": str(challenge.pk) if challenge else None, "title": challenge.title if challenge else row["challenge_slug"]})
    return {"items": selected, "next_page": page + 1 if page * 10 < len(rows) else None,
            "repository": settings.RELEASE_POLL_REPO, "source_ref": shared.poller.expected_source_ref()}


def metadata(slug, sha):
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise EnvironmentError("KOTH 문제 폴더를 선택해주세요")
    snapshot = RepositorySnapshot(slug, sha)
    try:
        value = yaml.safe_load(snapshot.read("info.yaml"))
    except yaml.YAMLError:
        raise EnvironmentError("info.yaml 형식을 확인해주세요") from None
    if (not isinstance(value, dict) or str(value.get("category", "")).lower() != "koth"
            or not isinstance(value.get("name"), str) or not 1 <= len(value["name"].strip()) <= 100
            or not isinstance(value.get("description", ""), str) or len(value.get("description", "")) > 100000
            or not isinstance(value.get("author", ""), str) or len(value.get("author", "")) > 200):
        raise EnvironmentError("info.yaml의 KOTH 제목·지문·출제자를 확인해주세요")
    return {"title": value["name"].strip(), "description": value.get("description", ""),
            "author": value.get("author", ""), "dynamic": bool(value.get("deployment"))}


def prepare(artifact_id):
    prepared = shared.prepare_publication(artifact_id, metadata_reader=metadata)
    if prepared["release"] is None or prepared["file_validated"] is None:
        raise EnvironmentError("이미지와 참가자 파일 발행 기록이 모두 있는 버전을 선택해주세요", "KOTH_PUBLICATION_INCOMPLETE", 409)
    validated = validate_release_payload({"artifact": prepared["release"]})
    snapshot = RepositorySnapshot(prepared["slug"], prepared["source_sha"])
    found = {}
    for container in validated["containers"]:
        name = container["name"]
        path = f"env/{name}.env"
        if path not in snapshot.files:
            found[name] = None
        else:
            discovery = discover_environment(snapshot, name)
            found[name] = discovery["candidates"]
    prepared["validated"], prepared["environment_candidates"] = validated, found
    return prepared


def runtime_profile(validated):
    return {key: copy.deepcopy(validated[key]) for key in (
        "runtime_type", "architecture", "isolation_profile", "cpu_millicores", "memory_mib",
        "ephemeral_storage_mib", "healthcheck")} | {
            "containers": [{"name": c["name"], "ports": c["ports"]} for c in validated["containers"]]}


@sensitive_variables()
def build_settings(challenge, prepared, base=None):
    previous = {c["name"]: c for c in decode_settings(base)["containers"]} if base else {}
    missing, containers = [], []
    for container in prepared["validated"]["containers"]:
        name = container["name"]
        old = previous.get(name, {}).get("entries", {})
        # Operator-owned values and game credentials are never replaced by author placeholders.
        entries = {key: copy.deepcopy(item) for key, item in old.items()
                   if item["locked"] or item["operator_override"] or key == "FLAG"}
        found = prepared["environment_candidates"].get(name)
        if found is None:
            missing.append({"container": name, "name": None, "reason": "env_file_missing"})
        for item in found or []:
            key, value = item["name"], item["value"]
            if key in entries:
                continue
            if key in PROTECTED_NAMES:
                missing.append({"container": name, "name": key, "reason": "operator_value_required"})
                continue
            before = old.get(key, {})
            kind = "secret" if SENSITIVE_NAME.search(key) or item["sensitive"] or before.get("kind") == "secret" else item["kind"]
            unresolved = value is None and item.get("resolution") != "unset"
            if unresolved:
                missing.append({"container": name, "name": key, "reason": "value_required"})
            entries[key] = {"value": value, "kind": kind, "required": value is not None and value != "",
                            "locked": False, "operator_override": False}
        containers.append({"name": name, "image": container["image_ref"], "entries": entries})
    if previous and set(previous) != {c["name"] for c in containers}:
        missing.append({"container": None, "name": None, "reason": "container_profile_changed"})
    return {"containers": containers}, missing


def selected_base(challenge):
    config = KothEnvironmentConfig.objects.filter(challenge=challenge).select_related("application__version", "applied_version").first()
    # A just-saved operator edit also belongs in the next collected publication.
    return KothEnvironmentVersion.objects.filter(challenge=challenge).order_by("-version").first() or (config.applied_version if config else None)


def preview_data(challenge, prepared, actor):
    base = selected_base(challenge)
    _, missing = build_settings(challenge, prepared, base)
    claims = {"actor": actor, "challenge": str(challenge.pk), "artifact": prepared["artifact_id"],
              "fingerprint": prepared["fingerprint"], "base": str(base.pk) if base else None}
    return {"preview_token": signing.dumps(claims, salt=SALT), "challenge_id": str(challenge.pk),
            "slug": prepared["slug"], "source_sha": prepared["source_sha"], "registry_revision": prepared["revision"],
            "metadata": prepared["metadata"], "missing": missing,
            "containers": [{"name": c["name"], "image_ref": c["image_ref"],
                            "env_count": sum(not (item["sensitive"] or item["kind"] == "secret") for item in prepared["environment_candidates"].get(c["name"]) or []),
                            "secret_count": sum(item["sensitive"] or item["kind"] == "secret" for item in prepared["environment_candidates"].get(c["name"]) or [])}
                           for c in prepared["validated"]["containers"]],
            "files": {key: prepared["file_validated"][key] for key in ("present", "size_bytes", "file_count", "sha256")}}


def check_preview(challenge, prepared, token, actor):
    try:
        claims = signing.loads(token, salt=SALT, max_age=1200)
    except (signing.BadSignature, TypeError):
        raise EnvironmentError("미리보기가 만료됐습니다 다시 가져와주세요", "KOTH_PUBLICATION_STALE", 409) from None
    base = selected_base(challenge)
    expected = {"actor": actor, "challenge": str(challenge.pk), "artifact": prepared["artifact_id"],
                "fingerprint": prepared["fingerprint"], "base": str(base.pk) if base else None}
    if claims != expected:
        raise EnvironmentError("발행본이나 환경변수 설정이 바뀌었습니다 미리보기를 다시 확인해주세요", "KOTH_PUBLICATION_STALE", 409)


@sensitive_variables()
def register(challenge, prepared, actor, *, preview_token=None):
    if challenge.repository_slug != prepared["slug"]:
        raise EnvironmentError("이 문제에 연결된 저장소 폴더와 다릅니다", "KOTH_PUBLICATION_CONFLICT", 409)
    with transaction.atomic():
        challenge = KothChallenge.objects.select_for_update().get(pk=challenge.pk)
        if challenge.repository_slug != prepared["slug"]:
            raise EnvironmentError("연결된 저장소 폴더가 바뀌었습니다 다시 확인해주세요", "KOTH_PUBLICATION_CONFLICT", 409)
        existing = KothPublication.objects.filter(challenge=challenge, registry_revision=prepared["revision"]).first()
        if existing:
            if not constant_time_compare(existing.fingerprint, prepared["fingerprint"]):
                raise EnvironmentError("같은 발행번호에 다른 내용이 등록돼 있습니다", "KOTH_PUBLICATION_CONFLICT", 409)
            return existing, False
        if preview_token is not None:
            check_preview(challenge, prepared, preview_token, actor)
        base = selected_base(challenge)
        values, missing = build_settings(challenge, prepared, base)
        profile = runtime_profile(prepared["validated"])
        previous = KothPublication.objects.filter(challenge=challenge, source_sha=challenge.source_revision).first()
        if previous and previous.runtime_profile != profile:
            missing.append({"container": None, "name": None, "reason": "runtime_profile_changed"})
        if challenge.source_revision and prepared["source_sha"] != challenge.source_revision and previous is None:
            missing.append({"container": None, "name": None, "reason": "running_publication_required"})
        status, bundle = register_koth_user_files(prepared["file_validated"], prepared["archive"], prepared["artifacts"]["user-files"])
        if status not in ("registered", "duplicate"):
            raise EnvironmentError("참가자 파일을 등록하지 못했습니다", "KOTH_FILES_UNAVAILABLE", 409)
        publication = KothPublication.objects.create(challenge=challenge, registry_revision=prepared["revision"],
            source_sha=prepared["source_sha"], artifact_id=prepared["artifacts"]["publish"]["id"], run_id=prepared["run_id"],
            fingerprint=prepared["fingerprint"], metadata=prepared["metadata"], runtime_profile=profile,
            missing=missing, created_by=actor)
        version = create_version(challenge, values, publication.source_sha, actor, uuid.uuid4(), publication.fingerprint, based_on=base)
        version.publication = publication
        version.save(update_fields=["publication"])
        if publication.source_sha == challenge.source_revision:
            images = {c["name"]: c["image"] for c in values["containers"]}
            for legacy in KothEnvironmentVersion.objects.filter(challenge=challenge, source_revision=publication.source_sha, publication__isnull=True):
                if {c["name"]: c["image"] for c in decode_settings(legacy)["containers"]} == images:
                    legacy.publication = publication
                    legacy.save(update_fields=["publication"])
        return publication, True


def serialize(publication):
    version = publication.environment_versions.order_by("-version").first()
    bundle = KothUserFileBundle.objects.filter(challenge_id=publication.challenge_id, registry_revision=publication.registry_revision,
                                              source_sha=publication.source_sha).first()
    return {"publication_id": str(publication.pk), "registry_revision": publication.registry_revision, "run_id": publication.run_id,
            "source_sha": publication.source_sha, "title": publication.metadata["title"],
            "description": publication.metadata.get("description", ""), "author": publication.metadata.get("author", ""),
            "version_id": str(version.pk) if version else None, "environment_version": version.version if version else None,
            "missing": unresolved(publication, version) if version else publication.missing, "created_at": publication.created_at.isoformat(),
            "files": {"present": bundle.present, "file_count": bundle.file_count, "size_bytes": bundle.size_bytes} if bundle else None,
            "source_url": f"https://github.com/{settings.RELEASE_POLL_REPO}/tree/{publication.source_sha}/{publication.challenge.repository_slug}"}


def unresolved(publication, version):
    values = {c["name"]: c["entries"] for c in decode_settings(version)["containers"]}
    remaining = []
    for item in publication.missing:
        if item["reason"] == "value_required" and values.get(item["container"], {}).get(item["name"], {}).get("value") is not None:
            continue
        if item["reason"] == "running_publication_required":
            baseline = KothPublication.objects.filter(challenge_id=version.challenge_id, source_sha=version.challenge.source_revision).first()
            if baseline and baseline.runtime_profile == publication.runtime_profile:
                continue
        remaining.append(item)
    return remaining


def source_allowed(challenge, version):
    if version.challenge_id != challenge.pk:
        return False
    publication = version.publication
    if publication:
        if publication.challenge_id != challenge.pk or publication.source_sha != version.source_revision:
            return False
        return not unresolved(publication, version)
    return bool(version.source_revision == challenge.source_revision and challenge.source_revision)


def commit_applied(challenge, version, actor):
    publication = version.publication
    if publication and challenge.active_publication_id != publication.pk:
        challenge.active_publication = publication
        challenge.source_revision = version.source_revision
        challenge.metadata_revision = version.source_revision
        challenge.title = publication.metadata["title"]
        challenge.description = publication.metadata.get("description", "")
        challenge.updated_by = actor
        challenge.save(update_fields=["active_publication", "source_revision", "metadata_revision", "title", "description", "updated_by", "updated_at"])


def collect_artifact(artifact):
    identity = shared.artifact_identity(artifact)
    if not identity or not SLUG.fullmatch(identity["challenge_slug"]):
        return None
    challenge = KothChallenge.objects.filter(repository_slug=identity["challenge_slug"]).first()
    if challenge is None:
        return "unmatched", identity["challenge_slug"]
    if KothPublication.objects.filter(artifact_id=artifact["id"]).exists():
        return "duplicate", artifact["id"]
    prepared = prepare(artifact["id"])
    publication, created = register(challenge, prepared, "publication-poller")
    return ("registered" if created else "duplicate"), publication
