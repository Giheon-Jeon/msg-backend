"""A scoped VM receives a short-lived ticket for one approved image, never registry credentials."""
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

from apps.common.response import ok
from .environment import EnvironmentError, canonical_uuid, decode_settings, require_source
from .environment_views import AgentBase, environment_errors


@method_decorator(never_cache, name="dispatch")
class ImageExportTicketView(AgentBase):
    @environment_errors
    def post(self, request, challenge_id):
        config = self.authenticate_config(request, challenge_id)
        body = request.data
        if not isinstance(body, dict) or set(body) != {"application_id", "version_id", "image_ref"}:
            raise EnvironmentError("이미지 준비 요청을 확인해주세요")
        application = config.application
        if (not application or canonical_uuid(body["application_id"]) != application.pk
                or canonical_uuid(body["version_id"]) != application.version_id):
            raise EnvironmentError("이미 다른 적용 요청으로 바뀌었습니다", "KOTH_ENV_CONFLICT", 409)
        version = application.version
        require_source(config.challenge, version)
        if body["image_ref"] not in [container["image"] for container in decode_settings(version)["containers"]]:
            raise EnvironmentError("현재 승인한 이미지가 아닙니다", "KOTH_IMAGE_NOT_APPROVED", 409)
        url = settings.KOTH_IMAGE_EXPORT_URL
        parsed = urlsplit(url)
        key = settings.KOTH_IMAGE_EXPORT_SIGNING_KEY
        if (not key or len(key) < 32 or parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path != "/internal/v1/koth_image_exports"):
            raise EnvironmentError("관리 서버의 이미지 준비 연결을 확인해주세요", "KOTH_IMAGE_EXPORT_UNAVAILABLE", 503)
        claims = {"aud": "msg-koth-image-v1", "challenge_id": str(challenge_id), "application_id": str(application.pk),
                  "version_id": str(version.pk), "image_ref": body["image_ref"], "exp": int(time.time()) + 300}
        encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = base64.urlsafe_b64encode(hmac.new(key.encode(), encoded.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        expires = datetime.fromtimestamp(claims["exp"], tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return ok({"url": url, "ticket": encoded + "." + signature, "image_ref": body["image_ref"], "expires_at": expires})
