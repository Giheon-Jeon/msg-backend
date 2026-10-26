from django.urls import path

from apps.challenge.exe_views import ExeSubmissionCreateView, ExeSubmissionDetailView

from apps.challenge.views import (
    ChallengeDetailView,
    ChallengeFileDownloadView,
    ChallengeSubmitView,
)

urlpatterns = [
    path(
        "challenges/<uuid:challenge_id>/exe-submissions",
        ExeSubmissionCreateView.as_view(),
        name="exe-submission-create",
    ),
    path(
        "challenges/<uuid:challenge_id>/exe-submissions/<uuid:submission_id>",
        ExeSubmissionDetailView.as_view(),
        name="exe-submission-detail",
    ),
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
