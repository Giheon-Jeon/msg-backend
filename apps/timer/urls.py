from django.urls import path
from .views import contest_timer

urlpatterns = [
    path("timer", contest_timer),
]
