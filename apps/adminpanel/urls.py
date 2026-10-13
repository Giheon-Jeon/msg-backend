from django.urls import path

from . import views

urlpatterns = [
    path("admin/teams", views.team_list),
    path("admin/teams/<str:team_id>/ban", views.team_ban),
    path("admin/teams/<str:team_id>/mileage", views.team_mileage),
    path("admin/teams/<str:team_id>", views.team_detail),
    path("admin/dashboard", views.dashboard),
    path("admin/challenges", views.challenge_list),
    path("admin/challenges/<uuid:challenge_id>", views.challenge_detail),
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
    path("admin/settings", views.settings_view),
    path("admin/events", views.event_list),
    path("admin/resources", views.admin_resources),
]
