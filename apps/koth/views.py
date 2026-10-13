import uuid
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, OuterRef, Prefetch, Subquery
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from apps.accounts.models import Team
from apps.common.exceptions import (
    ClubNotFound,
    InvalidClubId,
    InvalidInternalToken,
    InvalidKothChallengeId,
    InvalidRequest,
    KothChallengeIdRequired,
    KothChallengeNotFound,
    UserHasNoTeam,
)
from apps.common.jwt import hash_token
from apps.common.permissions import IsAuthenticated
from apps.common.response import ok
from apps.common.utils import num

from .models import (
    KothChallenge,
    KothSolve,
    KothTeamToken,
    KothTokenVerificationAttempt,
)
from .tokens import build_team_token, matches_token


def _challenges_with_owner():
    # Fetch only the winner's fields, not every solve and its complete Team object.
    leaders = KothSolve.objects.filter(
        challenge_id=OuterRef("pk"), earned_score__gt=0, team__is_banned=False
    ).order_by("-earned_score", "solved_at", "team__team_name", "team_id")
    return KothChallenge.objects.annotate(
        owner_team_id=Subquery(leaders.values("team_id")[:1]),
        owner_team_name=Subquery(leaders.values("team__team_name")[:1]),
        owner_score=Subquery(leaders.values("earned_score")[:1]),
    )


def _challenge_payload(challenge, include_times=True):
    data = {
        "koth_challenge_id": str(challenge.koth_challenge_id),
        "title": challenge.title,
        "challenge_url": challenge.challenge_url or None,
        "status": challenge.status,
        "open_group": challenge.open_group,
        "current_owner_team_id": (
            str(uuid.UUID(str(challenge.owner_team_id)))
            if challenge.owner_team_id
            else None
        ),
        "current_owner_team_name": challenge.owner_team_name,
        "current_score": (
            num(challenge.owner_score) if challenge.owner_score is not None else 0
        ),
    }
    if include_times:
        data.update(
            {"opened_at": challenge.opened_at, "closed_at": challenge.closed_at}
        )
    return data


@api_view(["GET"])
@permission_classes([AllowAny])
def clubs(request):
    from .models import KothClub

    club_rows = KothClub.objects.prefetch_related(
        Prefetch(
            "challenges", queryset=_challenges_with_owner(), to_attr="owned_challenges"
        )
    )
    club_data = [
        {
            "club_id": str(club.club_id),
            "name": club.name,
            "challenges": [_challenge_payload(c) for c in club.owned_challenges],
        }
        for club in club_rows
    ]
    challenges = [challenge for club in club_data for challenge in club["challenges"]]
    return ok(
        {
            "clubs": club_data,
            "total_count": len(club_data),
            "challenge_count": len(challenges),
            "active_count": sum(
                challenge["status"] == "ACTIVE" for challenge in challenges
            ),
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def club_detail(request, club_id):
    try:
        uuid.UUID(str(club_id))
    except (ValueError, TypeError, AttributeError):
        raise InvalidClubId()
    from .models import KothClub

    try:
        club = KothClub.objects.prefetch_related(
            Prefetch(
                "challenges",
                queryset=_challenges_with_owner(),
                to_attr="owned_challenges",
            )
        ).get(pk=club_id)
    except KothClub.DoesNotExist:
        raise ClubNotFound()
    challenges = [_challenge_payload(challenge) for challenge in club.owned_challenges]
    return ok(
        {
            "club_id": str(club.club_id),
            "name": club.name,
            "challenges": challenges,
            "challenge_count": len(challenges),
        }
    )


def _request_team(request):
    if request.user.team_id is None:
        raise UserHasNoTeam()
    return request.user.team


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    team = _request_team(request)
    higher_scores = (
        KothSolve.objects.filter(
            challenge_id=OuterRef("challenge_id"),
            earned_score__gt=OuterRef("earned_score"),
            team__is_banned=False,
        )
        .order_by()
        .values("challenge_id")
        .annotate(total=Count("pk"))
        .values("total")
    )
    solves = {
        solve.challenge_id: solve
        for solve in KothSolve.objects.filter(team=team).annotate(
            higher_count=Subquery(higher_scores)
        )
    }
    challenge_data = []
    for challenge in KothChallenge.objects.all():
        solve = solves.get(challenge.koth_challenge_id)
        score = solve.earned_score if solve else Decimal("0")
        rank = None
        if score > 0 and not team.is_banned:
            rank = 1 + (solve.higher_count or 0)
        challenge_data.append(
            {
                "koth_challenge_id": str(challenge.koth_challenge_id),
                "club_id": str(challenge.club_id),
                "title": challenge.title,
                "challenge_url": challenge.challenge_url or None,
                "status": challenge.status,
                "earned_score": num(score),
                "rank": rank,
                "solved_at": solve.solved_at if solve else None,
                "opened_at": challenge.opened_at,
                "closed_at": challenge.closed_at,
            }
        )
    total = sum((solve.earned_score for solve in solves.values()), Decimal("0"))
    return ok(
        {
            "team_id": str(team.team_id),
            "team_name": team.team_name,
            "total_koth_score": num(total),
            "challenges": challenge_data,
            "total_count": len(challenge_data),
            "active_count": sum(row["status"] == "ACTIVE" for row in challenge_data),
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def leaderboard(request):
    challenge_id = request.query_params.get("koth_challenge_id")
    if not challenge_id:
        raise KothChallengeIdRequired()

    try:
        parsed_id = uuid.UUID(str(challenge_id))
    except (ValueError, TypeError, AttributeError):
        raise InvalidKothChallengeId()

    # Authentication runs before this view. Only cache the shared JSON ranking;
    # browsable HTML can contain request-specific content and must never be shared.
    cache_seconds = settings.KOTH_LEADERBOARD_CACHE_SECONDS
    cache_key = f"koth:leaderboard:json:v1:{parsed_id}"
    cacheable = cache_seconds > 0 and request.accepted_media_type == "application/json"
    if cacheable:
        try:
            cached_content = cache.get(cache_key)
        except Exception:
            cached_content = None
        if cached_content is not None:
            return HttpResponse(cached_content, content_type="application/json")

    try:
        challenge = KothChallenge.objects.get(pk=parsed_id)
    except KothChallenge.DoesNotExist:
        raise KothChallengeNotFound()

    solves = (
        KothSolve.objects.filter(
            challenge=challenge, earned_score__gt=0, team__is_banned=False
        )
        .order_by("-earned_score", "solved_at", "team__team_name", "team_id")
        .values("team_id", "team__team_name", "earned_score", "solved_at")
    )
    rows = []
    previous_score = None
    rank = 0
    for index, solve in enumerate(solves, start=1):
        if solve["earned_score"] != previous_score:
            rank = index
            previous_score = solve["earned_score"]
        rows.append(
            {
                "rank": rank,
                "team_id": str(solve["team_id"]),
                "team_name": solve["team__team_name"],
                "earned_score": num(solve["earned_score"]),
                "solved_at": solve["solved_at"],
            }
        )

    response = ok(
        {
            "koth_challenge_id": str(challenge.koth_challenge_id),
            "title": challenge.title,
            "status": challenge.status,
            "leaderboard": rows,
            "total_count": len(rows),
            "updated_at": timezone.now(),
        }
    )
    if cacheable:

        def cache_rendered_response(rendered):
            try:
                # Cache only bytes: hits avoid both JSON encoding and rebuilding
                # the 1,000-row Python object graph from a pickled Response.
                cache.set(cache_key, rendered.content, timeout=cache_seconds)
            except Exception:
                pass  # Redis is an optional read optimization, not a dependency.

        response.add_post_render_callback(cache_rendered_response)
    return response


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def team_token(request):
    team = _request_team(request)
    raw_token = build_team_token(team.team_id)
    with transaction.atomic():
        stored, created = KothTeamToken.objects.get_or_create(
            team=team, defaults={"token_hash": hash_token(raw_token)}
        )
        if not created and not matches_token(raw_token, stored.token_hash):
            # 비밀값이 교체됐을 때는 운영자가 토큰을 의도적으로 재발급할 수 있게 갱신한다.
            stored.token_hash = hash_token(raw_token)
            stored.save(update_fields=["token_hash"])
    return ok(
        {
            "team_id": str(team.team_id),
            "team_name": team.team_name,
            "team_token": raw_token,
            "issued_at": stored.issued_at,
        }
    )


def _internal_challenge(request):
    challenge_id = (
        request.data.get("koth_challenge_id")
        if request.method == "POST"
        else request.query_params.get("koth_challenge_id")
    )
    try:
        parsed_id = uuid.UUID(str(challenge_id))
    except (ValueError, TypeError, AttributeError):
        raise InvalidRequest()
    try:
        challenge = KothChallenge.objects.get(pk=parsed_id)
    except KothChallenge.DoesNotExist:
        raise InvalidRequest()
    if not matches_token(
        request.headers.get("X-Internal-Token", ""),
        challenge.inbound_internal_token_hash,
    ):
        raise InvalidInternalToken()
    return challenge


@api_view(["POST"])
@permission_classes([AllowAny])
def verify_team_token(request):
    if set(request.data) != {"koth_challenge_id", "team_token"} or not isinstance(
        request.data.get("team_token"), str
    ):
        raise InvalidRequest()
    challenge = _internal_challenge(request)
    candidate_hash = hash_token(request.data["team_token"])
    token = (
        KothTeamToken.objects.select_related("team")
        .filter(token_hash=candidate_hash)
        .first()
    )
    valid = token is not None and not token.team.is_banned
    if not valid:
        KothTokenVerificationAttempt.objects.create(challenge=challenge)
    return ok(
        {
            "valid": valid,
            "team_id": str(token.team_id) if valid else None,
            "team_name": token.team.team_name if valid else None,
            "koth_challenge_id": str(challenge.koth_challenge_id),
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def internal_teams(request):
    _internal_challenge(request)
    teams = [
        {"team_id": str(team.team_id), "team_name": team.team_name}
        for team in Team.objects.filter(is_banned=False).order_by("team_name")
    ]
    return ok({"teams": teams, "total_count": len(teams)})
