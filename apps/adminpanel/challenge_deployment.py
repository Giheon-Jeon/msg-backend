from django.db.models import OuterRef, Subquery, UUIDField

from apps.challenge.models import Challenge
from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig


def deployment_summaries(challenges):
    """목록에 표시할 현재 배포 환경. 환경변수와 비밀값의 원문은 포함하지 않는다."""
    current = ChallengeRuntimeConfig.objects.filter(challenge_id=OuterRef("pk")).values("current_release_id")[:1]
    latest = ChallengeRelease.objects.filter(challenge_id=OuterRef("pk")).order_by("-version").values("pk")[:1]
    selection = list(Challenge.objects.filter(pk__in=[item.pk for item in challenges]).annotate(
        selected_current_id=Subquery(current, output_field=UUIDField()), selected_latest_id=Subquery(latest),
    ).values("pk", "selected_current_id", "selected_latest_id"))
    releases = {item.pk: item for item in ChallengeRelease.objects.filter(
        pk__in=[row["selected_current_id"] or row["selected_latest_id"] for row in selection],
    ).prefetch_related("containers")}
    result = {}
    for row in selection:
        release = releases.get(row["selected_current_id"] or row["selected_latest_id"])
        if release is None or release.challenge_id != row["pk"]:
            result[row["pk"]] = None
            continue
        result[row["pk"]] = {
            "release_id": str(release.pk), "version": release.version,
            "registry_revision": release.registry_revision,
            "is_current": release.pk == row["selected_current_id"],
            "runtime_type": release.runtime_type, "architecture": release.architecture,
            "isolation_profile": release.isolation_profile,
            "resource_profile": {"cpu_millicores": release.cpu_millicores,
                                 "memory_mib": release.memory_mib,
                                 "ephemeral_storage_mib": release.ephemeral_storage_mib},
            "containers": [{"name": container.name, "ports": container.ports,
                            "env_count": len(container.env), "secret_count": len(container.secret_env)}
                           for container in sorted(release.containers.all(), key=lambda item: item.name)],
        }
    return result
