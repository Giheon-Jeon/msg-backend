from django.urls import path

from . import views
from .environment_views import EnvironmentAgentStatusView, EnvironmentAgentView

urlpatterns = [
    path("internal/koth/team_tokens/verify", views.verify_team_token),
    path("internal/teams", views.internal_teams),
    path("internal/v1/koth-environments/<uuid:challenge_id>", EnvironmentAgentView.as_view()),
    path("internal/v1/koth-environments/<uuid:challenge_id>/status", EnvironmentAgentStatusView.as_view()),
]
