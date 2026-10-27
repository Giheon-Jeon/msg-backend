import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Team, User


@pytest.mark.django_db
def test_banned_team_existing_jwt_cannot_read_koth_profile_or_token():
    team = Team.objects.create(team_name="qa-banned-jwt")
    User.objects.create_user(
        login_id="qa-banned-leader",
        password="local-test-password",
        nickname="qa-leader",
        team=team,
    )
    client = APIClient()
    login = client.post(
        "/api/v1/auth/login",
        {"login_id": "qa-banned-leader", "password": "local-test-password"},
        format="json",
    )
    assert login.status_code == 200
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['data']['access_token']}")

    for path in ("/api/v1/koth/me", "/api/v1/koth/team_token"):
        allowed = client.get(path)
        assert allowed.status_code == 200

    team.is_banned = True
    team.save(update_fields=["is_banned"])
    for path in ("/api/v1/koth/me", "/api/v1/koth/team_token"):
        denied = client.get(path)
        assert denied.status_code == 403
        assert denied.data["code"] == "TEAM_BANNED"
        assert denied.data.get("data") is None
