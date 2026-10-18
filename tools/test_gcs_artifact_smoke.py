import unittest

from tools.gcs_artifact_smoke import participant_object_key, select_artifacts


class ArtifactSelectionTests(unittest.TestCase):
    def test_only_fixed_artifacts_are_selected(self):
        entries = [{"id": 1}, {"id": 2}, {"id": 3}]
        self.assertEqual(select_artifacts(entries, {1, 3}, []), [entries[0], entries[2]])

    def test_already_processed_artifacts_need_not_be_rediscovered(self):
        self.assertEqual(select_artifacts([], {1, 2}, [1, 2]), [])

    def test_missing_or_expired_target_fails_instead_of_substituting_latest(self):
        with self.assertRaisesRegex(RuntimeError, "missing or expired: \\[2\\]"):
            select_artifacts([{"id": 1}, {"id": 3}], {1, 2}, [])

    def test_partly_processed_discovery(self):
        self.assertEqual(select_artifacts([{"id": 2}], {1, 2}, [1]), [{"id": 2}])


class ArtifactObjectKeyTests(unittest.TestCase):
    def test_native_participant_key_is_allowed(self):
        self.assertTrue(participant_object_key(
            "participant-files/pwn-random6/455/" + "a" * 40 + "/" + "b" * 64 + ".zip",
        ))

    def test_foreign_or_unsafe_keys_are_not_tracked_for_cleanup(self):
        for key in ("", "/participant-files/x.zip", "prod/x.zip", "../x.zip", "participant-files/../x.zip"):
            with self.subTest(key=key):
                self.assertFalse(participant_object_key(key))


if __name__ == "__main__":
    unittest.main()
