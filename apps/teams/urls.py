from django.urls import path

from . import views

urlpatterns = [
    path("teams/me", views.team_me),
    path("teams/me/mileage_history", views.mileage_history),
    path("teams/me/qr_token", views.qr_token),
    path("teams/me/solves", views.solves),
]
