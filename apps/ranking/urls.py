from django.urls import path
from .views import team_ranking, my_team_ranking, member_ranking


urlpatterns = [
    path("ranking", team_ranking),
    path("ranking/me", my_team_ranking),
    path("ranking/member", member_ranking),
]
