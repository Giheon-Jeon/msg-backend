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

from apps.accounts.models import User
from apps.board.models import TeamChallengeAccess
from apps.challenge.models import Challenge, ExeSubmission, Solve
from apps.common.exceptions import APIError
from apps.instances.models import ArchitectureType, ChallengeRuntimeConfig, RuntimeType
from apps.instances.services import isoformat_z

logger = logging.getLogger(__name__)
MAX_PAYLOAD_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
ATTEMPT_LIMIT = 3
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
        "attempt_limit": ATTEMPT_LIMIT,
        "used_attempts": counts["used"],
        "reserved_attempts": counts["reserved"],
        "remaining_attempts": max(0, ATTEMPT_LIMIT - counts["used"]),
        "available_attempts": max(
            0, ATTEMPT_LIMIT - counts["used"] - counts["reserved"]
        ),
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
    storage = private_payload_storage()
    payload_key = None
    try:
        with transaction.atomic():
            # One lock serializes reservations and their future execution transitions.
            challenge = Challenge.objects.select_for_update().get(pk=challenge_id)
            team = submission_team(user)
            check_challenge_access(team, challenge)
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
            if attempt_counts(team.pk, challenge.pk)["available_attempts"] == 0:
                raise ExeSubmissionError(
                    "ATTEMPT_LIMIT_REACHED",
                    "All execution attempts are used or reserved.",
                    409,
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
            ):
                raise ExeSubmissionError(
                    "RELEASE_NOT_READY", "An AMD64 VM release is required.", 409
                )
            submission_id = uuid.uuid4()
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
            )
        return submission, True
    except Exception as error:
        if payload_key:
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
        status = "PREPARING"
    if submission.finished_at and submission.cleanup_complete is not True:
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
        "vm_readiness": "UNKNOWN",
        "cleanup_complete": submission.cleanup_complete,
        "requires_review": submission.status == ExeSubmission.Status.START_UNKNOWN,
        "output": output,
        **attempt_counts(submission.team_id, submission.challenge_id),
        "submitted_at": isoformat_z(submission.created_at),
        "started_at": isoformat_z(submission.started_at),
        "finished_at": isoformat_z(submission.finished_at),
        "updated_at": isoformat_z(submission.updated_at),
    }
