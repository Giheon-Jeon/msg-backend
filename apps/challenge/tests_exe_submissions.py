import hashlib
import io
import json
import os
import struct
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest import skipUnless
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from cryptography.fernet import Fernet
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.db import (
    DatabaseError,
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.db.models.deletion import ProtectedError
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.board.models import Cell, TeamChallengeAccess
from apps.challenge.exe_submissions import (
    MAX_OUTPUT_BYTES,
    MAX_PAYLOAD_BYTES,
    ExeSubmissionError,
    normalize_exe_output,
    private_payload_storage,
)
from apps.challenge.models import (
    Challenge,
    ExeSubmission,
    FlagSubmission,
    FlagSubmissionLock,
    Solve,
    RemoteRunConfig,
)
from apps.challenge.services import hash_flag
from apps.challenge.windows_assignments import (
    _NoRedirect,
    _post_scheduler,
    assign_oldest_exe_submission,
    release_windows_assignment,
    request_windows_assignment,
)
from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig
from apps.instances.services import SchedulerError
from apps.instances.runtime_secrets import create_runtime_secret
from apps.teams.models import MileageHistory


def harmless_pe():
    # Synthetic PE header with a single RET byte; never executed by these tests.
    image = bytearray(1024)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 0x80)
    image[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", image, 0x84, 0x8664, 1, 0, 0, 0, 240, 0x22)
    optional = 0x98
    struct.pack_into("<H", image, optional, 0x20B)
    struct.pack_into("<I", image, optional + 16, 0x1000)
    struct.pack_into("<Q", image, optional + 24, 0x140000000)
    for offset, value in ((32, 4096), (36, 512), (56, 8192), (60, 512), (108, 16)):
        struct.pack_into("<I", image, optional + offset, value)
    struct.pack_into("<H", image, optional + 68, 3)
    section = optional + 240
    image[section : section + 8] = b".text\0\0\0"
    for offset, value in ((8, 1), (12, 0x1000), (16, 512), (20, 512), (36, 0x60000020)):
        struct.pack_into("<I", image, section + offset, value)
    image[512] = 0xC3
    return bytes(image)


class ExeFixtures:
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        configured = override_settings(
            EXE_SUBMISSIONS_ENABLED=True,
            RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode("ascii")],
            EXE_DISPATCH_ENABLED=True,
            EXE_CHECKER_SIGNING_KEY="fixture-signing-key-" * 3,
            EXE_CHECKERS={
                "00000000-0000-0000-0000-000000000001": {
                    "base_url": "https://checker.example.invalid",
                    "service_token_env": "EXE_CHECKER_FIXTURE_TOKEN",
                    "checker_commit_sha": "a" * 40,
                    "vm_template_sha256": "b" * 64,
                    "driver_sha256": "c" * 64,
                    "config_version": "fixture-v1",
                }
            },
            EXE_SUBMISSION_STORAGE_ROOT=str(self.root),
            SECURE_SSL_REDIRECT=False,
            SCHEDULER_API_TOKEN="local-" + "test",
            SCHEDULER_BASE_URL="http://127.0.0.1:8001",
        )
        configured.enable()
        self.addCleanup(configured.disable)
        env = patch.dict(
            os.environ, {"EXE_CHECKER_FIXTURE_TOKEN": "fixture-token-" * 4}
        )
        env.start()
        self.addCleanup(env.stop)
        network = patch(
            "apps.challenge.windows_assignments.build_opener",
            side_effect=AssertionError("Live Scheduler forbidden"),
        )
        network.start()
        self.addCleanup(network.stop)
        self.team = Team.objects.create(team_name="exe-team")
        self.user = User.objects.create_user(
            login_id="exe-user", nickname="exe", team=self.team
        )
        self.challenge = Challenge.objects.create(
            challenge_slug="pwn-msgdriver",
            title="Windows EXE test",
            category="PWN",
            difficulty="EASY",
            score=500,
            flag_hash=hash_flag("local-exe-test"),
            is_published=True,
        )
        self.remote = RemoteRunConfig.objects.create(
            challenge=self.challenge, enabled=True
        )
        create_runtime_secret(self.challenge, "flag", "local-exe-test", "test")
        cell = Cell.objects.create(
            cell_index=1, type="CHALLENGE", difficulty="EASY", name="exe-cell"
        )
        TeamChallengeAccess.objects.create(
            team=self.team, challenge=self.challenge, source_cell=cell
        )
        self.release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            challenge_slug="pwn-msgdriver",
            version=1,
            registry_revision=1,
            runtime_type="VM",
            architecture="AMD64",
            isolation_profile="PWN",
            cpu_millicores=1000,
            memory_mib=4096,
            ephemeral_storage_mib=1024,
            approved_at=timezone.now(),
        )
        ChallengeRuntimeConfig.objects.create(
            challenge=self.challenge, current_release=self.release
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/v1/challenges/{self.challenge.pk}/exe-submissions"

    def post_exe(self, key=None, payload=None, client=None, **extra):
        return (client or self.client).post(
            self.url,
            {
                "file": SimpleUploadedFile(
                    "untrusted.exe", harmless_pe() if payload is None else payload
                ),
                **extra,
            },
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(key or uuid.uuid4()),
        )

    def accepted(self):
        response = self.post_exe()
        self.assertEqual(response.status_code, 202, response.data)
        return ExeSubmission.objects.get(pk=response.data["data"]["submission_id"])

    def accepted_other_team(self):
        team = Team.objects.create(team_name="another-exe-team")
        user = User.objects.create_user(
            login_id="another-exe-user", nickname="other", team=team
        )
        TeamChallengeAccess.objects.create(
            team=team,
            challenge=self.challenge,
            source_cell=Cell.objects.get(cell_index=1),
        )
        client = APIClient()
        client.force_authenticate(user)
        response = self.post_exe(client=client)
        self.assertEqual(response.status_code, 202, response.data)
        return ExeSubmission.objects.get(pk=response.data["data"]["submission_id"])

    def assignment(self, submission):
        return {
            "submission_id": str(submission.pk),
            "status": "LEASED",
            "lease_id": str(uuid.uuid4()),
            "worker_id": str(uuid.uuid4()),
            "address": "10.90.20.10",
            "expires_at": (timezone.now() + timedelta(minutes=5)).isoformat(),
        }

    def http_response(self, payload):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = payload
        return opener


class ExeSubmissionTests(ExeFixtures, TestCase):
    def test_accept_is_private_reserved_and_does_not_grade(self):
        submission = self.accepted()
        path = self.root / submission.payload_key
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), harmless_pe())
        self.assertEqual(
            submission.payload_sha256, hashlib.sha256(harmless_pe()).hexdigest()
        )
        self.assertEqual(submission.release_id, self.release.pk)
        data = self.client.get(f"{self.url}/{submission.pk}").data["data"]
        self.assertEqual(
            (
                data["used_attempts"],
                data["reserved_attempts"],
                data["available_attempts"],
            ),
            (0, 1, None),
        )
        self.assertIsNone(data["output"])
        self.assertFalse(Solve.objects.exists())
        self.assertFalse(FlagSubmission.objects.exists())
        self.assertFalse(FlagSubmissionLock.objects.exists())
        self.assertFalse(MileageHistory.objects.exists())

    @override_settings(EXE_SUBMISSIONS_ENABLED=False)
    def test_disabled_rejects_before_upload_or_network(self):
        response = self.post_exe(payload=b"not an executable")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "EXE_SUBMISSIONS_DISABLED")
        self.assertFalse(ExeSubmission.objects.exists())
        self.assertFalse(list(self.root.iterdir()))

    def test_same_key_replay_and_conflict(self):
        key = uuid.uuid4()
        first, second = self.post_exe(key), self.post_exe(key)
        self.assertEqual((first.status_code, second.status_code), (202, 200))
        self.assertEqual(
            first.data["data"]["submission_id"], second.data["data"]["submission_id"]
        )
        changed = bytearray(harmless_pe())
        changed[-1] = 1
        response = self.post_exe(key, bytes(changed))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "IDEMPOTENCY_KEY_CONFLICT")
        self.assertEqual(ExeSubmission.objects.count(), 1)
        self.assertEqual(len(list(self.root.rglob("payload.exe"))), 1)

    def test_request_interval_blocks_new_keys_without_counting_as_wrong_flags(self):
        self.assertEqual(
            [self.post_exe().status_code for _ in range(4)], [202, 429, 429, 429]
        )
        self.assertEqual(ExeSubmission.objects.count(), 1)
        self.assertFalse(FlagSubmissionLock.objects.exists())

    def test_missing_invalid_key_and_extra_fields(self):
        self.assertEqual(self.post_exe(key="invalid").status_code, 400)
        self.assertEqual(
            self.client.post(
                self.url,
                {"file": SimpleUploadedFile("x.exe", harmless_pe())},
                format="multipart",
            ).status_code,
            400,
        )
        self.assertEqual(self.post_exe(team_id=uuid.uuid4()).status_code, 400)
        self.assertEqual(self.post_exe(verdict="CORRECT").status_code, 400)
        response = self.client.post(
            self.url,
            {
                "file": [
                    SimpleUploadedFile("a", harmless_pe()),
                    SimpleUploadedFile("b", harmless_pe()),
                ]
            },
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ExeSubmission.objects.exists())

    def test_oversized_stream_is_rejected(self):
        response = self.post_exe(payload=b"MZ" + b"x" * MAX_PAYLOAD_BYTES)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.data["code"], "PAYLOAD_TOO_LARGE")
        self.assertFalse(ExeSubmission.objects.exists())
        self.assertFalse(list(self.root.iterdir()))

    def test_invalid_and_wrong_architecture_and_dll(self):
        for payload in (b"", b"plain text", harmless_pe()[:600]):
            with self.subTest(size=len(payload)):
                self.assertEqual(self.post_exe(payload=payload).status_code, 422)
        changed = bytearray(harmless_pe())
        struct.pack_into("<H", changed, 0x84, 0x14C)
        response = self.post_exe(payload=bytes(changed))
        self.assertEqual(response.data["code"], "UNSUPPORTED_ARCHITECTURE")
        struct.pack_into("<H", changed, 0x84, 0x8664)
        struct.pack_into("<H", changed, 0x96, 0x2022)
        self.assertEqual(self.post_exe(payload=bytes(changed)).status_code, 422)

    def test_unauthenticated_teamless_banned_and_locked(self):
        self.assertEqual(self.post_exe(client=APIClient()).status_code, 401)
        User.objects.filter(pk=self.user.pk).update(team=None)
        self.assertEqual(self.post_exe().status_code, 404)
        User.objects.filter(pk=self.user.pk).update(team=self.team)
        Team.objects.filter(pk=self.team.pk).update(is_banned=True)
        self.assertEqual(self.post_exe().status_code, 403)
        Team.objects.filter(pk=self.team.pk).update(is_banned=False)
        TeamChallengeAccess.objects.all().delete()
        self.assertEqual(self.post_exe().status_code, 403)

    def test_unpublished_other_challenge_and_missing_release(self):
        Challenge.objects.filter(pk=self.challenge.pk).update(is_published=False)
        self.assertEqual(self.post_exe().status_code, 403)
        Challenge.objects.filter(pk=self.challenge.pk).update(
            is_published=True, challenge_slug="other"
        )
        self.assertEqual(self.post_exe().status_code, 409)
        Challenge.objects.filter(pk=self.challenge.pk).update(
            challenge_slug="pwn-msgdriver"
        )
        ChallengeRuntimeConfig.objects.all().delete()
        self.assertEqual(self.post_exe().data["code"], "RELEASE_NOT_READY")

    def test_replay_survives_release_switch_and_solve(self):
        key = uuid.uuid4()
        first = self.post_exe(key)
        Solve.objects.create(
            team=self.team, challenge=self.challenge, earned_score=1, earned_mileage=0
        )
        ChallengeRuntimeConfig.objects.update(current_release=None)
        self.assertEqual(
            self.post_exe(key).data["data"]["submission_id"],
            first.data["data"]["submission_id"],
        )
        self.assertEqual(self.post_exe().data["code"], "ALREADY_SOLVED")

    def test_private_storage_required_and_no_media_directory(self):
        for root in ("", "relative", str(self.root / "public" / "nested")):
            with (
                self.subTest(root=root),
                override_settings(
                    EXE_SUBMISSION_STORAGE_ROOT=root,
                    MEDIA_ROOT=str(self.root / "public"),
                ),
            ):
                with self.assertRaises(ExeSubmissionError):
                    private_payload_storage()

    def test_unicode_output_limit_does_not_split_utf8_or_store_nul(self):
        text, truncated = normalize_exe_output("\x00\x1b" + "\uac00" * MAX_OUTPUT_BYTES)
        self.assertTrue(truncated)
        self.assertNotIn("\x00", text)
        self.assertNotIn("\x1b", text)
        self.assertLessEqual(len(text.encode("utf-8")), MAX_OUTPUT_BYTES)

    def test_failed_database_insert_removes_payload_and_reservation(self):
        with patch(
            "apps.challenge.exe_submissions.ExeSubmission.objects.create",
            side_effect=DatabaseError("unavailable"),
        ):
            self.assertEqual(self.post_exe().status_code, 503)
        self.assertFalse(ExeSubmission.objects.exists())
        self.assertFalse(list(self.root.rglob("payload.exe")))

    def test_storage_failure_does_not_reserve(self):
        with patch(
            "apps.challenge.exe_submissions.FileSystemStorage.save",
            side_effect=OSError("unavailable"),
        ):
            self.assertEqual(self.post_exe().status_code, 503)
        self.assertFalse(ExeSubmission.objects.exists())

    def test_other_team_problem_and_team_move_cannot_read(self):
        submission = self.accepted()
        detail = f"{self.url}/{submission.pk}"
        self.assertEqual(
            self.client.get(
                f"/api/v1/challenges/{uuid.uuid4()}/exe-submissions/{submission.pk}"
            ).status_code,
            404,
        )
        other = Team.objects.create(team_name="another")
        User.objects.filter(pk=self.user.pk).update(team=other)
        self.assertEqual(self.client.get(detail).status_code, 404)
        User.objects.filter(pk=self.user.pk).update(team=None)
        self.assertEqual(self.client.get(detail).status_code, 404)

    def test_same_team_member_can_read_after_acceptance_disabled(self):
        submission = self.accepted()
        member = User.objects.create_user(
            login_id="member", nickname="member", team=self.team
        )
        self.client.force_authenticate(member)
        with override_settings(EXE_SUBMISSIONS_ENABLED=False):
            response = self.client.get(f"{self.url}/{submission.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])

    def test_output_is_bounded_plain_text_and_not_an_automatic_solve(self):
        submission = self.accepted()
        submission.status = "COMPLETED"
        submission.attempt_state = "USED"
        submission.started_at = timezone.now()
        submission.finished_at = timezone.now()
        submission.stdout, submission.stdout_truncated = normalize_exe_output(
            "local-exe-test\n<script>text</script>\x1b\x00" + "x" * MAX_OUTPUT_BYTES
        )
        submission.stderr = ""
        submission.worker_address = "10.90.20.10"
        submission.last_error_code = "private-diagnostic"
        submission.save()
        response = self.client.get(f"{self.url}/{submission.pk}")
        data = response.data["data"]
        self.assertEqual(data["status"], "CLEANUP_PENDING")
        self.assertTrue(data["output"]["stdout_truncated"])
        self.assertIn("local-exe-test", data["output"]["stdout"])
        self.assertNotIn("\x1b", data["output"]["stdout"])
        self.assertNotIn("\x00", data["output"]["stdout"])
        self.assertLessEqual(len(data["output"]["stdout"].encode()), MAX_OUTPUT_BYTES)
        for private in (
            "payload_key",
            "payload_sha256",
            "worker_address",
            "lease_id",
            "flag_hash",
            "verdict",
            "last_error_code",
        ):
            self.assertNotIn(private, data)
        self.assertFalse(Solve.objects.exists())
        self.assertFalse(FlagSubmission.objects.exists())
        self.assertFalse(MileageHistory.objects.exists())
        submitted = self.client.post(
            f"/api/v1/challenges/{self.challenge.pk}/submit",
            {"flag": "local-exe-test"},
            format="json",
        )
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self.assertEqual(Solve.objects.count(), 1)

    def test_team_and_challenge_deletion_protect_execution_records(self):
        self.accepted()
        with self.assertRaises(ProtectedError):
            self.team.delete()
        admin = User.objects.create_user(
            login_id="exe-admin", nickname="admin", role=Role.ADMIN
        )
        self.client.force_authenticate(admin)
        response = self.client.delete(
            f"/api/v1/admin/teams/{self.team.pk}", {"reason": "test"}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        Challenge.objects.filter(pk=self.challenge.pk).update(is_published=False)
        response = self.client.delete(f"/api/v1/admin/challenges/{self.challenge.pk}")
        self.assertEqual(response.status_code, 409)


class ExeExecutionProvenanceTests(ExeFixtures, TestCase):
    fields = (
        "checker_commit_sha",
        "vm_template_sha256",
        "driver_sha256",
        "config_version",
    )

    def test_new_submission_provenance_is_unknown(self):
        submission = self.accepted()
        submission.refresh_from_db()
        self.assertEqual(
            {name: getattr(submission, name) for name in self.fields},
            dict.fromkeys(self.fields),
        )
        submission.full_clean()

    def test_reported_versions_persist_without_changing_execution_or_grading(self):
        submission = self.accepted()
        versions = {
            "checker_commit_sha": "a" * 40,
            "vm_template_sha256": "b" * 64,
            "driver_sha256": "c" * 64,
            "config_version": "checker-config-v1",
        }
        for name, value in versions.items():
            setattr(submission, name, value)
        submission.full_clean()
        submission.save(update_fields=list(self.fields))
        submission.refresh_from_db()
        self.assertEqual(
            {name: getattr(submission, name) for name in self.fields}, versions
        )
        response = self.post_exe(key=submission.idempotency_key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["submission_id"], str(submission.pk))
        submission.refresh_from_db()
        self.assertEqual(
            {name: getattr(submission, name) for name in self.fields}, versions
        )
        for data in (
            response.data["data"],
            self.client.get(f"{self.url}/{submission.pk}").data["data"],
        ):
            for name in self.fields:
                self.assertNotIn(name, data)
            self.assertEqual(data["used_attempts"], 0)
            self.assertEqual(data["reserved_attempts"], 1)
        self.assertFalse(Solve.objects.exists())
        self.assertFalse(FlagSubmission.objects.exists())
        self.assertFalse(MileageHistory.objects.exists())

    def test_partial_provenance_does_not_fabricate_remaining_values(self):
        submission = self.accepted()
        submission.checker_commit_sha = "a" * 40
        submission.full_clean()
        submission.save(update_fields=["checker_commit_sha"])
        submission.refresh_from_db()
        self.assertEqual(submission.checker_commit_sha, "a" * 40)
        for name in self.fields[1:]:
            self.assertIsNone(getattr(submission, name))

    def test_invalid_hashes_fail_model_validation(self):
        submission = self.accepted()
        for name, length in (
            ("checker_commit_sha", 40),
            ("vm_template_sha256", 64),
            ("driver_sha256", 64),
        ):
            for value in (
                "a" * (length - 1),
                "a" * (length + 1),
                "g" * length,
                "A" * length,
                "a" * length + "\n",
            ):
                with self.subTest(field=name, size=len(value)):
                    setattr(submission, name, value)
                    with self.assertRaises(ValidationError):
                        submission.full_clean()
                    setattr(submission, name, None)
        submission.config_version = "v" * 201
        with self.assertRaises(ValidationError):
            submission.full_clean()

    def test_invalid_hashes_cannot_bypass_validation_with_queryset_update(self):
        submission = self.accepted()
        for name, length in (
            ("checker_commit_sha", 40),
            ("vm_template_sha256", 64),
            ("driver_sha256", 64),
        ):
            for value in (
                "",
                "a" * (length - 1),
                "g" * length,
                "A" * length,
                "a" * (length - 1) + "\n",
            ):
                with self.subTest(field=name, size=len(value)):
                    with self.assertRaises(IntegrityError), transaction.atomic():
                        ExeSubmission.objects.filter(pk=submission.pk).update(
                            **{name: value}
                        )
            submission.refresh_from_db()
            self.assertIsNone(getattr(submission, name))

    def test_participant_cannot_supply_execution_versions(self):
        for name in self.fields:
            with self.subTest(field=name):
                self.assertEqual(
                    self.post_exe(**{name: "forged-version"}).status_code, 400
                )
        self.assertFalse(ExeSubmission.objects.exists())


class WindowsAssignmentTests(ExeFixtures, TestCase):
    @override_settings(SCHEDULER_API_TOKEN="")
    def test_scheduler_service_token_is_required(self):
        with patch("apps.challenge.windows_assignments.build_opener") as opener:
            with self.assertRaises(SchedulerError):
                _post_scheduler("/api/windows-assignments")
            opener.assert_not_called()

    @override_settings(EXE_SUBMISSIONS_ENABLED=False)
    def test_disabled_queue_does_not_assign(self):
        with patch(
            "apps.challenge.windows_assignments.request_windows_assignment"
        ) as called:
            with self.assertRaises(ExeSubmissionError):
                assign_oldest_exe_submission()
            called.assert_not_called()

    def test_request_body_and_raw_response(self):
        submission = self.accepted()
        opener = self.http_response(json.dumps(self.assignment(submission)).encode())
        with patch(
            "apps.challenge.windows_assignments.build_opener", return_value=opener
        ):
            result = request_windows_assignment(submission)
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url, "http://127.0.0.1:8001/api/windows-assignments"
        )
        body = json.loads(request.data)
        self.assertEqual(body["submission_id"], str(submission.pk))
        self.assertEqual(body["payload_sha256"], submission.payload_sha256)
        self.assertNotIn("payload_key", body)
        self.assertEqual(result["worker_address"], "10.90.20.10")

    def test_enveloped_response_and_no_redirect(self):
        submission = self.accepted()
        opener = self.http_response(
            json.dumps(
                {"code": "SUCCESS", "data": self.assignment(submission)}
            ).encode()
        )
        with patch(
            "apps.challenge.windows_assignments.build_opener", return_value=opener
        ):
            self.assertIn("lease_id", request_windows_assignment(submission))
        self.assertIsNone(
            _NoRedirect().redirect_request(
                None, None, 302, None, None, "https://untrusted.invalid"
            )
        )

    def test_malformed_wrong_submission_expired_and_unsafe_address(self):
        submission = self.accepted()
        for changes in (
            {"submission_id": str(uuid.uuid4())},
            {"lease_id": "bad"},
            {"expires_at": "2026-01-01T00:00:00Z"},
            {"expires_at": "2026-12-01T00:00:00"},
            {"address": "https://untrusted.invalid"},
            {"address": "10.0.0.1\nheader"},
        ):
            with (
                self.subTest(changes=changes),
                patch(
                    "apps.challenge.windows_assignments._post_scheduler",
                    return_value={**self.assignment(submission), **changes},
                ),
            ):
                with self.assertRaises(SchedulerError):
                    request_windows_assignment(submission)

    def test_http_busy_auth_timeout_and_invalid_json(self):
        for status, code, expected in (
            (409, "NO_WINDOWS_WORKER_AVAILABLE", "NO_WINDOWS_WORKER_AVAILABLE"),
            (409, "WINDOWS_ASSIGNMENT_ENDED", "WINDOWS_ASSIGNMENT_ENDED"),
            (502, "BROKER_CALL_FAILED", "BROKER_CALL_FAILED"),
            (404, "BROKER_CALL_FAILED", "SCHEDULER_UNAVAILABLE"),
            (502, "WINDOWS_ASSIGNMENT_NOT_FOUND", "SCHEDULER_UNAVAILABLE"),
            (401, "TOKEN_INVALID", "SCHEDULER_UNAVAILABLE"),
            (409, "CONFLICT", "SCHEDULER_UNAVAILABLE"),
        ):
            error = HTTPError(
                "http://scheduler.invalid",
                status,
                "error",
                {},
                io.BytesIO(json.dumps({"code": code}).encode()),
            )
            opener = MagicMock()
            opener.open.side_effect = error
            with (
                self.subTest(status=status, code=code),
                patch(
                    "apps.challenge.windows_assignments.build_opener",
                    return_value=opener,
                ),
            ):
                with self.assertRaises(SchedulerError) as raised:
                    _post_scheduler("/api/windows-assignments")
                self.assertEqual(raised.exception.code, expected)
        opener = MagicMock()
        opener.open.side_effect = URLError("connection lost")
        with (
            patch(
                "apps.challenge.windows_assignments.build_opener", return_value=opener
            ),
            self.assertRaises(SchedulerError),
        ):
            _post_scheduler("/api/windows-assignments")
        for payload in (b"invalid JSON", b"x" * 65537, b"[]", b'{"code":"FAILURE"}'):
            with (
                self.subTest(size=len(payload)),
                patch(
                    "apps.challenge.windows_assignments.build_opener",
                    return_value=self.http_response(payload),
                ),
                self.assertRaises(SchedulerError),
            ):
                _post_scheduler("/api/windows-assignments")

    def test_fifo_busy_and_uncertain_responses_keep_oldest_id(self):
        first, second = self.accepted(), self.accepted_other_team()
        for code, status in (
            ("NO_WINDOWS_WORKER_AVAILABLE", 409),
            ("SCHEDULER_UNAVAILABLE", 503),
        ):
            with patch(
                "apps.challenge.windows_assignments.request_windows_assignment",
                side_effect=SchedulerError(code, "test", status),
            ) as called:
                result = assign_oldest_exe_submission()
                self.assertEqual(result.pk, first.pk)
                self.assertEqual(called.call_args.args[0].pk, first.pk)
                self.assertEqual(result.attempt_state, "RESERVED")
                self.assertEqual(result.status, "QUEUED")
        with patch(
            "apps.challenge.windows_assignments._post_scheduler",
            return_value=self.assignment(first),
        ):
            result = assign_oldest_exe_submission()
            self.assertEqual(result.status, "ASSIGNED")
            self.assertEqual(result.attempt_state, "RESERVED")
        with patch(
            "apps.challenge.windows_assignments._post_scheduler",
            return_value=self.assignment(second),
        ):
            self.assertEqual(assign_oldest_exe_submission().pk, second.pk)
        self.assertIsNone(assign_oldest_exe_submission())

    def test_ended_assignment_leaves_fifo_without_releasing_unknown_execution(self):
        first, second = self.accepted(), self.accepted_other_team()
        error = HTTPError(
            "http://scheduler.invalid",
            409,
            "ended",
            {},
            io.BytesIO(json.dumps({"code": "WINDOWS_ASSIGNMENT_ENDED"}).encode()),
        )
        opener = MagicMock()
        opener.open.side_effect = error
        with patch(
            "apps.challenge.windows_assignments.build_opener", return_value=opener
        ):
            result = assign_oldest_exe_submission()
        self.assertEqual(result.pk, first.pk)
        first.refresh_from_db()
        self.assertEqual(first.status, "START_UNKNOWN")
        self.assertEqual(first.last_error_code, "WINDOWS_ASSIGNMENT_ENDED")
        self.assertEqual(first.attempt_state, "RESERVED")
        self.assertIsNone(first.started_at)
        self.assertIsNone(first.finished_at)
        self.assertIsNone(first.cleanup_complete)
        self.assertIsNone(first.lease_released_at)
        self.assertTrue((self.root / first.payload_key).exists())
        self.assertTrue(
            self.client.get(f"{self.url}/{first.pk}").data["data"]["requires_review"]
        )
        replay = self.post_exe(first.idempotency_key)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.data["data"]["submission_id"], str(first.pk))
        ExeSubmission.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timedelta(minutes=2)
        )
        self.assertEqual(self.post_exe().data["code"], "EXECUTION_IN_PROGRESS")
        with patch(
            "apps.challenge.windows_assignments.request_windows_assignment",
            return_value={
                "lease_id": uuid.uuid4(),
                "worker_id": uuid.uuid4(),
                "worker_address": "10.90.20.11",
                "lease_expires_at": timezone.now() + timedelta(minutes=5),
            },
        ) as called:
            self.assertEqual(assign_oldest_exe_submission().pk, second.pk)
            self.assertEqual(called.call_args.args[0].pk, second.pk)
            self.assertIsNone(assign_oldest_exe_submission())
        self.assertEqual(ExeSubmission.objects.count(), 2)

    def test_release_rejects_missing_wrong_or_unfinished_ack_and_keeps_payload(self):
        submission = self.accepted()
        submission.lease_id = uuid.uuid4()
        submission.status, submission.attempt_state = "COMPLETED", "USED"
        submission.started_at = submission.finished_at = timezone.now()
        submission.cleanup_complete = True
        submission.cleanup_confirmed_at = timezone.now()
        submission.save()
        for result in (
            None,
            {},
            [],
            {"lease_id": str(uuid.uuid4()), "status": "COMPLETED"},
            {"lease_id": "bad", "status": "COMPLETED"},
            {"lease_id": str(submission.lease_id)},
            {"lease_id": str(submission.lease_id), "status": "RUNNING"},
            {"lease_id": str(submission.lease_id), "status": ["COMPLETED"]},
        ):
            with (
                self.subTest(result=result),
                patch(
                    "apps.challenge.windows_assignments._post_scheduler",
                    return_value=result,
                ),
                self.assertRaises(SchedulerError),
            ):
                release_windows_assignment(submission.pk)
            submission.refresh_from_db()
            self.assertIsNone(submission.lease_released_at)
            self.assertTrue((self.root / submission.payload_key).exists())

    def test_expired_release_ack_is_valid_and_does_not_rewrite_checker_result(self):
        submission = self.accepted()
        submission.lease_id = uuid.uuid4()
        submission.status, submission.attempt_state = "TIMEOUT", "USED"
        submission.started_at = submission.finished_at = timezone.now()
        submission.cleanup_complete = True
        submission.cleanup_confirmed_at = timezone.now()
        submission.save()
        opener = self.http_response(
            json.dumps(
                {
                    "data": {
                        "lease_id": str(submission.lease_id),
                        "status": "EXPIRED",
                    },
                    "code": "SUCCESS",
                }
            ).encode()
        )
        with patch(
            "apps.challenge.windows_assignments.build_opener", return_value=opener
        ):
            release_windows_assignment(submission.pk)
            release_windows_assignment(submission.pk)
        self.assertEqual(opener.open.call_count, 1)
        submission.refresh_from_db()
        self.assertIsNotNone(submission.lease_released_at)
        self.assertEqual(submission.status, "TIMEOUT")
        self.assertEqual(submission.attempt_state, "USED")

    def test_release_http_errors_never_mark_released_and_retry_same_lease(self):
        submission = self.accepted()
        submission.lease_id = uuid.uuid4()
        submission.status, submission.attempt_state = "COMPLETED", "USED"
        submission.started_at = submission.finished_at = timezone.now()
        submission.cleanup_complete = True
        submission.cleanup_confirmed_at = timezone.now()
        submission.save()
        url = f"/api/windows-assignments/{submission.lease_id}/release"
        for status, code in (
            (502, "BROKER_CALL_FAILED"),
            (404, "WINDOWS_ASSIGNMENT_NOT_FOUND"),
        ):
            opener = MagicMock()
            opener.open.side_effect = HTTPError(
                "http://scheduler.invalid",
                status,
                "error",
                {},
                io.BytesIO(json.dumps({"code": code}).encode()),
            )
            with (
                self.subTest(status=status),
                patch(
                    "apps.challenge.windows_assignments.build_opener",
                    return_value=opener,
                ),
                self.assertRaises(SchedulerError),
            ):
                release_windows_assignment(submission.pk)
            self.assertEqual(
                opener.open.call_args.args[0].full_url, "http://127.0.0.1:8001" + url
            )
            submission.refresh_from_db()
            self.assertIsNone(submission.lease_released_at)
            self.assertTrue((self.root / submission.payload_key).exists())
        opener = self.http_response(
            json.dumps(
                {"lease_id": str(submission.lease_id), "status": "COMPLETED"}
            ).encode()
        )
        with patch(
            "apps.challenge.windows_assignments.build_opener", return_value=opener
        ):
            release_windows_assignment(submission.pk)
        self.assertEqual(opener.open.call_args.args[0].data, b"")
        self.assertEqual(
            opener.open.call_args.args[0].full_url, "http://127.0.0.1:8001" + url
        )

    def test_empty_release_response_is_uncertain(self):
        submission = self.accepted()
        submission.lease_id = uuid.uuid4()
        submission.status, submission.attempt_state = "COMPLETED", "USED"
        submission.started_at = submission.finished_at = timezone.now()
        submission.cleanup_complete = True
        submission.cleanup_confirmed_at = timezone.now()
        submission.save()
        with (
            patch(
                "apps.challenge.windows_assignments.build_opener",
                return_value=self.http_response(b""),
            ),
            self.assertRaises(SchedulerError),
        ):
            release_windows_assignment(submission.pk)
        submission.refresh_from_db()
        self.assertIsNone(submission.lease_released_at)
        self.assertTrue((self.root / submission.payload_key).exists())

    def test_assignment_does_not_run_checker_or_expose_address(self):
        submission = self.accepted()
        with patch(
            "apps.challenge.windows_assignments._post_scheduler",
            return_value=self.assignment(submission),
        ):
            assign_oldest_exe_submission()
        response = self.client.get(f"{self.url}/{submission.pk}")
        self.assertNotIn(b"10.90.20.10", response.content)
        self.assertIsNone(response.data["data"]["started_at"])
        self.assertEqual(response.data["data"]["used_attempts"], 0)
        self.assertEqual(response.data["data"]["status"], "PREPARING")
        self.assertEqual(response.data["data"]["vm_readiness"], "UNKNOWN")

    def test_release_retry_is_idempotent_and_not_cleanup_proof(self):
        submission = self.accepted()
        submission.lease_id = uuid.uuid4()
        submission.status = "COMPLETED"
        submission.attempt_state = "USED"
        submission.started_at = timezone.now()
        submission.finished_at = timezone.now()
        submission.cleanup_complete = True
        submission.cleanup_confirmed_at = timezone.now()
        submission.save()
        with (
            patch(
                "apps.challenge.windows_assignments._post_scheduler",
                side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "test", 503),
            ),
            self.assertRaises(SchedulerError),
        ):
            release_windows_assignment(submission.pk)
        submission.refresh_from_db()
        self.assertIsNone(submission.lease_released_at)
        with patch(
            "apps.challenge.windows_assignments._post_scheduler",
            return_value={"lease_id": str(submission.lease_id), "status": "COMPLETED"},
        ) as called:
            release_windows_assignment(submission.pk)
            release_windows_assignment(submission.pk)
            called.assert_called_once_with(
                f"/api/windows-assignments/{submission.lease_id}/release"
            )
        submission.refresh_from_db()
        self.assertIsNotNone(submission.lease_released_at)
        self.assertTrue(submission.cleanup_complete)

    def test_unknown_or_running_execution_cannot_release(self):
        submission = self.accepted()
        submission.lease_id = uuid.uuid4()
        for state in ("ASSIGNED", "RUNNING", "START_UNKNOWN"):
            submission.status = state
            submission.save()
            with (
                self.subTest(state=state),
                patch("apps.challenge.windows_assignments._post_scheduler") as called,
            ):
                with self.assertRaises(ExeSubmissionError):
                    release_windows_assignment(submission.pk)
                called.assert_not_called()


class ExeProvenanceMigrationTests(ExeFixtures, TransactionTestCase):
    def test_existing_cleanup_flag_is_not_fabricated_into_separate_report_proof(self):
        submission = self.accepted()
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()

        def restore_schema():
            MigrationExecutor(connection).migrate(latest)

        self.addCleanup(restore_schema)
        previous = [("challenge", "0010_remote_run_config_and_events")]
        executor.migrate(previous)
        historical = executor.loader.project_state(previous).apps.get_model("challenge", "ExeSubmission")
        historical.objects.filter(pk=submission.pk).update(
            status="COMPLETED", attempt_state="USED", finished_at=timezone.now(), cleanup_complete=True,
        )
        restore_schema()
        submission.refresh_from_db()
        self.assertTrue(submission.cleanup_complete)
        self.assertIsNone(submission.cleanup_confirmed_at)
        self.assertIsNone(submission.lease_released_at)
        self.assertTrue((self.root / submission.payload_key).is_file())

    def test_existing_submission_survives_provenance_migration(self):
        submission = self.accepted()
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()

        def restore_schema():
            MigrationExecutor(connection).migrate(latest)

        self.addCleanup(restore_schema)
        previous = [("challenge", "0008_exesubmission")]
        executor.migrate(previous)
        historical = executor.loader.project_state(previous).apps.get_model(
            "challenge", "ExeSubmission"
        )
        started_at = timezone.now()
        historical.objects.filter(pk=submission.pk).update(
            status="RUNNING",
            attempt_state="USED",
            started_at=started_at,
            stdout="existing output",
            stderr="",
        )
        restore_schema()
        submission.refresh_from_db()
        self.assertEqual(submission.status, "RUNNING")
        self.assertEqual(submission.attempt_state, "USED")
        self.assertEqual(submission.started_at, started_at)
        self.assertEqual(submission.stdout, "existing output")
        self.assertEqual(submission.release_id, self.release.pk)
        self.assertEqual(
            submission.payload_sha256, hashlib.sha256(harmless_pe()).hexdigest()
        )
        self.assertEqual(
            (self.root / submission.payload_key).read_bytes(), harmless_pe()
        )
        for name in ExeExecutionProvenanceTests.fields:
            self.assertIsNone(getattr(submission, name))


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking")
class ConcurrentExeSubmissionTests(ExeFixtures, TransactionTestCase):
    def run_posts(self, keys):
        ready = Barrier(len(keys))

        def submit(key):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=self.user.pk))
                ready.wait(timeout=10)
                result = self.post_exe(key=key, client=client)
                return result.status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(keys)) as pool:
            return sorted(pool.map(submit, keys))

    def test_simultaneous_new_keys_create_one_job_and_rate_limit_others(self):
        self.assertEqual(
            self.run_posts([uuid.uuid4() for _ in range(4)]), [202, 429, 429, 429]
        )
        self.assertEqual(ExeSubmission.objects.count(), 1)

    def test_concurrent_same_key_creates_one_file_and_reservation(self):
        key = uuid.uuid4()
        self.assertEqual(self.run_posts([key, key]), [200, 202])
        self.assertEqual(ExeSubmission.objects.count(), 1)
        self.assertEqual(len(list(self.root.rglob("payload.exe"))), 1)

    def test_parallel_queue_steps_do_not_assign_the_same_submission(self):
        first, second = self.accepted(), self.accepted_other_team()
        ready = Barrier(2)
        requested = []

        def scheduler(path, body):
            submission = ExeSubmission.objects.get(pk=body["submission_id"])
            requested.append(submission.pk)
            return self.assignment(submission)

        def assign(_):
            close_old_connections()
            try:
                ready.wait(timeout=10)
                return assign_oldest_exe_submission().pk
            finally:
                connections.close_all()

        with patch(
            "apps.challenge.windows_assignments._post_scheduler", side_effect=scheduler
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(assign, range(2)))
        self.assertEqual(set(results), {first.pk, second.pk})
        self.assertEqual(requested, [first.pk, second.pk])
