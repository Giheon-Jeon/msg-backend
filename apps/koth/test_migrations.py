from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class PositiveScoreSolvedAtMigrationTests(TransactionTestCase):
    migrate_from = [
        ("accounts", "0003_team_koth_division"),
        ("koth", "0003_one_challenge_per_club"),
    ]
    migrate_to = [
        ("accounts", "0003_team_koth_division"),
        ("koth", "0004_positive_score_requires_solved_at"),
    ]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        latest_migrations = executor.loader.graph.leaf_nodes()
        self.addCleanup(self._restore_migrations, latest_migrations)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        Team = old_apps.get_model("accounts", "Team")
        KothClub = old_apps.get_model("koth", "KothClub")
        KothChallenge = old_apps.get_model("koth", "KothChallenge")
        KothSolve = old_apps.get_model("koth", "KothSolve")

        self.team = Team.objects.create(team_name="backfill-team")
        self.other_team = Team.objects.create(team_name="constraint-team")
        club = KothClub.objects.create(name="migration-club")
        self.challenge = KothChallenge.objects.create(
            club=club,
            title="migration-challenge",
            open_group=1,
            inbound_internal_token_hash="a" * 64,
        )
        self.solve = KothSolve.objects.create(
            team=self.team,
            challenge=self.challenge,
            earned_score=Decimal("40"),
            solved_at=None,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def _restore_migrations(self, targets):
        MigrationExecutor(connection).migrate(targets)

    def test_backfills_existing_rows_and_rejects_new_invalid_rows(self):
        KothSolve = self.apps.get_model("koth", "KothSolve")

        migrated = KothSolve.objects.get(pk=self.solve.pk)
        self.assertEqual(migrated.solved_at, migrated.created_at)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                KothSolve.objects.create(
                    team_id=self.other_team.pk,
                    challenge_id=self.challenge.pk,
                    earned_score=Decimal("1"),
                    solved_at=None,
                )

        KothSolve.objects.create(
            team_id=self.other_team.pk,
            challenge_id=self.challenge.pk,
            earned_score=Decimal("0"),
            solved_at=None,
        )
