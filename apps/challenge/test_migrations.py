from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from apps.board.services import get_opened_challenges_summary


class MergeOpenedChallengesMigrationTests(TransactionTestCase):
    migrate_from = [
        ("board", "0002_idempotencyrequest"),
        ("challenge", "0003_alter_challenge_category"),
    ]
    migrate_to = [("challenge", "0004_merge_opened_challenges_into_team_access")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        self.addCleanup(self.restore_migrations, executor.loader.graph.leaf_nodes())
        executor.migrate(self.migrate_from)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps

    def restore_migrations(self, targets):
        MigrationExecutor(connection).migrate(targets)

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_merges_solved_and_unsolved_rows_with_and_without_board_access(self):
        Team = self.old_apps.get_model("accounts", "Team")
        Challenge = self.old_apps.get_model("challenge", "Challenge")
        OpenedChallenge = self.old_apps.get_model("challenge", "OpenedChallenge")
        Solve = self.old_apps.get_model("challenge", "Solve")
        Cell = self.old_apps.get_model("board", "Cell")
        Access = self.old_apps.get_model("board", "TeamChallengeAccess")
        BoardChallenge = self.old_apps.get_model("board", "BoardChallenge")
        team = Team.objects.create(team_name="migration-team")
        opened_at = timezone.now() - timedelta(hours=1)
        board_opened_at = opened_at - timedelta(minutes=5)
        solved_at = opened_at + timedelta(minutes=2)
        cases = []

        for has_access, solved in (
            (False, False),
            (False, True),
            (True, False),
            (True, True),
        ):
            cell = Cell.objects.create(
                cell_index=len(cases) + 1,
                type="CHALLENGE",
                name="migration-cell",
            )
            challenge = Challenge.objects.create(
                title=f"migration-{has_access}-{solved}",
                category="WEB",
                difficulty="EASY",
                score=1000,
                flag_hash="a" * 64,
            )
            BoardChallenge.objects.create(
                challenge=challenge,
                challenge_number=len(cases) + 1,
                club_name="migration-club",
            )
            opened = OpenedChallenge.objects.create(
                team=team,
                challenge=challenge,
                cell_index=cell.pk,
                solve_deadline_at=opened_at + timedelta(minutes=15),
            )
            OpenedChallenge.objects.filter(pk=opened.pk).update(opened_at=opened_at)
            access = None
            if has_access:
                access = Access.objects.create(
                    team=team, challenge=challenge, source_cell=cell
                )
                Access.objects.filter(pk=access.pk).update(opened_at=board_opened_at)
            if solved:
                solve = Solve.objects.create(
                    team=team,
                    challenge=challenge,
                    earned_score=1000,
                    earned_mileage=30,
                )
                Solve.objects.filter(pk=solve.pk).update(solved_at=solved_at)
            cases.append((challenge.pk, cell.pk, access.pk if access else None, solved))

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        Access = apps.get_model("board", "TeamChallengeAccess")
        self.assertEqual(Access.objects.count(), len(cases))
        for challenge_id, cell_id, access_id, solved in cases:
            with self.subTest(has_access=access_id is not None, solved=solved):
                access = Access.objects.get(team_id=team.pk, challenge_id=challenge_id)
                self.assertEqual(access.source_cell_id, cell_id)
                self.assertEqual(access.status, "CLEARED" if solved else "OPENED")
                self.assertEqual(access.cleared_at, solved_at if solved else None)
                self.assertEqual(
                    access.opened_at, board_opened_at if access_id else opened_at
                )
                if access_id:
                    self.assertEqual(access.pk, access_id)

        # 현재 모델을 사용하는 서비스 호출 전에 DB 스키마도 최신 상태로 복원한다.
        self.migrate_to_latest()
        summary = get_opened_challenges_summary(team.pk)
        self.assertEqual(summary["total_count"], 4)
        self.assertEqual(summary["solved_count"], 2)
        items = {item["challenge_id"]: item for item in summary["opened_challenges"]}
        for challenge_id, cell_id, access_id, solved in cases:
            item = items[challenge_id]
            self.assertEqual(item["cell_index"], cell_id)
            self.assertEqual(item["club_name"], "migration-club")
            self.assertEqual(item["is_solved"], solved)
            self.assertEqual(item["solved_at"], solved_at if solved else None)
            self.assertEqual(
                item["opened_at"], board_opened_at if access_id else opened_at
            )

    def test_missing_board_metadata_stops_before_changes_and_can_be_repaired(self):
        Team = self.old_apps.get_model("accounts", "Team")
        Challenge = self.old_apps.get_model("challenge", "Challenge")
        OpenedChallenge = self.old_apps.get_model("challenge", "OpenedChallenge")
        Solve = self.old_apps.get_model("challenge", "Solve")
        Cell = self.old_apps.get_model("board", "Cell")
        Access = self.old_apps.get_model("board", "TeamChallengeAccess")
        BoardChallenge = self.old_apps.get_model("board", "BoardChallenge")
        team = Team.objects.create(team_name="missing-metadata-team")
        missing_ids = []
        opened_at = timezone.now() - timedelta(hours=1)

        # Include a valid row first, and missing metadata both with and without
        # legacy rows / existing board accesses. No access may be silently lost.
        for index, (has_legacy, has_access, has_meta) in enumerate(
            (
                (True, False, True),
                (True, False, False),
                (True, True, False),
                (False, True, False),
            ),
            start=1,
        ):
            cell = Cell.objects.create(
                cell_index=index, type="CHALLENGE", name=str(index)
            )
            challenge = Challenge.objects.create(
                title=f"metadata-{index}",
                category="WEB",
                difficulty="EASY",
                score=1000,
                flag_hash="a" * 64,
            )
            if has_legacy:
                opened = OpenedChallenge.objects.create(
                    team=team,
                    challenge=challenge,
                    cell_index=cell.pk,
                    solve_deadline_at=opened_at + timedelta(minutes=15),
                )
                OpenedChallenge.objects.filter(pk=opened.pk).update(opened_at=opened_at)
            if has_access:
                Access.objects.create(team=team, challenge=challenge, source_cell=cell)
            Solve.objects.create(
                team=team, challenge=challenge, earned_score=1000, earned_mileage=30
            )
            if has_meta:
                BoardChallenge.objects.create(
                    challenge=challenge, challenge_number=index
                )
            else:
                missing_ids.append(challenge.pk)
                # Also repair on assertion failure so cleanup can restore the schema.
                self.addCleanup(
                    BoardChallenge.objects.get_or_create,
                    challenge_id=challenge.pk,
                    defaults={"challenge_number": index, "club_name": "repaired-club"},
                )

        original_accesses = list(Access.objects.order_by("pk").values())
        original_opened = list(OpenedChallenge.objects.order_by("pk").values())
        with self.assertRaisesRegex(RuntimeError, "BoardChallenge") as error:
            MigrationExecutor(connection).migrate(self.migrate_to)
        for challenge_id in missing_ids:
            self.assertIn(str(challenge_id), str(error.exception))
        self.assertEqual(
            list(Access.objects.order_by("pk").values()), original_accesses
        )
        self.assertEqual(
            list(OpenedChallenge.objects.order_by("pk").values()), original_opened
        )
        self.assertNotIn(
            self.migrate_to[0],
            MigrationExecutor(connection).loader.applied_migrations,
        )

        for index, challenge_id in enumerate(missing_ids, start=2):
            BoardChallenge.objects.create(
                challenge_id=challenge_id,
                challenge_number=index,
                club_name="repaired-club",
            )
        MigrationExecutor(connection).migrate(self.migrate_to)
        # 현재 모델을 사용하는 서비스 호출 전에 DB 스키마도 최신 상태로 복원한다.
        self.migrate_to_latest()
        summary = get_opened_challenges_summary(team.pk)
        self.assertEqual(summary["total_count"], 4)
        self.assertEqual(summary["solved_count"], 3)
        self.assertCountEqual(
            [item["challenge_id"] for item in summary["opened_challenges"]],
            Challenge.objects.values_list("pk", flat=True),
        )
