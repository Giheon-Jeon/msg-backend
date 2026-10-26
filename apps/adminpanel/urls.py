from django.urls import path

from . import views, challenge_settings, challenge_files, challenge_solves, published_challenges, board_readiness, challenge_preview
from . import submission_history

urlpatterns = [
    path("admin/teams", views.team_list),
    path("admin/teams/<str:team_id>/ban", views.team_ban),
    path("admin/teams/<str:team_id>/mileage", views.team_mileage),
    path("admin/teams/<str:team_id>", views.team_detail),
    path("admin/dashboard", views.dashboard),
    path("admin/challenges", views.challenge_list),
    path("admin/board/readiness", board_readiness.board_readiness),
    path("admin/published_challenges", published_challenges.published_challenges),
    path("admin/published_challenges/preview", published_challenges.publication_preview),
    path("admin/published_challenges/import", published_challenges.publication_import),
    path("admin/challenges/<uuid:challenge_id>", views.challenge_detail),
    path("admin/challenges/<uuid:challenge_id>/preview", challenge_preview.challenge_preview),
    path("admin/challenges/<uuid:challenge_id>/solves", challenge_solves.challenge_solves),
    path("admin/challenges/<uuid:challenge_id>/submissions", submission_history.challenge_submissions),
    path("admin/signatures/<uuid:signature_id>/submissions", submission_history.signature_submissions),
    path("admin/challenges/<uuid:challenge_id>/flag", challenge_settings.challenge_flag),
    path("admin/challenges/<uuid:challenge_id>/flag/reveal", challenge_settings.challenge_flag_reveal),
    path("admin/challenges/<uuid:challenge_id>/repository-files", challenge_settings.challenge_repository_files),
    path("admin/challenges/<uuid:challenge_id>/files", challenge_files.challenge_files),
    path("admin/challenges/<uuid:challenge_id>/files/<uuid:file_id>/download", challenge_files.challenge_file_download),
    path("admin/payment/checkout", views.payment_checkout),
    path("admin/payment/history", views.payment_history),
    path("admin/payment/<str:history_id>/refund", views.payment_refund),
    path("admin/instances", views.instance_list),
    path("admin/instances/<uuid:instance_id>", views.instance_force_delete),
    path("admin/instances/<uuid:instance_id>/reset", views.instance_force_reset),
    path("admin/mileage_history", views.mileage_history),
    path("admin/accounts", views.account_create),
    path("admin/challenges/<uuid:challenge_id>/visibility", views.challenge_visibility),
    path("admin/teams/<uuid:team_id>/board/dice", views.board_dice),
    path("admin/teams/<uuid:team_id>/board/position", views.board_position),
    path(
        "admin/teams/<uuid:team_id>/board/cells/<str:cell_index>",
        views.board_cell_status,
    ),
    path(
        "admin/teams/<uuid:team_id>/line_monopolies/<str:line_number>",
        views.line_monopoly_score,
    ),
    path("admin/settings", views.settings_view),
    path("admin/events", views.event_list),
    path("admin/resources", views.admin_resources),
]
