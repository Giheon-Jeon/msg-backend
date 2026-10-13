import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from unittest import skipUnless

from django.core.cache import cache
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import (
    BoardChallenge,
    Cell,
    TeamBoardState,
    TeamCellCandidate,
    TeamChallengeAccess,
)
from apps.challenge.models import Challenge
from apps.challenge.services import hash_flag


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class BoardChallengeVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Cell.objects.create(cell_index=1, type=Cell.CellType.START, name="Start")
        cls.cell = Cell.objects.create(
            cell_index=2,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="Challenge",
        )
        cls.team = Team.objects.create(team_name="visibility-team")
        cls.user = User.objects.create_user(
            login_id="visibility-user",
            nickname="player",
            team=cls.team,
        )
        cls.state = TeamBoardState.objects.create(team=cls.team, position=cls.cell)
        cls.challenges = []
        for number in range(1, 5):
            challenge = Challenge.objects.create(
                title=f"Challenge {number}",
                category=Challenge.CategoryType.WEB,
                difficulty=Challenge.DifficultyType.EASY,
                score=100,
                flag_hash=hash_flag("MSG{visibility}"),
                is_published=True,
            )
            BoardChallenge.objects.create(challenge=challenge, challenge_number=number)
            cls.challenges.append(challenge)

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def candidate_ids(self):
        response = self.client.get("/api/v1/board/cell/current")
        self.assertEqual(response.status_code, 200)
        return [
            row["challenge_id"]
            for row in response.json()["data"]["challenge_candidates"]
        ]

    def open_challenge(self, challenge_id, key="visibility-open"):
        return self.client.post(
            "/api/v1/board/cell/open",
            {"challenge_id": str(challenge_id)},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_new_candidates_only_include_published_challenges_when_pool_is_small(self):
        published = self.challenges[0]
        Challenge.objects.exclude(pk=published.pk).update(is_published=False)

        self.assertEqual(self.candidate_ids(), [str(published.pk)])
        self.assertEqual(
            TeamCellCandidate.objects.filter(team=self.team).count(),
            1,
        )

    def test_no_candidates_when_all_challenges_are_unpublished(self):
        Challenge.objects.update(is_published=False)

        self.assertEqual(self.candidate_ids(), [])
        self.assertFalse(TeamCellCandidate.objects.filter(team=self.team).exists())

    def test_cached_unselected_candidate_is_removed_and_its_slot_refilled(self):
        original_ids = self.candidate_ids()
        hidden_id = original_ids[1]
        hidden_candidate = TeamCellCandidate.objects.get(
            team=self.team,
            challenge_id=hidden_id,
        )
        retained = list(
            TeamCellCandidate.objects.filter(team=self.team)
            .exclude(pk=hidden_candidate.pk)
            .values_list("pk", "display_order")
        )
        Challenge.objects.filter(pk=hidden_id).update(is_published=False)

        refreshed_ids = self.candidate_ids()

        self.assertEqual(len(refreshed_ids), 3)
        self.assertNotIn(hidden_id, refreshed_ids)
        self.assertFalse(
            TeamCellCandidate.objects.filter(pk=hidden_candidate.pk).exists()
        )
        for candidate_id, display_order in retained:
            candidate = TeamCellCandidate.objects.get(pk=candidate_id)
            self.assertEqual(candidate.display_order, display_order)
            self.assertIn(str(candidate.challenge_id), refreshed_ids)
        replacement = TeamCellCandidate.objects.get(
            team=self.team,
            display_order=hidden_candidate.display_order,
        )
        self.assertNotIn(str(replacement.challenge_id), original_ids)
        self.assertEqual(self.candidate_ids(), refreshed_ids)

    def test_cached_candidates_are_removed_even_without_replacements(self):
        self.assertEqual(len(self.candidate_ids()), 3)
        Challenge.objects.update(is_published=False)

        self.assertEqual(self.candidate_ids(), [])
        self.assertFalse(TeamCellCandidate.objects.filter(team=self.team).exists())

        republished = self.challenges[0]
        Challenge.objects.filter(pk=republished.pk).update(is_published=True)
        self.assertEqual(self.candidate_ids(), [str(republished.pk)])

    def test_open_rechecks_visibility_without_refreshing_candidates(self):
        challenge_id = self.candidate_ids()[0]
        Challenge.objects.filter(pk=challenge_id).update(is_published=False)

        response = self.open_challenge(challenge_id)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "CHALLENGE_NOT_CANDIDATE")
        self.assertFalse(TeamChallengeAccess.objects.filter(team=self.team).exists())
        self.state.refresh_from_db()
        self.assertIsNone(self.state.active_challenge_access_id)
        candidate = TeamCellCandidate.objects.get(
            team=self.team, challenge_id=challenge_id
        )
        self.assertEqual(candidate.status, TeamCellCandidate.Status.OFFERED)
        self.assertIsNone(candidate.selected_at)

        Challenge.objects.filter(pk=challenge_id).update(is_published=True)
        reopened = self.open_challenge(challenge_id)
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(
            TeamChallengeAccess.objects.filter(
                team=self.team, challenge_id=challenge_id
            ).count(),
            1,
        )

    def test_unpublishing_preserves_opened_access_but_excludes_other_teams(self):
        challenge_id = self.candidate_ids()[0]
        opened = self.open_challenge(challenge_id)
        self.assertEqual(opened.status_code, 200)
        access = TeamChallengeAccess.objects.get(
            team=self.team, challenge_id=challenge_id
        )
        Challenge.objects.filter(pk=challenge_id).update(is_published=False)

        replayed = self.open_challenge(challenge_id)
        self.assertEqual(replayed.status_code, 200)
        self.assertEqual(replayed.json(), opened.json())
        self.assertEqual(self.candidate_ids(), [])
        access.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(access.status, TeamChallengeAccess.Status.OPENED)
        self.assertEqual(self.state.active_challenge_access_id, access.pk)
        detail = self.client.get(f"/api/v1/challenges/{challenge_id}")
        self.assertEqual(detail.status_code, 200)
        submitted = self.client.post(
            f"/api/v1/challenges/{challenge_id}/submit",
            {"flag": "MSG{visibility}"},
            format="json",
        )
        self.assertEqual(submitted.status_code, 200)
        access.refresh_from_db()
        self.assertEqual(access.status, TeamChallengeAccess.Status.CLEARED)

        other_team = Team.objects.create(team_name="unopened-team")
        other_user = User.objects.create_user(
            login_id="unopened-user",
            nickname="other",
            team=other_team,
        )
        TeamBoardState.objects.create(team=other_team, position=self.cell)
        self.client.force_authenticate(other_user)
        other_candidate_ids = self.candidate_ids()
        self.assertEqual(len(other_candidate_ids), 3)
        self.assertNotIn(challenge_id, other_candidate_ids)
        rejected = self.open_challenge(challenge_id, key="other-team-open")
        self.assertEqual(rejected.status_code, 409)
        self.assertFalse(TeamChallengeAccess.objects.filter(team=other_team).exists())


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class BoardChallengeVisibilityConcurrencyTests(TransactionTestCase):
    @skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock regression")
    def test_open_waits_for_visibility_change_and_rechecks_after_commit(self):
        cache.clear()
        Cell.objects.create(cell_index=1, type=Cell.CellType.START, name="Start")
        cell = Cell.objects.create(
            cell_index=2,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="Challenge",
        )
        team = Team.objects.create(team_name="concurrent-visibility-team")
        user = User.objects.create_user(
            login_id="concurrent-visibility-user",
            nickname="player",
            team=team,
        )
        state = TeamBoardState.objects.create(team=team, position=cell)
        challenge = Challenge.objects.create(
            title="Concurrent challenge",
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=100,
            flag_hash="unused",
            is_published=True,
        )
        BoardChallenge.objects.create(challenge=challenge, challenge_number=1)
        client = APIClient()
        client.force_authenticate(user)
        current = client.get("/api/v1/board/cell/current")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(len(current.json()["data"]["challenge_candidates"]), 1)

        worker_pids = Queue()

        def open_in_another_connection():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '10s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    worker_pids.put(cursor.fetchone()[0])
                worker_client = APIClient()
                worker_client.force_authenticate(user)
                return worker_client.post(
                    "/api/v1/board/cell/open",
                    {"challenge_id": str(challenge.pk)},
                    format="json",
                    HTTP_IDEMPOTENCY_KEY="concurrent-visibility-open",
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                # Use the same challenge lock and update as the admin visibility API.
                locked = Challenge.objects.select_for_update().get(pk=challenge.pk)
                locked.is_published = False
                locked.save(update_fields=["is_published"])
                future = executor.submit(open_in_another_connection)
                worker_pid = worker_pids.get(timeout=5)
                blocked = False
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not future.done():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_blocking_pids(%s)", [worker_pid])
                        blocked = bool(cursor.fetchone()[0])
                    if blocked:
                        break
                    time.sleep(0.01)
            response = future.result(timeout=15)

        self.assertTrue(
            blocked, "The selection must wait for the visibility transaction"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "CHALLENGE_NOT_CANDIDATE")
        self.assertFalse(TeamChallengeAccess.objects.filter(team=team).exists())
        state.refresh_from_db()
        self.assertIsNone(state.active_challenge_access_id)
