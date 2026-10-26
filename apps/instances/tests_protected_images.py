import json
import tempfile
import uuid
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge
from apps.instances.models import (
    ChallengeRelease, ChallengeRuntimeConfig, DeploymentTest, Instance, ReleaseContainer,
)


class ProtectedImageInventoryTests(TransactionTestCase):
    def setUp(self):
        self.challenge = Challenge.objects.create(
            title="inventory", challenge_slug="web-inventory", flag_hash="not-exported",
            score=500, category="WEB", difficulty="EASY",
        )
        self.release = self.make_release(1)
        self.image = "ghcr.io/msg-ctf/challenges/web-inventory/app@sha256:" + "a" * 64
        ReleaseContainer.objects.create(
            release=self.release, name="app", image_ref=self.image,
            env={"FLAG": "do-not-export-this-value"}, secret_env={"TOKEN": "private-secret-reference"},
        )

    def make_release(self, version):
        return ChallengeRelease.objects.create(
            challenge=self.challenge, challenge_slug="web-inventory", version=version,
            registry_revision=version, isolation_profile="WEB", cpu_millicores=500,
            memory_mib=512, ephemeral_storage_mib=1024, note="private-note",
        )

    def export(self, **options):
        output = StringIO()
        call_command("export_protected_images", environment="isolated-test", deployment_sha="b" * 40, stdout=output, **options)
        return json.loads(output.getvalue()) if not options.get("output") else None

    def test_all_releases_and_all_containers_are_preserved_with_references(self):
        ChallengeRuntimeConfig.objects.create(challenge=self.challenge, current_release=self.release)
        old = self.make_release(2)
        ReleaseContainer.objects.create(release=old, name="helper", image_ref=self.image)
        future = self.make_release(3)
        ReleaseContainer.objects.create(release=future, name="app", image_ref=self.image.replace("a" * 64, "c" * 64))
        team = Team.objects.create(team_name="inventory-team")
        user = User.objects.create_user(login_id="inventory-user", nickname="inventory", team=team)
        Instance.objects.create(user=user, team=team, challenge=self.challenge, release=old, status="RUNNING")
        Instance.objects.create(user=user, team=team, challenge=self.challenge, release=old, status="FAILED")
        Instance.objects.create(user=user, team=team, challenge=self.challenge, status="CLEANUP_PENDING")
        DeploymentTest.objects.create(requested_by=user, request_id=uuid.uuid4(), release=old, expires_at=timezone.now())
        report = self.export()
        self.assertEqual(report["coverage"]["release_count"], 3)
        self.assertEqual(report["coverage"]["unique_image_count"], 2)
        refs = report["images"][0]["references"]
        self.assertEqual(len(refs), 2)
        by_release = {ref["release_id"]: ref for ref in refs}
        self.assertEqual(by_release[str(self.release.pk)]["references"][0]["reason"], "active_release")
        old_refs = by_release[str(old.pk)]["references"]
        self.assertEqual(len(old_refs), 3)
        self.assertTrue(all(ref["cleanup_verification"] == "unverified" for ref in old_refs))
        self.assertEqual(report["unresolved"][0]["issue"], "missing_release")
        self.assertEqual(report["images"][1]["references"][0]["references"], [])
        self.assertFalse(report["deletion_authorized"])

    def test_missing_images_and_identity_are_not_silently_omitted(self):
        self.make_release(2)
        self.release.challenge_slug = ""
        self.release.save(update_fields=["challenge_slug"])
        ReleaseContainer.objects.filter(release=self.release).update(image_ref="ghcr.io/user:secret@host/latest")
        report = self.export()
        self.assertEqual(report["images"], [])
        self.assertEqual({item["issue"] for item in report["unresolved"]}, {
            "no_containers", "missing_publication_identity", "invalid_or_unpinned_image_reference",
        })
        self.assertNotIn("user:secret", json.dumps(report))

    def test_only_allowlisted_metadata_is_exported_without_writes(self):
        with CaptureQueriesContext(connection) as queries, patch.object(
            ChallengeRelease, "save", side_effect=AssertionError("must not write")
        ):
            report = self.export()
        sql = [query["sql"].upper() for query in queries]
        self.assertFalse(any(query.startswith(("INSERT", "UPDATE", "DELETE", "ALTER", "DROP")) for query in sql))
        if connection.vendor == "postgresql":
            self.assertIn("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY", sql)
        payload = json.dumps(report)
        for value in ("do-not-export-this-value", "private-secret-reference", "private-note", "not-exported"):
            self.assertNotIn(value, payload)
        self.assertIn("Scheduler", payload)
        self.assertIn("KOTH", report["excluded_from_deletion_review"])
        self.assertEqual(ChallengeRelease.objects.count(), 1)

    def test_private_helper_and_cleaned_history_are_not_filtered_out(self):
        helper_image = self.image.replace("/app@", "/db@").replace("a" * 64, "d" * 64)
        ReleaseContainer.objects.create(release=self.release, name="db", image_ref=helper_image, ports=[])
        team = Team.objects.create(team_name="cleaned-team")
        user = User.objects.create_user(login_id="cleaned-user", nickname="cleaned", team=team)
        Instance.objects.create(user=user, team=team, challenge=self.challenge, release=self.release, status="CLEANED")
        report = self.export()
        self.assertEqual({row["image"] for row in report["images"]}, {self.image, helper_image})
        for image in report["images"]:
            self.assertEqual(image["references"][0]["references"][0]["status"], "CLEANED")

    def test_new_file_output_and_overwrite_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "inventory.json"
            self.export(output=str(target))
            original = target.read_bytes()
            self.assertEqual(json.loads(original)["images"][0]["image"], self.image)
            with self.assertRaises(CommandError):
                self.export(output=str(target))
            self.assertEqual(target.read_bytes(), original)

    def test_invalid_metadata_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("export_protected_images", environment="password=secret", deployment_sha="b" * 40)
        with self.assertRaises(CommandError):
            call_command("export_protected_images", environment="dev", deployment_sha="unknown")

    def test_empty_database_is_not_reported_as_global_non_use(self):
        self.challenge.delete()
        report = self.export()
        self.assertEqual(report["images"], [])
        self.assertTrue(report["unverified_scopes"])
        self.assertFalse(report["deletion_authorized"])
