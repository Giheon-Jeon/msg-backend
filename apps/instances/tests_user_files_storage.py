from unittest.mock import patch

from google.api_core.exceptions import Forbidden, ServiceUnavailable
from google.auth.exceptions import RefreshError

from apps.instances.models import PollerArtifact
from apps.instances.tests_user_files import (
    UserFilesTestBase, user_archive, user_artifact, user_manifest, workflow_run,
)
from apps.instances.user_files import poll_user_files_once


class UserFileStorageRecoveryTests(UserFilesTestBase):
    def check_storage_failure(self, exception_type):
        # Both the existence check and the upload can fail inside the GCS SDK.
        for index, operation in enumerate(("exists", "save")):
            with self.subTest(operation=operation):
                first_id, second_id = index * 2 + 1, index * 2 + 2
                first, second = user_artifact(first_id), user_artifact(second_id)
                archive = user_archive()
                manifest = user_manifest(archive, revision=first_id)
                with (
                    patch("apps.instances.poller.list_artifacts_by_suffix", side_effect=[[first, second], []]),
                    patch("apps.instances.user_files.get_workflow_run", side_effect=[
                        workflow_run(first_id), workflow_run(second_id), workflow_run(first_id),
                    ]),
                    patch("apps.instances.user_files.download_user_files_bundle", side_effect=[
                        (manifest, archive), (user_manifest(revision=second_id), None), (manifest, archive),
                    ]),
                ):
                    with (
                        patch(f"apps.instances.user_files.default_storage.{operation}",
                              side_effect=exception_type("sensitive-storage-error-detail")),
                        self.assertLogs("apps.instances.user_files", level="WARNING") as logs,
                    ):
                        failed = poll_user_files_once(token="storage-recovery-test")
                    self.assertEqual(failed["error"], 1)
                    self.assertEqual(failed["registered"], 1)
                    self.assertIsNone(PollerArtifact.objects.get(pk=first_id).processed_at)
                    self.assertIsNotNone(PollerArtifact.objects.get(pk=second_id).processed_at)
                    self.assertNotIn("sensitive-storage-error-detail", " ".join(logs.output))

                    recovered = poll_user_files_once(token="storage-recovery-test")
                    self.assertEqual(recovered["registered"], 1)
                    self.assertEqual(recovered["error"], 0)
                    self.assertIsNotNone(PollerArtifact.objects.get(pk=first_id).processed_at)

    def test_gcs_service_outage_continues_and_retries(self):
        self.check_storage_failure(ServiceUnavailable)

    def test_gcs_permission_failure_continues_and_retries(self):
        self.check_storage_failure(Forbidden)

    def test_gcs_credential_failure_continues_and_retries(self):
        self.check_storage_failure(RefreshError)

    def test_local_storage_failure_continues_and_retries(self):
        self.check_storage_failure(OSError)
