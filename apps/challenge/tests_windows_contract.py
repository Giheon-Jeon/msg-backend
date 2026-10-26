import json
import socket
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import build_opener

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.challenge.models import ExeSubmission
from apps.challenge.tests_exe_submissions import ExeFixtures
from apps.challenge.windows_assignments import (
    assign_oldest_exe_submission,
    release_windows_assignment,
    refresh_windows_assignment,
    request_windows_assignment,
)
from apps.instances.services import SchedulerError


class WindowsAssignmentContractTests(ExeFixtures, TestCase):
    @contextmanager
    def scheduler(self, replies):
        """Local protocol fixture; this never provisions or executes a VM."""
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                requests.append({"path": self.path, "body": body})
                reply = replies.pop(0)
                if reply is None:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                status, body = reply if isinstance(reply, tuple) else (200, reply)
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"

        def local_opener(*handlers):
            opener = build_opener(*handlers)

            class LocalOnly:
                def open(self, request, **kwargs):
                    if not request.full_url.startswith(origin + "/"):
                        raise AssertionError(
                            "Only the local protocol fixture is allowed"
                        )
                    return opener.open(request, **kwargs)

            return LocalOnly()

        try:
            with (
                override_settings(
                    SCHEDULER_BASE_URL=origin,
                    SCHEDULER_API_TOKEN=uuid.uuid4().hex,
                ),
                patch(
                    "apps.challenge.windows_assignments.build_opener",
                    side_effect=local_opener,
                ),
            ):
                yield requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_ended_status_in_success_response_quarantines_job_without_dispatch(self):
        row = self.accepted()
        for status in ("COMPLETED", "EXPIRED"):
            row.status = "QUEUED"
            row.save(update_fields=["status"])
            response = {**self.assignment(row), "status": status}
            with self.subTest(status=status), self.scheduler([response]):
                result = assign_oldest_exe_submission()
                self.assertEqual(result.status, "START_UNKNOWN")
                self.assertEqual(result.last_error_code, "WINDOWS_ASSIGNMENT_ENDED")
                row.refresh_from_db()
                self.assertIsNone(row.lease_released_at)
                self.assertIsNone(row.finished_at)
                self.assertTrue((self.root / row.payload_key).is_file())

    def test_unknown_lease_status_cannot_become_a_usable_assignment(self):
        row = self.accepted()
        for status in (
            None,
            False,
            True,
            1,
            "",
            ["LEASED"],
            {},
            "RECREATING",
            "RUNNING",
            "leased",
            " LEASED",
            "LEASED\n",
        ):
            response = {**self.assignment(row), "status": status}
            with self.subTest(status=status), self.scheduler([response]):
                with self.assertRaises(SchedulerError):
                    request_windows_assignment(row)
        row.refresh_from_db()
        self.assertEqual(row.status, "QUEUED")
        self.assertIsNone(row.lease_id)

    def test_missing_status_never_assigns_and_retry_keeps_the_same_submission(self):
        row = self.accepted()
        valid = self.assignment(row)
        missing = {key: value for key, value in valid.items() if key != "status"}
        missing.update(
            vm_readiness="READY",
            vm_id="fixture-vm",
            readiness_check_id="guest-proof",
            readiness_checked_at=timezone.now().isoformat(),
        )
        path = self.root / row.payload_key
        with self.scheduler(
            [missing, {"code": "SUCCESS", "data": missing}, valid]
        ) as requests:
            for _ in range(2):
                result = assign_oldest_exe_submission()
                self.assertEqual(result.pk, row.pk)
                self.assertEqual(result.status, "QUEUED")
                self.assertEqual(result.last_error_code, "SCHEDULER_UNAVAILABLE")
                row.refresh_from_db()
                self.assertIsNone(row.lease_id)
                self.assertIsNone(row.worker_id)
                self.assertEqual(row.vm_readiness, "UNKNOWN")
                self.assertEqual(row.dispatch_state, "PENDING")
                self.assertIsNone(row.lease_released_at)
                self.assertTrue(path.is_file())
            result = assign_oldest_exe_submission()
        self.assertEqual(result.pk, row.pk)
        self.assertEqual(result.status, "ASSIGNED")
        self.assertEqual(str(result.lease_id), valid["lease_id"])
        self.assertEqual(ExeSubmission.objects.count(), 1)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(request == requests[0] for request in requests))

    def test_expired_success_response_leaves_fifo_for_other_teams(self):
        row = self.accepted()
        other = self.accepted_other_team()
        expired = {
            **self.assignment(row),
            "expires_at": (timezone.now() - timedelta(seconds=1)).isoformat(),
        }
        with self.scheduler([expired, self.assignment(other)]):
            first = assign_oldest_exe_submission()
            second = assign_oldest_exe_submission()
        self.assertEqual(first.status, "START_UNKNOWN")
        self.assertEqual(first.last_error_code, "WINDOWS_ASSIGNMENT_ENDED")
        self.assertEqual(second.pk, other.pk)
        self.assertEqual(second.status, "ASSIGNED")
        self.assertEqual(ExeSubmission.objects.count(), 2)

    def test_conflicting_assignment_is_quarantined_and_other_teams_continue(self):
        row = self.accepted()
        other = self.accepted_other_team()
        response = (
            409,
            {
                "code": "WINDOWS_ASSIGNMENT_CONFLICT",
                "message": "private-provider-detail",
            },
        )
        with self.scheduler([response, self.assignment(other)]) as requests:
            first = assign_oldest_exe_submission()
            second = assign_oldest_exe_submission()
            self.assertIsNone(assign_oldest_exe_submission())
        self.assertEqual(first.pk, row.pk)
        self.assertEqual(first.status, "START_UNKNOWN")
        self.assertEqual(first.attempt_state, "RESERVED")
        self.assertEqual(first.last_error_code, "WINDOWS_ASSIGNMENT_CONFLICT")
        self.assertEqual(second.pk, other.pk)
        self.assertEqual(second.status, "ASSIGNED")
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            json.loads(requests[0]["body"]),
            {
                "submission_id": str(row.pk),
                "team_id": str(row.team_id),
                "challenge_id": str(row.challenge_id),
                "release_id": str(row.release_id),
                "payload_sha256": row.payload_sha256,
            },
        )
        row.refresh_from_db()
        self.assertIsNone(row.lease_id)
        self.assertIsNone(row.lease_released_at)
        self.assertIsNone(row.finished_at)
        self.assertTrue((self.root / row.payload_key).is_file())
        detail = self.client.get(f"{self.url}/{row.pk}")
        self.assertTrue(detail.data["data"]["requires_review"])
        self.assertNotIn(b"private-provider-detail", detail.content)
        ExeSubmission.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(minutes=1)
        )
        self.assertEqual(self.post_exe().data["code"], "EXECUTION_IN_PROGRESS")

    def test_explicit_active_lease_is_accepted(self):
        row = self.accepted()
        response = {**self.assignment(row), "status": "LEASED"}
        with self.scheduler([{"code": "SUCCESS", "data": response}]) as requests:
            result = request_windows_assignment(row)
        self.assertEqual(str(result["lease_id"]), response["lease_id"])
        self.assertEqual(json.loads(requests[0]["body"])["submission_id"], str(row.pk))

    def test_same_assignment_post_refreshes_guest_proof_with_identical_body(self):
        row = self.accepted()
        initial = {**self.assignment(row), "vm_readiness": "UNKNOWN"}
        ready = {
            **initial,
            "vm_readiness": "READY",
            "vm_id": "fixture-vm",
            "readiness_check_id": "fresh-proof",
            "readiness_checked_at": timezone.now().isoformat(),
        }
        with self.scheduler(
            [
                (201, {"code": "SUCCESS", "data": initial}),
                (200, {"code": "SUCCESS", "data": ready}),
            ]
        ) as requests:
            assigned = assign_oldest_exe_submission()
            self.assertEqual(assigned.vm_readiness, "UNKNOWN")
            self.assertTrue(refresh_windows_assignment(assigned))
        self.assertEqual(requests[0], requests[1])
        assigned.refresh_from_db()
        self.assertEqual(assigned.pk, row.pk)
        self.assertEqual(str(assigned.lease_id), initial["lease_id"])
        self.assertEqual(str(assigned.worker_id), initial["worker_id"])
        self.assertEqual(assigned.vm_readiness, "READY")
        self.assertEqual(assigned.readiness_check_id, "fresh-proof")
        self.assertEqual(assigned.dispatch_state, "PENDING")
        self.assertEqual(ExeSubmission.objects.count(), 1)

    def test_lost_release_ack_preserves_payload_and_retries_same_lease(self):
        row = self.accepted()
        row.lease_id = uuid.uuid4()
        row.status, row.attempt_state = "COMPLETED", "USED"
        row.started_at = row.finished_at = timezone.now()
        row.cleanup_complete = True
        row.cleanup_confirmed_at = timezone.now()
        row.save()
        stored_path = self.root / row.payload_key
        acknowledgement = {"lease_id": str(row.lease_id), "status": "COMPLETED"}
        with self.scheduler([None, acknowledgement]) as requests:
            with self.assertRaises(SchedulerError):
                release_windows_assignment(row.pk)
            row.refresh_from_db()
            self.assertIsNone(row.lease_released_at)
            self.assertTrue(stored_path.is_file())
            release_windows_assignment(row.pk)
            release_windows_assignment(row.pk)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(
            requests[0]["path"], f"/api/windows-assignments/{row.lease_id}/release"
        )
        row.refresh_from_db()
        self.assertIsNotNone(row.lease_released_at)
        self.assertFalse(stored_path.exists())
        self.assertEqual(ExeSubmission.objects.count(), 1)

    def test_release_http_failure_codes_are_persisted_and_success_clears_them(self):
        row = self.accepted()
        row.lease_id = uuid.uuid4()
        row.status, row.attempt_state = "COMPLETED", "USED"
        row.started_at = row.finished_at = timezone.now()
        row.cleanup_complete = True
        row.cleanup_confirmed_at = timezone.now()
        row.save()
        path = self.root / row.payload_key
        errors = [(502, "BROKER_CALL_FAILED"), (404, "WINDOWS_ASSIGNMENT_NOT_FOUND")]
        replies = [
            (status, {"code": code, "message": "private-provider-detail"})
            for status, code in errors
        ]
        replies.append({"lease_id": str(row.lease_id), "status": "COMPLETED"})
        with self.scheduler(replies) as requests:
            for status, code in errors:
                with self.subTest(code=code), self.assertRaises(
                    SchedulerError
                ) as raised:
                    release_windows_assignment(row.pk)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.status_code, status)
                self.assertNotIn("private-provider-detail", raised.exception.message)
                row.refresh_from_db()
                self.assertEqual(row.last_error_code, code)
                self.assertIsNone(row.lease_released_at)
                self.assertEqual(row.status, "COMPLETED")
                self.assertTrue(path.is_file())
            release_windows_assignment(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.last_error_code, "")
        self.assertIsNotNone(row.lease_released_at)
        self.assertFalse(path.exists())
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(request == requests[0] for request in requests))
