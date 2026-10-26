"""Administrator KOTH content, publication and scoring inspection APIs."""
import hashlib
import io
import json
import re
import secrets
import time
import zipfile
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, quote

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from rest_framework.views import APIView

from apps.accounts.models import Team
from apps.adminpanel.models import AdminEvent
from apps.common.permissions import IsAdmin
from apps.common.response import ok, fail
from apps.instances import published_challenges as shared
from apps.instances.repository_source import RepositorySnapshot, RepositorySourceUnavailable
from apps.instances.releases import ReleaseValidationError
from . import publications, services
from .environment import EnvironmentError, canonical_uuid
from .environment_views import environment_errors, get_challenge
from .models import KothChallenge, KothChallengeStatus, KothClub, KothScorePeriod, KothScorePeriodStatus, KothSolve, KothUserFileBundle


def reason(body):
    value = body.get("reason") if isinstance(body, dict) else None
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
        raise EnvironmentError("변경 사유를 1~500자로 적어주세요", "INVALID_REQUEST")
    return value.strip()


def audit(challenge, actor, message, why):
    AdminEvent.objects.create(type=AdminEvent.EventType.CHALLENGE_UPDATED, actor=actor,
                              message=f"KOTH {challenge.title} ({challenge.pk}) {message} / 사유: {why}")


def utc(value):
    return services.format_utc(value) if value else None


def selected_bundle(challenge):
    bundles = challenge.user_file_bundles.filter(source_sha=challenge.source_revision)
    if challenge.active_publication_id:
        bundles = bundles.filter(registry_revision=challenge.active_publication.registry_revision)
    return bundles.order_by("-registry_revision").first()


def bundle_data(challenge, bundle):
    if not bundle or not bundle.present or not bundle.object_key or challenge.files_mode == "NONE":
        return None
    return {"file_name": challenge.repository_slug + ".zip", "size_bytes": bundle.size_bytes,
            "file_count": bundle.file_count, "built_at": utc(bundle.created_at), "source_revision": bundle.source_sha,
            "sha256": bundle.sha256, "registry_revision": bundle.registry_revision}


def scores(challenge):
    return list(KothSolve.objects.filter(challenge=challenge, team__is_banned=False, earned_score__gt=0)
                .select_related("team").order_by("-earned_score", "solved_at", "team__team_name", "team_id"))


def challenge_data(challenge, detail=False):
    leaders = scores(challenge)
    latest = challenge.publications.first()
    period = challenge.score_periods.exclude(status="PENDING").order_by("-period_id").first()
    source = challenge.source_revision
    result = {"koth_challenge_id": str(challenge.pk), "club_id": str(challenge.club_id), "club_name": challenge.club.name,
              "title": challenge.title, "description": challenge.description, "status": challenge.status,
              "open_group": challenge.open_group, "opened_at": utc(challenge.opened_at), "closed_at": utc(challenge.closed_at),
              "challenge_url": challenge.challenge_url or None, "score_api_url": challenge.score_api_url or None,
              "score_api_token_env": challenge.score_api_token_env or None,
              "score_api_configured": bool(challenge.score_api_url and challenge.score_api_token_env),
              "files": {"mode": challenge.files_mode, "bundle": bundle_data(challenge, selected_bundle(challenge))},
              "current_owner_team_id": str(leaders[0].team_id) if leaders else None,
              "current_owner_team_name": leaders[0].team.team_name if leaders else None,
              "current_score": float(leaders[0].earned_score) if leaders else 0, "scored_team_count": len(leaders),
              "last_period": {"period_id": utc(period.period_id), "status": period.status, "applied_at": utc(period.applied_at)} if period else None,
              "failed_period_count": challenge.score_periods.filter(status="FAILED").count(),
              "repository": {"slug": challenge.repository_slug, "source_revision": source or None,
                  "active_publication_id": str(challenge.active_publication_id) if challenge.active_publication_id else None,
                  "metadata_revision": challenge.metadata_revision or source or None,
                  "latest_revision": latest.source_sha if latest else source or None,
                  "latest_registry_revision": latest.registry_revision if latest else None,
                  "author": latest.metadata.get("author") if latest else None,
                  "synced_at": utc(latest.created_at) if latest else None,
                  "source_url": f"https://github.com/{settings.RELEASE_POLL_REPO}/tree/{source}/{challenge.repository_slug}" if source else None} if challenge.repository_slug else None,
              "updated_at": utc(challenge.updated_at), "updated_by": challenge.updated_by or None}
    if detail:
        result["leaderboard"] = [{"rank": index, "team_id": str(row.team_id), "team_name": row.team.team_name,
                                  "earned_score": float(row.earned_score), "solved_at": utc(row.solved_at)} for index, row in enumerate(leaders, 1)]
    return result


def checked_url(value):
    if value is None or value == "":
        return ""
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        if (parsed is None or len(value) > 500 or parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.port == 0
                or any(ord(c) < 33 for c in value)):
            raise ValueError
    except ValueError:
        raise EnvironmentError("주소는 계정 정보와 # 없이 HTTP(S)로 입력해주세요", "INVALID_REQUEST") from None
    return value


def validated_fields(body, challenge=None, create=False):
    allowed = {"title", "description", "challenge_url", "open_group", "opened_at", "closed_at", "score_api_url", "score_api_token_env", "reason"}
    allowed |= {"club_id", "club_name", "repository_slug"} if create else {"status"}
    if not isinstance(body, dict) or set(body) - allowed:
        raise EnvironmentError("문제 수정 항목을 확인해주세요", "INVALID_REQUEST")
    reason(body)
    values = {}
    for key in ("title", "description"):
        if key in body:
            value = body[key]
            if not isinstance(value, str) or len(value) > (100 if key == "title" else 100000) or key == "title" and not value.strip():
                raise EnvironmentError("제목과 지문 길이를 확인해주세요", "INVALID_REQUEST")
            values[key] = value.strip() if key == "title" else value
    if create and ("title" not in values or "open_group" not in body):
        raise EnvironmentError("제목과 공개 순서를 입력해주세요", "INVALID_REQUEST")
    if "open_group" in body:
        if type(body["open_group"]) is not int or not 1 <= body["open_group"] <= 12:
            raise EnvironmentError("공개 순서는 1~12로 입력해주세요", "INVALID_REQUEST")
        values["open_group"] = body["open_group"]
    for key in ("challenge_url", "score_api_url"):
        if key in body:
            values[key] = checked_url(body[key])
    if "score_api_token_env" in body:
        value = "" if body["score_api_token_env"] is None else body["score_api_token_env"]
        if not isinstance(value, str) or value and not re.fullmatch(r"KOTH_[A-Z0-9_]{1,87}_TOKEN", value):
            raise EnvironmentError("KOTH 전용 점수 조회 토큰의 환경변수 이름을 입력해주세요", "INVALID_REQUEST")
        values["score_api_token_env"] = value
    # A token's destination is an operator trust boundary, not an arbitrary admin form URL.
    if (challenge and challenge.score_api_token_env) or values.get("score_api_token_env"):
        old = (challenge.score_api_url, challenge.score_api_token_env) if challenge else ("", "")
        new = (values.get("score_api_url", old[0]), values.get("score_api_token_env", old[1]))
        if new != old and new[0] and new[1]:
            allowed_origins = set(getattr(settings, "KOTH_SCORE_API_ALLOWED_ORIGINS", []))
            parsed = urlsplit(new[0])
            if f"{parsed.scheme}://{parsed.netloc}" not in allowed_origins:
                raise EnvironmentError("새 점수 조회 서버는 운영 허용 주소에 먼저 등록해주세요", "KOTH_SCORE_ORIGIN_NOT_ALLOWED", 409)
    for key in ("opened_at", "closed_at"):
        if key in body:
            value = body[key]
            parsed = parse_datetime(value) if isinstance(value, str) else None
            if value is not None and (parsed is None or timezone.is_naive(parsed)):
                raise EnvironmentError("공개·마감 시각은 시간대가 있는 날짜로 입력해주세요", "INVALID_REQUEST")
            values[key] = parsed
    if "status" in body:
        if body["status"] not in KothChallengeStatus.values:
            raise EnvironmentError("문제 공개 상태를 확인해주세요", "INVALID_REQUEST")
        values["status"] = body["status"]
        if body["status"] == "ACTIVE" and not values.get("opened_at", challenge.opened_at if challenge else None):
            values["opened_at"] = timezone.now()
        if body["status"] == "CLOSED" and challenge and challenge.status != "CLOSED":
            values["closed_at"] = timezone.now()
    opened = values.get("opened_at", challenge.opened_at if challenge else None)
    closed = values.get("closed_at", challenge.closed_at if challenge else None)
    if opened and closed and closed <= opened:
        raise EnvironmentError("마감 시각은 공개 시각보다 뒤여야 합니다", "INVALID_REQUEST")
    return values


@method_decorator(never_cache, name="dispatch")
class ChallengesView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        rows = [challenge_data(c) for c in KothChallenge.objects.select_related("club")]
        division_counts = dict(
            Team.objects.filter(is_banned=False)
            .values_list("koth_division")
            .annotate(total=Count("team_id"))
        )
        points_by_division = {
            division: list(services.rank_points(division_counts.get(division, 0)))
            for division in Team.KothDivision.values
        }
        return ok(
            {
                "challenges": rows,
                "total_count": len(rows),
                "scoring": {
                    "period_minutes": 15,
                    "first_place_points": services.FIRST_PLACE_POINTS,
                    "points_by_division": points_by_division,
                },
            }
        )

    @environment_errors
    def post(self, request):
        values = validated_fields(request.data, create=True)
        body = request.data
        club_id, club_name = body.get("club_id"), body.get("club_name")
        if bool(club_id) == bool(club_name) or club_name and (not isinstance(club_name, str) or not 1 <= len(club_name.strip()) <= 100):
            raise EnvironmentError("동아리를 고르거나 새 동아리 이름을 입력해주세요", "INVALID_REQUEST")
        slug = body.get("repository_slug") or ""
        if slug and (not isinstance(slug, str) or not publications.SLUG.fullmatch(slug)):
            raise EnvironmentError("KOTH 저장소 폴더 이름을 확인해주세요", "INVALID_REQUEST")
        source = RepositorySnapshot(slug) if slug else None
        if source:
            publications.metadata(slug, source.revision)
        with transaction.atomic():
            if club_id:
                club = KothClub.objects.select_for_update().filter(pk=canonical_uuid(club_id)).first()
                if club is None:
                    raise EnvironmentError("동아리를 찾지 못했습니다", "CLUB_NOT_FOUND", 404)
            else:
                if KothClub.objects.filter(name=club_name.strip()).exists():
                    raise EnvironmentError("이미 등록된 동아리 이름입니다", "KOTH_CLUB_NAME_TAKEN", 409)
                club = KothClub.objects.create(name=club_name.strip())
            if KothChallenge.objects.filter(club=club).exists():
                raise EnvironmentError("이 동아리의 KOTH 문제가 이미 있습니다", "KOTH_CLUB_HAS_CHALLENGE", 409)
            if slug and KothChallenge.objects.filter(repository_slug=slug).exists():
                raise EnvironmentError("다른 문제에 연결된 폴더입니다", "REPOSITORY_ALREADY_LINKED", 409)
            token = secrets.token_urlsafe(40)
            challenge = KothChallenge.objects.create(club=club, repository_slug=slug, metadata_revision=source.revision if source else "",
                inbound_internal_token_hash=hashlib.sha256(token.encode()).hexdigest(), updated_by=request.user.login_id, **values)
            audit(challenge, request.user.login_id, "등록", reason(body))
        return ok({**challenge_data(challenge, True), "internal_token": token}, status=201)


@method_decorator(never_cache, name="dispatch")
class ChallengeView(APIView):
    permission_classes = [IsAdmin]

    @environment_errors
    def get(self, request, challenge_id):
        return ok(challenge_data(get_challenge(challenge_id), True))

    @environment_errors
    def patch(self, request, challenge_id):
        with transaction.atomic():
            challenge = get_challenge(challenge_id, lock=True)
            values = validated_fields(request.data, challenge)
            changed = [key for key, value in values.items() if getattr(challenge, key) != value]
            for key, value in values.items():
                setattr(challenge, key, value)
            challenge.updated_by = request.user.login_id
            challenge.save(update_fields=[*values, "updated_by", "updated_at"])
            audit(challenge, request.user.login_id, "수정: " + ", ".join(changed), reason(request.data))
        return ok(challenge_data(challenge, True))


def repository_catalog():
    key = "koth-repository-v1:" + settings.RELEASE_POLL_REPO
    result = cache.get(key)
    if result is None:
        ref = settings.REPOSITORY_ENV_REF.removeprefix("refs/heads/").removeprefix("origin/")
        commit = shared._json(f"/commits/{quote(ref, safe='')}")
        sha = commit.get("sha", "")
        folders = shared._json(f"/contents?ref={sha}")
        if not isinstance(folders, list):
            raise RepositorySourceUnavailable()
        rows = []
        for item in folders:
            slug = item.get("name", "")
            if item.get("type") != "dir" or not publications.SLUG.fullmatch(slug):
                continue
            data = publications.metadata(slug, sha)
            rows.append({"slug": slug, **data, "source_url": f"https://github.com/{settings.RELEASE_POLL_REPO}/tree/{sha}/{slug}",
                         "last_commit": {"revision": sha, "author": commit.get("commit", {}).get("author", {}).get("name"),
                                         "committed_at": commit.get("commit", {}).get("committer", {}).get("date")}})
        result = {"repository": settings.RELEASE_POLL_REPO, "ref": ref, "challenges": rows, "total_count": len(rows)}
        cache.set(key, result, timeout=120)
    linked = dict(KothChallenge.objects.exclude(repository_slug="").values_list("repository_slug", "pk"))
    return {**result, "challenges": [{**row, "linked_koth_challenge_id": str(linked[row["slug"]]) if row["slug"] in linked else None}
                                      for row in result["challenges"]]}


def source_errors(method):
    from functools import wraps
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except shared.PublishedImportError as error:
            return fail(error.code, str(error), error.status_code)
        except (HTTPError, URLError, TimeoutError, ValueError, zipfile.BadZipFile):
            return fail("KOTH_SOURCE_UNAVAILABLE", "GitHub 발행본을 읽지 못했습니다 잠시 뒤 다시 확인해주세요", 503)
        except (OSError, GoogleAPIError, GoogleAuthError):
            return fail("KOTH_FILES_UNAVAILABLE", "참가자 파일 저장소를 읽지 못했습니다", 503)
    return environment_errors(wrapped)


@method_decorator(never_cache, name="dispatch")
class RepositoryView(APIView):
    permission_classes = [IsAdmin]

    @source_errors
    def get(self, request):
        return ok(repository_catalog())


@method_decorator(never_cache, name="dispatch")
class RepositoryLinkView(APIView):
    permission_classes = [IsAdmin]

    @source_errors
    def put(self, request, challenge_id):
        body = request.data
        why = reason(body)
        if set(body) - {"slug", "apply", "reason"} or not isinstance(body.get("apply"), list) or set(body["apply"]) - {"title", "description"}:
            raise EnvironmentError("가져올 제목·지문을 선택해주세요", "INVALID_REQUEST")
        source = RepositorySnapshot(body.get("slug"))
        data = publications.metadata(source.slug, source.revision)
        with transaction.atomic():
            challenge = get_challenge(challenge_id, lock=True)
            if challenge.repository_slug != source.slug and (challenge.source_revision or challenge.publications.exists() or challenge.environment_versions.exists()):
                raise EnvironmentError("실행 설정이나 발행본이 등록된 문제는 다른 저장소 폴더로 바꿀 수 없습니다", "KOTH_PUBLICATION_CONFLICT", 409)
            if KothChallenge.objects.filter(repository_slug=source.slug).exclude(pk=challenge.pk).exists():
                raise EnvironmentError("다른 문제에 연결된 폴더입니다", "REPOSITORY_ALREADY_LINKED", 409)
            for key in body["apply"]:
                setattr(challenge, key, data[key])
            challenge.repository_slug, challenge.metadata_revision, challenge.updated_by = source.slug, source.revision, request.user.login_id
            challenge.save(update_fields=[*body["apply"], "repository_slug", "metadata_revision", "updated_by", "updated_at"])
            audit(challenge, request.user.login_id, "저장소 지문 연결", why)
        return ok(challenge_data(challenge, True))


def files_data(challenge, publication_id=None):
    if publication_id:
        publication = challenge.publications.filter(pk=canonical_uuid(publication_id)).first()
        if publication is None:
            raise EnvironmentError("이 문제의 발행본을 찾지 못했습니다", "KOTH_PUBLICATION_NOT_FOUND", 404)
        source = publication.source_sha
        bundle = challenge.user_file_bundles.filter(registry_revision=publication.registry_revision, source_sha=source).first()
    else:
        source, bundle = challenge.source_revision, selected_bundle(challenge)
    # No running source means a staged file is only an admin preview, never a participant download.
    files = []
    if bundle and bundle.present and bundle.object_key:
        key = "koth-file-list-v1:" + bundle.sha256
        files = cache.get(key)
        if files is None:
            with default_storage.open(bundle.object_key, "rb") as stream:
                raw = stream.read(bundle.size_bytes + 1)
            if len(raw) != bundle.size_bytes or hashlib.sha256(raw).hexdigest() != bundle.sha256:
                raise EnvironmentError("저장된 참가자 ZIP의 검증값이 다릅니다", "KOTH_FILES_INVALID", 409)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                files = [{"path": item.filename, "size_bytes": item.file_size} for item in archive.infolist() if not item.is_dir()]
            cache.set(key, files, timeout=3600)
    return {"mode": challenge.files_mode, "repository": {"slug": challenge.repository_slug, "revision": source,
                "path": challenge.repository_slug + "/prob/for_user", "latest_revision": source,
                "source_url": f"https://github.com/{settings.RELEASE_POLL_REPO}/tree/{source}/{challenge.repository_slug}/prob/for_user"} if source else None,
            "files": files, "total_size_bytes": sum(row["size_bytes"] for row in files),
            "bundle": bundle_data(challenge, bundle), "updated_at": utc(challenge.updated_at), "updated_by": challenge.updated_by or None}


@method_decorator(never_cache, name="dispatch")
class FilesView(APIView):
    permission_classes = [IsAdmin]

    @source_errors
    def get(self, request, challenge_id):
        return ok(files_data(get_challenge(challenge_id), request.query_params.get("publication_id")))

    @source_errors
    def put(self, request, challenge_id):
        why = reason(request.data)
        if set(request.data) != {"mode", "reason"} or request.data["mode"] not in ("REPOSITORY", "NONE"):
            raise EnvironmentError("참가자 파일 제공 여부를 선택해주세요", "INVALID_REQUEST")
        with transaction.atomic():
            challenge = get_challenge(challenge_id, lock=True)
            bundle = selected_bundle(challenge)
            if request.data["mode"] == "REPOSITORY" and (not bundle or not bundle.present):
                raise EnvironmentError("현재 실행 버전에는 참가자 파일이 없습니다 파일이 있는 발행본을 먼저 가져와주세요", "KOTH_FILES_NOT_FOUND", 409)
            challenge.files_mode, challenge.updated_by = request.data["mode"], request.user.login_id
            challenge.save(update_fields=["files_mode", "updated_by", "updated_at"])
            audit(challenge, request.user.login_id, "참가자 파일 " + challenge.files_mode, why)
        return ok(files_data(challenge))


@method_decorator(never_cache, name="dispatch")
class PublicationsView(APIView):
    permission_classes = [IsAdmin]

    @source_errors
    def get(self, request, challenge_id):
        challenge = get_challenge(challenge_id)
        rows = [publications.serialize(row) for row in challenge.publications.select_related("challenge")[:100]]
        return ok({"items": rows, "running_source_sha": challenge.source_revision or None})


@method_decorator(never_cache, name="dispatch")
class PublishedCatalogView(APIView):
    permission_classes = [IsAdmin]

    @source_errors
    def get(self, request):
        page = bounded_int(request.query_params.get("page", "1"), 1, 10000)
        return ok(publications.catalog(page, request.query_params.get("slug")))


@method_decorator(never_cache, name="dispatch")
class PublicationImportView(APIView):
    permission_classes = [IsAdmin]

    @source_errors
    def post(self, request, challenge_id):
        body = request.data
        if not isinstance(body, dict) or set(body) - {"artifact_id", "preview_token", "reason"} or type(body.get("artifact_id")) is not int or body["artifact_id"] <= 0:
            raise EnvironmentError("가져올 GitHub 발행본을 선택해주세요", "INVALID_REQUEST")
        challenge = get_challenge(challenge_id)
        prepared = publications.prepare(body["artifact_id"])
        if challenge.repository_slug != prepared["slug"]:
            raise EnvironmentError("이 문제에 연결된 저장소 폴더와 다릅니다", "KOTH_PUBLICATION_CONFLICT", 409)
        if "preview_token" not in body:
            return ok(publications.preview_data(challenge, prepared, request.user.login_id))
        why = reason(body)
        publication, created = publications.register(challenge, prepared, request.user.login_id, preview_token=body["preview_token"])
        if created:
            audit(challenge, request.user.login_id, f"발행본 {publication.registry_revision} 가져오기", why)
        return ok({**publications.serialize(publication), "created": created})


def bounded_int(value, low, high):
    try:
        result = int(value)
        if not low <= result <= high:
            raise ValueError
    except (ValueError, TypeError):
        raise EnvironmentError("페이지 번호와 조회 개수를 확인해주세요", "INVALID_REQUEST") from None
    return result


def period_data(record):
    payload = (record.response_payload or {}).get("data") or {}
    results = payload.get("results") or []
    teams = {str(t.pk): t for t in Team.objects.filter(pk__in=[r.get("team_id") for r in results])}
    award_snapshot = record.award_snapshot or {}
    awards = award_snapshot.get("awarded_scores", {})
    rows = []
    for item in results:
        team = teams.get(item.get("team_id"))
        rank = item.get("rank", item.get("period_rank"))
        rows.append({"team_id": item.get("team_id"), "team_name": team.team_name if team else "알 수 없는 팀",
                     "period_rank": rank, "metric_score": item.get("score", item.get("metric_score")),
                     "awarded_score": awards.get(item.get("team_id"), 0) if record.status == "APPLIED" else 0})
    return {"period_id": utc(record.period_id), "status": record.status, "attempts": record.attempts,
            "last_error": "점수 수집에 실패했습니다 연결 설정과 응답을 확인해주세요" if record.last_error else "",
            "applied_at": utc(record.applied_at),
            "award_provenance": award_snapshot.get("provenance"), "results": rows}


@method_decorator(never_cache, name="dispatch")
class PeriodsView(APIView):
    permission_classes = [IsAdmin]

    @environment_errors
    def get(self, request, challenge_id):
        challenge = get_challenge(challenge_id)
        page = bounded_int(request.query_params.get("page", "1"), 1, 100000)
        size = bounded_int(request.query_params.get("size", "100"), 1, 100)
        rows = challenge.score_periods.order_by("-period_id")
        return ok({"periods": [period_data(r) for r in rows[(page - 1) * size:page * size]], "total_count": rows.count(), "page": page, "size": size})


@method_decorator(never_cache, name="dispatch")
class PeriodRetryView(APIView):
    permission_classes = [IsAdmin]

    @environment_errors
    def post(self, request, challenge_id, period_id):
        challenge = get_challenge(challenge_id)
        try:
            period = services.parse_period(period_id)
        except services.ScoreFetchError:
            raise EnvironmentError("점수 수집 구간을 확인해주세요", "INVALID_REQUEST") from None
        row = KothScorePeriod.objects.filter(challenge=challenge, period_id=period).first()
        if row is None:
            raise EnvironmentError("수집 구간을 찾지 못했습니다", "KOTH_PERIOD_NOT_FOUND", 404)
        if row.status == "APPLIED":
            raise EnvironmentError("이미 점수가 반영된 구간입니다", "KOTH_PERIOD_ALREADY_APPLIED", 409)
        if period + timedelta(minutes=15) > timezone.now():
            raise EnvironmentError("아직 끝나지 않은 구간입니다", "KOTH_PERIOD_NOT_ENDED", 409)
        try:
            services.poll_challenge_period(challenge, period)
        except services.ScoreFetchError:
            pass
        row.refresh_from_db()
        return ok(period_data(row))


@method_decorator(never_cache, name="dispatch")
class ConnectionTestView(APIView):
    permission_classes = [IsAdmin]

    @environment_errors
    def post(self, request, challenge_id):
        challenge = get_challenge(challenge_id)
        body = request.data
        if not isinstance(body, dict) or set(body) - {"challenge_url", "score_api_url", "score_api_token_env"}:
            raise EnvironmentError("접속 확인 항목을 확인해주세요", "INVALID_REQUEST")
        # Test only the registered destinations. Editing a form must not turn this into an SSRF/token oracle.
        for key in body:
            if (body[key] or "") != (getattr(challenge, key) or ""):
                raise EnvironmentError("접속 확인은 저장된 주소와 전용 토큰 설정으로 진행해주세요", "KOTH_CONNECTION_CHANGED", 409)
        result = {"challenge": {"ok": False, "skipped": True}, "score_api": {"ok": False, "skipped": True}, "checked_at": utc(timezone.now())}
        if challenge.score_api_url and challenge.score_api_token_env:
            started = time.monotonic()
            try:
                period = services.current_period()
                payload = services._request_scores(challenge, period)
                services._validated_results(challenge, period, payload)
                result["score_api"] = {"ok": True, "status": 200, "result_count": len((payload.get("data") or {}).get("results", [])), "latency_ms": round((time.monotonic() - started) * 1000)}
            except services.ScoreFetchError:
                result["score_api"] = {"ok": False, "error": "점수 서버 연결 또는 응답을 확인해주세요", "latency_ms": round((time.monotonic() - started) * 1000)}
        if challenge.challenge_url:
            import http.client
            parsed = urlsplit(checked_url(challenge.challenge_url))
            cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
            connection = cls(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), timeout=5)
            started = time.monotonic()
            try:
                connection.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""))
                response = connection.getresponse()
                result["challenge"] = {"ok": 200 <= response.status < 400, "status": response.status, "latency_ms": round((time.monotonic() - started) * 1000)}
            except (OSError, http.client.HTTPException):
                result["challenge"] = {"ok": False, "error": "문제 서버에 연결하지 못했습니다", "latency_ms": round((time.monotonic() - started) * 1000)}
            finally:
                connection.close()
        return ok(result)
