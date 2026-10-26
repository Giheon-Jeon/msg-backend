"""Render saved participant content for admins without opening a team's problem."""
from django.views.decorators.cache import never_cache
from rest_framework.decorators import api_view, permission_classes

from apps.challenge.models import Challenge, Solve
from apps.challenge.views import number_value
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances.models import ChallengeRuntimeConfig
from apps.instances.user_files import current_user_file_bundle, serialize_user_file
from apps.challenge.remote_run_config import remote_run_data


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def challenge_preview(request, challenge_id):
    challenge = Challenge.objects.filter(pk=challenge_id).first()
    if challenge is None:
        return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제입니다", 404)
    config = ChallengeRuntimeConfig.objects.filter(challenge=challenge).select_related("current_release").first()
    release = config.current_release if config else None
    bundle = current_user_file_bundle(challenge)
    return ok({
        "challenge_id": str(challenge.pk), "title": challenge.title,
        "description": challenge.description, "category": challenge.category,
        "difficulty": challenge.difficulty, "score": number_value(challenge.current_score),
        "is_published": challenge.is_published,
        "release_version": release.version if release else None,
        "requires_instance": release is not None,
        "files": [serialize_user_file(challenge, bundle)] if bundle else [],
        "solved_team_count": Solve.objects.filter(challenge=challenge).count(),
        "is_solved": False, "instance": None,
        "remote_run": remote_run_data(challenge),
    })
