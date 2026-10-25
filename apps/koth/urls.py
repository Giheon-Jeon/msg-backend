from django.urls import path

from . import files, views
from .environment_views import EnvironmentApplyView, EnvironmentPreviewView, EnvironmentVersionsView, EnvironmentView

urlpatterns = [
    path("koth/clubs", views.clubs),
    path("koth/clubs/<str:club_id>", views.club_detail),
    path("koth/me", views.me),
    path("koth/leaderboard", views.leaderboard),
    path("koth/team_token", views.team_token),
    path("koth/challenges/<uuid:challenge_id>/file", files.challenge_file),
    path("admin/koth/challenges/<uuid:challenge_id>/file", files.admin_challenge_file),
    path("admin/koth/challenges/<uuid:challenge_id>/environment", EnvironmentView.as_view()),
    path("admin/koth/challenges/<uuid:challenge_id>/environment/preview", EnvironmentPreviewView.as_view()),
    path("admin/koth/challenges/<uuid:challenge_id>/environment/versions", EnvironmentVersionsView.as_view()),
    path("admin/koth/challenges/<uuid:challenge_id>/environment/versions/<uuid:version_id>/apply", EnvironmentApplyView.as_view()),
]
