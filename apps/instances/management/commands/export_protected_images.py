import json
import re
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.instances.models import (
    ChallengeRelease, ChallengeRuntimeConfig, DeploymentTest, Instance,
)
from apps.instances.releases import IMAGE_REF_PATTERN


class Command(BaseCommand):
    help = "Export all registered release images for temporary protection; never authorizes deletion."

    def add_arguments(self, parser):
        parser.add_argument("--environment", required=True, help="Operator-confirmed DB environment label")
        parser.add_argument("--deployment-sha", required=True, help="Confirmed running Backend commit SHA")
        parser.add_argument("--output", help="New JSON file; omit to write JSON to stdout")

    def handle(self, *args, **options):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", options["environment"]):
            raise CommandError("environment must be a non-secret label (letters, digits, dot, dash, underscore).")
        if not re.fullmatch(r"[0-9a-f]{40}", options["deployment_sha"]):
            raise CommandError("deployment-sha must be the full 40-character lowercase commit SHA.")
        if connection.in_atomic_block:
            raise CommandError("Run outside an existing transaction to obtain an independent snapshot.")
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            report = self.collect(options)
        payload = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
        if options["output"]:
            try:
                with Path(options["output"]).open("x", encoding="utf-8") as stream:
                    stream.write(payload)
            except OSError:
                raise CommandError("Cannot create output file; use a new path in an existing directory.") from None
        else:
            self.stdout.write(payload, ending="")

    def collect(self, options):
        observed_at = timezone.now().isoformat()
        refs = defaultdict(list)
        unresolved = []
        configs = list(ChallengeRuntimeConfig.objects.order_by("pk").values("challenge_id", "current_release_id"))
        instances = list(Instance.objects.order_by("pk").values("instance_id", "challenge_id", "release_id", "status"))
        tests = list(DeploymentTest.objects.order_by("pk").values("test_id", "release_id", "status", "finished_at"))
        for config in configs:
            if config["current_release_id"]:
                refs[config["current_release_id"]].append({
                    "reason": "active_release", "challenge_id": str(config["challenge_id"]),
                })
        for kind, records, id_field in (("instance", instances, "instance_id"), ("deployment_test", tests, "test_id")):
            for record in records:
                reference = {"reason": kind + "_reference", "id": str(record[id_field]), "status": record["status"]}
                if record["status"] != "CLEANED":
                    reference["cleanup_verification"] = "unverified"
                if record["release_id"] is None:
                    unresolved.append({**reference, "issue": "missing_release"})
                else:
                    refs[record["release_id"]].append(reference)

        images = defaultdict(list)
        release_count = container_count = 0
        for release in ChallengeRelease.objects.order_by("pk").prefetch_related("containers"):
            release_count += 1
            references = refs.pop(release.pk, [])
            metadata = {
                "system": "Backend", "challenge_id": str(release.challenge_id),
                "challenge_slug": release.challenge_slug or None,
                "registry_revision": release.registry_revision, "release_id": str(release.pk),
                "version": release.version, "runtime_type": release.runtime_type,
                "architecture": release.architecture,
                "protection_reason": "registered_release_temporary_preservation",
                "references": references,
            }
            if not release.challenge_slug or release.registry_revision <= 0:
                unresolved.append({"release_id": str(release.pk), "issue": "missing_publication_identity"})
            containers = list(release.containers.all())
            if not containers:
                unresolved.append({"release_id": str(release.pk), "issue": "no_containers"})
            for container in sorted(containers, key=lambda row: row.name):
                container_count += 1
                ref = {**metadata, "container_name": container.name, "container_id": str(container.pk)}
                if not IMAGE_REF_PATTERN.fullmatch(container.image_ref):
                    # Malformed legacy values may contain credentials; do not echo them.
                    unresolved.append({**ref, "issue": "invalid_or_unpinned_image_reference"})
                else:
                    images[container.image_ref].append(ref)
        for release_id, references in refs.items():
            unresolved.append({"release_id": str(release_id), "references": references, "issue": "release_not_found"})
        return {
            "schema_version": 1, "observed_at": observed_at,
            "source": {
                "system": "Backend", "environment": options["environment"],
                "deployment_commit_sha": options["deployment_sha"],
                "identity_verification": "operator_supplied_not_independently_verified",
                "database_alias": connection.alias, "database_vendor": connection.vendor,
            },
            "coverage": {
                "database_scan_complete": True,
                "snapshot_consistency": "repeatable_read_read_only" if connection.vendor == "postgresql" else "unverified",
                "release_count": release_count, "container_count": container_count,
                "runtime_config_count": len(configs), "instance_count": len(instances),
                "deployment_test_count": len(tests), "unique_image_count": len(images),
                "unresolved_count": len(unresolved),
            },
            "deletion_authorized": False,
            "unverified_scopes": [
                "GHCR existence and pull permissions", "Scheduler queued/create/reset/retry jobs",
                "Runtime actual images and approved image policy", "Node pre-download state",
                "Cleanup completion in Scheduler/Runtime", "Approved rollback and planned deployment lists",
                "Rollback execution settings, secret availability and participant file integrity",
            ],
            "excluded_from_deletion_review": ["KOTH", "Windows", "platform images"],
            "rollback_preservation": "Inventory only; retain release settings, secrets and participant files separately. No data is modified.",
            "images": [{"image": image, "references": images[image]} for image in sorted(images)],
            "unresolved": unresolved,
        }
