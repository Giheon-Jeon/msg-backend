import io
from unittest.mock import patch

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.accounts.models import Team, User
from apps.board.models import Cell


class SeedPasswordValidationTests(TestCase):
    def test_seed_validates_both_demo_users_and_preserves_login(self):
        with patch(
            "apps.board.management.commands.seed_board.validate_password",
            wraps=validate_password,
        ) as validate:
            call_command("seed_board", stdout=io.StringIO())
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(
            {call.kwargs["user"].login_id for call in validate.call_args_list},
            {"demo_leader", "demo_member"},
        )
        for login in ("demo_leader", "demo_member"):
            self.assertTrue(User.objects.get(login_id=login).check_password("demo1234"))

    @override_settings(
        AUTH_PASSWORD_VALIDATORS=[
            {
                "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
                "OPTIONS": {"min_length": 12},
            }
        ]
    )
    def test_password_rejection_rolls_back_seed_without_deleting_existing_board(self):
        cell = Cell.objects.create(
            cell_index=1, type=Cell.CellType.START, name="existing board"
        )
        with self.assertRaises(ValidationError):
            call_command("seed_board", stdout=io.StringIO())
        cell.refresh_from_db()
        self.assertEqual(cell.name, "existing board")
        self.assertEqual(Cell.objects.count(), 1)
        self.assertFalse(
            User.objects.filter(login_id__in=["demo_leader", "demo_member"]).exists()
        )
        self.assertFalse(Team.objects.filter(team_name="test-team").exists())
