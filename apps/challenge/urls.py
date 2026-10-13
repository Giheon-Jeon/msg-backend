from django.urls import path

from apps.challenge.views import (
    ChallengeDetailView,
    ChallengeFileDownloadView,
    ChallengeSubmitView,
)

urlpatterns = [
    path(
        "challenges/<uuid:challenge_id>",
        ChallengeDetailView.as_view(),
        name="challenge-detail",
    ),
    path(
        "challenges/<uuid:challenge_id>/files/<uuid:file_id>/download",
        ChallengeFileDownloadView.as_view(),
        name="challenge-file-download",
    ),
    path(
        "challenges/<uuid:challenge_id>/submit",
        ChallengeSubmitView.as_view(),
        name="challenge-submit",
    ),
]
