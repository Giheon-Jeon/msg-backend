import copy
import hashlib
import json
import tempfile
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.adminpanel.models import AdminEvent
from apps.instances.models import PollerArtifact
from apps.instances.releases import validate_release_payload
from apps.instances.tests_releases import artifact_payload
from apps.instances.tests_user_files import user_archive, user_manifest
from apps.instances.user_files import validate_user_files_manifest
from . import publications
from .environment import EnvironmentError, create_version, decode_settings
from .files import register_koth_user_files
from .models import KothChallenge, KothClub, KothEnvironmentVersion, KothPublication, KothScorePeriod
from . import tests_environment as env_tests

SHA, IMAGE = env_tests.SHA, env_tests.IMAGE

NEW = "c" * 40
SLUG = "koth-test"
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def artifact(identifier, kind, sha=NEW, run=88):
    return {"id": identifier, "name": f"{SLUG}-{run}-1-abcdef-{kind}-bundle", "expired": False,
            "created_at": "2026-10-26T00:00:00Z", "workflow_run": {"id": run, "head_branch": "main", "head_sha": sha}}


def prepared(revision=2):
    archive = user_archive({"participant/readme.txt": b"original author file"})
    release = artifact_payload(revision=revision, slug=SLUG, source_sha=NEW, containers=[{
        "name": "service", "image": IMAGE.replace("a" * 64, "b" * 64), "ports": [{"port": 8080, "public": True}]}])["artifact"]
    manifest = user_manifest(archive, revision=revision, slug=SLUG, source_sha=NEW)
    return {"artifact_id": 200 + revision * 2, "slug": SLUG, "source_sha": NEW, "revision": revision, "run_id": 88,
            "metadata": {"title": "출제자 새 제목", "description": "출제자가 쓴 지문\n두 번째 줄", "author": "Author", "dynamic": True},
            "release": release, "validated": validate_release_payload({"artifact": release}),
            "file_manifest": manifest, "file_validated": validate_user_files_manifest(manifest), "archive": archive,
            "artifacts": {"publish": artifact(200 + revision * 2, "publish"), "user-files": artifact(201 + revision * 2, "user-files")},
            "fingerprint": hashlib.sha256(str(revision).encode()).hexdigest(), "environment_candidates": {"service": [
                {"name": "PORT", "value": "9090", "kind": "env", "sensitive": False, "resolution": "configured"},
                {"name": "INTERNAL_TOKEN", "value": "author-placeholder", "kind": "secret", "sensitive": True, "resolution": "configured"},
                {"name": "NEW_SECRET", "value": "private-author-value", "kind": "secret", "sensitive": True, "resolution": "configured"},
                {"name": "OPTION", "value": None, "kind": "secret", "sensitive": False, "resolution": "unset"}]}}


@override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()], CACHES=LOCMEM,
                   STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}})
class PublicationTests(TestCase):
    entry = staticmethod(env_tests.EnvironmentTests.entry)
    apply = env_tests.EnvironmentTests.apply
    status = env_tests.EnvironmentTests.status

    def setUp(self):
        env_tests.EnvironmentTests.setUp(self)
        cache.clear()
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.media.name)
        self.override.enable(); self.addCleanup(self.override.disable)
        self.path = f"/api/v1/admin/koth/challenges/{self.challenge.pk}"
        self.plan = prepared()
        baseline = KothPublication.objects.create(challenge=self.challenge, registry_revision=1, source_sha=SHA,
            artifact_id=190, run_id=87, fingerprint="a" * 64,
            metadata={"title": "Game", "description": "", "author": "Author", "dynamic": True},
            runtime_profile=publications.runtime_profile(self.plan["validated"]), created_by="baseline")
        self.source.publication = baseline; self.source.save(update_fields=["publication"])
        self.challenge.active_publication = baseline; self.challenge.save(update_fields=["active_publication"])

    def register(self, plan=None):
        return publications.register(self.challenge, plan or self.plan, self.admin.login_id)[0]

    def test_staging_preserves_running_image_metadata_and_file_source(self):
        publication = self.register()
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.source_revision, SHA)
        self.assertEqual(self.challenge.title, "Game")
        self.config.refresh_from_db()
        self.assertEqual(self.config.applied_version, self.source)
        row = publication.environment_versions.get()
        entries = decode_settings(row)["containers"][0]["entries"]
        self.assertEqual(entries["PORT"]["value"], "9090")
        self.assertEqual(entries["INTERNAL_TOKEN"]["value"], "test-runtime-internal-value")
        self.assertIsNone(entries["OPTION"]["value"])
        self.assertFalse(entries["OPTION"]["required"])
        self.assertNotIn("private-author-value", row.encrypted_settings)
        response = self.client.get(self.path + "/publications")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("private-author-value", json.dumps(response.data))

    def test_operator_override_is_preserved_but_new_default_is_collected(self):
        values = decode_settings(self.source)
        values["containers"][0]["entries"]["PORT"].update(value="8088", operator_override=True)
        create_version(self.challenge, values, SHA, "operator", uuid.uuid4(), "9" * 64, based_on=self.source)
        publication = self.register()
        entries = decode_settings(publication.environment_versions.get())["containers"][0]["entries"]
        self.assertEqual(entries["PORT"]["value"], "8088")
        self.assertEqual(entries["NEW_SECRET"]["value"], "private-author-value")

    def test_duplicate_import_is_idempotent_and_conflicting_revision_is_rejected(self):
        first = self.register()
        self.assertEqual(first.pk, self.register().pk)
        self.assertEqual(KothPublication.objects.count(), 2)
        self.assertEqual(KothEnvironmentVersion.objects.count(), 2)
        self.plan["fingerprint"] = "0" * 64
        with self.assertRaises(EnvironmentError): self.register()

    def test_preview_is_bound_to_actor_challenge_source_and_current_settings(self):
        token = publications.preview_data(self.challenge, self.plan, self.admin.login_id)["preview_token"]
        with self.assertRaises(EnvironmentError):
            publications.register(self.challenge, self.plan, "other-admin", preview_token=token)
        create_version(self.challenge, self.settings, SHA, "operator", uuid.uuid4(), "1" * 64, based_on=self.source)
        with self.assertRaises(EnvironmentError):
            publications.register(self.challenge, self.plan, self.admin.login_id, preview_token=token)
        self.assertEqual(KothPublication.objects.count(), 1)

    def test_applied_ack_atomically_switches_source_metadata_and_participant_files(self):
        publication = self.register(); version = publication.environment_versions.get()
        response = self.apply(version)
        self.assertEqual(response.status_code, 200, response.data)
        application_id = response.data["data"]["application_id"]
        self.challenge.refresh_from_db(); self.assertEqual(self.challenge.source_revision, SHA)
        before = self.client.get(self.path + "/files/download")
        self.assertEqual(before.status_code, 404)
        response = self.status(application_id, version)
        self.assertEqual(response.status_code, 200, response.data)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.source_revision, NEW)
        self.assertEqual(self.challenge.title, self.plan["metadata"]["title"])
        after = self.client.get(self.path + "/files/download")
        self.assertEqual(after.status_code, 200)
        self.assertEqual(hashlib.sha256(b"".join(after.streaming_content)).hexdigest(), self.plan["file_validated"]["sha256"])

    def test_failed_apply_and_late_ack_do_not_change_old_metadata_or_files(self):
        publication = self.register(); version = publication.environment_versions.get()
        response = self.apply(version); application = response.data["data"]["application_id"]
        self.assertEqual(self.status(application, version, "FAILED").status_code, 200)
        self.assertEqual(self.status(application, version, "APPLIED").status_code, 409)
        self.challenge.refresh_from_db(); self.assertEqual(self.challenge.source_revision, SHA)
        replacement = self.apply(self.source)
        self.assertEqual(replacement.status_code, 200)
        self.assertEqual(self.status(application, version).status_code, 409)
        self.challenge.refresh_from_db(); self.assertEqual(self.challenge.title, "Game")

    def test_missing_and_changed_runtime_profiles_block_activation(self):
        self.plan["environment_candidates"]["service"].append({"name": "MISSING", "value": None, "kind": "secret", "sensitive": True, "resolution": "needs_value"})
        publication = self.register()
        self.assertTrue(publication.missing)
        self.assertEqual(self.apply(publication.environment_versions.get()).status_code, 409)

    def test_registered_runtime_profile_changes_require_operator_review(self):
        first = self.register()
        self.challenge.source_revision = NEW; self.challenge.save(update_fields=["source_revision"])
        next_plan = prepared(3); next_plan["validated"]["cpu_millicores"] += 500
        next_publication = self.register(next_plan)
        self.assertIn("runtime_profile_changed", [item["reason"] for item in next_publication.missing])
        self.assertEqual(self.apply(next_publication.environment_versions.get()).status_code, 409)

    def test_active_game_cannot_apply_new_publication(self):
        publication = self.register()
        self.challenge.status = "ACTIVE"; self.challenge.save(update_fields=["status"])
        self.assertEqual(self.apply(publication.environment_versions.get()).status_code, 409)

    def test_missing_value_can_be_filled_in_admin_without_losing_publication_binding(self):
        self.plan["environment_candidates"]["service"].append({"name": "MISSING", "value": None, "kind": "secret", "sensitive": True, "resolution": "needs_value"})
        publication = self.register(); version = publication.environment_versions.get()
        self.assertEqual(self.apply(version).status_code, 409)
        body = {"base_version_id": str(version.pk), "container": "service", "method": "file", "content": "MISSING=resolved", "request_id": str(uuid.uuid4())}
        response = self.client.post(self.base + "/versions", body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        repaired = KothEnvironmentVersion.objects.get(pk=response.data["data"]["version_id"])
        self.assertEqual(repaired.publication_id, publication.pk)
        self.assertEqual(publications.serialize(publication)["missing"], [])
        self.assertEqual(self.apply(repaired).status_code, 200)

    def test_environment_edit_does_not_overwrite_manually_edited_description(self):
        self.challenge.description = "관리자가 확인한 지문"; self.challenge.save()
        response = self.apply(self.source)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.status(response.data["data"]["application_id"], self.source).status_code, 200)
        self.challenge.refresh_from_db(); self.assertEqual(self.challenge.description, "관리자가 확인한 지문")

    def test_old_publication_can_be_rolled_back_after_new_source_was_applied(self):
        publication = self.register(); version = publication.environment_versions.get()
        response = self.apply(version)
        self.assertEqual(self.status(response.data["data"]["application_id"], version).status_code, 200)
        response = self.apply(self.source)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.status(response.data["data"]["application_id"], self.source).status_code, 200)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.source_revision, SHA)
        self.assertEqual(self.challenge.title, "Game")
        self.assertEqual(self.challenge.active_publication_id, self.source.publication_id)

    def test_newer_files_from_same_source_do_not_replace_selected_publication_files(self):
        publication = self.register(); version = publication.environment_versions.get()
        response = self.apply(version)
        self.assertEqual(self.status(response.data["data"]["application_id"], version).status_code, 200)
        archive = user_archive({"other.txt": b"later build"})
        later = validate_user_files_manifest(user_manifest(archive, revision=3, slug=SLUG, source_sha=NEW))
        register_koth_user_files(later, archive, artifact(700, "user-files"))
        self.challenge.refresh_from_db(); self.challenge.status = "ACTIVE"; self.challenge.save(update_fields=["status"])
        self.client.force_authenticate(self.participant)
        response = self.client.get("/api/v1/koth/clubs")
        selected = response.data["data"]["clubs"][0]["challenges"][0]["file"]
        self.assertEqual(selected["sha256"], self.plan["file_validated"]["sha256"])
        response = self.client.get(f"/api/v1/koth/challenges/{self.challenge.pk}/file")
        self.assertEqual(hashlib.sha256(b"".join(response.streaming_content)).hexdigest(), selected["sha256"])

    def test_admin_files_preview_is_source_pinned_and_cannot_select_another_game(self):
        publication = self.register()
        response = self.client.get(self.path + "/files", {"publication_id": str(publication.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["files"][0]["path"], "participant/readme.txt")
        self.assertEqual(response.data["data"]["bundle"]["source_revision"], NEW)
        other = KothChallenge.objects.create(club=KothClub.objects.create(name="Other"), title="Other", open_group=2,
            repository_slug="koth-other", inbound_internal_token_hash="2" * 64)
        for suffix in ("/files", "/files/download"):
            response = self.client.get(f"/api/v1/admin/koth/challenges/{other.pk}" + suffix, {"publication_id": str(publication.pk)})
            self.assertEqual(response.status_code, 404)

    def test_files_mode_none_hides_participant_metadata_and_download(self):
        self.challenge.source_revision = NEW; self.challenge.status = "ACTIVE"; self.challenge.save()
        self.register()
        response = self.client.put(self.path + "/files", {"mode": "NONE", "reason": "운영 확인"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.client.force_authenticate(self.participant)
        self.assertEqual(self.client.get(f"/api/v1/koth/challenges/{self.challenge.pk}/file").status_code, 404)
        response = self.client.get("/api/v1/koth/clubs")
        self.assertIsNone(response.data["data"]["clubs"][0]["challenges"][0]["file"])

    def test_admin_detail_exposes_scheduled_description_without_changing_public_gate(self):
        self.challenge.description = "공개 전 지문"; self.challenge.save()
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["description"], "공개 전 지문")
        self.client.force_authenticate(self.participant)
        response = self.client.get("/api/v1/koth/clubs")
        self.assertEqual(response.data["data"]["clubs"][0]["challenges"][0]["description"], "")

    def test_all_admin_import_routes_deny_participant_and_anonymous(self):
        endpoints = ["/api/v1/admin/koth/challenges", self.path, self.path + "/files", self.path + "/publications", "/api/v1/admin/koth/repository", "/api/v1/admin/koth/published"]
        for actor, status in ((self.participant, 403), (None, 401)):
            self.client.force_authenticate(actor)
            for path in endpoints: self.assertEqual(self.client.get(path).status_code, status, path)
            self.assertEqual(self.client.post(self.path + "/publications/import", {}, format="json").status_code, status)

    def test_reason_validation_and_partial_patch_preserve_schedule(self):
        self.challenge.opened_at = timezone.now(); self.challenge.closed_at = timezone.now() + timezone.timedelta(hours=4); self.challenge.save()
        old = (self.challenge.opened_at, self.challenge.closed_at)
        for why in ("", "  ", [], 2, "a" * 501):
            self.assertEqual(self.client.patch(self.path, {"title": "New", "reason": why}, format="json").status_code, 400)
        response = self.client.patch(self.path, {"description": "지문 수정", "reason": "출제자 지문 반영"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.challenge.refresh_from_db(); self.assertEqual((self.challenge.opened_at, self.challenge.closed_at), old)
        self.assertEqual(self.challenge.source_revision, SHA)
        self.assertIn("출제자 지문 반영", AdminEvent.objects.latest("created_at").message)

    def test_create_one_challenge_per_club_and_token_returned_only_once(self):
        body = {"club_name": "New club", "title": "New", "open_group": 2, "reason": "신규 등록"}
        response = self.client.post("/api/v1/admin/koth/challenges", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        token = response.data["data"]["internal_token"]
        key = response.data["data"]["koth_challenge_id"]
        row = KothChallenge.objects.get(pk=key)
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), row.inbound_internal_token_hash)
        self.assertNotIn(token, json.dumps(self.client.get(f"/api/v1/admin/koth/challenges/{key}").data))
        self.assertEqual(self.client.post("/api/v1/admin/koth/challenges", body, format="json").status_code, 409)

    def test_unregistered_destinations_cannot_exfiltrate_platform_tokens(self):
        response = self.client.patch(self.path, {"score_api_url": "https://attacker.example/", "score_api_token_env": "KOTH_JENGA_TOKEN", "reason": "확인"}, format="json")
        self.assertEqual(response.status_code, 409)
        response = self.client.patch(self.path, {"score_api_token_env": "RELEASE_POLL_GITHUB_TOKEN", "reason": "확인"}, format="json")
        self.assertEqual(response.status_code, 400)
        with patch("apps.koth.services._request_scores") as fetch:
            response = self.client.post(self.path + "/connection_test", {"score_api_url": "http://169.254.169.254"}, format="json")
            self.assertEqual(response.status_code, 409); fetch.assert_not_called()

    def test_pending_periods_do_not_crash_and_applied_periods_cannot_be_retried(self):
        from .services import current_period
        row = KothScorePeriod.objects.create(challenge=self.challenge, period_id=current_period() - timezone.timedelta(minutes=30))
        response = self.client.get(self.path + "/periods")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["periods"][0]["results"], [])
        row.status = "APPLIED"; row.save()
        response = self.client.post(self.path + "/periods/" + row.period_id.isoformat().replace("+00:00", "Z") + "/retry")
        self.assertEqual(response.status_code, 409, response.data)

    def test_import_api_previews_then_registers_without_auto_activation(self):
        with patch("apps.koth.publications.prepare", return_value=self.plan):
            url = self.path + "/publications/import"
            response = self.client.post(url, {"artifact_id": self.plan["artifact_id"]}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertNotIn("private-author-value", json.dumps(response.data))
            body = {"artifact_id": self.plan["artifact_id"], "preview_token": response.data["data"]["preview_token"], "reason": "발행본 확인"}
            response = self.client.post(url, body, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(self.client.post(url, body, format="json").status_code, 200)
            self.challenge.refresh_from_db(); self.assertEqual(self.challenge.source_revision, SHA)

    def test_catalog_groups_only_koth_and_filters_registered_versions(self):
        artifacts = [artifact(1, "publish"), artifact(2, "user-files")]
        run = {"status": "completed", "conclusion": "success"}
        with patch.object(publications.shared, "_json", side_effect=lambda path: {"artifacts": artifacts} if "artifacts?" in path else run):
            response = self.client.get("/api/v1/admin/koth/published", {"slug": SLUG})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(len(response.data["data"]["items"]), 1)
            self.assertTrue(response.data["data"]["items"][0]["has_files_artifact"])
            self.assertEqual(response.data["data"]["items"][0]["workflow_state"], "success")

    def test_existing_release_poller_collects_koth_without_activating_it(self):
        from apps.instances import poller
        item = self.plan["artifacts"]["publish"]
        PollerArtifact.objects.create(artifact_id=item["id"], kind="RELEASE", payload=item)
        run = {"id": 88, "status": "completed", "conclusion": "success", "head_branch": "main", "head_sha": NEW}
        with patch.object(poller, "list_bundle_artifacts", return_value=[item]), patch.object(poller, "get_workflow_run", return_value=run), \
                patch.object(poller, "download_bundle", return_value=self.plan["release"]), patch.object(publications, "prepare", return_value=self.plan):
            self.assertEqual(poller.poll_once()["registered"], 1)
            self.assertEqual(poller.poll_once()["duplicate"], 1)
        self.challenge.refresh_from_db(); self.assertEqual(self.challenge.source_revision, SHA)
        self.assertIsNotNone(PollerArtifact.objects.get(pk=item["id"]).processed_at)
        self.assertEqual(KothPublication.objects.count(), 2)


@override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()], CACHES=LOCMEM,
                   STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}})
class ConcurrentPublicationTests(TransactionTestCase):
    setUp = PublicationTests.setUp
    entry = staticmethod(env_tests.EnvironmentTests.entry)

    def test_two_collectors_register_one_publication_and_one_environment_version(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        barrier = Barrier(2)
        def collect():
            close_old_connections()
            try:
                challenge = KothChallenge.objects.get(pk=self.challenge.pk)
                barrier.wait(timeout=10)
                publication, created = publications.register(challenge, copy.deepcopy(self.plan), "publication-poller")
                return str(publication.pk), created
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: collect(), range(2)))
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(KothPublication.objects.count(), 2)
        self.assertEqual(KothEnvironmentVersion.objects.count(), 2)
        self.challenge.refresh_from_db(); self.assertEqual(self.challenge.source_revision, SHA)
