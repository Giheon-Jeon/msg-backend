from django.urls import path

from . import views

urlpatterns = [
    path("auth/login", views.login),
    path("auth/me", views.me),
    path("auth/logout", views.logout),
    path("auth/refresh", views.refresh),
]
