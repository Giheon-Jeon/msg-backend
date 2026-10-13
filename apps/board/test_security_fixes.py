"""보드 보안 수정 회귀 테스트 — BK-04(후보 생성 경합), BK-07(남은 시간 동적 계산)."""
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

import apps.board.services as svc
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import Cell, TeamCellCandidate
from apps.board.services import get_or_create_board_state

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class BK04CandidateRaceTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        call_command("seed_board", verbosity=0)
        self.team = Team.objects.create(team_name="race-team")
        User.objects.create_user(
            login_id="race_l",
            password="pw1234",
            nickname="L",
            team=self.team,
            is_leader=True,
        )
        User.objects.create_user(
            login_id="race_m",
            password="pw1234",
            nickname="M",
            team=self.team,
            is_leader=False,
        )
        st = get_or_create_board_state(self.team)
        cell = Cell.objects.filter(
            type=Cell.CellType.CHALLENGE, difficulty__isnull=False
        ).first()
        st.position_id = cell.cell_index
        st.dice_rolls_left = 1
        st.save(update_fields=["position", "dice_rolls_left"])

    def _tok(self, lid):
        c = APIClient()
        r = c.post(
            "/api/v1/auth/login", {"login_id": lid, "password": "pw1234"}, format="json"
        )
        return r.data["data"]["access_token"]

    def test_concurrent_first_read_no_500_with_widened_window(self):
        # 읽기-삽입 창을 강제로 벌려도 보드 상태 잠금으로 직렬화돼야 한다.
        real_sample = svc.random.sample

        def slow_sample(population, k):
            time.sleep(0.4)
            return real_sample(population, k)

        tokens = [self._tok("race_l"), self._tok("race_m")]

        def call(tok):
            c = APIClient()
            c.credentials(HTTP_AUTHORIZATION=f"Bearer {tok}")
            try:
                return c.get("/api/v1/board/cell/current").status_code
            finally:
                connections.close_all()

        svc.random.sample = slow_sample
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                codes = list(ex.map(call, tokens))
        finally:
            svc.random.sample = real_sample

        self.assertNotIn(500, codes)
        self.assertEqual(codes, [200, 200])
        self.assertEqual(TeamCellCandidate.objects.filter(team=self.team).count(), 3)

    @skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
    def test_different_teams_on_same_cell_do_not_lock_each_other(self):
        other_team = Team.objects.create(team_name="other-race-team")
        User.objects.create_user(
            login_id="other_race_l",
            password="pw1234",
            nickname="Other",
            team=other_team,
            is_leader=True,
        )
        other_state = get_or_create_board_state(other_team)
        other_state.position_id = self.team.board_state.position_id
        other_state.dice_rolls_left = 1
        other_state.save(update_fields=["position", "dice_rolls_left"])

        tokens = [self._tok("race_l"), self._tok("other_race_l")]
        rendezvous = Barrier(2)
        real_sample = svc.random.sample

        def synchronized_sample(population, k):
            # With an accidental lock on the shared Cell row, only one request
            # can reach this point and the barrier times out.
            rendezvous.wait(timeout=5)
            return real_sample(population, k)

        def call(token):
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            try:
                return client.get("/api/v1/board/cell/current").status_code
            finally:
                connections.close_all()

        svc.random.sample = synchronized_sample
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                codes = list(executor.map(call, tokens))
        finally:
            svc.random.sample = real_sample

        self.assertEqual(codes, [200, 200])
        self.assertEqual(
            TeamCellCandidate.objects.filter(team__in=[self.team, other_team]).count(),
            6,
        )


@override_settings(CACHES=LOCMEM)
class BK07RemainingSecondsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_board", verbosity=0)

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="rs-team")
        User.objects.create_user(
            login_id="rs_l",
            password="pw1234",
            nickname="L",
            team=self.team,
            is_leader=True,
        )
        self.state = get_or_create_board_state(self.team)
        cell = Cell.objects.filter(
            type=Cell.CellType.CHALLENGE, difficulty__isnull=False
        ).first()
        self.state.position_id = cell.cell_index
        self.state.dice_rolls_left = 1
        self.state.save(update_fields=["position", "dice_rolls_left"])

    def auth(self):
        r = self.client.post(
            "/api/v1/auth/login",
            {"login_id": "rs_l", "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {r.data['data']['access_token']}"
        )

    def test_open_remaining_seconds_matches_board_me(self):
        self.auth()
        cand = self.client.get("/api/v1/board/cell/current").data["data"][
            "challenge_candidates"
        ]
        r = self.client.post(
            "/api/v1/board/cell/open",
            {"challenge_id": cand[0]["challenge_id"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="rs-open",
        )
        open_remaining = r.data["data"]["remaining_seconds"]
        me = self.client.get("/api/v1/board/me").data["data"]["active_challenge"]
        # 개방 응답과 /board/me의 남은 시간이 같은 기준(마감-현재)으로 계산돼야 한다.
        self.assertLessEqual(abs(open_remaining - me["remaining_seconds"]), 2)
        self.assertLessEqual(open_remaining, 900)
