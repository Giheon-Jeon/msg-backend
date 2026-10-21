"""Fixed real artifacts; all GitHub HTTP, database, storage and API calls stay real."""

import hashlib
import io
import json
import re
import zipfile
from contextlib import ExitStack
from unittest.mock import patch


BASELINE_ARTIFACT = 10955805059  # pwn-random6 revision 330
PUBLISH_ARTIFACTS = {11360925900, 11358877179}
FILE_ARTIFACTS = {
    11359944048: ("pwn-random6", 455, True, "PWN"),
    11358034774: ("misc-cashout", 449, False, "MISC"),
    11364086714: ("misc-hidden-order", 469, True, "MISC"),
    11363875661: ("forensic-is-this-job-legit", 466, False, "FORENSIC"),
}
SOURCE_SHA = "1dea73b652f9eeb98204c1fda7f66b9afb89505d"


def select_artifacts(artifacts, allowed, processed):
    selected = [item for item in artifacts if item.get("id") in allowed]
    missing = allowed - set(processed) - {item["id"] for item in selected}
    if missing:
        raise RuntimeError("Target artifacts missing or expired: " + str(sorted(missing)))
    return selected


def participant_object_key(name):
    return bool(re.fullmatch(
        r"participant-files/[a-z0-9]+(?:-[a-z0-9]+)*/[1-9][0-9]*/[0-9a-f]{40}/[0-9a-f]{64}\.zip",
        name,
    ))


def verify_github_artifacts(check, request, tokens, team, storage, object_keys, report):
    from django.conf import settings

    from apps.board.models import Cell, TeamChallengeAccess
    from apps.challenge.models import Challenge
    from apps.instances import poller, user_files
    from apps.instances.models import (
        ChallengeRelease, ChallengeRuntimeConfig, ChallengeUserFileBundle, PollerArtifact,
    )
    from apps.instances.releases import validate_release_payload

    check("github_repository", settings.RELEASE_POLL_REPO == "MSG-CTF/2026_MSG_CTF")
    check("github_api", settings.RELEASE_POLL_API_BASE.rstrip("/") == "https://api.github.com")
    check("github_source_ref", settings.RELEASE_POLL_SOURCE_REF == "refs/heads/main")
    check("github_isolated_prefix", bool(re.fullmatch(
        r"dev/participant-files/qa-github-[0-9]+-[0-9]+", settings.GCS_OBJECT_PREFIX,
    )))
    check("github_no_previous_poller_rows", not PollerArtifact.objects.exists())
    slugs = [item[0] for item in FILE_ARTIFACTS.values()]
    check("github_no_previous_challenges", not Challenge.objects.filter(challenge_slug__in=slugs).exists())
    challenges = {}
    for index, (slug, _, _, category) in enumerate(FILE_ARTIFACTS.values(), 2):
        challenge = Challenge.objects.create(
            challenge_slug=slug, title=slug, category=category,
            difficulty="EASY", score=500, flag_hash="unused", is_published=True,
        )
        challenges[slug] = challenge
        cell = Cell.objects.create(
            cell_index=index, type="CHALLENGE", difficulty="EASY", name="Artifact QA", line_number=1,
        )
        TeamChallengeAccess.objects.create(team=team, challenge=challenge, source_cell=cell)
    for slug in ("pwn-random6", "misc-cashout"):
        ChallengeRuntimeConfig.objects.create(challenge=challenges[slug])

    evidence = {"selection": "ID filter over real Poller HTTP discovery", "files": [], "releases": []}
    report["github_artifacts"] = evidence
    report["not_verified"] = [
        "Restoring an actual revision-330 ZIP (its user-files artifact is not collected)",
        "A real same-challenge present:true to present:false publication transition",
        "Deployed backend, Scheduler and Runtime",
    ]
    allowed = {"-publish-bundle": {BASELINE_ARTIFACT}, "-user-files-bundle": set(FILE_ARTIFACTS)}
    real_list = poller.list_artifacts_by_suffix
    real_request = poller.github_request
    real_register = poller.register_bundle
    real_files = user_files.register_user_files_bundle
    real_save = storage.save
    paths, payloads, manifests = [], {}, {}

    def discover(suffix, **kwargs):
        processed = PollerArtifact.objects.filter(
            artifact_id__in=allowed[suffix], processed_at__isnull=False,
        ).values_list("artifact_id", flat=True)
        return select_artifacts(real_list(suffix, **kwargs), allowed[suffix], processed)

    def traced_request(path, **kwargs):
        paths.append(path)
        return real_request(path, **kwargs)

    def register(payload, note=None):
        validated = validate_release_payload({"artifact": payload, "note": note})
        payloads[(validated["challenge_slug"], validated["registry_revision"])] = validated
        return real_register(payload, note=note)

    def register_files(manifest, archive, artifact):
        manifests[artifact["id"]] = user_files.validate_user_files_manifest(manifest)
        return real_files(manifest, archive, artifact)

    def save(name, content, *args, **kwargs):
        if not participant_object_key(name):
            raise RuntimeError("Unexpected artifact object key")
        # Track before writing so a failed upload is also eligible for cleanup.
        object_keys.append(name)
        stored = real_save(name, content, *args, **kwargs)
        if stored != name:
            if not participant_object_key(stored):
                raise RuntimeError("Unexpected saved artifact object key")
            object_keys.append(stored)
        return stored

    def active():
        return dict(ChallengeRuntimeConfig.objects.filter(
            challenge__challenge_slug__in=slugs,
        ).values_list("challenge__challenge_slug", "current_release__registry_revision"))

    def poll(count, files=False):
        summary = (user_files.poll_user_files_once if files else poller.poll_once)(
            token=settings.RELEASE_POLL_GITHUB_TOKEN,
        )
        check(f"github_poll_{'files' if files else 'releases'}_{count}", summary == {
            "registered": count, "duplicate": 0, "unmatched": 0, "invalid": 0, "error": 0,
        })
        return summary

    def activate(slug, revision):
        challenge = challenges[slug]
        release = ChallengeRelease.objects.get(challenge=challenge, registry_revision=revision)
        status, _, _ = request(
            f"/api/v1/admin/challenges/{challenge.pk}/releases/{release.pk}/activate", {}, tokens["admin"],
        )
        check(f"github_activate_{slug}_{revision}", status == 200)
        check(f"github_active_pointer_{slug}_{revision}", active()[slug] == revision)

    with ExitStack() as stack:
        stack.enter_context(patch.object(poller, "list_artifacts_by_suffix", discover))
        stack.enter_context(patch.object(poller, "github_request", traced_request))
        stack.enter_context(patch.object(user_files, "github_request", traced_request))
        stack.enter_context(patch.object(poller, "register_bundle", register))
        stack.enter_context(patch.object(user_files, "register_user_files_bundle", register_files))
        stack.enter_context(patch.object(storage, "save", save))
        evidence["baseline_poll"] = poll(1)
        check("github_baseline_no_auto_activation", all(value is None for value in active().values()))
        activate("pwn-random6", 330)
        baseline = active()
        allowed["-publish-bundle"] = set(PUBLISH_ARTIFACTS)
        evidence["release_poll"] = poll(2)
        evidence["file_poll"] = poll(4, files=True)
        check("github_new_revision_preserves_active", active() == baseline)

        for (slug, revision), expected in payloads.items():
            release = ChallengeRelease.objects.get(challenge=challenges[slug], registry_revision=revision)
            fields = (
                "challenge_slug", "registry_revision", "runtime_type", "architecture", "isolation_profile",
                "cpu_millicores", "memory_mib", "ephemeral_storage_mib", "healthcheck", "source_ref",
            )
            check(f"github_release_fields_{slug}_{revision}", all(
                getattr(release, key) == expected[key] for key in fields
            ))
            columns = ("name", "image_ref", "ports", "env", "env_rules")
            containers = list(release.containers.order_by("name").values(*columns))
            check(f"github_containers_{slug}_{revision}", containers == sorted([
                {key: item.get(key, {}) for key in columns} for item in expected["containers"]
            ], key=lambda item: item["name"]))
            check(f"github_digest_pinned_{slug}_{revision}", all(
                re.fullmatch(r"ghcr\.io/.+@sha256:[0-9a-f]{64}", item["image_ref"]) for item in containers
            ))
            evidence["releases"].append({
                "slug": slug, "revision": revision,
                "resources": {key: expected[key] for key in (
                    "cpu_millicores", "memory_mib", "ephemeral_storage_mib",
                )},
                "containers": [
                    {key: item[key] for key in ("name", "image_ref", "ports")} for item in containers
                ],
            })

        bundles = {}
        for artifact_id, (slug, revision, present, _) in FILE_ARTIFACTS.items():
            bundle = ChallengeUserFileBundle.objects.get(artifact_id=artifact_id)
            bundles[slug] = bundle
            check(f"github_manifest_{slug}", bundle.challenge_id == challenges[slug].pk
                  and bundle.registry_revision == revision and bundle.present == present
                  and bundle.source_sha == SOURCE_SHA)
            check(f"github_file_metadata_{slug}", all(
                getattr(bundle, key) == manifests[artifact_id][key] for key in (
                    "source_ref", "source_sha", "sha256", "size_bytes", "file_count", "uncompressed_size_bytes",
                )
            ))
            check(f"github_release_link_{slug}", bundle.release_id == (
                ChallengeRelease.objects.get(challenge=challenges[slug], registry_revision=revision).pk
                if slug in ("pwn-random6", "misc-cashout") else None
            ))
            if present:
                with storage.open(bundle.object_key, "rb") as stored:
                    raw = stored.read()
                check(f"github_stored_sha256_{slug}", hashlib.sha256(raw).hexdigest() == bundle.sha256)
                user_files.validate_user_files_archive(raw, manifests[artifact_id])
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    check(f"github_stored_zip_crc_{slug}", archive.testzip() is None)
            else:
                check(f"github_no_object_{slug}", not bundle.object_key)
            evidence["files"].append({
                "slug": slug, "revision": revision, "artifact_id": artifact_id, "present": present,
                "sha256": bundle.sha256, "size_bytes": bundle.size_bytes, "object_key": bundle.object_key,
            })

        before = (ChallengeRelease.objects.count(), ChallengeUserFileBundle.objects.count(), PollerArtifact.objects.count())
        writes_before = len(object_keys)
        paths.clear()
        evidence["repeat_release_poll"] = poll(0)
        evidence["repeat_file_poll"] = poll(0, files=True)
        check("github_repeat_no_rows", before == (
            ChallengeRelease.objects.count(), ChallengeUserFileBundle.objects.count(), PollerArtifact.objects.count(),
        ))
        check("github_repeat_no_storage_writes", len(object_keys) == writes_before)
        check("github_repeat_no_workflow_or_zip_download", not any(
            "/actions/runs/" in path or path.endswith("/zip") for path in paths
        ))
        check("github_repeat_preserves_active", active() == baseline)

    def inspect(slug, visible):
        challenge, bundle = challenges[slug], bundles[slug]
        path = f"/api/v1/challenges/{challenge.pk}"
        status, raw, _ = request(path, token=tokens["player"])
        check(f"github_detail_{slug}", status == 200)
        files = json.loads(raw)["data"]["files"]
        check(f"github_visible_{slug}_{visible}", [item["file_id"] for item in files] == (
            [str(bundle.pk)] if visible else []
        ))
        download = path + f"/files/{bundle.pk}/download"
        status, raw, headers = request(download, token=tokens["player"])
        if visible:
            check(f"github_http_sha256_{slug}", status == 200
                  and hashlib.sha256(raw).hexdigest() == bundle.sha256 and len(raw) == bundle.size_bytes)
            check(f"github_http_headers_{slug}", headers.get("Content-Type") == "application/zip"
                  and "attachment" in headers.get("Content-Disposition", "")
                  and "no-store" in headers.get("Cache-Control", ""))
            check(f"github_anonymous_blocked_{slug}", request(download)[0] == 401)
            check(f"github_locked_team_blocked_{slug}", request(download, token=tokens["outsider"])[0] == 403)
        else:
            check(f"github_direct_hidden_{slug}", status == 404)

    inspect("pwn-random6", False)
    inspect("misc-hidden-order", True)
    inspect("forensic-is-this-job-legit", False)
    activate("pwn-random6", 455)
    inspect("pwn-random6", True)
    activate("misc-cashout", 449)
    inspect("misc-cashout", False)
    activate("pwn-random6", 330)
    inspect("pwn-random6", False)
    inspect("misc-hidden-order", True)
    for bundle in bundles.values():
        if bundle.present:
            check("github_rollback_preserves_object_" + bundle.challenge.challenge_slug,
                  storage.exists(bundle.object_key))
    evidence["final_active"] = active()
