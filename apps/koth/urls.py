from django.urls import path

from . import admin_preview, files, views
from . import admin_views as admin
from .environment_views import EnvironmentApplyView, EnvironmentPreviewView, EnvironmentVersionsView, EnvironmentView

urlpatterns = [
    path("admin/koth/challenges", admin.ChallengesView.as_view()),
    path("admin/koth/challenges/<uuid:challenge_id>", admin.ChallengeView.as_view()),
    path("admin/koth/challenges/<uuid:challenge_id>/repository", admin.RepositoryLinkView.as_view()),
    path("admin/koth/challenges/<uuid:challenge_id>/files", admin.FilesView.as_view()),
    path("admin/koth/challenges/<uuid:challenge_id>/files/download", files.admin_challenge_file),
    path("admin/koth/challenges/<uuid:challenge_id>/periods", admin.PeriodsView.as_view()),
    path("admin/koth/challenges/<uuid:challenge_id>/periods/<str:period_id>/retry", admin.PeriodRetryView.as_view()),
    path("admin/koth/challenges/<uuid:challenge_id>/connection_test", admin.ConnectionTestView.as_view()),
    path("admin/koth/challenges/<uuid:challenge_id>/preview_sessions", admin_preview.issue_preview),
    path("koth/preview_sessions/authorize", admin_preview.authorize_preview),
    path("admin/koth/challenges/<uuid:challenge_id>/publications", admin.PublicationsView.as_view()),
    path("admin/koth/challenges/<uuid:challenge_id>/publications/import", admin.PublicationImportView.as_view()),
    path("admin/koth/published", admin.PublishedCatalogView.as_view()),
    path("admin/koth/repository", admin.RepositoryView.as_view()),
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
