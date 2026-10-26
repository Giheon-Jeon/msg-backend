from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.views import APIView

from apps.challenge.exe_submissions import (
    MAX_PAYLOAD_BYTES,
    ExeSubmissionError,
    accept_exe,
    check_challenge_access,
    require_exe_enabled,
    serialize_exe_submission,
    submission_team,
)
from apps.challenge.models import ExeSubmission
from apps.common.permissions import IsAuthenticated
from apps.common.response import ok
from apps.challenge.remote_run_views import RemoteRunListView


class ExeSizeLimitHandler(FileUploadHandler):
    def __init__(self, request):
        super().__init__(request)
        self.received = 0

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > MAX_PAYLOAD_BYTES:
            self.request.exe_upload_too_large = True
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None


class ExeSubmissionCreateView(RemoteRunListView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if response.status_code == 429 and isinstance(getattr(exc, "wait", None), int):
            response["Retry-After"] = str(exc.wait)
        return response

    def initialize_request(self, request, *args, **kwargs):
        request.upload_handlers.insert(0, ExeSizeLimitHandler(request))
        return super().initialize_request(request, *args, **kwargs)

    def post(self, request, challenge_id):
        require_exe_enabled()
        files = request.FILES.getlist("file")
        if getattr(request._request, "exe_upload_too_large", False):
            raise ExeSubmissionError(
                "PAYLOAD_TOO_LARGE", "EXE files must not exceed 10 MiB.", 413
            )
        if len(files) != 1 or set(request.data) != {"file"}:
            raise ExeSubmissionError(
                "INVALID_REQUEST", "Exactly one file field is required."
            )
        submission, created = accept_exe(
            request.user, challenge_id, request.headers.get("Idempotency-Key"), files[0]
        )
        response = ok(
            serialize_exe_submission(submission),
            message="EXE submission accepted.",
            status=202 if created else 200,
        )
        response["Cache-Control"] = "private, no-store"
        return response


class ExeSubmissionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, challenge_id, submission_id):
        team = submission_team(request.user)
        submission = (
            ExeSubmission.objects.select_related("challenge")
            .filter(
                pk=submission_id,
                challenge_id=challenge_id,
                team=team,
            )
            .first()
        )
        if submission is None:
            raise ExeSubmissionError(
                "SUBMISSION_NOT_FOUND", "Submission not found.", 404
            )
        check_challenge_access(team, submission.challenge)
        response = ok(
            serialize_exe_submission(submission),
            message="EXE execution status and output.",
        )
        response["Cache-Control"] = "private, no-store"
        return response
