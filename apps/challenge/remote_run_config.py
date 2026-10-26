import re
from datetime import timedelta
from math import ceil

from django.conf import settings
from django.utils import timezone

from apps.challenge.models import ExeSubmission, RemoteRunConfig
from apps.instances.models import ArchitectureType, RuntimeType

WINDOWS_SLUG = "pwn-msgdriver"


def get_remote_config(challenge):
    if challenge.challenge_slug != WINDOWS_SLUG:
        return None
    return RemoteRunConfig.objects.filter(
        challenge=challenge
    ).first() or RemoteRunConfig(challenge=challenge)


def runtime_ready(challenge):
    config = getattr(challenge, "runtime_config", None)
    release = config.current_release if config else None
    return bool(
        release
        and release.challenge_id == challenge.pk
        and release.runtime_type == RuntimeType.VM
        and release.architecture == ArchitectureType.AMD64
        and release.approved_at is not None
    )


def _backend_ready(challenge):
    from apps.challenge.exe_submissions import (
        ExeSubmissionError,
        exe_flag_secret,
        private_payload_storage,
    )

    token = settings.SCHEDULER_API_TOKEN
    if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]+", token):
        return False
    try:
        private_payload_storage()
        exe_flag_secret(challenge)
    except (ExeSubmissionError, OSError, ValueError):
        return False
    return True


def retry_after_seconds(team_id, challenge_id, interval):
    last = (
        ExeSubmission.objects.filter(team_id=team_id, challenge_id=challenge_id)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    if last is None:
        return 0
    seconds = (last + timedelta(seconds=interval) - timezone.now()).total_seconds()
    return max(0, ceil(seconds))


def remote_run_data(challenge, team=None):
    from apps.challenge.checker_auth import checker_pool_ready

    config = get_remote_config(challenge)
    if config is None:
        return None
    ready = bool(
        settings.EXE_SUBMISSIONS_ENABLED
        and settings.EXE_DISPATCH_ENABLED
        and runtime_ready(challenge)
        and checker_pool_ready()
        and _backend_ready(challenge)
    )
    available = bool(config.enabled and ready)
    return {
        "environment": config.environment,
        "accept": [".exe"],
        "executable": "pe-amd64",
        "max_file_bytes": config.max_file_bytes,
        "timeout_seconds": config.timeout_seconds,
        "max_attempts": None,
        "rate_limit_seconds": config.rate_limit_seconds,
        "retry_after_seconds": retry_after_seconds(
            team.pk, challenge.pk, config.rate_limit_seconds
        )
        if team
        else 0,
        "available": available,
        "can_enable": ready,
        "enabled": config.enabled,
        "config_version": config.version,
        "availability_message": ""
        if available
        else "실행 접수를 꺼두었습니다"
        if ready
        else "Azure 실행 환경과 체커 연결을 준비하고 있습니다",
    }
