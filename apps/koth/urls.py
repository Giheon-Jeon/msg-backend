from django.urls import path

from . import files, views

urlpatterns = [
    path("koth/clubs", views.clubs),
    path("koth/clubs/<str:club_id>", views.club_detail),
    path("koth/me", views.me),
    path("koth/leaderboard", views.leaderboard),
    path("koth/team_token", views.team_token),
    path("koth/challenges/<uuid:challenge_id>/file", files.challenge_file),
    path("admin/koth/challenges/<uuid:challenge_id>/file", files.admin_challenge_file),
]
