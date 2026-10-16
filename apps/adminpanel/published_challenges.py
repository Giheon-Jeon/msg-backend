"""Admin-only GitHub publication selection and explicit import."""
from functools import wraps
from urllib.error import HTTPError, URLError
import zipfile

import yaml
from django.core import signing
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from rest_framework.decorators import api_view, permission_classes

from apps.challenge.models import Challenge
from apps.common.exceptions import InvalidRequest, IdempotencyKeyRequired
from apps.common.idempotency import MAX_KEY_LENGTH, run_idempotent
from apps.common.models import IdempotencyRecord
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances import published_challenges as publications
from apps.instances.releases import ReleaseValidationError
from apps.instances.runtime_secrets import RuntimeSecretUnavailable


def publication_errors(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except HTTPError as error:
            if error.code in (404, 410):
                return fail("PUBLICATION_UNAVAILABLE", "발행 파일을 찾지 못했습니다 만료 여부와 저장소 접근 권한을 확인해주세요", 409)
            return fail("PUBLICATION_SOURCE_UNAVAILABLE", "GitHub에 연결하지 못했습니다 서버의 저장소 읽기 권한을 확인해주세요", 503)
        except (URLError, TimeoutError):
            return fail("PUBLICATION_SOURCE_UNAVAILABLE", "GitHub 응답이 늦어지고 있습니다 잠시 후 다시 시도해주세요", 503)
        except (GoogleAPIError, GoogleAuthError, OSError, ImproperlyConfigured):
            # These escape run_idempotent so a temporary outage does not consume the key.
            return fail("PUBLICATION_STORAGE_UNAVAILABLE", "파일을 저장하지 못했습니다 저장소 설정과 권한을 확인한 뒤 다시 시도해주세요", 503)
        except RuntimeSecretUnavailable:
            return fail("RUNTIME_SECRET_UNAVAILABLE", "비밀값 저장 설정을 확인한 뒤 다시 시도해주세요", 503)
        except (ReleaseValidationError, ValueError, TypeError, KeyError, AttributeError, zipfile.BadZipFile, yaml.YAMLError):
            # Source parsers may include YAML snippets / secrets in their exception messages.
            return fail("INVALID_PUBLICATION", "발행본의 문제 정보·실행 설정·ZIP이 맞지 않습니다 GitHub 발행 결과를 확인해주세요", 400)
    return wrapped


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
@publication_errors
def published_challenges(request):
    try:
        page = int(request.query_params.get("page", "1"))
        if not 1 <= page <= 1000:
            raise ValueError
    except (TypeError, ValueError):
        raise InvalidRequest("발행 목록 페이지를 확인해주세요") from None
    return ok(publications.published_catalog(page))


@never_cache
@api_view(["POST"])
@permission_classes([IsAdmin])
@publication_errors
@sensitive_variables()
def publication_preview(request):
    if (not isinstance(request.data, dict) or set(request.data) != {"artifact_id"}
            or type(request.data.get("artifact_id")) is not int
            or not 1 <= request.data["artifact_id"] <= 2**53 - 1):
        raise InvalidRequest("목록에서 가져올 발행본을 선택해주세요")
    prepared = publications.prepare_publication(request.data["artifact_id"])
    return ok(publications.preview_publication(prepared, request.user))


@sensitive_variables()
def _prepare_import(request):
    try:
        selected = signing.loads(request.data["preview_token"], salt=publications.SALT, max_age=publications.PREVIEW_SECONDS)
        if selected.get("user_id") != str(request.user.pk):
            raise signing.BadSignature()
    except signing.BadSignature:
        raise publications.PublishedImportError("PUBLICATION_PREVIEW_EXPIRED", "미리보기가 만료되었거나 변경됐습니다 발행본을 다시 선택해주세요", 409) from None
    prepared = publications.prepare_publication(selected["artifact_id"])
    if not constant_time_compare(selected["fingerprint"], prepared["fingerprint"]):
        raise publications.PublishedImportError("PUBLICATION_CHANGED", "발행 내용이 변경됐습니다 발행본을 다시 선택해주세요", 409)
    return prepared


@transaction.non_atomic_requests
@never_cache
@api_view(["POST"])
@permission_classes([IsAdmin])
@publication_errors
@sensitive_variables()
def publication_import(request):
    data = request.data
    if (not isinstance(data, dict) or set(data) != {"preview_token", "difficulty"}
            or not isinstance(data.get("preview_token"), str) or not 1 <= len(data["preview_token"]) <= 2048
            or data.get("difficulty") not in Challenge.DifficultyType.values):
        raise InvalidRequest("선택한 발행본과 난이도를 확인해주세요")
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise IdempotencyKeyRequired()
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidRequest("요청 키가 너무 깁니다")

    def save(prepared):
        return publications.save_publication(prepared, request.user, data["difficulty"])

    # A completed retry stays valid even after GitHub expires the artifact or preview.
    # The shared helper still verifies the body hash and serializes concurrent retries.
    if IdempotencyRecord.objects.filter(user=request.user, endpoint=request.path, key=key).exists():
        return run_idempotent(request, data, lambda: save(_prepare_import(request)))
    prepared = _prepare_import(request)  # Network reads do not hold the challenge lock.
    return run_idempotent(request, data, lambda: save(prepared), message="발행본을 가져왔습니다")
