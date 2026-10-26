"""Gateway metadata must survive the Backend's Scheduler boundary and JSON storage."""

import uuid
from copy import deepcopy
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge
from apps.instances.deployment_tests import _apply_response, serialize_test, test_team_id
from apps.instances.models import ChallengeRelease, DeploymentTest, Instance, InstanceStatus
from apps.instances.services import (
    SchedulerError,
    create_instance_from_scheduler,
    isoformat_z,
    normalize_scheduler_endpoints,
    serialize_instance,
    update_instance_from_scheduler,
)


GATEWAY_TCP = {
    "container_name": "pwn",
    "port": 31337,
    "protocol": "TCP",
    "service_url": "tls://i-example.ctf.example:443",
    "hostname": "i-example.ctf.example",
    "connection_port": 443,
    "tls": True,
    "server_name": "i-example.ctf.example",
}


class GatewayEndpointNormalizationTests(SimpleTestCase):
    def test_tcp_connection_metadata_is_distinct_from_internal_port(self):
        self.assertEqual(normalize_scheduler_endpoints([deepcopy(GATEWAY_TCP)]), [GATEWAY_TCP])

    def test_http_metadata_and_explicit_null_are_preserved(self):
        endpoint = {
            "container_name": "web", "port": 8080, "protocol": "HTTP",
            "service_url": "https://i-web.ctf.example", "hostname": "i-web.ctf.example",
            "connection_port": 443, "tls": True, "server_name": None,
        }
        self.assertEqual(normalize_scheduler_endpoints([deepcopy(endpoint)]), [endpoint])

    def test_null_and_false_are_not_replaced_by_missing_or_defaults(self):
        endpoint = {
            "container_name": "pwn", "port": 31337, "protocol": "TCP",
            "service_url": "tcp://legacy.example:31000", "hostname": None,
            "connection_port": None, "tls": False, "server_name": None,
        }
        self.assertEqual(normalize_scheduler_endpoints([deepcopy(endpoint)]), [endpoint])
        endpoint["tls"] = None
        self.assertEqual(normalize_scheduler_endpoints([deepcopy(endpoint)]), [endpoint])

    def test_legacy_endpoints_do_not_gain_optional_fields(self):
        endpoint = {
            "container_name": "web", "port": 8080, "protocol": "HTTP",
            "service_url": "http://legacy.example:31000",
        }
        self.assertEqual(normalize_scheduler_endpoints([deepcopy(endpoint)]), [endpoint])

    def test_invalid_optional_metadata_is_rejected(self):
        invalid_values = {
            "hostname": ["", 123, False, [], {}],
            "server_name": ["", 123, False, [], {}],
            "connection_port": [0, 65536, -1, True, False, "443", 443.0],
            "tls": [0, 1, "false", [], {}],
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(SchedulerError) as caught:
                        normalize_scheduler_endpoints([{**GATEWAY_TCP, field: value}])
                    self.assertEqual(caught.exception.code, "SCHEDULER_UNAVAILABLE")


class GatewayEndpointPersistenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(team_name="gateway-endpoint-team")
        cls.user = User.objects.create_user(
            login_id="gateway-endpoint-user", nickname="Gateway", team=cls.team,
        )
        cls.challenge = Challenge.objects.create(
            title="Gateway endpoint", category=Challenge.CategoryType.PWN,
            difficulty=Challenge.DifficultyType.EASY, score=500, description="Gateway test",
            flag_hash="unused-test-flag-hash",
        )
        cls.release = ChallengeRelease.objects.create(
            challenge=cls.challenge, version=1, registry_revision=1,
            isolation_profile="PWN", cpu_millicores=500, memory_mib=512,
            ephemeral_storage_mib=1024,
        )

    def snapshot(self, instance_id, endpoints):
        return {
            "instance_id": str(instance_id), "team_id": str(self.team.pk),
            "user_id": str(self.user.pk), "challenge_id": str(self.challenge.pk),
            "status": "RUNNING", "endpoints": endpoints,
            "expires_at": isoformat_z(timezone.now() + timedelta(minutes=15)),
        }

    def test_participant_create_persists_and_serializes_gateway_metadata(self):
        instance = create_instance_from_scheduler(
            self.snapshot(uuid.uuid4(), [deepcopy(GATEWAY_TCP)]),
            user=self.user, team=self.team, challenge=self.challenge, release=self.release,
        )
        instance.refresh_from_db()
        self.assertEqual(instance.endpoints, [GATEWAY_TCP])
        response = serialize_instance(instance)
        self.assertEqual(response["endpoints"], [GATEWAY_TCP])
        self.assertEqual(response["host"], "tls://i-example.ctf.example:443")

    def test_participant_refresh_preserves_false_and_null_metadata(self):
        instance = Instance.objects.create(
            user=self.user, team=self.team, challenge=self.challenge,
            release=self.release, status=InstanceStatus.PROVISIONING,
        )
        endpoint = {**GATEWAY_TCP, "tls": False, "server_name": None}
        update_instance_from_scheduler(instance, self.snapshot(instance.pk, [deepcopy(endpoint)]))
        instance.refresh_from_db()
        self.assertEqual(instance.endpoints, [endpoint])
        self.assertEqual(serialize_instance(instance)["endpoints"], [endpoint])

    def test_administrator_test_persists_and_serializes_gateway_metadata(self):
        test = DeploymentTest.objects.create(
            requested_by=self.user, request_id=uuid.uuid4(), release=self.release,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        data = self.snapshot(uuid.uuid4(), [deepcopy(GATEWAY_TCP)])
        data.update({
            "team_id": str(test_team_id(test)), "release_id": str(self.release.pk),
            "registry_revision": 1, "expires_at": isoformat_z(test.expires_at),
        })
        _apply_response(test, data)
        test.refresh_from_db()
        self.assertEqual(test.endpoints, [GATEWAY_TCP])
        self.assertEqual(serialize_test(test)["endpoints"], [GATEWAY_TCP])
