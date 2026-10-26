import threading
import time
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.challenge.models import Challenge
from apps.common.jwt import issue_access_token
from apps.instances.models import Instance, InstanceLock, InstanceStatus

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class BanDuringInstanceCreateTests(TransactionTestCase):
    """생성 요청이 스케줄러를 기다리는 사이 정지돼도 그 인스턴스까지 종료한다 (QA TC-60 · 61)."""

    @patch("apps.adminpanel.views.scheduler_auth_header", return_value={})
    @patch("apps.adminpanel.views.call_scheduler_delete")
    def test_instance_created_while_ban_runs_is_stopped(self, mock_delete, _auth):
        admin = User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자", role=Role.ADMIN
        )
        team = Team.objects.create(team_name="생성중팀")
        player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=team
        )
        challenge = Challenge.objects.create(
            title="웹", category="WEB", difficulty="EASY", score=100, flag_hash="x"
        )
        holding = threading.Event()
        created = {}

        def create_instance():
            # InstanceCreateView 처럼 사용자 잠금을 쥔 채 스케줄러 응답을 기다린 뒤 행을 만든다.
            with transaction.atomic():
                InstanceLock.objects.select_for_update().get_or_create(user=player)
                holding.set()
                time.sleep(0.5)
                created["instance"] = Instance.objects.create(
                    user=player,
                    team=team,
                    challenge=challenge,
                    status=InstanceStatus.RUNNING,
                )
            connection.close()

        worker = threading.Thread(target=create_instance)
        worker.start()
        holding.wait(5)

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(admin)}")
        res = client.post(
            f"/api/v1/admin/teams/{team.pk}/ban", {"ban_reason": "부정"}, format="json"
        )
        worker.join()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["stopped_instance_count"], 1)
        instance = Instance.objects.get(pk=created["instance"].pk)
        self.assertEqual(instance.status, InstanceStatus.STOPPING)
