import datetime
import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import Cell, TeamChallengeAccess
from apps.challenge.models import Challenge
from apps.challenge.services import hash_flag
from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    DeleteReason,
    Instance,
    InstanceLock,
    InstanceStatus,
    ReleaseContainer,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

ENDPOINTS = [
    {
        "container_name": "web",
        "port": 8080,
        "protocol": "HTTP",
        "service_url": "https://web.instance.example",
    },
    {
        "container_name": "pwn",
        "port": 31337,
        "protocol": "TCP",
        "service_url": "tcp://pwn.instance.example:31337",
    },
]


@override_settings(CACHES=LOCMEM, SCHEDULER_API_TOKEN="test-scheduler-token")
class InstanceLockTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="인스턴스팀")
        self.user = User.objects.create_user(
            login_id="instance-user",
            password="pw1234",
            nickname="인스턴스유저",
            team=self.team,
        )
        self.challenge = Challenge.objects.create(
            title="Web Basic",
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=500,
            description="인스턴스 테스트 문제",
            flag_hash=hash_flag("MSG{flag}"),
            is_published=True,
        )
        self.release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=1,
            registry_revision=1,
            challenge_slug="web-basic",
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
            isolation_profile="WEB",
            source_ref="refs/heads/main",
        )
        ReleaseContainer.objects.create(
            release=self.release,
            name="app",
            image_ref=(
                "ghcr.io/msg-ctf/challenges/web-basic/app@sha256:"
                + "a" * 64
            ),
            ports=[{"port": 8080, "public": True}],
        )
        ChallengeRuntimeConfig.objects.create(
            challenge=self.challenge,
            current_release=self.release,
        )
        self.cell = Cell.objects.create(
            cell_index=1,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="challenge-cell-1",
        )
        TeamChallengeAccess.objects.create(
            team=self.team,
            challenge=self.challenge,
            source_cell=self.cell,
        )
        self.auth()

    def auth(self):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": "instance-user", "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def create_challenge(self, title, is_published=True):
        return Challenge.objects.create(
            title=title,
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=500,
            description=title,
            flag_hash=hash_flag("MSG{flag}"),
            is_published=is_published,
        )

    def create_cell(self, cell_index, name):
        return Cell.objects.create(
            cell_index=cell_index,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name=name,
        )

    @patch("apps.instances.views.call_scheduler_create")
    def test_instance_create_creates_user_lock(self, call_scheduler_create):
        # 인스턴스 생성 요청은 사용자 단위 잠금 row를 만든 뒤 처리된다
        instance_id = uuid.uuid4()
        call_scheduler_create.return_value = {
            "instance_id": str(instance_id),
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.user_id),
            "challenge_id": str(self.challenge.challenge_id),
            "status": "REQUESTED",
            "service_url": None,
            "expires_at": (
                timezone.now() + datetime.timedelta(minutes=120)
            ).isoformat(),
            "hard_expires_at": (
                timezone.now() + datetime.timedelta(minutes=180)
            ).isoformat(),
            "replaced_instance_id": None,
        }

        res = self.client.post(
            "/api/v1/instances",
            {"challenge_id": str(self.challenge.challenge_id)},
            format="json",
        )

        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertTrue(InstanceLock.objects.filter(user=self.user).exists())

    @patch("apps.instances.views.call_scheduler_create")
    def test_instance_create_stores_and_returns_endpoints(self, call_scheduler_create):
        instance_id = uuid.uuid4()
        call_scheduler_create.return_value = {
            "instance_id": str(instance_id),
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.user_id),
            "challenge_id": str(self.challenge.challenge_id),
            "status": "RUNNING",
            "endpoints": ENDPOINTS,
            "expires_at": (timezone.now() + datetime.timedelta(minutes=120)).isoformat(),
            "hard_expires_at": (timezone.now() + datetime.timedelta(minutes=180)).isoformat(),
            "replaced_instance_id": None,
        }

        res = self.client.post(
            "/api/v1/instances",
            {"challenge_id": str(self.challenge.challenge_id)},
            format="json",
        )

        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data["data"]["host"], ENDPOINTS[0]["service_url"])
        self.assertEqual(res.data["data"]["endpoints"], ENDPOINTS)
        instance = Instance.objects.get(instance_id=instance_id)
        self.assertEqual(instance.host, ENDPOINTS[0]["service_url"])
        self.assertEqual(instance.endpoints, ENDPOINTS)

    @patch("apps.instances.services.call_scheduler_detail")
    def test_detail_sync_stores_endpoints(self, call_scheduler_detail):
        instance = Instance.objects.create(
            user=self.user,
            team=self.team,
            challenge=self.challenge,
            status=InstanceStatus.REQUESTED,
            release=self.release,
        )
        call_scheduler_detail.return_value = {
            "instance_id": str(instance.instance_id),
            "status": "RUNNING",
            "service_url": ENDPOINTS[0]["service_url"],
            "endpoints": ENDPOINTS,
        }

        from apps.instances.services import sync_instance_from_scheduler

        sync_instance_from_scheduler(instance, "Bearer test-scheduler-token")

        instance.refresh_from_db()
        self.assertEqual(instance.host, ENDPOINTS[0]["service_url"])
        self.assertEqual(instance.endpoints, ENDPOINTS)

    @patch("apps.instances.views.call_scheduler_active")
    def test_active_lookup_returns_endpoints(self, call_scheduler_active):
        instance = Instance.objects.create(
            user=self.user,
            team=self.team,
            challenge=self.challenge,
            status=InstanceStatus.REQUESTED,
            release=self.release,
        )
        call_scheduler_active.return_value = {
            "instance_id": str(instance.instance_id),
            "team_id": str(self.team.team_id),
            "challenge_id": str(self.challenge.challenge_id),
            "status": "RUNNING",
            "service_url": ENDPOINTS[0]["service_url"],
            "endpoints": ENDPOINTS,
        }

        res = self.client.get("/api/v1/teams/me/instances")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["host"], ENDPOINTS[0]["service_url"])
        self.assertEqual(res.data["data"]["endpoints"], ENDPOINTS)
        instance.refresh_from_db()
        self.assertEqual(instance.endpoints, ENDPOINTS)

    @patch("apps.instances.views.call_scheduler_create")
    def test_instance_create_rejects_unpublished_challenge(self, call_scheduler_create):
        challenge = self.create_challenge("Private Web", is_published=False)
        TeamChallengeAccess.objects.create(
            team=self.team,
            challenge=challenge,
            source_cell=self.create_cell(3, "Private Web"),
        )

        res = self.client.post(
            "/api/v1/instances",
            {"challenge_id": str(challenge.challenge_id)},
            format="json",
        )

        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "CHALLENGE_LOCKED")
        call_scheduler_create.assert_not_called()

    @patch("apps.instances.views.call_scheduler_active")
    def test_active_lookup_hides_endpoints_until_scheduler_reports_running(self, scheduler_active):
        instance = Instance.objects.create(
            user=self.user,
            team=self.team,
            challenge=self.challenge,
            status=InstanceStatus.RUNNING,
            release=self.release,
            host=ENDPOINTS[0]["service_url"],
            endpoints=ENDPOINTS,
        )

        for scheduler_status in (InstanceStatus.PROVISIONING, InstanceStatus.FAILED):
            with self.subTest(status=scheduler_status):
                scheduler_active.return_value = {
                    "instance_id": str(instance.instance_id),
                    "challenge_id": str(self.challenge.challenge_id),
                    "status": scheduler_status,
                    "service_url": ENDPOINTS[0]["service_url"],
                    "endpoints": ENDPOINTS,
                }

                response = self.client.get("/api/v1/teams/me/instances")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["data"]["status"], scheduler_status)
                self.assertIsNone(response.data["data"]["host"])
                self.assertEqual(response.data["data"]["ports"], [])
                self.assertEqual(response.data["data"]["endpoints"], [])
                instance.refresh_from_db()
                self.assertEqual(instance.status, scheduler_status)

    @patch("apps.instances.views.call_scheduler_create")
    def test_instance_create_rejects_unopened_challenge(self, call_scheduler_create):
        challenge = self.create_challenge("Locked Web")

        res = self.client.post(
            "/api/v1/instances",
            {"challenge_id": str(challenge.challenge_id)},
            format="json",
        )

        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "CHALLENGE_LOCKED")
        call_scheduler_create.assert_not_called()

    @patch("apps.instances.views.call_scheduler_create")
    def test_instance_create_ignores_other_team_access(self, call_scheduler_create):
        challenge = self.create_challenge("Other Team Web")
        other_team = Team.objects.create(team_name="Other Team")
        TeamChallengeAccess.objects.create(
            team=other_team,
            challenge=challenge,
            source_cell=self.create_cell(4, "Other Team Web"),
        )

        res = self.client.post(
            "/api/v1/instances",
            {"challenge_id": str(challenge.challenge_id)},
            format="json",
        )

        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "CHALLENGE_LOCKED")
        call_scheduler_create.assert_not_called()

    @patch("apps.instances.views.call_scheduler_create")
    def test_instance_create_marks_replaced_instance_stopping(self, call_scheduler_create):
        old_instance = Instance.objects.create(
            user=self.user,
            team=self.team,
            challenge=self.challenge,
            status=InstanceStatus.RUNNING,
            release=self.release,
        )
        instance_id = uuid.uuid4()
        call_scheduler_create.return_value = {
            "instance_id": str(instance_id),
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.user_id),
            "challenge_id": str(self.challenge.challenge_id),
            "status": "REQUESTED",
            "service_url": None,
            "expires_at": (timezone.now() + datetime.timedelta(minutes=120)).isoformat(),
            "hard_expires_at": (timezone.now() + datetime.timedelta(minutes=180)).isoformat(),
            "replaced_instance_id": str(old_instance.instance_id),
        }

        res = self.client.post(
            "/api/v1/instances",
            {"challenge_id": str(self.challenge.challenge_id)},
            format="json",
        )

        self.assertEqual(res.status_code, 202)
        old_instance.refresh_from_db()
        self.assertEqual(old_instance.status, InstanceStatus.STOPPING)
        self.assertEqual(
            old_instance.delete_reason,
            DeleteReason.REPLACED_BY_NEW_INSTANCE,
        )

    @patch("apps.instances.views.call_scheduler_reset")
    def test_instance_reset_marks_replaced_instance_stopping(self, call_scheduler_reset):
        old_instance = Instance.objects.create(
            user=self.user,
            team=self.team,
            challenge=self.challenge,
            status=InstanceStatus.RUNNING,
            release=self.release,
            endpoints=ENDPOINTS,
        )
        instance_id = uuid.uuid4()
        call_scheduler_reset.return_value = {
            "instance_id": str(instance_id),
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.user_id),
            "challenge_id": str(self.challenge.challenge_id),
            "status": "REQUESTED",
            "service_url": None,
            "expires_at": (timezone.now() + datetime.timedelta(minutes=120)).isoformat(),
            "hard_expires_at": (timezone.now() + datetime.timedelta(minutes=180)).isoformat(),
            "replaced_instance_id": str(old_instance.instance_id),
        }

        res = self.client.post(
            f"/api/v1/instances/{old_instance.instance_id}/reset",
            format="json",
        )

        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data["data"]["endpoints"], [])
        new_instance = Instance.objects.get(instance_id=instance_id)
        self.assertEqual(new_instance.endpoints, [])
        old_instance.refresh_from_db()
        self.assertEqual(old_instance.endpoints, ENDPOINTS)
        self.assertEqual(old_instance.status, InstanceStatus.STOPPING)
        self.assertEqual(
            old_instance.delete_reason,
            DeleteReason.REPLACED_BY_NEW_INSTANCE,
        )

    @patch("apps.instances.services.scheduler_request")
    def test_reset_keeps_original_release_healthcheck_after_activation(self, scheduler_request):
        healthcheck = {"container": "web", "port": 8080, "path": "/health"}
        self.release.healthcheck = healthcheck
        self.release.save(update_fields=["healthcheck"])
        old_instance = Instance.objects.create(
            user=self.user,
            team=self.team,
            challenge=self.challenge,
            status=InstanceStatus.RUNNING,
            release=self.release,
        )
        new_release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=2,
            registry_revision=2,
            challenge_slug=self.release.challenge_slug,
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
            isolation_profile="WEB",
            source_ref="refs/heads/main",
            healthcheck={"container": "web", "port": 8080, "path": "/new-health"},
        )
        ChallengeRuntimeConfig.objects.filter(challenge=self.challenge).update(
            current_release=new_release,
        )
        new_instance_id = uuid.uuid4()
        scheduler_request.return_value = {
            "instance_id": str(new_instance_id),
            "challenge_id": str(self.challenge.challenge_id),
            "registry_revision": self.release.registry_revision,
            "status": "REQUESTED",
        }

        response = self.client.post(
            f"/api/v1/instances/{old_instance.instance_id}/reset",
            format="json",
        )

        self.assertEqual(response.status_code, 202)
        scheduler_request.assert_called_once_with(
            "POST",
            f"/api/instances/{old_instance.instance_id}/reset",
            body={},
            auth_header="Bearer test-scheduler-token",
        )
        new_instance = Instance.objects.select_related("release").get(pk=new_instance_id)
        self.assertEqual(new_instance.release_id, self.release.pk)
        self.assertEqual(new_instance.release.healthcheck, healthcheck)
        self.assertEqual(new_instance.replaced_instance_id, old_instance.pk)
