import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import (
    DatabaseError,
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.challenge.models import Challenge
from apps.common.models import IdempotencyRecord
from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    ChallengeUserFileBundle,
    PollerArtifact,
    PublicationRequest,
    RuntimeSecret,
)

BASE = "/api/v1/admin/publication_requests"
SHA = "a" * 40


class PublicationRequestFixtures:
    def setUp(self):
        super().setUp()
        configured = override_settings(
            PUBLICATION_REQUESTS_ENABLED=True,
            PUBLICATION_DISPATCH_ENABLED=False,
            SECURE_SSL_REDIRECT=False,
            REPOSITORY_ENV_EXCLUDED_SLUGS={"pwn-msgdriver"},
        )
        configured.enable()
        self.addCleanup(configured.disable)
        self.network = []
        for target in (
            "apps.instances.poller.github_request",
            "apps.instances.services.urlopen",
            "apps.adminpanel.resource_broker._opener.open",
        ):
            blocked = patch(
                target, side_effect=AssertionError("External calls are forbidden")
            )
            self.network.append(blocked.start())
            self.addCleanup(blocked.stop)
        self.admin = User.objects.create_user(
            login_id="publication-admin", nickname="admin", role=Role.ADMIN
        )
        self.player = User.objects.create_user(
            login_id="publication-player", nickname="player"
        )
        self.challenge = Challenge.objects.create(
            challenge_slug="web-open-house",
            title="Publication test",
            category="WEB",
            difficulty="EASY",
            score=500,
            flag_hash="not-a-real-flag-hash",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.body = {
            "challenge_slug": self.challenge.challenge_slug,
            "requested_sha": SHA,
            "runtime_smoke": False,
        }

    def record(self, body=None, key="publication-intent", client=None):
        return (client or self.client).post(
            BASE,
            self.body if body is None else body,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def recorded(self, key="publication-intent"):
        response = self.record(key=key)
        self.assertEqual(response.status_code, 200, response.data)
        return PublicationRequest.objects.get(pk=response.data["data"]["request_id"])


class PublicationRequestTests(PublicationRequestFixtures, TestCase):
    def test_records_intent_only_without_run_or_sha_guarantee(self):
        response = self.record()
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["dispatch_status"], "NOT_DISPATCHED")
        self.assertFalse(data["dispatch_supported"])
        self.assertFalse(data["sha_verified"])
        for field in (
            "run_id",
            "run_attempt",
            "workflow_status",
            "workflow_conclusion",
            "workflow_url",
        ):
            self.assertIsNone(data[field])
        self.assertEqual(data["requested_sha"], SHA)
        self.assertEqual(data["repository"], "MSG-CTF/2026_MSG_CTF")
        self.assertEqual(data["workflow"], "challenge-validation.yml")
        self.assertEqual(data["ref"], "main")
        self.assertEqual(data["requested_by"]["user_id"], str(self.admin.pk))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(PublicationRequest.objects.count(), 1)
        self.assertEqual(IdempotencyRecord.objects.count(), 1)
        for network in self.network:
            network.assert_not_called()
        for model in (
            ChallengeRelease,
            ChallengeRuntimeConfig,
            ChallengeUserFileBundle,
            PollerArtifact,
            RuntimeSecret,
        ):
            self.assertFalse(model.objects.exists())

    @override_settings(PUBLICATION_REQUESTS_ENABLED=False)
    def test_disabled_does_not_create_request_or_idempotency_record(self):
        response = self.record()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "PUBLICATION_REQUESTS_DISABLED")
        self.assertFalse(PublicationRequest.objects.exists())
        self.assertFalse(IdempotencyRecord.objects.exists())

    def test_post_list_detail_require_admin(self):
        record = self.recorded()
        for user, expected in ((self.player, 403), (None, 401)):
            self.client.force_authenticate(user)
            with self.subTest(user=user):
                self.assertEqual(self.record().status_code, expected)
                self.assertEqual(self.client.get(BASE).status_code, expected)
                self.assertEqual(
                    self.client.get(f"{BASE}/{record.pk}").status_code, expected
                )
        self.assertEqual(PublicationRequest.objects.count(), 1)

    def test_missing_and_oversized_key_do_not_create_requests(self):
        self.assertEqual(
            self.client.post(BASE, self.body, format="json").status_code, 400
        )
        self.assertEqual(self.record(key="x" * 201).status_code, 400)
        self.assertFalse(PublicationRequest.objects.exists())
        self.assertFalse(IdempotencyRecord.objects.exists())

    def test_same_key_replays_and_different_body_conflicts(self):
        first, second = self.record(), self.record()
        self.assertEqual(first.data, second.data)
        for changes in ({"requested_sha": "b" * 40}, {"requested_sha": None}):
            response = self.record({**self.body, **changes})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["code"], "IDEMPOTENCY_KEY_CONFLICT")
        self.assertEqual(PublicationRequest.objects.count(), 1)

    def test_defaults_are_canonical_for_replay(self):
        first = self.record({"challenge_slug": self.challenge.challenge_slug})
        replay = self.record(
            {
                "challenge_slug": self.challenge.challenge_slug,
                "requested_sha": None,
                "runtime_smoke": False,
            }
        )
        self.assertEqual(first.data, replay.data)
        self.assertIsNone(first.data["data"]["requested_sha"])

    def test_runtime_smoke_true_is_rejected_without_recording_or_consuming_key(self):
        with patch("apps.instances.publication_dispatch.github_json") as github:
            response = self.record({**self.body, "runtime_smoke": True})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(PublicationRequest.objects.exists())
        self.assertFalse(IdempotencyRecord.objects.exists())
        github.assert_not_called()
        for network in self.network:
            network.assert_not_called()
        response = self.record()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["runtime_smoke"])
        self.assertEqual(PublicationRequest.objects.count(), 1)
        self.assertEqual(IdempotencyRecord.objects.count(), 1)

    def test_distinct_keys_or_administrators_can_record_separate_intents(self):
        self.recorded()
        self.recorded(key="another-intent")
        other = User.objects.create_user(
            login_id="publication-other-admin", nickname="other", role=Role.ADMIN
        )
        self.client.force_authenticate(other)
        self.recorded()
        self.assertEqual(PublicationRequest.objects.count(), 3)

    def test_invalid_slug_sha_boolean_and_unknown_fields(self):
        cases = (
            [
                {**self.body, "challenge_slug": value}
                for value in (
                    "../web-open-house",
                    "/web-open-house",
                    "web/open-house",
                    "web--open-house",
                    "WEB-open-house",
                    "x" * 101,
                    "web-open-house\n",
                )
            ]
            + [
                {**self.body, "requested_sha": value}
                for value in (
                    "",
                    "a" * 39,
                    "a" * 41,
                    "g" * 40,
                    "A" * 40,
                    SHA + "\n",
                    True,
                )
            ]
            + [
                {**self.body, "runtime_smoke": value}
                for value in ("true", "false", 1, 0, None, [], {})
            ]
            + [
                {**self.body, field: value}
                for field, value in (
                    ("repository", "other/repo"),
                    ("ref", "feature"),
                    ("workflow", "other.yml"),
                    ("run_id", 123),
                    ("dispatch_status", "DISPATCHED"),
                    ("token", "not-a-real-credential"),
                    ("publish_images", False),
                    ("artifact_id", 123),
                    ("requested_by", str(self.player.pk)),
                )
            ]
        )
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.record(body).status_code, 400)
        self.assertFalse(PublicationRequest.objects.exists())
        self.assertFalse(IdempotencyRecord.objects.exists())

    def test_non_object_and_empty_body_are_rejected(self):
        for body in ([], [self.body], {}, {"runtime_smoke": False}):
            with self.subTest(body=body):
                self.assertEqual(self.record(body).status_code, 400)

    def test_unknown_or_separate_contract_challenge_is_rejected(self):
        response = self.record({**self.body, "challenge_slug": "web-missing"})
        self.assertEqual(response.status_code, 404)
        for slug in ("pwn-msgdriver", "koth-test"):
            self.assertEqual(
                self.record(
                    {**self.body, "challenge_slug": slug}, key=slug
                ).status_code,
                400,
            )
        self.assertFalse(PublicationRequest.objects.exists())

    def test_database_failure_rolls_back_request_and_key(self):
        with patch(
            "apps.adminpanel.publication_requests.PublicationRequest.objects.create",
            side_effect=DatabaseError("storage failed"),
        ), patch("apps.common.exceptions.logger.exception"):
            self.assertEqual(self.record().status_code, 500)
        self.assertFalse(PublicationRequest.objects.exists())
        self.assertFalse(IdempotencyRecord.objects.exists())
        self.assertEqual(self.record().status_code, 200)

    def test_list_and_detail_work_when_recording_disabled(self):
        record = self.recorded()
        with override_settings(PUBLICATION_REQUESTS_ENABLED=False):
            listing = self.client.get(BASE)
            detail = self.client.get(f"{BASE}/{record.pk}")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["data"]["total_count"], 1)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["data"]["request_id"], str(record.pk))
        self.assertIn("no-store", detail["Cache-Control"])

    def test_status_is_from_database_not_an_external_poll_or_import(self):
        record = self.recorded()
        PublicationRequest.objects.filter(pk=record.pk).update(
            dispatch_status="DISPATCHED",
            run_id=123,
            run_attempt=2,
            workflow_status="completed",
            workflow_conclusion="success",
        )
        data = self.client.get(f"{BASE}/{record.pk}").data["data"]
        self.assertEqual(data["run_id"], 123)
        self.assertEqual(data["run_attempt"], 2)
        self.assertEqual(data["workflow_conclusion"], "success")
        self.assertEqual(
            data["workflow_url"],
            "https://github.com/MSG-CTF/2026_MSG_CTF/actions/runs/123",
        )
        self.assertFalse(data["sha_verified"])
        for network in self.network:
            network.assert_not_called()
        self.assertFalse(ChallengeRelease.objects.exists())
        self.assertFalse(ChallengeRuntimeConfig.objects.exists())
        # POST replays the original receipt; GET is the current state.
        replay = self.record()
        self.assertEqual(replay.data["data"]["dispatch_status"], "NOT_DISPATCHED")
        self.assertEqual(PublicationRequest.objects.count(), 1)

    def test_recording_does_not_change_active_release_or_collected_files(self):
        release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            challenge_slug=self.challenge.challenge_slug,
            version=1,
            registry_revision=1,
            runtime_type="KUBERNETES",
            architecture="AMD64",
            isolation_profile="WEB",
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
        )
        config = ChallengeRuntimeConfig.objects.create(
            challenge=self.challenge, current_release=release
        )
        bundle = ChallengeUserFileBundle.objects.create(
            challenge=self.challenge,
            artifact_id=12,
            artifact_name="existing-user-files-bundle",
            registry_revision=1,
            source_ref="refs/heads/main",
            source_sha=SHA,
            present=False,
        )
        artifact = PollerArtifact.objects.create(
            artifact_id=12, kind="USER_FILES", payload={"id": 12}
        )
        record = self.recorded()
        self.client.get(BASE)
        self.client.get(f"{BASE}/{record.pk}")
        config.refresh_from_db()
        bundle.refresh_from_db()
        artifact.refresh_from_db()
        self.assertEqual(config.current_release_id, release.pk)
        self.assertEqual(bundle.source_sha, SHA)
        self.assertIsNone(artifact.processed_at)
        self.assertEqual(ChallengeRelease.objects.count(), 1)
        self.assertEqual(ChallengeUserFileBundle.objects.count(), 1)
        self.assertFalse(RuntimeSecret.objects.exists())

    def test_unknown_dispatch_is_not_automatically_retried(self):
        record = self.recorded()
        PublicationRequest.objects.filter(pk=record.pk).update(
            dispatch_status="DISPATCH_UNKNOWN"
        )
        self.record()
        data = self.client.get(f"{BASE}/{record.pk}").data["data"]
        self.assertEqual(data["dispatch_status"], "DISPATCH_UNKNOWN")
        self.assertIsNone(data["run_id"])
        self.assertIsNone(data["workflow_url"])
        self.assertEqual(PublicationRequest.objects.count(), 1)
        for network in self.network:
            network.assert_not_called()

    def test_stable_pagination_and_filters(self):
        records = [self.recorded(key=str(uuid.uuid4())) for _ in range(3)]
        PublicationRequest.objects.all().update(created_at=timezone.now())
        expected = [
            str(value)
            for value in sorted((record.pk for record in records), reverse=True)
        ]
        actual = []
        for page in (1, 2, 3):
            response = self.client.get(
                BASE,
                {
                    "page": page,
                    "size": 1,
                    "challenge_slug": self.challenge.challenge_slug,
                    "dispatch_status": "NOT_DISPATCHED",
                },
            )
            self.assertEqual(response.data["data"]["total_count"], 3)
            actual.extend(item["request_id"] for item in response.data["data"]["items"])
        self.assertEqual(actual, expected)
        self.assertEqual(
            self.client.get(BASE, {"challenge_slug": "web-other"}).data["data"][
                "total_count"
            ],
            0,
        )
        self.assertEqual(
            self.client.get(BASE, {"page": 10000}).data["data"]["items"], []
        )

    def test_invalid_filters_and_pagination(self):
        for query in (
            {"page": 0},
            {"page": 10001},
            {"size": 101},
            {"size": 0},
            {"dispatch_status": "QUEUED"},
            {"challenge_slug": "../x"},
            {"token": "unexpected"},
        ):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(BASE, query).status_code, 400)

    def test_unknown_request_and_mutating_methods(self):
        self.assertEqual(self.client.get(f"{BASE}/{uuid.uuid4()}").status_code, 404)
        record = self.recorded()
        for method in (self.client.patch, self.client.delete, self.client.post):
            self.assertEqual(
                method(f"{BASE}/{record.pk}", {}, format="json").status_code, 405
            )

    def test_deleting_actor_or_challenge_preserves_audit_snapshots(self):
        record = self.recorded()
        self.challenge.delete()
        self.admin.delete()
        record.refresh_from_db()
        self.assertIsNone(record.challenge_id)
        self.assertIsNone(record.requested_by_id)
        self.assertEqual(record.challenge_slug, "web-open-house")
        self.assertEqual(record.requested_by_login, "publication-admin")

    def test_constraint_rejects_invalid_sha_run_id_and_attempt(self):
        record = self.recorded()
        for values in (
            {"requested_sha": SHA[:-1]},
            {"requested_sha": "g" * 40},
            {"run_id": 0},
            {"run_id": 9007199254740992},
            {"run_attempt": 1},
            {"run_id": 123, "run_attempt": 0},
        ):
            with self.subTest(values=values):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    PublicationRequest.objects.filter(pk=record.pk).update(**values)


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL concurrency")
class ConcurrentPublicationRequestTests(
    PublicationRequestFixtures, TransactionTestCase
):
    def test_same_key_creates_one_intent_and_one_receipt(self):
        ready = Barrier(2)

        def record(_):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=self.admin.pk))
                ready.wait(timeout=10)
                response = self.record(client=client)
                return response.status_code, response.data["data"]["request_id"]
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(record, range(2)))
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(PublicationRequest.objects.count(), 1)
        self.assertEqual(IdempotencyRecord.objects.count(), 1)
