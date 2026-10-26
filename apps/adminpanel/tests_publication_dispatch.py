import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest import skipUnless
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from django.db import (
    DatabaseError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.adminpanel.tests_publication_requests import (
    BASE,
    SHA,
    PublicationRequestFixtures,
)
from apps.instances import publication_dispatch as dispatch
from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    PublicationRequest,
)


class PublicationDispatchTests(PublicationRequestFixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        configured = override_settings(
            PUBLICATION_DISPATCH_ENABLED=True,
            PUBLICATION_DISPATCH_GITHUB_TOKEN="fixture-publication-token",
        )
        configured.enable()
        self.addCleanup(configured.disable)
        self.calls, self.posts, self.runs = [], [], {}
        self.main_sha = SHA
        self.pages = None
        mocked = patch(
            "apps.instances.publication_dispatch.github_json", side_effect=self.github
        )
        self.github_mock = mocked.start()
        self.addCleanup(mocked.stop)

    def run_data(self, row, run_id, **changes):
        return {
            "id": run_id,
            "run_attempt": 1,
            "run_number": 50,
            "repository": {"full_name": dispatch.REPOSITORY},
            "head_repository": {"full_name": dispatch.REPOSITORY},
            "path": ".github/workflows/challenge-validation.yml",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": SHA,
            "display_title": f"문제 요청 {row.challenge_slug} / {row.pk}",
            "status": "queued",
            "conclusion": None,
            "created_at": (row.dispatch_started_at or timezone.now()).isoformat(),
            **changes,
        }

    def github(self, path, body=None):
        self.assertFalse(
            connection.in_atomic_block,
            "External calls require a committed dispatch marker",
        )
        self.calls.append((path, body))
        if path.endswith("/git/ref/heads/main"):
            return {
                "ref": "refs/heads/main",
                "object": {"type": "commit", "sha": self.main_sha},
            }
        if path.endswith("/dispatches"):
            row = PublicationRequest.objects.get(pk=body["inputs"]["request_id"])
            self.assertEqual(row.dispatch_status, "DISPATCH_UNKNOWN")
            self.assertIsNotNone(row.dispatch_started_at)
            self.posts.append(body)
            run_id = 1000 + len(self.posts)
            self.runs[run_id] = self.run_data(row, run_id)
            return {
                "workflow_run_id": run_id,
                "html_url": "https://untrusted.invalid/ignored",
            }
        if "/runs?" in path:
            if self.pages is not None:
                from urllib.parse import parse_qs, urlsplit

                return self.pages[int(parse_qs(urlsplit(path).query)["page"][0])]
            return {
                "total_count": len(self.runs),
                "workflow_runs": list(self.runs.values()),
            }
        return self.runs[int(path.rsplit("/", 1)[1])]

    def action(self, row, name="dispatch", client=None):
        return (client or self.client).post(
            f"{BASE}/{row.pk}/{name}", {}, format="json"
        )

    def dispatched(self, **changes):
        row = self.recorded(key=str(uuid.uuid4()))
        if changes:
            PublicationRequest.objects.filter(pk=row.pk).update(**changes)
        response = self.action(row)
        self.assertEqual(response.status_code, 200, response.data)
        row.refresh_from_db()
        return row

    def test_dispatch_is_explicit_and_receipt_precedes_one_fixed_request(self):
        row = self.recorded()
        self.assertEqual(self.calls, [])
        first, replay = self.action(row), self.action(row)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(first.data["data"]["run_id"], replay.data["data"]["run_id"])
        self.assertEqual(
            self.posts[0],
            {
                "ref": "main",
                "return_run_details": True,
                "inputs": {
                    "challenge_path": self.challenge.challenge_slug,
                    "expected_source_sha": SHA,
                    "request_id": str(row.pk),
                    "source_ref": "",
                    "publish_images": True,
                    "runtime_smoke": False,
                },
            },
        )
        self.assertFalse(first.data["data"]["sha_verified"])
        self.assertFalse(first.data["data"]["run_sha_verified"])
        self.assertNotIn("untrusted.invalid", str(first.data))
        self.assertIn("no-store", first["Cache-Control"])
        self.assertFalse(ChallengeRelease.objects.exists())
        self.assertFalse(ChallengeRuntimeConfig.objects.exists())

    def test_disabled_or_missing_token_never_attempts_external_calls(self):
        row = self.recorded()
        for values, code in (
            ({"PUBLICATION_DISPATCH_ENABLED": False}, "PUBLICATION_DISPATCH_DISABLED"),
            ({"PUBLICATION_DISPATCH_GITHUB_TOKEN": ""}, "PUBLICATION_TOKEN_MISSING"),
        ):
            with self.subTest(code=code), override_settings(**values):
                response = self.action(row)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.data["code"], code)
        self.assertEqual(self.calls, [])
        row.refresh_from_db()
        self.assertEqual(row.dispatch_status, "NOT_DISPATCHED")

    def test_action_permission_and_body_validation_precede_external_calls(self):
        row = self.recorded()
        for name in ("dispatch", "sync"):
            url = f"{BASE}/{row.pk}/{name}"
            for user, status in ((self.player, 403), (None, 401)):
                self.client.force_authenticate(user)
                self.assertEqual(self.action(row, name).status_code, status)
            self.client.force_authenticate(self.admin)
            for body in ({"sha": SHA}, [], {"repository": "other/repo"}):
                self.assertEqual(
                    self.client.post(url, body, format="json").status_code, 400
                )
            self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(
            self.action(type("Missing", (), {"pk": uuid.uuid4()})()).status_code, 404
        )
        self.assertEqual(self.calls, [])

    def test_null_sha_wrong_origin_separate_problem_and_deleted_challenge_do_not_dispatch(
        self,
    ):
        row = self.recorded()
        for values in (
            {"requested_sha": None},
            {"repository": "other/repo"},
            {"ref": "feature"},
            {"workflow": "other.yml"},
            {"publish_images": False},
            {"challenge_slug": "pwn-msgdriver"},
        ):
            PublicationRequest.objects.filter(pk=row.pk).update(
                repository=dispatch.REPOSITORY,
                ref="main",
                workflow=dispatch.WORKFLOW,
                requested_sha=SHA,
                publish_images=True,
                challenge_slug=self.challenge.challenge_slug,
            )
            PublicationRequest.objects.filter(pk=row.pk).update(**values)
            with self.subTest(values=values):
                self.assertIn(self.action(row).status_code, (400, 409))
        PublicationRequest.objects.filter(pk=row.pk).update(
            challenge_slug=self.challenge.challenge_slug
        )
        self.challenge.delete()
        self.assertEqual(self.action(row).status_code, 404)
        self.assertEqual(self.posts, [])

    def test_main_mismatch_is_rejected_without_silently_changing_requested_sha(self):
        row = self.recorded()
        self.main_sha = "b" * 40
        self.assertEqual(
            self.action(row).data["data"]["last_error_code"], "SOURCE_SHA_MISMATCH"
        )
        self.action(row)
        row.refresh_from_db()
        self.assertEqual(row.requested_sha, SHA)
        self.assertEqual(row.dispatch_status, "DISPATCH_REJECTED")
        self.assertEqual(self.posts, [])

    def test_invalid_or_unreadable_main_stays_unsent_and_can_be_retried(self):
        row = self.recorded()
        with patch(
            "apps.instances.publication_dispatch.github_json",
            return_value={"object": []},
        ):
            self.assertEqual(self.action(row).status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.dispatch_status, "NOT_DISPATCHED")
        self.assertEqual(self.action(row).data["data"]["dispatch_status"], "DISPATCHED")
        self.assertEqual(len(self.posts), 1)

    def test_timeout_after_acceptance_is_recovered_by_exact_title_without_redispatch(
        self,
    ):
        row = self.recorded()

        def lose_reply(path, body=None):
            result = self.github(path, body)
            if body:
                raise dispatch.PublicationDispatchError(
                    "GITHUB_RESPONSE_UNKNOWN", "Lost reply"
                )
            return result

        with patch(
            "apps.instances.publication_dispatch.github_json", side_effect=lose_reply
        ):
            self.assertEqual(
                self.action(row).data["data"]["dispatch_status"], "DISPATCH_UNKNOWN"
            )
        self.action(row)
        recovered = self.action(row, "sync").data["data"]
        self.assertEqual(recovered["run_id"], 1001)
        self.assertTrue(recovered["run_sha_verified"])
        self.assertFalse(recovered["sha_verified"])
        self.assertEqual(len(self.posts), 1)

    def test_empty_recovery_does_not_prove_failure_or_authorize_another_post(self):
        row = self.recorded()
        PublicationRequest.objects.filter(pk=row.pk).update(
            dispatch_status="DISPATCH_UNKNOWN", dispatch_started_at=timezone.now()
        )
        for _ in range(2):
            self.assertEqual(
                self.action(row, "sync").data["data"]["last_error_code"],
                "DISPATCH_UNCONFIRMED",
            )
            self.action(row)
        self.assertEqual(self.posts, [])

    def test_multiple_exact_candidates_are_conflict_not_latest_selection(self):
        row = self.recorded()
        PublicationRequest.objects.filter(pk=row.pk).update(
            dispatch_status="DISPATCH_UNKNOWN", dispatch_started_at=timezone.now()
        )
        self.runs = {value: self.run_data(row, value) for value in (1001, 1002)}
        response = self.action(row, "sync").data["data"]
        self.assertEqual(response["dispatch_status"], "DISPATCH_CONFLICT")
        self.assertIsNone(response["run_id"])
        self.action(row)
        self.assertEqual(self.posts, [])

    def test_recovery_exhausts_pages_ignores_substring_titles_and_rejects_incomplete_search(
        self,
    ):
        row = self.recorded()
        PublicationRequest.objects.filter(pk=row.pk).update(
            dispatch_status="DISPATCH_UNKNOWN", dispatch_started_at=timezone.now()
        )
        decoy = self.run_data(
            row, 1002, display_title=f"문제 요청 {row.challenge_slug} / {row.pk} extra"
        )
        self.pages = {
            1: {"total_count": 2, "workflow_runs": [decoy]},
            2: {"total_count": 2, "workflow_runs": [self.run_data(row, 1001)]},
        }
        self.assertEqual(self.action(row, "sync").data["data"]["run_id"], 1001)
        self.assertEqual(len([call for call in self.calls if "/runs?" in call[0]]), 2)
        other = self.recorded(key="incomplete")
        PublicationRequest.objects.filter(pk=other.pk).update(
            dispatch_status="DISPATCH_UNKNOWN", dispatch_started_at=timezone.now()
        )
        self.pages = {1: {"total_count": 1001, "workflow_runs": []}}
        self.assertEqual(
            self.action(other, "sync").data["data"]["last_error_code"],
            "PUBLICATION_RECOVERY_INCOMPLETE",
        )

    def test_same_run_rerun_updates_attempt_but_keeps_revision_and_never_marks_bundle_verified(
        self,
    ):
        row = self.dispatched()
        self.runs[row.run_id].update(status="completed", conclusion="success")
        first = self.action(row, "sync").data["data"]
        self.runs[row.run_id].update(
            status="in_progress", conclusion=None, run_attempt=2
        )
        second = self.action(row, "sync").data["data"]
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["run_number"], second["run_number"])
        self.assertEqual(second["run_attempt"], 2)
        self.assertFalse(second["sha_verified"])
        self.runs[row.run_id]["run_attempt"] = 1
        self.assertEqual(self.action(row, "sync").data["data"]["run_attempt"], 2)
        self.assertFalse(ChallengeRelease.objects.exists())

    def test_same_run_cannot_change_revision_or_regress_completed_status(self):
        row = self.dispatched()
        self.runs[row.run_id].update(status="completed", conclusion="success")
        self.action(row, "sync")
        self.runs[row.run_id].update(status="queued", conclusion=None)
        self.assertEqual(
            self.action(row, "sync").data["data"]["workflow_status"], "completed"
        )
        self.runs[row.run_id].update(run_attempt=2, run_number=51)
        data = self.action(row, "sync").data["data"]
        self.assertEqual(data["dispatch_status"], "DISPATCH_CONFLICT")
        self.assertEqual(data["run_number"], 50)

    def test_run_must_match_origin_sha_workflow_title_and_typed_identifiers(self):
        row = self.dispatched()
        original = self.runs[row.run_id]
        for changes in (
            {"head_sha": "b" * 40},
            {"event": "push"},
            {"head_branch": "feature"},
            {"path": ".github/workflows/other.yml"},
            {"repository": {"full_name": "other/repo"}},
            {"display_title": "another request"},
            {"id": True},
            {"run_attempt": 0},
            {"run_number": 0},
            {"status": []},
        ):
            self.runs[row.run_id] = {**original, **changes}
            with self.subTest(changes=changes):
                data = self.action(row, "sync").data["data"]
                self.assertEqual(data["last_error_code"], "PUBLICATION_RUN_MISMATCH")
                self.assertFalse(data["run_sha_verified"])

    def test_read_failures_are_not_workflow_failure_and_sync_works_when_dispatch_disabled(
        self,
    ):
        row = self.dispatched()
        self.action(row, "sync")
        with override_settings(PUBLICATION_DISPATCH_ENABLED=False), patch(
            "apps.instances.publication_dispatch.github_json",
            side_effect=dispatch.PublicationDispatchError(
                "GITHUB_RESPONSE_UNKNOWN", "read failed"
            ),
        ):
            data = self.action(row, "sync").data["data"]
        self.assertEqual(data["workflow_status"], "queued")
        self.assertIsNone(data["workflow_conclusion"])
        self.assertEqual(data["last_error_code"], "GITHUB_RESPONSE_UNKNOWN")
        before = len(self.calls)
        self.client.get(BASE)
        self.client.get(f"{BASE}/{row.pk}")
        self.assertEqual(len(self.calls), before)

    def test_duplicate_intents_reuse_original_request_even_if_main_advanced(self):
        first = self.dispatched()
        self.main_sha = "b" * 40
        second = self.recorded(key="new-click")
        data = self.action(second).data["data"]
        self.assertEqual(data["reused_request_id"], str(first.pk))
        self.assertEqual(data["dispatch_request_id"], str(first.pk))
        self.assertEqual(len(self.posts), 1)
        self.action(second, "sync")
        data = self.client.get(f"{BASE}/{second.pk}").data["data"]
        self.assertTrue(data["run_sha_verified"])
        listing = self.client.get(BASE, {"dispatch_status": "DISPATCHED"})
        self.assertEqual(listing.data["data"]["total_count"], 2)

    def test_dispatch_inside_outer_transaction_is_rejected_before_network(self):
        row = self.recorded()
        with transaction.atomic(), self.assertRaises(dispatch.PublicationDispatchError):
            dispatch.dispatch_publication(row.pk)
        self.assertEqual(self.calls, [])

    def test_claim_save_failure_prevents_post_and_does_not_consume_uuid(self):
        row = self.recorded()
        original = PublicationRequest.save

        def fail_claim(instance, *args, **kwargs):
            if "dispatch_started_at" in kwargs.get("update_fields", []):
                raise DatabaseError("Claim failed")
            return original(instance, *args, **kwargs)

        with patch.object(PublicationRequest, "save", fail_claim), patch(
            "apps.common.exceptions.logger.exception"
        ):
            self.assertEqual(self.action(row).status_code, 500)
        self.assertEqual(self.posts, [])
        row.refresh_from_db()
        self.assertEqual(row.dispatch_status, "NOT_DISPATCHED")
        self.assertEqual(self.action(row).data["data"]["dispatch_status"], "DISPATCHED")

    def test_different_smoke_options_dispatch_separately_and_new_request_after_failure_is_explicit(
        self,
    ):
        first = self.dispatched()
        second = self.recorded(key="smoke")
        PublicationRequest.objects.filter(pk=second.pk).update(runtime_smoke=True)
        self.action(second)
        self.assertEqual(len(self.posts), 2)
        self.runs[first.run_id].update(status="completed", conclusion="failure")
        self.action(first, "sync")
        self.action(first)
        self.assertEqual(len(self.posts), 2)
        self.dispatched()
        self.assertEqual(len(self.posts), 3)

    def test_ack_database_failure_keeps_durable_unknown_and_recovers(self):
        row = self.recorded()
        original = PublicationRequest.save

        def fail_ack(instance, *args, **kwargs):
            if "run_id" in kwargs.get("update_fields", []):
                raise DatabaseError("Acknowledgement storage failed")
            return original(instance, *args, **kwargs)

        with patch.object(PublicationRequest, "save", fail_ack), patch(
            "apps.common.exceptions.logger.exception"
        ):
            self.assertEqual(self.action(row).status_code, 500)
        row.refresh_from_db()
        self.assertEqual(row.dispatch_status, "DISPATCH_UNKNOWN")
        self.assertIsNone(row.run_id)
        self.action(row)
        self.assertEqual(self.action(row, "sync").data["data"]["run_id"], 1001)
        self.assertEqual(len(self.posts), 1)

    def test_rejected_post_never_retries_same_uuid(self):
        row = self.recorded()

        def reject(path, body=None):
            if body:
                raise dispatch.PublicationDispatchError(
                    "GITHUB_DISPATCH_REJECTED", "Rejected"
                )
            return self.github(path, body)

        with patch(
            "apps.instances.publication_dispatch.github_json", side_effect=reject
        ) as called:
            self.assertEqual(
                self.action(row).data["data"]["dispatch_status"], "DISPATCH_REJECTED"
            )
            self.action(row)
            self.action(row, "sync")
            self.assertEqual(called.call_count, 2)

    @skipUnless(connection.vendor == "postgresql", "Requires committed row locks")
    def test_parallel_dispatch_and_duplicate_intent_send_one_post(self):
        first = self.recorded()
        second = self.recorded(key="parallel")
        sending, finish = Event(), Event()

        def hold_reply(path, body=None):
            result = self.github(path, body)
            if body:
                sending.set()
                if not finish.wait(10):
                    raise AssertionError("Dispatch test was not released")
            return result

        def run(row):
            close_old_connections()
            try:
                return dispatch.dispatch_publication(row.pk).pk
            finally:
                connections.close_all()

        with patch(
            "apps.instances.publication_dispatch.github_json", side_effect=hold_reply
        ), ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(run, first)
            try:
                self.assertTrue(sending.wait(10))
                self.assertEqual(pool.submit(run, first).result(timeout=5), first.pk)
                self.assertEqual(pool.submit(run, second).result(timeout=5), second.pk)
            finally:
                finish.set()
            one.result(timeout=10)
        self.assertEqual(len(self.posts), 1)
        second.refresh_from_db()
        self.assertEqual(second.reused_request_id, first.pk)


class PublicationHTTPTests(TransactionTestCase):
    @override_settings(
        PUBLICATION_DISPATCH_GITHUB_TOKEN="fixture-write-token",
        RELEASE_POLL_GITHUB_TOKEN="fixture-read-token",
        RELEASE_POLL_API_BASE="https://untrusted.invalid",
    )
    def test_fixed_host_dedicated_token_no_redirect_and_bounded_reply(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{"ok":true}'
        with patch(
            "apps.instances.publication_dispatch.build_opener", return_value=opener
        ):
            self.assertEqual(
                dispatch.github_json(dispatch.REPO_PATH + "/test"), {"ok": True}
            )
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://api.github.com/repos/MSG-CTF/2026_MSG_CTF/test"
        )
        self.assertEqual(
            request.get_header("Authorization"), "Bearer fixture-write-token"
        )
        self.assertEqual(
            response.read.call_args.args[0], dispatch.MAX_RESPONSE_BYTES + 1
        )
        self.assertIsNone(
            dispatch._NoRedirect().redirect_request(
                None, None, 302, None, None, "https://untrusted.invalid"
            )
        )

    @override_settings(PUBLICATION_DISPATCH_GITHUB_TOKEN="fixture-write-token")
    def test_http_rejection_uncertainty_and_malformed_replies_are_safe(self):
        opener = MagicMock()
        for error, expected in (
            (
                HTTPError("https://api.github.com", 403, "denied", {}, io.BytesIO()),
                "GITHUB_DISPATCH_REJECTED",
            ),
            (
                HTTPError("https://api.github.com", 502, "failed", {}, io.BytesIO()),
                "GITHUB_RESPONSE_UNKNOWN",
            ),
            (URLError("secret-bearing upstream detail"), "GITHUB_RESPONSE_UNKNOWN"),
        ):
            opener.open.side_effect = error
            with self.subTest(expected=expected), patch(
                "apps.instances.publication_dispatch.build_opener", return_value=opener
            ), self.assertRaises(dispatch.PublicationDispatchError) as caught:
                dispatch.github_json(
                    dispatch.WORKFLOW_PATH + "/dispatches", {"ref": "main"}
                )
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("secret-bearing", str(caught.exception))
        opener.open.side_effect = None
        response = opener.open.return_value.__enter__.return_value
        for status, raw in (
            (204, b""),
            (200, b"invalid"),
            (200, b"[]"),
            (200, b"x" * (dispatch.MAX_RESPONSE_BYTES + 1)),
        ):
            response.status, response.read.return_value = status, raw
            with self.subTest(status=status), patch(
                "apps.instances.publication_dispatch.build_opener", return_value=opener
            ), self.assertRaises(dispatch.PublicationDispatchError):
                dispatch.github_json(
                    dispatch.WORKFLOW_PATH + "/dispatches", {"ref": "main"}
                )
