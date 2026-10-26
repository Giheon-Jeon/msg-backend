import copy
import json
import os
import uuid
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role
from apps.accounts.models import Team, User
from apps.board.models import Cell, TeamChallengeAccess
from apps.challenge.checker_auth import checker_configuration, mint_job_token
from apps.challenge.checker_dispatch import checker_request, dispatch_submission
from apps.challenge.exe_submissions import ExeSubmissionError
from apps.challenge.models import ExeExecutionEvent, ExeSubmission, Solve
from apps.challenge.tests_exe_submissions import ExeFixtures, harmless_pe
from apps.challenge.windows_assignments import (
    release_windows_assignment,
    request_windows_assignment,
)
from apps.instances.services import SchedulerError
from apps.teams.models import MileageHistory


PINS = {
    "checker_commit_sha": "a" * 40,
    "vm_template_sha256": "b" * 64,
    "driver_sha256": "c" * 64,
    "config_version": "fixture-v1",
}


class RemoteRunTests(ExeFixtures, TestCase):
    def test_old_request_key_after_team_reassignment_cannot_create_a_second_execution(
        self,
    ):
        key = uuid.uuid4()
        first = self.post_exe(key)
        self.assertEqual(first.status_code, 202)
        other = Team.objects.create(team_name="new-owner-team")
        TeamChallengeAccess.objects.create(
            team=other,
            challenge=self.challenge,
            source_cell=Cell.objects.get(cell_index=1),
        )
        User.objects.filter(pk=self.user.pk).update(team=other)
        response = self.post_exe(key)
        self.assertEqual(
            (response.status_code, response.data["code"]), (409, "TEAM_CHANGED")
        )
        self.assertEqual(ExeSubmission.objects.count(), 1)
        self.assertEqual(len(list(self.root.rglob("payload.exe"))), 1)

    @override_settings(EXE_SUBMISSIONS_ENABLED=False, EXE_DISPATCH_ENABLED=False)
    def test_registered_worker_command_stays_disabled_by_default(self):
        output = io.StringIO()
        with patch(
            "apps.challenge.management.commands.dispatch_exe_submissions.assign_oldest_exe_submission"
        ) as called:
            call_command("dispatch_exe_submissions", stdout=output)
        called.assert_not_called()
        self.assertIn("disabled", output.getvalue())

    def test_public_responses_match_the_versioned_boundary(self):
        from pathlib import Path

        schemas = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "docs/windows-remote-run.openapi.json"
            ).read_text()
        )["components"]["schemas"]
        row = self.accepted()
        detail = self.client.get(f"{self.url}/{row.pk}").data["data"]
        self.assertEqual(set(detail), set(schemas["Submission"]["properties"]))
        listing = self.client.get(self.url).data["data"]
        self.assertEqual(set(listing), set(schemas["SubmissionList"]["properties"]))
        self.assertEqual(
            set(listing["remote_run"]), set(schemas["RemoteRunConfig"]["properties"])
        )

    def assigned(self):
        row = self.accepted()
        row.lease_id, row.worker_id = uuid.uuid4(), uuid.uuid4()
        row.status, row.vm_readiness = "ASSIGNED", "READY"
        row.lease_expires_at = timezone.now() + timedelta(minutes=5)
        row.readiness_checked_at = timezone.now()
        row.readiness_check_id, row.vm_id = "guest-proof-1", "fixture-vm"
        row.config_snapshot = {**row.config_snapshot, "execution_provenance": PINS}
        row.save()
        return row

    def checker(self, row):
        configured = override_settings(
            EXE_CHECKER_SIGNING_KEY="k" * 48,
            EXE_CHECKERS={
                str(row.worker_id): {
                    "base_url": "https://checker.example.invalid",
                    "service_token_env": "EXE_CHECKER_FIXTURE_TOKEN",
                    **PINS,
                }
            },
        )
        configured.enable()
        self.addCleanup(configured.disable)
        env = patch.dict(os.environ, {"EXE_CHECKER_FIXTURE_TOKEN": "s" * 48})
        env.start()
        self.addCleanup(env.stop)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + mint_job_token(row))
        return client

    def event(self, row, kind="RESULT", **values):
        body = {
            "request_version": 1,
            "event_id": str(uuid.uuid4()),
            "assignment_id": str(row.lease_id),
            "payload_sha256": row.payload_sha256,
            "event_type": kind,
            "observed_at": timezone.now().isoformat(),
            **PINS,
        }
        if kind == "STARTED":
            body.update(started=True, started_at=timezone.now().isoformat())
        elif kind == "RESULT":
            body.update(
                execution_status="COMPLETED",
                started=True,
                started_at=timezone.now().isoformat(),
                finished_at=timezone.now().isoformat(),
                cleanup_complete=False,
                exit_code=0,
                output={
                    "stdout": "participant output",
                    "stderr": "",
                    "encoding": "utf-8",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                },
            )
        else:
            body["cleanup_complete"] = True
        return {**body, **values}

    def callback(self, client, row, event):
        return client.post(
            f"/internal/v1/checker-submissions/{row.pk}/events", event, format="json"
        )

    def test_no_lifetime_cap_after_more_than_five_completed_jobs(self):
        for _ in range(7):
            row = self.accepted()
            ExeSubmission.objects.filter(pk=row.pk).update(
                status="COMPLETED",
                attempt_state="USED",
                finished_at=timezone.now(),
                cleanup_complete=True,
                created_at=timezone.now() - timedelta(minutes=2),
            )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["total_count"], 7)
        self.assertIsNone(response.data["data"]["max_attempts"])
        self.assertFalse(Solve.objects.exists())
        self.assertFalse(MileageHistory.objects.exists())

    def test_rate_limit_has_retry_after_and_settings_preserve_elapsed_time(self):
        row = self.accepted()
        response = self.post_exe()
        self.assertEqual((response.status_code, response["Retry-After"]), (429, "30"))
        ExeSubmission.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(seconds=20)
        )
        self.user.role = Role.ADMIN
        self.user.save(update_fields=["role"])
        response = self.client.patch(
            f"/api/v1/admin/challenges/{self.challenge.pk}/remote_run",
            {"rate_limit_seconds": 60},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        response = self.post_exe()
        self.assertEqual(response.status_code, 429)
        self.assertLessEqual(response.data["data"]["retry_after_seconds"], 40)
        self.assertEqual(ExeSubmission.objects.count(), 1)

    def test_new_key_waits_for_prior_cleanup_even_after_interval(self):
        row = self.accepted()
        ExeSubmission.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(minutes=2)
        )
        response = self.post_exe()
        self.assertEqual(
            (response.status_code, response.data["code"]),
            (409, "EXECUTION_IN_PROGRESS"),
        )

    def test_response_loss_can_be_recovered_by_key_after_reloads_and_disabled_acceptance(
        self,
    ):
        key = uuid.uuid4()
        first = self.post_exe(key)
        with override_settings(EXE_SUBMISSIONS_ENABLED=False):
            response = self.client.get(self.url, {"idempotency_key": str(key)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["data"]["runs"][0]["submission_id"],
            first.data["data"]["submission_id"],
        )
        self.assertEqual(ExeSubmission.objects.count(), 1)

    def test_admin_cannot_enable_unapproved_or_missing_runtime(self):
        self.user.role = Role.ADMIN
        self.user.save(update_fields=["role"])
        self.release.approved_at = None
        self.release.save(update_fields=["approved_at"])
        response = self.client.patch(
            f"/api/v1/admin/challenges/{self.challenge.pk}/remote_run",
            {"enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, 409)

    def test_admin_rejects_old_total_cap_and_out_of_range_limits(self):
        self.user.role = Role.ADMIN
        self.user.save(update_fields=["role"])
        for body in [
            {"max_attempts": 3},
            {"rate_limit_seconds": 0},
            {"rate_limit_seconds": True},
            {"max_file_bytes": 10485761},
            {"timeout_seconds": 181},
            {"accept": [".dll"]},
        ]:
            with self.subTest(body=body):
                response = self.client.patch(
                    f"/api/v1/admin/challenges/{self.challenge.pk}/remote_run",
                    body,
                    format="json",
                )
                self.assertEqual(response.status_code, 400)

    def test_unready_assignment_cannot_dispatch(self):
        row = self.assigned()
        row.vm_readiness = "UNKNOWN"
        row.save(update_fields=["vm_readiness"])
        with patch("apps.challenge.checker_dispatch.checker_request") as called:
            result = dispatch_submission(row.pk)
        called.assert_not_called()
        self.assertEqual(result.last_error_code, "WINDOWS_NOT_READY")

    def test_fake_or_stale_guest_readiness_is_rejected(self):
        row = self.accepted()
        for body in [
            {**self.assignment(row), "vm_readiness": "READY"},
            {
                **self.assignment(row),
                "vm_readiness": "READY",
                "vm_id": "vm",
                "readiness_check_id": "proof",
                "readiness_checked_at": (
                    timezone.now() - timedelta(minutes=1)
                ).isoformat(),
            },
        ]:
            with (
                patch(
                    "apps.challenge.windows_assignments._post_scheduler",
                    return_value=body,
                ),
                self.assertRaises(SchedulerError),
            ):
                request_windows_assignment(row)

    def test_checker_origin_and_pins_come_only_from_operator_settings(self):
        row = self.assigned()
        self.checker(row)
        self.assertEqual(checker_configuration(row.worker_id)[2], PINS)
        for origin in [
            "http://checker.example.invalid",
            "https://checker.example.invalid/path",
            "https://user@checker.example.invalid",
            "https://checker.example.invalid?token=x",
        ]:
            with (
                override_settings(
                    EXE_CHECKERS={
                        str(row.worker_id): {
                            "base_url": origin,
                            "service_token_env": "EXE_CHECKER_FIXTURE_TOKEN",
                            **PINS,
                        }
                    }
                ),
                self.assertRaises(ExeSubmissionError),
            ):
                checker_configuration(row.worker_id)

    def test_dispatch_response_loss_and_restart_only_query_same_job(self):
        row = self.assigned()
        self.checker(row)
        with patch(
            "apps.challenge.checker_dispatch.checker_request",
            side_effect=ExeSubmissionError("CHECKER_RESPONSE_UNKNOWN", "test", 503),
        ) as called:
            dispatch_submission(row.pk)
            dispatch_submission(row.pk)
        self.assertEqual(
            [call.args[1] for call in called.call_args_list], ["POST", "GET"]
        )
        row.refresh_from_db()
        self.assertEqual(row.dispatch_state, "UNKNOWN")
        self.assertIsNone(row.lease_released_at)
        self.assertEqual(ExeSubmission.objects.count(), 1)
        body = self.event(row)
        response = self.callback(self.checker(row), row, body)
        self.assertEqual(response.status_code, 200, response.data)

    def test_dispatch_crash_after_sending_never_posts_again(self):
        row = self.assigned()
        self.checker(row)
        row.dispatch_state, row.dispatch_started_at = (
            "SENDING",
            timezone.now() - timedelta(minutes=1),
        )
        row.save()
        with patch(
            "apps.challenge.checker_dispatch.checker_request",
            return_value={"checker_job_id": "existing"},
        ) as called:
            dispatch_submission(row.pk)
        self.assertEqual(called.call_args.args[1], "GET")

    def test_old_job_without_frozen_limits_cannot_be_dispatched(self):
        row = self.assigned()
        row.config_snapshot = {}
        row.save(update_fields=["config_snapshot"])
        with patch("apps.challenge.checker_dispatch.checker_request") as called:
            result = dispatch_submission(row.pk)
        called.assert_not_called()
        self.assertEqual(result.last_error_code, "WINDOWS_CONFIG_NOT_READY")

    def test_payload_delete_failure_retries_without_releasing_the_lease_twice(self):
        row = self.assigned()
        client = self.checker(row)
        self.assertEqual(
            self.callback(
                client, row, self.event(row, cleanup_complete=True)
            ).status_code,
            200,
        )
        key = row.payload_key
        with patch(
            "apps.challenge.windows_assignments._post_scheduler", return_value=None
        ) as called:
            with patch(
                "django.core.files.storage.FileSystemStorage.delete",
                side_effect=OSError("fixture"),
            ):
                release_windows_assignment(row.pk)
            self.assertTrue((self.root / key).exists())
            release_windows_assignment(row.pk)
        self.assertEqual(called.call_count, 1)
        self.assertFalse((self.root / key).exists())

    def test_checker_request_does_not_send_payload_secret_or_download_url(self):
        row = self.assigned()
        self.checker(row)
        data = {
            "code": "SUCCESS",
            "data": {
                "submission_id": str(row.pk),
                "assignment_id": str(row.lease_id),
                "checker_job_id": "job",
            },
        }
        opener = self.http_response(json.dumps(data).encode())
        with patch("apps.challenge.checker_dispatch.build_opener", return_value=opener):
            checker_request(row, "POST")
        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertNotIn("FLAG", request.data.decode())
        self.assertFalse(any("url" in key or "token" in key for key in payload))
        self.assertEqual(payload["submission_id"], str(row.pk))
        self.assertEqual(
            payload["timeout_seconds"], row.config_snapshot["timeout_seconds"]
        )
        self.assertEqual(payload["max_output_bytes"], 65536)
        self.assertEqual(request.get_header("Idempotency-key"), str(row.pk))

    def test_unauthorized_or_wrong_job_token_cannot_fetch_executable_or_secret(self):
        row = self.assigned()
        self.checker(row)
        for suffix in ["payload", "secret"]:
            url = f"/internal/v1/checker-submissions/{row.pk}/{suffix}"
            self.assertEqual(self.client.get(url).status_code, 401)
            self.assertEqual(APIClient().get(url).status_code, 401)
        client = self.checker(row)
        self.assertEqual(
            client.get(
                f"/internal/v1/checker-submissions/{uuid.uuid4()}/payload"
            ).status_code,
            401,
        )

    def test_scoped_token_fetches_exact_private_payload_and_pinned_secret(self):
        row = self.assigned()
        client = self.checker(row)
        response = client.get(f"/internal/v1/checker-submissions/{row.pk}/payload")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), harmless_pe())
        self.assertIn("no-store", response["Cache-Control"])
        response = client.get(f"/internal/v1/checker-submissions/{row.pk}/secret")
        self.assertEqual(response.data["data"]["env"], {"FLAG": "local-exe-test"})
        self.assertIn("no-store", response["Cache-Control"])

    def test_modified_payload_and_expired_or_cleaned_lease_are_not_served(self):
        row = self.assigned()
        client = self.checker(row)
        (self.root / row.payload_key).write_bytes(b"modified")
        self.assertEqual(
            client.get(
                f"/internal/v1/checker-submissions/{row.pk}/payload"
            ).status_code,
            503,
        )
        row.cleanup_complete = True
        row.save()
        self.assertEqual(
            client.get(f"/internal/v1/checker-submissions/{row.pk}/secret").status_code,
            404,
        )

    def test_result_replay_and_conflicting_event_are_atomic_and_never_grade(self):
        row = self.assigned()
        client = self.checker(row)
        body = self.event(row, exit_code=3221225477)
        for duplicate in [False, True]:
            response = self.callback(client, row, body)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["data"]["duplicate"], duplicate)
        changed = copy.deepcopy(body)
        changed["output"]["stdout"] = "changed"
        self.assertEqual(self.callback(client, row, changed).status_code, 409)
        row.refresh_from_db()
        self.assertEqual(row.stdout, "participant output")
        self.assertEqual(row.exit_code, 3221225477)
        self.assertEqual(ExeExecutionEvent.objects.count(), 1)
        self.assertFalse(Solve.objects.exists())
        self.assertFalse(MileageHistory.objects.exists())

    def test_missing_fields_unapproved_provenance_and_grading_fields_are_rejected(self):
        row = self.assigned()
        client = self.checker(row)
        for field in ["started", "finished_at", "output", "exit_code"]:
            body = self.event(row)
            del body[field]
            self.assertEqual(self.callback(client, row, body).status_code, 400)
        for values, status in [
            ({"driver_sha256": "d" * 64}, 409),
            ({"verdict": "CORRECT"}, 400),
            ({"driverSha256": "d" * 64}, 400),
            ({"event_type": []}, 400),
            ({"payload_sha256": "d" * 64}, 409),
            ({"assignment_id": str(uuid.uuid4())}, 409),
        ]:
            self.assertEqual(
                self.callback(client, row, self.event(row, **values)).status_code,
                status,
            )
        self.assertFalse(ExeExecutionEvent.objects.exists())

    def test_outputs_are_normalized_before_postgres_and_empty_differs_from_missing(
        self,
    ):
        row = self.assigned()
        client = self.checker(row)
        body = self.event(row)
        body["output"]["stdout"] = "<script>alert(1)</script>\x00\x1b\n한글"
        self.assertEqual(self.callback(client, row, body).status_code, 200)
        row.refresh_from_db()
        self.assertNotIn("\x00", row.stdout)
        self.assertIn("한글", row.stdout)
        self.assertEqual(row.stderr, "")

    def test_oversized_unicode_output_and_event_body_do_not_change_db(self):
        row = self.assigned()
        client = self.checker(row)
        body = self.event(row)
        body["output"]["stdout"] = "한" * 22000
        self.assertEqual(self.callback(client, row, body).status_code, 400)
        response = client.post(
            f"/internal/v1/checker-submissions/{row.pk}/events",
            b"x" * (1024 * 1024 + 1),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)
        self.assertFalse(ExeExecutionEvent.objects.exists())

    def test_release_requires_cleanup_proof_and_release_retry_is_idempotent(self):
        row = self.assigned()
        client = self.checker(row)
        self.assertEqual(self.callback(client, row, self.event(row)).status_code, 200)
        with (
            patch("apps.challenge.windows_assignments._post_scheduler") as called,
            self.assertRaises(ExeSubmissionError),
        ):
            release_windows_assignment(row.pk)
        called.assert_not_called()
        self.assertEqual(
            self.callback(client, row, self.event(row, "CLEANUP")).status_code, 200
        )
        with patch(
            "apps.challenge.windows_assignments._post_scheduler", return_value=None
        ) as called:
            release_windows_assignment(row.pk)
            release_windows_assignment(row.pk)
        self.assertEqual(called.call_count, 1)
        row.refresh_from_db()
        self.assertTrue(row.cleanup_complete)
        self.assertIsNotNone(row.lease_released_at)
        self.assertEqual(row.payload_key, "")
        self.assertFalse(list(self.root.rglob("payload.exe")))

    def test_cleanup_before_result_preserves_proof_and_late_start_cannot_restart(self):
        row = self.assigned()
        client = self.checker(row)
        self.assertEqual(
            self.callback(client, row, self.event(row, "CLEANUP")).status_code, 200
        )
        self.assertEqual(
            self.callback(client, row, self.event(row, "STARTED")).status_code, 409
        )
        self.assertEqual(self.callback(client, row, self.event(row)).status_code, 200)
        row.refresh_from_db()
        self.assertTrue(row.cleanup_complete)

    def test_started_is_only_recorded_once_and_result_cannot_contradict_it(self):
        row = self.assigned()
        client = self.checker(row)
        start = self.event(row, "STARTED")
        self.assertEqual(self.callback(client, row, start).status_code, 200)
        self.assertEqual(self.callback(client, row, start).status_code, 200)
        result = self.event(
            row, execution_status="INFRA_ERROR", started=False, started_at=None
        )
        self.assertEqual(self.callback(client, row, result).status_code, 409)
        row.refresh_from_db()
        self.assertEqual(row.status, "RUNNING")
        self.assertEqual(row.attempt_state, "USED")

    def test_admin_active_filter_keeps_finished_jobs_until_cleanup_and_release(self):
        row = self.assigned()
        client = self.checker(row)
        self.assertEqual(self.callback(client, row, self.event(row)).status_code, 200)
        self.user.role = Role.ADMIN
        self.user.save(update_fields=["role"])
        response = self.client.get(
            f"/api/v1/admin/challenges/{self.challenge.pk}/remote_runs",
            {"status": "ACTIVE"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["total_count"], 1)
        self.assertEqual(response.data["data"]["runs"][0]["status"], "CLEANUP_PENDING")


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking")
class ConcurrentRemoteDispatchTests(ExeFixtures, TransactionTestCase):
    assigned = RemoteRunTests.assigned
    checker = RemoteRunTests.checker

    def test_two_workers_send_one_create_request(self):
        row = self.assigned()
        self.checker(row)
        started, finish = Event(), Event()
        calls = []

        def checker_request(submission, method):
            calls.append(method)
            started.set()
            if not finish.wait(10):
                raise RuntimeError("Fixture worker timed out")
            return {"checker_job_id": "single-job"}

        def execute():
            close_old_connections()
            try:
                return dispatch_submission(row.pk).pk
            finally:
                connections.close_all()

        with patch(
            "apps.challenge.checker_dispatch.checker_request",
            side_effect=checker_request,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(execute)
                try:
                    self.assertTrue(started.wait(10))
                    self.assertEqual(pool.submit(execute).result(timeout=10), row.pk)
                finally:
                    finish.set()
                self.assertEqual(first.result(timeout=10), row.pk)
        self.assertEqual(calls, ["POST"])
