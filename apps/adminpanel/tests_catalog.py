import uuid

from django.contrib.auth.hashers import check_password, is_password_usable, make_password
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.board.models import BoardChallenge, Cell, TeamChallengeAccess
from apps.challenge.models import Challenge
from apps.instances.models import ChallengeRuntimeConfig, DeploymentTest, Instance
from apps.instances.services import serialize_instance
from apps.adminpanel.tests_challenge_files import AdminChallengeFilesTests
from apps.koth.models import KothClub
from apps.signature.drafts import EVENT_CLUBS, ensure_signature_drafts
from apps.signature.models import SignatureChallenge


class AdminPreviewTests(AdminChallengeFilesTests):
    # Reuse the release/file fixtures; inherited file-download checks also run.
    def test_preview_is_read_only_unpublished_and_does_not_reveal_execution_secrets(self):
        self.challenge.is_published = False
        self.challenge.save(update_fields=["is_published"])
        old, latest = self.bundle(1), self.bundle(2)
        config = self.select(1)
        container = config.current_release.containers.first()
        container.env = {"PRIVATE_CONFIG": "do-not-render"}
        container.secret_env = {"FLAG": "secret-ref"}
        container.save()
        before = (TeamChallengeAccess.objects.count(), Instance.objects.count(), DeploymentTest.objects.count())
        response = self.client.get(f"/api/v1/admin/challenges/{self.challenge.pk}/preview")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(data["files"][0]["file_id"], str(old.pk))
        self.assertNotEqual(data["files"][0]["file_id"], str(latest.pk))
        self.assertFalse(data["is_published"])
        self.assertTrue(data["requires_instance"])
        self.assertEqual(before, (TeamChallengeAccess.objects.count(), Instance.objects.count(), DeploymentTest.objects.count()))
        for secret in ("flag_hash", "secret-ref", "do-not-render", "object_key", "image_ref"):
            self.assertNotIn(secret, response.content.decode())
        self.assertIn("no-store", response["Cache-Control"])

    def test_preview_requires_admin_even_for_a_known_unpublished_id(self):
        url = f"/api/v1/admin/challenges/{self.challenge.pk}/preview"
        self.auth("player")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.credentials()
        self.assertEqual(self.client.get(url).status_code, 401)
        self.auth("root")
        self.assertEqual(self.client.get(f"/api/v1/admin/challenges/{uuid.uuid4()}/preview").status_code, 404)

    def test_separate_health_link_is_annotated_from_the_instances_original_release(self):
        release = self.create_legacy_release()
        release.healthcheck = {"container": "app", "port": 9090, "path": "/healthz"}
        release.save(update_fields=["healthcheck"])
        endpoints = [
            {"container_name": "app", "port": 8080, "service_url": "http://example.test:30001"},
            {"container_name": "app", "port": 9090, "service_url": "http://example.test:30002"},
        ]
        instance = Instance.objects.create(user=self.player, team=self.team, challenge=self.challenge, release=release, status="RUNNING", endpoints=endpoints)
        data = serialize_instance(instance)
        self.assertNotIn("healthcheck_path", data["endpoints"][0])
        self.assertEqual(data["endpoints"][1]["healthcheck_path"], "/healthz")
        instance.refresh_from_db()
        self.assertEqual(instance.endpoints, endpoints)
        instance.status = "STOPPED"
        self.assertEqual(serialize_instance(instance)["endpoints"], [])


class BoardReadinessTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(login_id="catalog-admin", password="pw", nickname="관리자", role=Role.ADMIN)
        self.client.force_authenticate(user=self.admin)
        self.url = "/api/v1/admin/board/readiness"

    def test_actual_cells_and_eligible_published_problems_determine_shortfalls(self):
        Cell.objects.all().delete()
        for index, difficulty in enumerate(("EASY", "EASY", "HARD")):
            Cell.objects.create(cell_index=index, type="CHALLENGE", difficulty=difficulty, line_number=1, name="문제")
        for number, (difficulty, published, enrolled) in enumerate((
            ("EASY", True, True), ("EASY", False, True), ("HARD", True, False), ("UNASSIGNED", False, False),
        ), start=1):
            challenge = Challenge.objects.create(title=str(number), category="WEB", difficulty=difficulty, score=100, flag_hash="unused", is_published=published)
            if enrolled:
                BoardChallenge.objects.create(challenge=challenge, challenge_number=number)
        response = self.client.get(self.url)
        data = response.data["data"]
        rows = {row["difficulty"]: row for row in data["difficulties"]}
        self.assertEqual(data["required_count"], 3)
        self.assertEqual(data["published_count"], 1)
        self.assertEqual(rows["EASY"]["shortfall_count"], 1)
        self.assertEqual(rows["HARD"]["shortfall_count"], 1)
        self.assertEqual(data["unenrolled_published_count"], 1)
        self.assertEqual(data["unassigned_count"], 1)
        self.assertFalse(data["ready"])
        self.assertIn("no-store", response["Cache-Control"])
        self.admin.role = Role.PARTICIPANT
        self.assertEqual(self.client.get(self.url).status_code, 403)


class SignatureDraftTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(login_id="draft-admin", password="pw", nickname="관리자", role=Role.ADMIN)
        self.client.force_authenticate(user=self.admin)

    def test_seed_preserves_existing_content_and_never_creates_an_answerable_draft(self):
        club = KothClub.objects.create(name="MJSEC")
        existing = SignatureChallenge.objects.create(club=club, title="출제자 원문", description="원문", flag_hash=make_password("real"), score=420, is_published=True)
        ensure_signature_drafts()
        ensure_signature_drafts()
        self.assertEqual(SignatureChallenge.objects.count(), len(EVENT_CLUBS))
        existing.refresh_from_db()
        self.assertEqual((existing.title, existing.description, existing.score, existing.is_published), ("출제자 원문", "원문", 420, True))
        self.assertTrue(check_password("real", existing.flag_hash))
        for draft in SignatureChallenge.objects.exclude(pk=existing.pk):
            self.assertFalse(draft.is_published)
            self.assertFalse(is_password_usable(draft.flag_hash))

    def test_draft_can_be_saved_in_parts_but_needs_content_and_flag_to_publish(self):
        ensure_signature_drafts()
        draft = SignatureChallenge.objects.first()
        url = f"/api/v1/admin/signatures/{draft.pk}"
        self.assertEqual(self.client.patch(url, {"title": "새 제목", "description": ""}, format="json").status_code, 200)
        blocked = self.client.patch(url + "/publish", {"is_published": True}, format="json")
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.data["code"], "SIGNATURE_NOT_READY")
        updated = self.client.patch(url, {"description": "실제 지문", "flag": "MSG{test-only}"}, format="json")
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data["data"]["missing_fields"], [])
        self.assertNotIn("MSG{test-only}", updated.content.decode())
        self.assertEqual(self.client.patch(url + "/publish", {"is_published": True}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(url, {"description": ""}, format="json").status_code, 400)
