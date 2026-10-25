"""Administrator staging and application; scoped VM agents receive only their game."""
from datetime import timedelta
from functools import wraps
import re
import secrets

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from rest_framework.views import APIView

from apps.adminpanel.models import AdminEvent
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances.repository_source import RepositorySourceUnavailable
from apps.instances.releases import ReleaseValidationError
from apps.instances.runtime_secrets import RuntimeSecretUnavailable

from .environment import (
    EnvironmentError, agent_token_hash, canonical_uuid, create_version, decode_settings,
    prepare_changes, request_identity, require_source, serialize_version,
)
from .models import KothChallenge, KothChallengeStatus, KothEnvironmentApply, KothEnvironmentConfig, KothEnvironmentVersion

ERRORS = {
    "ROLLOUT_FAILED": "설정을 적용하지 못해 이전 설정으로 복구했습니다",
    "ROLLBACK_CONFLICT": "VM의 다른 변경과 겹쳐 복구하지 못했습니다 운영 설정을 확인해주세요",
    "IMAGE_MISMATCH": "현재 VM의 이미지와 등록된 실행 이미지가 다릅니다",
    "ENVIRONMENT_MISMATCH": "설정한 값과 실제 실행 환경이 다릅니다",
    "VM_UNAVAILABLE": "실행 VM에 연결하지 못했습니다",
    "GAME_ACTIVE": "진행 중인 KOTH에는 새 설정을 적용하지 않습니다",
    "SOURCE_CHANGED": "실행 이미지의 소스 커밋이 변경됐습니다",
    "SECRET_CONFLICT": "VM에 같은 이름의 다른 설정이 있습니다 운영 설정을 확인해주세요",
}


def environment_errors(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except EnvironmentError as error:
            return fail(error.code, error.message, error.status)
        except RuntimeSecretUnavailable:
            return fail("KOTH_ENV_UNAVAILABLE", "비밀값 저장 설정을 확인해주세요", 503)
        except RepositorySourceUnavailable:
            return fail("KOTH_ENV_SOURCE_UNAVAILABLE", "문제 저장소를 읽지 못했습니다 잠시 뒤 다시 시도해주세요", 503)
        except ReleaseValidationError:
            return fail("KOTH_ENV_INVALID", "문제 저장소의 환경변수 형식을 확인해주세요", 400)
        except IntegrityError:
            return fail("KOTH_ENV_CONFLICT", "저장 요청이 겹쳤습니다 같은 요청으로 다시 확인해주세요", 409)
    return wrapped


def get_challenge(challenge_id, lock=False):
    query = KothChallenge.objects.select_for_update() if lock else KothChallenge.objects
    challenge = query.filter(pk=challenge_id).first()
    if challenge is None:
        raise EnvironmentError("KOTH 문제를 찾지 못했습니다", "KOTH_CHALLENGE_NOT_FOUND", 404)
    return challenge


def get_version(challenge, version_id):
    version = KothEnvironmentVersion.objects.filter(challenge=challenge, pk=canonical_uuid(version_id)).first()
    if version is None:
        raise EnvironmentError("이 문제의 설정 버전을 찾지 못했습니다", "KOTH_ENV_VERSION_NOT_FOUND", 404)
    return version


def config_query():
    return KothEnvironmentConfig.objects.select_related("application__version", "applied_version", "challenge")


def config_data(config):
    if config is None:
        return {"state": "UNCONFIGURED", "desired_version_id": None, "applied_version_id": None,
                "application_id": None, "agent_configured": False, "checked_at": None, "applied_at": None,
                "error_code": None, "error_message": None, "target_id": None}
    return {"state": config.state, "desired_version_id": str(config.application.version_id) if config.application else None,
            "application_id": str(config.application_id) if config.application_id else None,
            "applied_version_id": str(config.applied_version_id) if config.applied_version_id else None,
            "agent_configured": bool(config.agent_token_hash), "target_id": config.target_id,
            "checked_at": config.checked_at.isoformat() if config.checked_at else None,
            "applied_at": config.applied_at.isoformat() if config.applied_at else None,
            "error_code": config.error_code or None, "error_message": ERRORS.get(config.error_code)}


def audit(challenge, actor, message):
    AdminEvent.objects.create(type=AdminEvent.EventType.CHALLENGE_UPDATED, actor=actor,
                              message=f"KOTH {challenge.title} ({challenge.pk}) 환경변수 {message}")


@method_decorator(never_cache, name="dispatch")
class EnvironmentView(APIView):
    permission_classes = [IsAdmin]

    @environment_errors
    def get(self, request, challenge_id):
        challenge = get_challenge(challenge_id)
        versions = list(KothEnvironmentVersion.objects.filter(challenge=challenge)[:20])
        config = config_query().filter(challenge=challenge).first()
        selected = get_version(challenge, request.query_params["version_id"]) if "version_id" in request.query_params else (versions[0] if versions else None)
        for extra in (selected, config.applied_version if config else None, config.application.version if config and config.application else None):
            if extra and all(v.pk != extra.pk for v in versions):
                versions.append(extra)
        can_apply = bool(selected and config and config.agent_token_hash and challenge.status != KothChallengeStatus.ACTIVE
                         and selected.source_revision == challenge.source_revision)
        return ok({"challenge_id": str(challenge.pk), "source_revision": challenge.source_revision,
                   "state": config_data(config), "can_apply": can_apply,
                   "versions": [serialize_version(v) for v in versions],
                   "selected": serialize_version(selected, details=True) if selected else None})


@method_decorator(never_cache, name="dispatch")
class EnvironmentPreviewView(APIView):
    permission_classes = [IsAdmin]

    @environment_errors
    @sensitive_variables()
    def post(self, request, challenge_id):
        challenge = get_challenge(challenge_id)
        if not isinstance(request.data, dict):
            raise EnvironmentError("환경변수 변경 요청 형식을 확인해주세요")
        source = get_version(challenge, request.data.get("base_version_id"))
        plan = prepare_changes(challenge, source, request.data)
        return ok({key: plan[key] for key in ("entries", "container", "source_revision")} | {"can_save": plan["changed"]})


@method_decorator(never_cache, name="dispatch")
class EnvironmentVersionsView(APIView):
    permission_classes = [IsAdmin]

    @environment_errors
    @sensitive_variables()
    def post(self, request, challenge_id):
        if not isinstance(request.data, dict):
            raise EnvironmentError("환경변수 변경 요청 형식을 확인해주세요")
        request_id, fingerprint = request_identity(request.data, request.user.login_id)
        challenge = get_challenge(challenge_id)
        existing = KothEnvironmentVersion.objects.filter(request_id=request_id).first()
        if existing:
            if existing.challenge_id != challenge.pk or not constant_time_compare(existing.request_fingerprint, fingerprint):
                raise EnvironmentError("다른 내용에 사용한 저장 요청입니다", "KOTH_ENV_CONFLICT", 409)
            return ok(serialize_version(existing, details=True))
        source = get_version(challenge, request.data.get("base_version_id"))
        plan = prepare_changes(challenge, source, request.data)
        with transaction.atomic():
            challenge = get_challenge(challenge_id, lock=True)
            require_source(challenge, source)
            existing = KothEnvironmentVersion.objects.filter(request_id=request_id).first()
            if existing:
                if existing.challenge_id != challenge.pk or not constant_time_compare(existing.request_fingerprint, fingerprint):
                    raise EnvironmentError("다른 내용에 사용한 저장 요청입니다", "KOTH_ENV_CONFLICT", 409)
                version = existing
            else:
                latest = KothEnvironmentVersion.objects.filter(challenge=challenge).first()
                if latest.pk != source.pk:
                    raise EnvironmentError("다른 관리자가 새 설정을 저장했습니다 최신 버전을 확인해주세요", "KOTH_ENV_CONFLICT", 409)
                if not plan["changed"]:
                    raise EnvironmentError("바뀐 설정이 없습니다", "KOTH_ENV_UNCHANGED", 409)
                version = create_version(challenge, plan["settings"], source.source_revision, request.user.login_id,
                                         request_id, fingerprint, based_on=source)
                audit(challenge, request.user.login_id, f"버전 {version.version} 저장")
        return ok(serialize_version(version, details=True))


@method_decorator(never_cache, name="dispatch")
class EnvironmentApplyView(APIView):
    permission_classes = [IsAdmin]

    @environment_errors
    def post(self, request, challenge_id, version_id):
        if not isinstance(request.data, dict) or set(request.data) != {"request_id"}:
            raise EnvironmentError("적용 요청 형식을 확인해주세요")
        body = {**request.data, "challenge_id": str(challenge_id), "version_id": str(version_id)}
        application_id, fingerprint = request_identity(body, request.user.login_id)
        with transaction.atomic():
            challenge = get_challenge(challenge_id, lock=True)
            config = config_query().select_for_update(of=("self",)).filter(challenge=challenge).first()
            version = get_version(challenge, str(version_id))
            existing = KothEnvironmentApply.objects.filter(pk=application_id).first()
            if existing:
                if existing.challenge_id != challenge.pk or not constant_time_compare(existing.request_fingerprint, fingerprint):
                    raise EnvironmentError("다른 내용에 사용한 적용 요청입니다", "KOTH_ENV_CONFLICT", 409)
                return ok({"application_id": str(existing.pk), "version_id": str(existing.version_id),
                           "superseded": not config or config.application_id != existing.pk, "state": config_data(config)})
            if config is None or not config.agent_token_hash:
                raise EnvironmentError("실행 VM 연결을 먼저 등록해주세요", "KOTH_ENV_UNCONFIGURED", 409)
            require_source(challenge, version)
            if challenge.status == KothChallengeStatus.ACTIVE:
                raise EnvironmentError("경기 진행 중에는 새 설정을 적용하지 않습니다", "KOTH_ENV_ACTIVE", 409)
            if (config.state == KothEnvironmentConfig.State.APPLYING and config.checked_at
                    and config.checked_at > timezone.now() - timedelta(minutes=2)):
                raise EnvironmentError("이전 설정을 적용 중입니다 완료 상태를 먼저 확인해주세요", "KOTH_ENV_APPLYING", 409)
            application = KothEnvironmentApply.objects.create(application_id=application_id, challenge=challenge,
                version=version, request_fingerprint=fingerprint, requested_by=request.user.login_id)
            already_confirmed = config.state == KothEnvironmentConfig.State.READY and config.applied_version_id == version.pk
            config.application = application
            config.state = KothEnvironmentConfig.State.READY if already_confirmed else KothEnvironmentConfig.State.QUEUED
            config.apply_actor = request.user.login_id
            config.error_code = ""
            config.save()
            audit(challenge, request.user.login_id, f"버전 {version.version} 적용 요청")
        return ok({"application_id": str(application.pk), "version_id": str(version.pk), "superseded": False, "state": config_data(config)})


class AgentBase(APIView):
    authentication_classes = []
    permission_classes = []

    def authenticate_config(self, request, challenge_id, lock=False):
        header = request.headers.get("Authorization", "")
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{32,256})", header)
        query = config_query()
        if lock:
            query = query.select_for_update(of=("self",))
        config = query.filter(challenge_id=challenge_id).first()
        if not match or not config or not config.agent_token_hash or not secrets.compare_digest(agent_token_hash(match[1]), config.agent_token_hash):
            raise EnvironmentError("실행 VM 인증을 확인해주세요", "KOTH_ENV_AGENT_UNAUTHORIZED", 401)
        return config


@method_decorator(never_cache, name="dispatch")
class EnvironmentAgentView(AgentBase):
    @environment_errors
    @sensitive_variables()
    def get(self, request, challenge_id):
        config = self.authenticate_config(request, challenge_id)
        now = timezone.now()
        KothEnvironmentConfig.objects.filter(pk=config.pk).filter(Q(checked_at__isnull=True) | Q(checked_at__lt=now - timedelta(seconds=60))).update(checked_at=now)
        application = config.application
        if not application or application.challenge_id != config.pk:
            raise EnvironmentError("등록된 설정을 확인해주세요", "KOTH_ENV_UNCONFIGURED", 409)
        version = application.version
        require_source(config.challenge, version)
        known = request.query_params.get("known_application_id")
        if known is not None:
            canonical_uuid(known)
        changed = known != str(application.pk)
        return ok({"challenge_id": str(challenge_id), "application_id": str(application.pk), "version_id": str(version.pk),
                   "version": version.version, "target_id": config.target_id, "source_revision": version.source_revision,
                   "can_apply": config.challenge.status != KothChallengeStatus.ACTIVE,
                   "state": config.state, "changed": changed,
                   "settings": decode_settings(version) if changed else None})


@method_decorator(never_cache, name="dispatch")
class EnvironmentAgentStatusView(AgentBase):
    @environment_errors
    def post(self, request, challenge_id):
        self.authenticate_config(request, challenge_id)
        body = request.data
        if not isinstance(body, dict) or not {"application_id", "version_id", "state"} <= set(body) or set(body) - {"application_id", "version_id", "state", "error_code"}:
            raise EnvironmentError("적용 결과 형식을 확인해주세요")
        application_id, version_id = canonical_uuid(body["application_id"]), canonical_uuid(body["version_id"])
        if body["state"] not in ("APPLYING", "APPLIED", "FAILED") or body.get("error_code", "") not in ("", *ERRORS):
            raise EnvironmentError("적용 결과 상태를 확인해주세요")
        with transaction.atomic():
            config = self.authenticate_config(request, challenge_id, lock=True)
            application = config.application
            if (config.application_id != application_id or not application or application.challenge_id != config.pk
                    or application.version_id != version_id or application.version.challenge_id != config.pk):
                raise EnvironmentError("이미 다른 적용 요청으로 변경됐습니다", "KOTH_ENV_CONFLICT", 409)
            if config.state == "READY" and config.applied_version_id == version_id:
                return ok(config_data(config))
            if body["state"] == "APPLIED":
                config.applied_version_id = version_id
                config.applied_at = timezone.now()
                config.state, config.error_code = "READY", ""
                audit(config.challenge, config.apply_actor or "environment-agent", f"버전 {application.version.version} 적용 확인")
            elif body["state"] == "APPLYING":
                if config.state != "FAILED":
                    config.state = "APPLYING"
            else:
                config.state, config.error_code = "FAILED", body.get("error_code") or "ROLLOUT_FAILED"
            config.checked_at = timezone.now()
            config.save()
        return ok(config_data(config))
