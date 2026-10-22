from copy import deepcopy
from uuid import uuid4

from django.test import TestCase

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge
from apps.instances.models import Instance
from apps.adminpanel.node_allocation import enrich_allocations


class NodeAllocationTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(team_name="배치 QA")
        self.user = User.objects.create_user(login_id="allocation-qa", password="local-test", nickname="QA", team=self.team)
        self.challenge = Challenge.objects.create(title="예약량 확인", category="WEB", difficulty="EASY", score=100, flag_hash="test")
        self.instance = Instance.objects.create(user=self.user, team=self.team, challenge=self.challenge, status="RUNNING")
        self.target = {"resource_allocation": {"complete": True, "allocatable": {"cpu_millicores": 1000, "memory_mib": 2048},
            "other_reserved": {"cpu_millicores": 0, "memory_mib": 0}, "allocations": [{
                "reservation_id": str(uuid4()), "instance_id": str(self.instance.pk), "team_id": str(self.team.pk),
                "challenge_id": str(self.challenge.pk), "reservation_status": "COMMITTED",
                "resources": {"cpu_millicores": 250, "memory_mib": 512}}]}}

    def test_cleanup_and_terminal_state_keep_reservation_until_broker_releases(self):
        for status in ("RUNNING", "STOPPING", "CLEANUP_PENDING", "CLEANED", "FAILED"):
            with self.subTest(status=status):
                self.instance.status = status
                self.instance.save(update_fields=["status"])
                target = deepcopy(self.target)
                enrich_allocations([target])
                record = target["resource_allocation"]["allocations"][0]
                self.assertEqual(record["status"], status)
                self.assertEqual(record["challenge_title"], "예약량 확인")
                self.assertEqual(record["team_name"], "배치 QA")
                self.assertEqual(record["resources"]["cpu_millicores"], 250)

    def test_missing_or_legacy_instance_keeps_unknown_reservation(self):
        record = self.target["resource_allocation"]["allocations"][0]
        record["instance_id"] = "legacy-instance"
        record["challenge_id"] = str(uuid4())
        enrich_allocations([self.target])
        self.assertIsNone(record["status"])
        self.assertIsNone(record["challenge_title"])
        self.assertEqual(record["resources"]["memory_mib"], 512)

    def test_mismatched_identity_does_not_borrow_local_status(self):
        record = self.target["resource_allocation"]["allocations"][0]
        record["team_id"] = str(uuid4())
        enrich_allocations([self.target])
        self.assertIsNone(record["status"])
        self.assertIsNone(record["team_name"])

    def test_enrichment_uses_bounded_queries_for_many_nodes(self):
        targets = [deepcopy(self.target) for _ in range(1000)]
        with self.assertNumQueries(4):
            enrich_allocations(targets)

    def test_old_broker_response_remains_uncollected(self):
        targets = [{"resource_target_id": str(uuid4())}]
        with self.assertNumQueries(0):
            enrich_allocations(targets)
        self.assertNotIn("resource_allocation", targets[0])
