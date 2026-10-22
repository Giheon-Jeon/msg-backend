"""Enrich broker reservations without inferring placement from local instance state."""
from uuid import UUID

from apps.accounts.models import Team
from apps.challenge.models import Challenge
from apps.instances.models import DeploymentTest, Instance
from apps.instances.deployment_tests import test_team_id


def _uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def enrich_allocations(targets):
    records = [record for target in targets
               for record in (target.get("resource_allocation") or {}).get("allocations", [])]
    if not records:
        return
    def ids(key):
        return {parsed for record in records if (parsed := _uuid(record.get(key))) is not None}

    challenges = {str(pk): title for pk, title in Challenge.objects.filter(
        pk__in=ids("challenge_id")).values_list("pk", "title")}
    teams = {str(pk): name for pk, name in Team.objects.filter(
        pk__in=ids("team_id")).values_list("pk", "team_name")}
    instances = {str(pk): (str(team), str(challenge), status)
                 for pk, team, challenge, status in Instance.objects.filter(
                     pk__in=ids("instance_id")).values_list("pk", "team_id", "challenge_id", "status")}
    deployment_tests = {str(test.instance_id): test for test in DeploymentTest.objects.filter(
        instance_id__in=ids("instance_id")).select_related("release", "requested_by")}
    for record in records:
        team_id = str(_uuid(record.get("team_id")))
        challenge_id = str(_uuid(record.get("challenge_id")))
        record["challenge_title"] = challenges.get(challenge_id)
        record["team_name"] = teams.get(team_id)
        instance = instances.get(str(_uuid(record.get("instance_id"))))
        # A missing/stale local row cannot free a broker reservation.
        record["status"] = instance[2] if instance and instance[:2] == (team_id, challenge_id) else None
        test = deployment_tests.get(str(_uuid(record.get("instance_id"))))
        if test and str(test_team_id(test)) == team_id and str(test.release.challenge_id) == challenge_id:
            record["status"] = test.status
            record["team_name"] = "관리자 배포 테스트"
