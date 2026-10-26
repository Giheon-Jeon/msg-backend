import copy
import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.client import IncompleteRead
from threading import Event
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connections, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.challenge.models import Challenge
from apps.instances.image_preparations import (
    PreparationError,
    _call_runtime,
    _validate_result,
    configuration,
    image_set_id,
    process_preparation,
    queue_preparation,
    queue_retry,
    schedule_refresh,
    serialize_preparation,
)
from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    ImagePreparationRequest,
    ReleaseContainer,
)
from apps.instances.tests_releases import DIGEST_A, DIGEST_B, ReleaseTestBase

CONFIG = {
    "SECURE_SSL_REDIRECT": False,
    "CACHES": {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    "IMAGE_PREPARATION_ENABLED": True,
    "IMAGE_PREPARATION_RUNTIME_URL": "https://runtime.example",
    "IMAGE_PREPARATION_API_TOKEN": "unit-test-preparation-token",
    "IMAGE_PREPARATION_TARGET_IDS": "target-b,target-a",
    "IMAGE_PREPARATION_TIMEOUT_SECONDS": 5,
    "IMAGE_PREPARATION_POLL_SECONDS": 15,
    "IMAGE_PREPARATION_MAX_ATTEMPTS": 3,
}


def accepted(row, status="QUEUED", operation_id="prep-001"):
    data = {
        "operation_id": operation_id,
        "image_set_id": row.expected_image_set_id,
        "status": status,
    }
    if row.parent_id:
        data["parent_operation_id"] = row.parent.operation_id
    return data


def detail(row, status="SUCCEEDED", states=("READY",)):
    data = {
        **accepted(row, status, row.operation_id or "prep-001"),
        "request_id": str(row.pk),
        "release_id": str(row.release_id),
        "architecture": row.spec["architecture"],
        "nodes": [],
    }
    for index, state in enumerate(states):
        images = [
            {
                "image": image,
                "status": state,
                "attempts": 1,
                "observed_at": timezone.now().isoformat(),
                **(
                    {"resolved_manifest_digest": "sha256:" + DIGEST_A}
                    if state == "READY"
                    else {}
                ),
            }
            for image in sorted({item["image"] for item in row.spec["containers"]})
        ]
        data["nodes"].append(
            {
                "target_id": "target-a",
                "node_uid": f"node-{index}",
                "node_name": f"worker-{index}",
                "architecture": row.spec["architecture"],
                "status": state,
                "observed_at": timezone.now().isoformat(),
                "images": images,
            }
        )
    return data


@override_settings(**CONFIG)
class PreparationApiTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.network = patch(
            "apps.instances.image_preparations._call_runtime",
            side_effect=AssertionError("HTTP in API forbidden"),
        )
        self.network.start()
        self.addCleanup(self.network.stop)
        self.auth("root")
        self.release_id = self.register().data["data"]["release_id"]
        self.url = f"{self.base_url}/{self.release_id}/image_preparations"

    def post(self, url=None, body=None, key="test-key"):
        return self.client.post(
            url or self.url, body or {}, format="json", HTTP_IDEMPOTENCY_KEY=key
        )

    def test_unapproved_release_cannot_be_queued(self):
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "RELEASE_NOT_APPROVED")
        self.assertFalse(ImagePreparationRequest.objects.exists())

    def test_activation_queues_only_one_frozen_request_without_http(self):
        self.assertEqual(self.activate(self.release_id).status_code, 200)
        self.assertEqual(self.activate(self.release_id).status_code, 200)
        row = ImagePreparationRequest.objects.get()
        self.assertEqual(row.spec["target_ids"], ["target-a", "target-b"])
        self.assertEqual(row.payload, {"request_id": str(row.pk), **row.spec})
        self.assertEqual(row.spec["containers"][0]["ports"], [8080])
        self.assertEqual(
            ChallengeRuntimeConfig.objects.get().current_release_id, row.release_id
        )
        self.assertFalse(row.operation_id)

    def test_disabled_activation_and_bad_optional_configuration_do_not_gate_release(
        self,
    ):
        for config in (
            {"IMAGE_PREPARATION_ENABLED": False},
            {"IMAGE_PREPARATION_TARGET_IDS": ""},
        ):
            with self.subTest(config=config), override_settings(**config):
                self.assertEqual(self.activate(self.release_id).status_code, 200)
        self.assertFalse(ImagePreparationRequest.objects.exists())

    def test_optional_outbox_database_failure_does_not_rollback_activation(self):
        with patch(
            "apps.instances.image_preparations.queue_preparation",
            side_effect=DatabaseError("test"),
        ):
            self.assertEqual(self.activate(self.release_id).status_code, 200)
        self.assertEqual(
            str(ChallengeRuntimeConfig.objects.get().current_release_id),
            self.release_id,
        )

    def test_outer_rollback_removes_outbox_and_activation(self):
        with transaction.atomic():
            self.assertEqual(self.activate(self.release_id).status_code, 200)
            transaction.set_rollback(True)
        self.assertFalse(ImagePreparationRequest.objects.exists())
        self.assertFalse(ChallengeRuntimeConfig.objects.exists())

    def test_idempotency_and_explicit_new_root(self):
        self.activate(self.release_id)
        first = self.post()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, self.post().data)
        second = self.post(key="another-key")
        self.assertNotEqual(
            first.data["data"]["request_id"], second.data["data"]["request_id"]
        )
        self.assertEqual(ImagePreparationRequest.objects.count(), 3)

    def test_history_remains_readable_when_disabled(self):
        self.activate(self.release_id)
        row = ImagePreparationRequest.objects.get()
        with override_settings(IMAGE_PREPARATION_ENABLED=False):
            self.assertEqual(self.client.get(self.url).status_code, 200)
            self.assertEqual(self.client.get(self.detail_url(row)).status_code, 200)
            self.assertEqual(self.post().status_code, 503)

    def detail_url(self, row):
        return (
            f"/api/v1/admin/challenges/{self.challenge.pk}/image_preparations/{row.pk}"
        )

    def test_admin_only_all_routes(self):
        self.activate(self.release_id)
        row = ImagePreparationRequest.objects.get()
        self.auth("player")
        for response in (
            self.client.get(self.url),
            self.post(),
            self.client.get(self.detail_url(row)),
            self.post(self.detail_url(row) + "/retry"),
            self.post(self.detail_url(row) + "/refresh"),
        ):
            self.assertEqual(response.status_code, 403)

    def test_unknown_body_fields_and_invalid_pages_rejected(self):
        for body in (
            {"target_ids": ["attacker"]},
            {"image": "latest"},
            {"token": "do-not-store"},
        ):
            self.assertEqual(self.post(body=body).status_code, 400)
        for page in ("0", "10001", "bad"):
            self.assertEqual(self.client.get(self.url, {"page": page}).status_code, 400)

    def test_other_challenge_cannot_read_retry_or_refresh_request(self):
        self.activate(self.release_id)
        row = ImagePreparationRequest.objects.get()
        url = self.detail_url(row).replace(str(self.challenge.pk), str(uuid.uuid4()))
        for response in (
            self.client.get(url),
            self.post(url + "/retry"),
            self.post(url + "/refresh"),
        ):
            self.assertEqual(response.status_code, 404)

    def test_refresh_resumes_same_request_not_new_operation(self):
        self.activate(self.release_id)
        row = ImagePreparationRequest.objects.get()
        ImagePreparationRequest.objects.filter(pk=row.pk).update(
            delivery_status="UNKNOWN", next_attempt_at=None, consecutive_failures=3
        )
        response = self.post(self.detail_url(row) + "/refresh")
        self.assertEqual(response.status_code, 202)
        row.refresh_from_db()
        self.assertEqual(row.consecutive_failures, 0)
        self.assertIsNotNone(row.next_attempt_at)
        self.assertEqual(ImagePreparationRequest.objects.count(), 1)

    def test_retry_endpoint_creates_idempotent_child_and_preserves_parent(self):
        self.activate(self.release_id)
        row = ImagePreparationRequest.objects.get()
        row.operation_id, row.operation_status = "prep-parent", "PARTIAL"
        row.progress = _validate_result(
            row, detail(row, "PARTIAL", ("READY", "FAILED")), True
        )[2]
        row.save()
        url = self.detail_url(row) + "/retry"
        first = self.post(url, {"node_uids": ["node-1"]})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, self.post(url, {"node_uids": ["node-1"]}).data)
        self.assertEqual(
            self.post(url, {"node_uids": ["node-0"]}, key="bad-nodes").status_code, 400
        )
        self.assertEqual(
            self.post(url, {"node_uids": None}, key="null").status_code, 400
        )
        child = ImagePreparationRequest.objects.exclude(pk=row.pk).get()
        self.assertEqual(child.parent_id, row.pk)
        self.assertEqual(
            child.payload, {"request_id": str(child.pk), "node_uids": ["node-1"]}
        )
        row.refresh_from_db()
        self.assertEqual(row.operation_status, "PARTIAL")


@override_settings(**CONFIG)
class PreparationWorkerTests(TransactionTestCase):
    def setUp(self):
        self.challenge = Challenge.objects.create(
            title="Preparation",
            category="WEB",
            difficulty="EASY",
            score=500,
            flag_hash="unused",
        )
        self.release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=1,
            registry_revision=5,
            challenge_slug="web-basic",
            isolation_profile="WEB",
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
            approved_at=timezone.now(),
        )
        ReleaseContainer.objects.create(
            release=self.release,
            name="app",
            image_ref=f"ghcr.io/msg-ctf/challenges/web-basic/app@sha256:{DIGEST_A}",
            ports=[{"port": 8080, "public": True}],
            env={"PRIVATE": "hidden-env"},
            secret_env={"TOKEN": str(uuid.uuid4())},
        )
        ReleaseContainer.objects.create(
            release=self.release,
            name="db",
            image_ref=f"ghcr.io/msg-ctf/challenges/web-basic/db@sha256:{DIGEST_B}",
            ports=[{"port": 5432, "public": False}],
        )
        self.row = queue_preparation(self.release, "root")
        self.network = patch("apps.instances.image_preparations._call_runtime")
        self.remote = self.network.start()
        self.remote.side_effect = AssertionError("Unexpected Runtime call")
        self.addCleanup(self.network.stop)

    def due(self, row=None):
        row = row or self.row
        ImagePreparationRequest.objects.filter(pk=row.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
        row.refresh_from_db()

    def run_once(self, response=None, error=None, row=None):
        row = row or self.row
        self.due(row)
        self.remote.side_effect = error
        self.remote.return_value = response
        self.assertTrue(process_preparation(row.pk))
        row.refresh_from_db()

    def accept(self):
        self.run_once(accepted(self.row))

    def test_all_containers_frozen_without_env_or_credentials(self):
        payload = copy.deepcopy(self.row.payload)
        self.assertEqual(len(payload["containers"]), 2)
        self.assertEqual(payload["containers"][1]["ports"], [5432])
        encoded = json.dumps(payload)
        for value in ("hidden-env", "unit-test-preparation-token", "secret_env", "env"):
            self.assertNotIn(value, encoded)
        self.release.containers.filter(name="app").update(
            image_ref="changed-after-queue"
        )
        self.accept()
        self.assertEqual(self.remote.call_args.args[2], payload)
        self.assertEqual(self.row.delivery_status, "ACCEPTED")

    def test_lost_ack_reposts_same_id_and_exact_body(self):
        self.run_once(error=TimeoutError())
        first = self.remote.call_args.args
        self.assertEqual(self.row.delivery_status, "UNKNOWN")
        self.accept()
        self.assertEqual(first, self.remote.call_args.args)
        self.assertEqual(self.row.attempts, 2)

    def test_truncated_http_response_recovers_with_same_request(self):
        self.run_once(error=IncompleteRead(b"partial"))
        self.assertEqual(self.row.last_error_code, "RUNTIME_UNAVAILABLE")
        self.assertEqual(self.row.delivery_status, "UNKNOWN")
        self.accept()
        self.assertEqual(self.remote.call_args.args[2]["request_id"], str(self.row.pk))

    def test_get_after_ack_and_terminal_history_is_not_live_readiness(self):
        self.accept()
        self.run_once(detail(self.row))
        self.assertEqual(
            self.remote.call_args.args[1], "/v1/image-preparations/prep-001"
        )
        self.assertIsNone(self.remote.call_args.args[2])
        self.assertEqual(self.row.operation_status, "SUCCEEDED")
        self.assertIsNone(self.row.next_attempt_at)
        calls = self.remote.call_count
        self.assertFalse(process_preparation(self.row.pk))
        self.assertEqual(self.remote.call_count, calls)

    def test_lost_terminal_ack_is_followed_by_detail_poll(self):
        self.run_once(accepted(self.row, "SUCCEEDED"))
        self.assertIsNotNone(self.row.next_attempt_at)
        self.run_once(detail(self.row))
        self.assertIsNone(self.row.next_attempt_at)
        self.assertTrue(self.row.progress)

    def test_terminal_ack_cannot_be_rewritten_by_later_detail(self):
        self.run_once(accepted(self.row, "FAILED"))
        self.run_once(detail(self.row))
        self.assertEqual(self.row.operation_status, "FAILED")
        self.assertEqual(self.row.last_error_code, "RUNTIME_INVALID_RESPONSE")

    def test_transient_retry_exhaustion_does_not_invent_remote_failure(self):
        for _ in range(3):
            self.run_once(error=URLError("private upstream URL"))
        self.assertEqual(self.row.delivery_status, "UNKNOWN")
        self.assertFalse(self.row.operation_status)
        self.assertIsNone(self.row.next_attempt_at)
        self.assertEqual(self.row.last_error_code, "RUNTIME_UNAVAILABLE")

    def test_http_failure_classification_and_no_error_body_persistence(self):
        for code, expected in (
            (401, "RUNTIME_UNAUTHORIZED"),
            (403, "RUNTIME_FORBIDDEN"),
            (409, "RUNTIME_REQUEST_CONFLICT"),
            (422, "RUNTIME_POLICY_REJECTED"),
            (302, "RUNTIME_HTTP_ERROR"),
        ):
            with self.subTest(code=code):
                self.run_once(
                    error=HTTPError(
                        "private-url",
                        code,
                        "private-token",
                        {},
                        io.BytesIO(b"secret-body"),
                    )
                )
                self.assertEqual(self.row.last_error_code, expected)
                self.assertIsNone(self.row.next_attempt_at)
                self.assertNotIn("private", json.dumps(self.row.progress))

    def test_get_404_preserves_operation_and_never_recreates(self):
        self.accept()
        self.run_once(error=HTTPError("private", 404, "", {}, io.BytesIO()))
        self.assertEqual(self.row.operation_id, "prep-001")
        self.assertEqual(self.row.delivery_status, "ACCEPTED")
        self.assertEqual(self.row.last_error_code, "RUNTIME_OPERATION_NOT_FOUND")
        self.assertIsNone(self.row.next_attempt_at)

    def test_child_retry_uses_new_id_parent_path_and_original_failed_nodes(self):
        self.accept()
        self.run_once(detail(self.row, "PARTIAL", ("READY", "FAILED")))
        parent_snapshot = copy.deepcopy(self.row.progress)
        child = queue_retry(self.row, "root")
        self.assertNotEqual(child.pk, self.row.pk)
        self.assertEqual(child.payload, {"request_id": str(child.pk)})
        self.run_once(accepted(child, operation_id="prep-child"), row=child)
        self.assertEqual(
            self.remote.call_args.args[1], "/v1/image-preparations/prep-001/retry"
        )
        response = detail(child)
        response["nodes"][0]["node_uid"] = "node-1"
        self.run_once(response, row=child)
        self.assertEqual(child.operation_status, "SUCCEEDED")
        self.row.refresh_from_db()
        self.assertEqual(self.row.progress, parent_snapshot)

    def test_no_eligible_nodes_requires_fresh_root_not_empty_retry(self):
        self.accept()
        self.run_once(
            {**detail(self.row, "FAILED", ()), "error_code": "NO_ELIGIBLE_NODES"}
        )
        with self.assertRaises(PreparationError) as error:
            queue_retry(self.row, "root")
        self.assertEqual(error.exception.code, "IMAGE_PREPARATION_NO_RETRYABLE_NODES")
        self.assertTrue(serialize_preparation(self.row)["requires_review"])

    def test_retry_result_cannot_omit_requested_failed_nodes(self):
        self.accept()
        self.run_once(detail(self.row, "FAILED", ("FAILED", "FAILED")))
        child = queue_retry(self.row, "root", ["node-0", "node-1"])
        self.run_once(accepted(child, operation_id="prep-child"), row=child)
        self.run_once(detail(child), row=child)
        self.assertEqual(child.last_error_code, "RUNTIME_INVALID_RESPONSE")
        self.assertFalse(child.progress)

    def test_claim_prevents_concurrent_delivery(self):
        self.due()

        def during_call(*args):
            self.assertFalse(process_preparation(self.row.pk))
            return accepted(self.row)

        self.remote.side_effect = during_call
        self.assertTrue(process_preparation(self.row.pk))
        self.assertEqual(self.remote.call_count, 1)

    def test_two_database_connections_deliver_only_once(self):
        entered, finish = Event(), Event()

        def remote(*args):
            entered.set()
            if not finish.wait(5):
                raise AssertionError("worker did not finish")
            return accepted(self.row)

        def worker():
            close_old_connections()
            try:
                return process_preparation(self.row.pk)
            finally:
                connections.close_all()

        self.remote.side_effect = remote
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(process_preparation(self.row.pk))
            finally:
                finish.set()
            self.assertTrue(future.result(timeout=5))
        self.assertEqual(self.remote.call_count, 1)

    def test_expired_delivery_lease_recovers_with_original_request(self):
        ImagePreparationRequest.objects.filter(pk=self.row.pk).update(
            delivery_status="SENDING"
        )
        self.accept()
        self.assertEqual(self.remote.call_args.args[2]["request_id"], str(self.row.pk))

    def test_late_worker_result_cannot_overwrite_new_claim(self):
        self.due()

        def remote(*args):
            ImagePreparationRequest.objects.filter(pk=self.row.pk).update(
                next_attempt_at=timezone.now() + timedelta(seconds=300)
            )
            return accepted(self.row)

        self.remote.side_effect = remote
        self.assertFalse(process_preparation(self.row.pk))
        self.row.refresh_from_db()
        self.assertFalse(self.row.operation_id)

    def test_worker_refuses_outer_transaction(self):
        with transaction.atomic(), self.assertRaises(RuntimeError):
            process_preparation(self.row.pk)
        self.remote.assert_not_called()

    def test_disabled_and_unsupported_release_never_sent(self):
        for runtime in ("DOCKER", "VM"):
            self.release.runtime_type = runtime
            with self.subTest(runtime=runtime), self.assertRaises(PreparationError):
                queue_preparation(self.release, "root")
        self.remote.assert_not_called()

    def test_origin_change_pauses_without_sending_token_to_new_server(self):
        with override_settings(IMAGE_PREPARATION_RUNTIME_URL="https://other.example"):
            self.assertFalse(process_preparation(self.row.pk))
        self.row.refresh_from_db()
        self.assertEqual(self.row.last_error_code, "IMAGE_PREPARATION_ORIGIN_CHANGED")
        self.remote.assert_not_called()

    def test_revoked_target_scope_pauses_frozen_request(self):
        ImagePreparationRequest.objects.filter(pk=self.row.pk).update(
            delivery_status="SENDING"
        )
        with override_settings(IMAGE_PREPARATION_TARGET_IDS="target-a"):
            self.assertFalse(process_preparation(self.row.pk))
        self.row.refresh_from_db()
        self.assertEqual(
            self.row.last_error_code, "IMAGE_PREPARATION_TARGET_SCOPE_CHANGED"
        )
        self.remote.assert_not_called()
        schedule_refresh(self.row)
        self.accept()

    def test_invalid_stored_ports_do_not_cross_runtime_boundary(self):
        for port in (True, 0, 65536, "8080"):
            with self.subTest(port=port):
                self.release.containers.filter(name="app").update(
                    ports=[{"port": port, "public": True}]
                )
                with self.assertRaises(PreparationError):
                    queue_preparation(self.release, "root")
        self.remote.assert_not_called()

    def test_invalid_response_does_not_overwrite_valid_progress(self):
        self.accept()
        progress = detail(self.row, "RUNNING", ("PULLING",))
        self.run_once(progress)
        saved = copy.deepcopy(self.row.progress)
        mutations = [
            lambda data: data.update(request_id=str(uuid.uuid4())),
            lambda data: data.update(image_set_id="sha256:" + "0" * 64),
            lambda data: data.update(operation_id="other-operation"),
            lambda data: data.update(release_id=str(uuid.uuid4())),
            lambda data: data.update(architecture="ARM64"),
            lambda data: data["nodes"][0].update(target_id="outside-scope"),
            lambda data: data["nodes"][0].update(node_uid="replacement-node"),
            lambda data: data["nodes"][0]["images"].pop(),
            lambda data: data["nodes"][0]["images"][0].update(attempts=True),
            lambda data: data.update(status="QUEUED"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                response = copy.deepcopy(progress)
                mutate(response)
                self.run_once(response)
                self.assertEqual(self.row.last_error_code, "RUNTIME_INVALID_RESPONSE")
                self.assertEqual(self.row.progress, saved)

    def test_ready_requires_every_image_and_verified_digest_and_time(self):
        self.accept()
        for mutate in (
            lambda data: data["nodes"][0]["images"][1].update(status="FAILED"),
            lambda data: data["nodes"][0]["images"][0].pop("resolved_manifest_digest"),
            lambda data: data["nodes"][0].update(observed_at="2026-10-26T00:00:00"),
            lambda data: data["nodes"][0].update(
                observed_at=(timezone.now() + timedelta(days=1)).isoformat()
            ),
            lambda data: data.update(nodes=[]),
        ):
            with self.subTest(mutate=mutate):
                response = detail(self.row)
                mutate(response)
                self.run_once(response)
                self.assertEqual(self.row.last_error_code, "RUNTIME_INVALID_RESPONSE")

    def test_unknown_failure_code_sanitized_and_other_response_fields_dropped(self):
        self.accept()
        data = detail(self.row, "FAILED", ("FAILED",))
        data.update(error_code="Bearer secret", token="secret", message="private URL")
        data["nodes"][0]["images"][0].update(error_code="secret", message="private")
        self.run_once(data)
        self.assertEqual(
            self.row.progress["error_code"], "UNCLASSIFIED_RUNTIME_FAILURE"
        )
        self.assertNotIn("secret", json.dumps(self.row.progress))
        self.assertNotIn("private", json.dumps(self.row.progress))

    def test_disabled_command_does_not_query_requests_or_send(self):
        with override_settings(IMAGE_PREPARATION_ENABLED=False), self.assertNumQueries(
            0
        ):
            call_command("process_image_preparations", stdout=io.StringIO())
        self.remote.assert_not_called()

    def test_command_processes_bounded_due_batch(self):
        self.remote.side_effect = None
        self.remote.return_value = accepted(self.row)
        call_command("process_image_preparations", limit=1, stdout=io.StringIO())
        self.assertEqual(self.remote.call_count, 1)


@override_settings(**CONFIG)
class PreparationTransportTests(SimpleTestCase):
    def test_configuration_rejects_unsafe_url_token_targets_and_bounds(self):
        values = {
            "IMAGE_PREPARATION_RUNTIME_URL": [
                "http://runtime.example",
                "https://user:pw@runtime.example",
                "https://runtime.example/",
                "https://runtime.example?x=1",
                "https://runtime.example:99999",
            ],
            "IMAGE_PREPARATION_API_TOKEN": ["", "bad\ntoken"],
            "IMAGE_PREPARATION_TARGET_IDS": ["", "one,one", "one,"],
            "IMAGE_PREPARATION_TIMEOUT_SECONDS": ["bad", 0, 31],
            "IMAGE_PREPARATION_POLL_SECONDS": [0, 301],
            "IMAGE_PREPARATION_MAX_ATTEMPTS": [0, 11],
        }
        for setting, candidates in values.items():
            for value in candidates:
                with self.subTest(setting=setting, value=value), override_settings(
                    **{setting: value}
                ), self.assertRaises(PreparationError):
                    configuration()

    def test_disabled_does_not_require_valid_configuration(self):
        with override_settings(
            IMAGE_PREPARATION_ENABLED=False, IMAGE_PREPARATION_TIMEOUT_SECONDS="bad"
        ):
            with self.assertRaises(PreparationError) as error:
                configuration()
            self.assertEqual(error.exception.code, "IMAGE_PREPARATION_DISABLED")

    def test_image_set_hash_deduplicates_and_ignores_release_and_target_order(self):
        image = "ghcr.io/msg-ctf/challenges/x/app@sha256:" + DIGEST_A
        spec = {
            "architecture": "AMD64",
            "containers": [{"image": image}, {"image": image}],
            "release_id": "one",
            "target_ids": ["a"],
        }
        digest = image_set_id(spec)
        spec.update(release_id="two", target_ids=["b"], containers=[{"image": image}])
        self.assertEqual(image_set_id(spec), digest)
        spec["architecture"] = "ARM64"
        self.assertNotEqual(image_set_id(spec), digest)

    @patch("apps.instances.image_preparations.build_opener")
    def test_http_uses_fixed_origin_no_proxy_no_redirect_and_bounded_body(self, opener):
        response = opener.return_value.open.return_value.__enter__.return_value
        response.status, response.read.return_value = 202, b"{}"
        config = configuration()
        self.assertEqual(
            _call_runtime(config, "/v1/image-preparations", {"request_id": "one"}), {}
        )
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://runtime.example/v1/image-preparations"
        )
        self.assertEqual(
            request.get_header("Authorization"), "Bearer unit-test-preparation-token"
        )
        proxy, redirect = opener.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(
            redirect.redirect_request(
                None, None, 302, "", {}, "https://attacker.example"
            )
        )
        response.read.return_value = b"x" * (2 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            _call_runtime(config, "/v1/image-preparations", {})
