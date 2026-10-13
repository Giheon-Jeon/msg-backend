"""Admin-only inspection of grading flags and repository file metadata."""
import logging
from pathlib import PurePosixPath
from urllib.parse import quote

from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from rest_framework.decorators import api_view, permission_classes

from apps.challenge.models import Challenge
from apps.challenge.services import is_correct_flag
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances.models import ChallengeRuntimeConfig, RuntimeSecret
from apps.instances.releases import ReleaseValidationError
from apps.instances.repository_environment import _yaml
from apps.instances.repository_source import RepositorySnapshot, RepositorySourceUnavailable
from apps.instances.runtime_secrets import decrypt_runtime_secret, RuntimeSecretUnavailable

logger = logging.getLogger(__name__)


@sensitive_variables()
def _flag_settings(challenge):
    config = ChallengeRuntimeConfig.objects.select_related("current_release").filter(challenge=challenge).first()
    release = config.current_release if config else None
    secrets = {str(secret.pk): secret for secret in RuntimeSecret.objects.filter(challenge=challenge)}
    bindings, checked = [], {}
    value, source, source_revision = None, None, None
    source_status = "missing"

    def check_secret(secret):
        key = str(secret.pk)
        if key not in checked:
            try:
                candidate = decrypt_runtime_secret(secret)
                matches = bool(candidate) and is_correct_flag(candidate, challenge.flag_hash)
                checked[key] = ("match" if matches else "mismatch", candidate if matches else None)
            except RuntimeSecretUnavailable:
                checked[key] = ("unavailable", None)
        return checked[key]

    if release and release.challenge_id == challenge.pk:
        for container in release.containers.order_by("name"):
            for env_name, secret_id in container.secret_env.items():
                secret = secrets.get(str(secret_id))
                if env_name != "FLAG" and (secret is None or secret.name != "flag"):
                    continue
                status, candidate = check_secret(secret) if secret else ("missing", None)
                bindings.append({"container": container.name, "env_name": env_name,
                                 "secret_name": secret.name if secret else None,
                                 "secret_version": secret.version if secret else None, "status": status})
                if candidate is not None and value is None:
                    value, source, source_status = candidate, "runtime_secret", "match"

    if value is None:
        latest = max((secret for secret in secrets.values() if secret.name == "flag"),
                     key=lambda secret: secret.version, default=None)
        if latest:
            source_status, value = check_secret(latest)
            if value is not None:
                source = "runtime_secret"

    if value is None and challenge.challenge_slug:
        try:
            snapshot = RepositorySnapshot(challenge.challenge_slug)
            metadata = _yaml(snapshot, "info.yaml")
            candidate = metadata.get("flag") if isinstance(metadata, dict) else None
            if isinstance(candidate, str) and candidate:
                if is_correct_flag(candidate, challenge.flag_hash):
                    value, source, source_status = candidate, "repository", "match"
                    source_revision = snapshot.revision
                else:
                    source_status = "mismatch"
        except (RepositorySourceUnavailable, ReleaseValidationError):
            if source_status == "missing":
                source_status = "unavailable"

    return {
        "configured": bool(challenge.flag_hash),
        "reveal_available": value is not None,
        "source": source,
        "source_status": source_status,
        "source_revision": source_revision,
        "release_version": release.version if release else None,
        "bindings": bindings,
    }, value


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
@sensitive_variables()
def challenge_flag(request, challenge_id):
    challenge = Challenge.objects.filter(pk=challenge_id).first()
    if challenge is None:
        return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다", 404)
    metadata, _ = _flag_settings(challenge)
    return ok(metadata)


@never_cache
@api_view(["POST"])
@permission_classes([IsAdmin])
@sensitive_variables()
def challenge_flag_reveal(request, challenge_id):
    challenge = Challenge.objects.filter(pk=challenge_id).first()
    if challenge is None:
        return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다", 404)
    metadata, value = _flag_settings(challenge)
    if value is None:
        return fail("FLAG_ORIGINAL_UNAVAILABLE", "채점 값과 일치하는 플래그 원문을 찾지 못했습니다", 409)
    logger.info("Admin viewed challenge flag", extra={"actor": request.user.login_id, "challenge_id": str(challenge.pk)})
    return ok({"flag": value, "source": metadata["source"], "source_revision": metadata["source_revision"]})


def _file_kind(path):
    if path.startswith("prob/for_user/"):
        return "participant"
    name = PurePosixPath(path).name.lower()
    if (path == "info.yaml" or path.startswith("env/") or name.startswith("dockerfile")
            or (name.startswith(("compose.", "docker-compose.")) and name.endswith((".yml", ".yaml")))
            or name == ".env" or name.startswith(".env.")):
        return "configuration"
    return "source"


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def challenge_repository_files(request, challenge_id):
    challenge = Challenge.objects.filter(pk=challenge_id).first()
    if challenge is None:
        return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다", 404)
    try:
        snapshot = RepositorySnapshot(challenge.challenge_slug)
    except ReleaseValidationError as error:
        return fail("REPOSITORY_NOT_LINKED", str(error), 400)
    except RepositorySourceUnavailable as error:
        return fail("REPOSITORY_UNAVAILABLE", str(error), 503)
    files = []
    for path, item in sorted(snapshot.files.items()):
        url = None
        if snapshot.published:
            url = f"https://github.com/{snapshot.repository}/blob/{snapshot.revision}/{quote(snapshot.slug + '/' + path, safe='/')}"
        files.append({"path": path, "name": PurePosixPath(path).name, "size_bytes": item["size"],
                      "kind": _file_kind(path), "source_url": url})
    return ok({"repository": snapshot.repository, "source_revision": snapshot.revision,
               "source_published": snapshot.published, "files": files})
