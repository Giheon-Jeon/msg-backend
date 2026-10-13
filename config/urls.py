from django.conf import settings
from django.contrib import admin
from django.urls import path, include

from apps.board.views import (
    BoardView,
    DebugSolveActiveChallengeView,
)
from apps.common.health import healthz

urlpatterns = [
    path("healthz", healthz),
    path("admin/", admin.site.urls),
    path("api/v1/", include("apps.timer.urls")),
    path("api/v1/", include("apps.accounts.urls")),
    path("api/v1/", include("apps.adminpanel.urls")),
    path("api/v1/", include("apps.teams.urls")),
    path("api/v1/", include("apps.ranking.urls")),
    path("api/v1/", include("apps.challenge.urls")),
    path("api/v1/", include("apps.instances.urls")),
    path("api/v1/", include("apps.leaderboard.urls")),
    path("api/v1/", include("apps.signature.urls")),
    path("api/v1/board", BoardView.as_view(), name="board"),
    path("api/v1/board/", include("apps.board.urls")),
    path("api/v1/", include("apps.koth.urls")),
    path("", include("apps.koth.internal_urls")),
]

if settings.DEBUG:
    urlpatterns += [
        # /api/v1 명세 밖, 로컬 프리뷰 전용.
        path(
            "board/_debug/solve",
            DebugSolveActiveChallengeView.as_view(),
            name="board-debug-solve",
        ),
    ]
