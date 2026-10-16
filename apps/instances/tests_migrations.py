from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class LegacyReleaseMigrationTests(TransactionTestCase):
    def test_upgrade_from_poller_migration_adds_user_file_bundle(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(self.restore_schema, latest)
        target = [("instances", "0006_pollerartifact")]
        executor.migrate(target)

        apps = executor.loader.project_state(target).apps
        PollerArtifact = apps.get_model("instances", "PollerArtifact")
        PollerArtifact.objects.create(
            artifact_id=1,
            kind="RELEASE",
            payload={"name": "existing-publish-bundle"},
        )

        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        apps = executor.loader.project_state(latest).apps
        PollerArtifact = apps.get_model("instances", "PollerArtifact")
        UserFileBundle = apps.get_model("instances", "ChallengeUserFileBundle")
        self.assertTrue(PollerArtifact.objects.filter(artifact_id=1).exists())
        self.assertEqual(UserFileBundle.objects.count(), 0)

    def test_backfill_preserves_runtime_settings_and_pwn_isolation(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(self.restore_schema, latest)
        target = [("instances", "0002_instancelock")]
        executor.migrate(target)
        executor = MigrationExecutor(connection)
        old_apps = executor.loader.project_state(
            [node for node in executor.loader.applied_migrations
             if node in executor.loader.graph.nodes]
        ).apps
        Challenge = old_apps.get_model("challenge", "Challenge")
        RuntimeConfig = old_apps.get_model("instances", "ChallengeRuntimeConfig")
        challenge_ids = []
        for category in ("WEB", "PWN"):
            challenge = Challenge.objects.create(
                title=f"Legacy {category}", category=category, difficulty="EASY",
                score=500, description="Migration fixture", flag_hash="legacy-hash",
                is_published=True,
            )
            challenge_ids.append((challenge.pk, category))
            RuntimeConfig.objects.create(
                challenge=challenge, container_image="legacy/service:old",
                container_port=31337, architecture="ARM64", cpu_millicores=750,
                memory_mib=768, ephemeral_storage_mib=2048,
                ttl_minutes=45, hard_timeout_minutes=90,
            )

        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        apps = executor.loader.project_state(latest).apps
        RuntimeConfig = apps.get_model("instances", "ChallengeRuntimeConfig")
        Release = apps.get_model("instances", "ChallengeRelease")
        self.assertEqual(Release.objects.count(), 2)
        for challenge_id, category in challenge_ids:
            with self.subTest(category=category):
                config = RuntimeConfig.objects.get(challenge_id=challenge_id)
                release = config.current_release
                self.assertIsNotNone(release)
                self.assertEqual(release.registry_revision, 0)
                self.assertEqual(release.challenge_id, challenge_id)
                self.assertEqual(release.isolation_profile, category)
                self.assertEqual(release.architecture, "ARM64")
                self.assertEqual(release.cpu_millicores, 750)
                self.assertEqual(release.memory_mib, 768)
                self.assertEqual(release.ephemeral_storage_mib, 2048)
                self.assertEqual((config.ttl_minutes, config.hard_timeout_minutes), (45, 90))
                container = release.containers.get()
                self.assertEqual(container.image_ref, "legacy/service:old")
                self.assertEqual(container.ports, [{"port": 31337, "public": True}])

    @staticmethod
    def restore_schema(targets):
        # Restore the schema even when a migration assertion fails.
        MigrationExecutor(connection).migrate(targets)
