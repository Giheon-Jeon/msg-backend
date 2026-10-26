from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .publications import unresolved


class LateOperatorBaselineTests(SimpleTestCase):
    def check(self, entries, name="INTERNAL_TOKEN", reason="operator_value_required"):
        publication = SimpleNamespace(missing=[{"container": "backend", "name": name, "reason": reason}])
        with patch("apps.koth.publications.decode_settings", return_value={
            "containers": [{"name": "backend", "entries": entries}]
        }):
            return unresolved(publication, SimpleNamespace())

    def test_reviewed_locked_operator_value_resolves_only_the_new_version(self):
        self.assertEqual(self.check({"INTERNAL_TOKEN": {"locked": True, "value": "configured"}}), [])
        self.assertEqual(len(self.check({})), 1)

    def test_unlocked_missing_and_null_operator_values_remain_blocked(self):
        for entry in ({}, {"locked": False, "value": "candidate"}, {"locked": True, "value": None}):
            self.assertEqual(len(self.check({"INTERNAL_TOKEN": entry})), 1)

    def test_file_backed_role_can_explicitly_disable_unused_direct_token(self):
        self.assertEqual(self.check({"INTERNAL_TOKEN": {"locked": True, "value": ""}}), [])

    def test_unknown_operator_names_and_other_blockers_are_preserved(self):
        self.assertEqual(len(self.check({"PORT": {"locked": True, "value": "8080"}}, name="PORT")), 1)
        self.assertEqual(len(self.check({"INTERNAL_TOKEN": {"locked": True, "value": "configured"}}, reason="container_profile_changed")), 1)
