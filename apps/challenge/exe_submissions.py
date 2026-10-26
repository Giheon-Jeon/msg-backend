import hashlib
import logging
import uuid
from pathlib import Path

import pefile
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db import DatabaseError, transaction
from django.db.models import Count, Q

from apps.accounts.models import Team, User
from apps.board.models import TeamChallengeAccess
from apps.challenge.models import Challenge, ExeSubmission, Solve
from apps.challenge.remote_run_config import (
    get_remote_config,
    remote_run_data,
    retry_after_seconds,
)
from apps.common.exceptions import APIError
from apps.instances.models import ArchitectureType, ChallengeRuntimeConfig, RuntimeType
from apps.instances.models import RuntimeSecret
from apps.instances.runtime_secrets import (
    decrypt_runtime_secret,
    RuntimeSecretUnavailable,
)
from apps.challenge.services import is_correct_flag
from apps.instances.services import isoformat_z

logger = logging.getLogger(__name__)
MAX_PAYLOAD_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
EXE_CHALLENGE_SLUG = "pwn-msgdriver"


class ExeSubmissionError(APIError):
    def __init__(self, code, message, status=400):
        self.code = code
        self.status_code = status
        super().__init__(message)


def require_exe_enabled():
    if not settings.EXE_SUBMISSIONS_ENABLED:
        raise ExeSubmissionError(
            "EXE_SUBMISSIONS_DISABLED", "EXE submissions are not enabled.", 503
        )


def private_payload_storage():
    root = Path(settings.EXE_SUBMISSION_STORAGE_ROOT)
    if not settings.EXE_SUBMISSION_STORAGE_ROOT or not root.is_absolute():
        raise ExeSubmissionError(
            "SUBMISSION_UNAVAILABLE", "Private storage is not configured.", 503
        )
    root = root.resolve()
    media = Path(settings.MEDIA_ROOT).resolve()
    if root == media or media in root.parents:
        raise ExeSubmissionError(
            "SUBMISSION_UNAVAILABLE", "Private storage must be outside media.", 503
        )
    return FileSystemStorage(
        location=root, file_permissions_mode=0o600, directory_permissions_mode=0o700
    )


def exe_flag_secret(challenge):
    secret = (
        RuntimeSecret.objects.filter(challenge=challenge, name="flag")
        .order_by("-version")
        .first()
    )
    try:
        if secret is None or not is_correct_flag(
            decrypt_runtime_secret(secret), challenge.flag_hash
        ):
            raise RuntimeSecretUnavailable()
    except RuntimeSecretUnavailable:
        raise ExeSubmissionError(
            "FLAG_NOT_READY", "플래그 주입 설정을 확인해주세요", 503
        ) from None
    return secret


def validate_executable(payload):
    if not payload or len(payload) > MAX_PAYLOAD_BYTES:
        raise ExeSubmissionError(
            "PAYLOAD_TOO_LARGE" if payload else "INVALID_EXECUTABLE",
            "Invalid executable size.",
            413 if payload else 422,
        )
    try:
        with pefile.PE(data=payload, fast_load=True) as image:
            if (
                image.FILE_HEADER.Machine != 0x8664
                or image.OPTIONAL_HEADER.Magic != 0x20B
            ):
                raise ExeSubmissionError(
                    "UNSUPPORTED_ARCHITECTURE",
                    "An AMD64 PE32+ executable is required.",
                    422,
                )
            if (
                not image.FILE_HEADER.Characteristics & 0x0002
                or image.FILE_HEADER.Characteristics & 0x2000
                or not 1 <= image.FILE_HEADER.NumberOfSections <= 96
                or len(image.sections) != image.FILE_HEADER.NumberOfSections
                or image.OPTIONAL_HEADER.SizeOfHeaders > len(payload)
                or image.OPTIONAL_HEADER.Subsystem not in (2, 3)
                or any(
                    section.SizeOfRawData
                    and section.PointerToRawData + section.SizeOfRawData > len(payload)
                    for section in image.sections
                )
            ):
                raise pefile.PEFormatError("Invalid executable structure")
            entry = image.OPTIONAL_HEADER.AddressOfEntryPoint
            section = image.get_section_by_rva(entry)
            if (
                section is None
                or not section.Characteristics & 0x20000000
                or image.get_offset_from_rva(entry) >= len(payload)
            ):
                raise pefile.PEFormatError("Invalid executable entry point")
    except pefile.PEFormatError:
        raise ExeSubmissionError(
            "INVALID_EXECUTABLE", "The file is not a supported executable.", 422
        ) from None


def submission_team(user):
    current = User.objects.select_related("team").get(pk=user.pk)
    if current.team is None:
        raise ExeSubmissionError(
            "USER_HAS_NO_TEAM", "Team membership is required.", 404
        )
    if current.team.is_banned:
        raise ExeSubmissionError("TEAM_BANNED", "The team is banned.", 403)
    return current.team


def check_challenge_access(team, challenge):
    if (
        not challenge.is_published
        or not TeamChallengeAccess.objects.filter(
            team=team, challenge=challenge
        ).exists()
    ):
        raise ExeSubmissionError(
            "CHALLENGE_LOCKED", "The challenge is not available.", 403
        )
    if challenge.challenge_slug != EXE_CHALLENGE_SLUG:
        raise ExeSubmissionError(
            "EXE_SUBMISSION_NOT_SUPPORTED",
            "This challenge does not accept EXE files.",
            409,
        )


def attempt_counts(team_id, challenge_id):
    counts = ExeSubmission.objects.filter(
        team_id=team_id, challenge_id=challenge_id
    ).aggregate(
        used=Count("pk", filter=Q(attempt_state=ExeSubmission.AttemptState.USED)),
        reserved=Count(
            "pk", filter=Q(attempt_state=ExeSubmission.AttemptState.RESERVED)
        ),
    )
    return {
        "attempt_limit": None,
        "used_attempts": counts["used"],
        "reserved_attempts": counts["reserved"],
        "remaining_attempts": None,
        "available_attempts": None,
    }


def accept_exe(user, challenge_id, key, uploaded_file):
    require_exe_enabled()
    try:
        key = uuid.UUID(key)
    except (ValueError, TypeError, AttributeError):
        raise ExeSubmissionError(
            "INVALID_REQUEST", "Idempotency-Key must be a UUID."
        ) from None
    team = submission_team(user)
    challenge = Challenge.objects.filter(pk=challenge_id).first()
    if challenge is None:
        raise ExeSubmissionError("CHALLENGE_NOT_FOUND", "Challenge not found.", 404)
    check_challenge_access(team, challenge)
    payload = uploaded_file.read(MAX_PAYLOAD_BYTES + 1)
    validate_executable(payload)
    digest = hashlib.sha256(payload).hexdigest()
    payload_key = None
    storage = None
    try:
        with transaction.atomic():
            # Match flag submission's challenge -> team and admin's team -> user.
            # Never hold a member/team lock while waiting for the challenge.
            try:
                challenge = Challenge.objects.select_for_update().get(pk=challenge_id)
            except Challenge.DoesNotExist:
                raise ExeSubmissionError(
                    "CHALLENGE_NOT_FOUND", "Challenge not found.", 404
                ) from None
            try:
                locked_team = Team.objects.select_for_update(no_key=True).get(pk=team.pk)
            except Team.DoesNotExist:
                raise ExeSubmissionError(
                    "TEAM_CHANGED", "팀 배정이 바뀌었습니다 현재 팀을 확인한 뒤 다시 요청해주세요", 409
                ) from None
            try:
                member = User.objects.select_for_update(no_key=True).get(pk=user.pk)
            except User.DoesNotExist:
                raise ExeSubmissionError(
                    "USER_HAS_NO_TEAM", "계정과 팀 배정을 확인해주세요", 404
                ) from None
            if member.team_id is None:
                raise ExeSubmissionError(
                    "USER_HAS_NO_TEAM", "팀 배정을 확인해주세요", 404
                )
            if member.team_id != locked_team.pk:
                raise ExeSubmissionError(
                    "TEAM_CHANGED", "팀 배정이 바뀌었습니다 현재 팀을 확인한 뒤 다시 요청해주세요", 409
                )
            if locked_team.is_banned:
                raise ExeSubmissionError("TEAM_BANNED", "참가할 수 없는 팀입니다", 403)
            team = locked_team
            check_challenge_access(team, challenge)
            if (
                ExeSubmission.objects.filter(
                    user_id=user.pk, challenge=challenge, idempotency_key=key
                )
                .exclude(team=team)
                .exists()
            ):
                raise ExeSubmissionError(
                    "TEAM_CHANGED",
                    "팀 배정이 바뀌었습니다 현재 팀을 확인한 뒤 다시 요청해주세요",
                    409,
                )
            previous = ExeSubmission.objects.filter(
                team=team, challenge=challenge, idempotency_key=key
            ).first()
            if previous is not None:
                if previous.payload_sha256 != digest or previous.payload_size != len(
                    payload
                ):
                    raise ExeSubmissionError(
                        "IDEMPOTENCY_KEY_CONFLICT",
                        "The key belongs to another payload.",
                        409,
                    )
                return previous, False
            if Solve.objects.filter(team=team, challenge=challenge).exists():
                raise ExeSubmissionError(
                    "ALREADY_SOLVED", "The challenge is already solved.", 409
                )
            remote = get_remote_config(challenge)
            if remote is None or not remote_run_data(challenge)["available"]:
                raise ExeSubmissionError(
                    "RELEASE_NOT_READY",
                    "실행 환경 연결을 준비하고 있습니다",
                    409,
                )
            retry = retry_after_seconds(
                team.pk, challenge.pk, remote.rate_limit_seconds
            )
            if retry:
                error = ExeSubmissionError(
                    "EXE_RATE_LIMITED", "잠시 후 다시 실행해주세요", 429
                )
                error.wait = retry
                error.data = {"retry_after_seconds": retry}
                raise error
            if (
                ExeSubmission.objects.filter(team=team, challenge=challenge)
                .filter(
                    Q(status__in=["QUEUED", "ASSIGNED", "RUNNING", "START_UNKNOWN"])
                    | Q(lease_id__isnull=False, lease_released_at__isnull=True)
                )
                .exists()
            ):
                raise ExeSubmissionError(
                    "EXECUTION_IN_PROGRESS",
                    "이전 실행의 종료와 정리를 기다려주세요",
                    409,
                )
            name = uploaded_file.name.replace("\\", "/").rsplit("/", 1)[-1]
            name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:200]
            if (
                not name.lower().endswith(".exe")
                or len(payload) > remote.max_file_bytes
            ):
                raise ExeSubmissionError(
                    "INVALID_EXECUTABLE", "설정한 크기의 .exe 파일을 선택해주세요", 422
                )
            config = (
                ChallengeRuntimeConfig.objects.filter(challenge=challenge)
                .select_related("current_release")
                .first()
            )
            release = config.current_release if config else None
            if (
                release is None
                or release.challenge_id != challenge.pk
                or release.runtime_type != RuntimeType.VM
                or release.architecture != ArchitectureType.AMD64
                or release.approved_at is None
            ):
                raise ExeSubmissionError(
                    "RELEASE_NOT_READY", "An AMD64 VM release is required.", 409
                )
            secret = exe_flag_secret(challenge)
            submission_id = uuid.uuid4()
            storage = private_payload_storage()
            payload_key = storage.save(
                f"{submission_id}/payload.exe", ContentFile(payload)
            )
            submission = ExeSubmission.objects.create(
                submission_id=submission_id,
                team=team,
                challenge=challenge,
                user_id=user.pk,
                release=release,
                idempotency_key=key,
                payload_key=payload_key,
                payload_sha256=digest,
                payload_size=len(payload),
                file_name=name,
                flag_secret=secret,
                config_snapshot={
                    "version": remote.version,
                    "environment": remote.environment,
                    "timeout_seconds": remote.timeout_seconds,
                    "max_file_bytes": remote.max_file_bytes,
                    "rate_limit_seconds": remote.rate_limit_seconds,
                },
            )
        return submission, True
    except Exception as error:
        if payload_key and storage is not None:
            try:
                storage.delete(payload_key)
            except OSError:
                logger.error(
                    "EXE payload rollback cleanup failed; private storage reconciliation required"
                )
        if isinstance(error, (OSError, DatabaseError)):
            raise ExeSubmissionError(
                "SUBMISSION_UNAVAILABLE",
                "Submission storage is temporarily unavailable.",
                503,
            ) from None
        raise


def normalize_exe_output(value, truncated=False):
    """Normalize before DB storage as well as response serialization (PostgreSQL rejects NUL)."""
    if value is None:
        return None, truncated
    raw = value.encode("utf-8", errors="replace")
    # Output is untrusted plain text; never render it as HTML or terminal commands.
    text = raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    text = "".join(
        char
        for char in text
        if char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159)
    )
    return text, truncated or len(raw) > MAX_OUTPUT_BYTES


def serialize_exe_submission(submission):
    stdout, stdout_truncated = normalize_exe_output(
        submission.stdout, submission.stdout_truncated
    )
    stderr, stderr_truncated = normalize_exe_output(
        submission.stderr, submission.stderr_truncated
    )
    output = (
        None
        if stdout is None and stderr is None
        else {
            "stdout": stdout,
            "stderr": stderr,
            "encoding": "utf-8",
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
    )
    status = submission.status
    if status == ExeSubmission.Status.ASSIGNED:
        status = (
            "START_UNKNOWN" if submission.dispatch_state == "UNKNOWN" else "PREPARING"
        )
    cleanup = submission.cleanup_complete
    if cleanup is True and submission.cleanup_confirmed_at is None:
        cleanup = None
    if submission.finished_at and cleanup is not True:
        status = "CLEANUP_PENDING"
    return {
        "submission_id": str(submission.pk),
        "challenge_id": str(submission.challenge_id),
        "status": status,
        "execution_status": (
            "QUEUED"
            if submission.status == ExeSubmission.Status.ASSIGNED
            else submission.status
        ),
        # A lease is not proof that the Windows guest/checker is ready to execute.
        "vm_readiness": submission.vm_readiness,
        "cleanup_complete": cleanup,
        "requires_review": submission.status == ExeSubmission.Status.START_UNKNOWN
        or submission.dispatch_state == "UNKNOWN",
        "file_name": submission.file_name,
        "file_size": submission.payload_size,
        "exit_code": submission.exit_code,
        "duration_ms": max(
            0,
            int(
                (submission.finished_at - submission.started_at).total_seconds() * 1000
            ),
        )
        if submission.finished_at and submission.started_at
        else None,
        "release_pending": bool(
            submission.lease_id and submission.lease_released_at is None
        ),
        "output": output,
        **attempt_counts(submission.team_id, submission.challenge_id),
        "submitted_at": isoformat_z(submission.created_at),
        "started_at": isoformat_z(submission.started_at),
        "finished_at": isoformat_z(submission.finished_at),
        "updated_at": isoformat_z(submission.updated_at),
    }
