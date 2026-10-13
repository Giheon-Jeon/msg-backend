import json
from datetime import timezone as dt_timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils.dateparse import parse_datetime

from apps.challenge.models import Challenge
from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    DeleteReason,
    Instance,
    InstanceStatus,
    IsolationProfile,
)


ACTIVE_INSTANCE_STATUSES = [
    InstanceStatus.REQUESTED,
    InstanceStatus.SCHEDULING,
    InstanceStatus.PROVISIONING,
    InstanceStatus.RUNNING,
    InstanceStatus.RESTARTING,
    InstanceStatus.RESETTING,
]

RESETTABLE_INSTANCE_STATUSES = [
    InstanceStatus.RUNNING,
]

DELETABLE_INSTANCE_STATUSES = [
    InstanceStatus.RUNNING,
]

EXTENDABLE_INSTANCE_STATUSES = [
    InstanceStatus.RUNNING,
]

FINAL_INSTANCE_STATUSES = [
    InstanceStatus.STOPPED,
    InstanceStatus.FAILED,
    InstanceStatus.EXPIRED,
    InstanceStatus.CLEANED,
]


class SchedulerError(Exception):
    def __init__(self, code, message, status_code):
        # Scheduler 호출 실패를 API 응답 코드로 전달하기 위해 사용한다
        self.code = code
        self.message = message
        self.status_code = status_code


def isoformat_z(value):
    # datetime 값을 API 응답용 UTC 문자열로 바꾼다
    if value is None:
        return None

    return value.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def parse_scheduler_datetime(value):
    # Scheduler가 내려준 ISO datetime 문자열을 Django datetime으로 바꾼다
    if not value:
        return None

    return parse_datetime(value)


def scheduler_auth_header(request=None):
    # Scheduler 호출에 사용할 내부 API 토큰 헤더를 만든다
    if not settings.SCHEDULER_API_TOKEN:
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "인스턴스 서버 설정이 올바르지 않습니다.",
            503,
        )

    return f"Bearer {settings.SCHEDULER_API_TOKEN}"


def get_active_instance(user):
    # 현재 사용자의 최신 활성 인스턴스 한 개를 조회한다
    return (
        Instance.objects.filter(user=user, status__in=ACTIVE_INSTANCE_STATUSES)
        .select_related("challenge")
        .order_by("-created_at")
        .first()
    )


def get_challenge_runtime_config(challenge):
    # 문제의 실행 설정을 현재 릴리스와 함께 조회한다
    return (
        ChallengeRuntimeConfig.objects
        .select_related("current_release")
        .filter(challenge=challenge)
        .first()
    )


def get_release_from_scheduler_data(challenge, scheduler_data):
    registry_revision = scheduler_data.get("registry_revision")
    if (
        isinstance(registry_revision, bool)
        or not isinstance(registry_revision, int)
        or registry_revision <= 0
    ):
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "Scheduler 응답의 registry_revision을 확인할 수 없습니다.",
            503,
        )

    release = ChallengeRelease.objects.filter(
        challenge=challenge,
        registry_revision=registry_revision,
    ).first()
    if release is None:
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "Scheduler 응답의 registry_revision에 해당하는 릴리스를 찾을 수 없습니다.",
            503,
        )

    return release


def release_container_ports(container):
    return [entry["port"] for entry in container.ports]


def release_container_public_ports(container):
    return [entry["port"] for entry in container.ports if entry.get("public")]


def serialize_release_container(container):
    return {
        "name": container.name,
        "image": container.image_ref,
        "ports": release_container_ports(container),
        "exposed_ports": release_container_public_ports(container),
    }


def validate_release_for_scheduler(release):
    from apps.instances.releases import (
        ReleaseValidationError,
        has_sufficient_container_resources,
        validate_container_image,
        validate_healthcheck,
    )

    containers = list(release.containers.all())

    if release.registry_revision <= 0:
        raise SchedulerError(
            "RELEASE_NOT_DEPLOYABLE",
            "Scheduler에 전달할 수 없는 legacy 릴리스입니다.",
            400,
        )

    if not 1 <= len(containers) <= 8:
        raise SchedulerError(
            "RELEASE_NOT_DEPLOYABLE",
            "현재 Scheduler 계약으로 배포할 수 없는 릴리스입니다.",
            400,
        )

    if not has_sufficient_container_resources(release, len(containers)):
        raise SchedulerError(
            "RELEASE_NOT_DEPLOYABLE",
            "컨테이너 수에 비해 릴리스 자원이 부족합니다.",
            400,
        )

    exposed_port_count = 0
    exposed_containers = []
    for container in containers:
        ports = release_container_ports(container)
        public_ports = release_container_public_ports(container)
        if not 1 <= len(ports) <= 8:
            raise SchedulerError(
                "RELEASE_NOT_DEPLOYABLE",
                "컨테이너 포트 설정을 확인해주세요.",
                400,
            )
        if public_ports:
            exposed_containers.append((ports, public_ports))
        exposed_port_count += len(public_ports)

    if not 1 <= exposed_port_count <= 8:
        raise SchedulerError(
            "RELEASE_NOT_DEPLOYABLE",
            "공개 포트 수는 1개 이상 8개 이하여야 합니다.",
            400,
        )

    if release.isolation_profile == IsolationProfile.PWN and (
        len(exposed_containers) != 1
        or len(exposed_containers[0][0]) != 1
        or len(exposed_containers[0][1]) != 1
    ):
        raise SchedulerError(
            "RELEASE_NOT_DEPLOYABLE",
            "PWN 릴리스는 포트가 하나인 공개 컨테이너를 정확히 하나 사용해야 합니다.",
            400,
        )

    try:
        for container in containers:
            validate_container_image(container.image_ref, release.challenge_slug, container.name)
        validate_healthcheck(
            release.healthcheck,
            {container.name: release_container_ports(container) for container in containers},
        )
    except ReleaseValidationError as error:
        raise SchedulerError("RELEASE_NOT_DEPLOYABLE", error.message, 400) from error


def normalize_scheduler_endpoints(raw_endpoints):
    if raw_endpoints is None:
        return []
    if not isinstance(raw_endpoints, list):
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "Scheduler 응답의 endpoints 형식이 올바르지 않습니다.",
            503,
        )

    endpoints = []
    for raw in raw_endpoints:
        if not isinstance(raw, dict):
            raise SchedulerError(
                "SCHEDULER_UNAVAILABLE",
                "Scheduler 응답의 endpoint 형식이 올바르지 않습니다.",
                503,
            )
        container_name = raw.get("container_name")
        port = raw.get("port")
        protocol = raw.get("protocol")
        service_url = raw.get("service_url")
        if (
            not isinstance(container_name, str)
            or not container_name
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
            or protocol not in {"HTTP", "TCP"}
            or not isinstance(service_url, str)
            or not service_url
        ):
            raise SchedulerError(
                "SCHEDULER_UNAVAILABLE",
                "Scheduler 응답의 endpoint 값이 올바르지 않습니다.",
                503,
            )
        endpoints.append(
            {
                "container_name": container_name,
                "port": port,
                "protocol": protocol,
                "service_url": service_url,
            }
        )
    return endpoints


def scheduler_network_values(scheduler_data):
    endpoints = normalize_scheduler_endpoints(scheduler_data.get("endpoints", []))
    service_url = scheduler_data.get("service_url")
    if not service_url and endpoints:
        service_url = endpoints[0]["service_url"]
    return service_url, endpoints


def serialize_instance(instance, include_title=False, include_replaced=False):
    # 인스턴스 응답 데이터를 API 명세 형식으로 정리한다
    if instance is None:
        return None

    is_running = instance.status == InstanceStatus.RUNNING
    data = {
        "instance_id": str(instance.instance_id),
        "challenge_id": str(instance.challenge_id),
        "host": instance.host if is_running else None,
        "ports": instance.ports if is_running else [],
        "endpoints": instance.endpoints if is_running else [],
        "status": instance.status,
        "expires_at": isoformat_z(instance.expires_at),
        "hard_expires_at": isoformat_z(instance.hard_expires_at),
    }

    if include_title:
        data["challenge_title"] = instance.challenge.title

    if include_replaced:
        data["replaced_instance_id"] = (
            str(instance.replaced_instance_id)
            if instance.replaced_instance_id
            else None
        )

    return data


def scheduler_request(method, path, body=None, auth_header=None, query=None):
    # Scheduler HTTP API를 호출하고 공통 응답 data를 반환한다
    base_url = settings.SCHEDULER_BASE_URL.rstrip("/")
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"

    payload = None
    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header

    if body is not None:
        payload = json.dumps(body).encode("utf-8")

    request = Request(url, data=payload, headers=headers, method=method)

    try:
        # settings에서 URL scheme과 authority를 검증한 배포 설정만 사용한다.
        with urlopen(  # nosec B310
            request, timeout=settings.SCHEDULER_TIMEOUT_SECONDS
        ) as response:
            response_body = response.read().decode("utf-8")
    except HTTPError as error:
        raise scheduler_error_from_response(error) from error
    except (TimeoutError, URLError) as error:
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "인스턴스 서버와 연결할 수 없습니다. 잠시 후 다시 시도해주세요.",
            503,
        ) from error

    if not response_body:
        return None

    result = json.loads(response_body)
    return result.get("data")


def scheduler_error_from_response(error):
    # Scheduler 에러 응답을 백엔드 공통 에러 형식으로 변환한다
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}

    if error.code >= 500:
        return SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "인스턴스 서버와 연결할 수 없습니다. 잠시 후 다시 시도해주세요.",
            503,
        )

    return SchedulerError(
        payload.get("code", "INTERNAL_ERROR"),
        payload.get("message", "요청 처리 중 오류가 발생했습니다."),
        error.code,
    )


def build_scheduler_create_body(user, team, challenge, runtime_config, release):
    # Scheduler 인스턴스 생성 요청 body를 현재 릴리스 값으로 만든다
    validate_release_for_scheduler(release)
    containers = release.containers.order_by("name")
    body = {
        "team_id": str(team.team_id),
        "user_id": str(user.user_id),
        "challenge_id": str(challenge.challenge_id),
        "containers": [
            serialize_release_container(container)
            for container in containers
        ],
        "registry_revision": release.registry_revision,
        "isolation_profile": release.isolation_profile,
        "architecture": release.architecture,
        "resource_profile": {
            "cpu_millicores": release.cpu_millicores,
            "memory_mib": release.memory_mib,
            "ephemeral_storage_mib": release.ephemeral_storage_mib,
        },
        "ttl_minutes": runtime_config.ttl_minutes,
        "hard_timeout_minutes": runtime_config.hard_timeout_minutes,
    }
    if release.healthcheck is not None:
        body["healthcheck"] = release.healthcheck
    return body


def call_scheduler_create(user, team, challenge, runtime_config, release, auth_header=None):
    # Scheduler에 인스턴스 생성을 요청한다
    return scheduler_request(
        "POST",
        "/api/instances",
        body=build_scheduler_create_body(user, team, challenge, runtime_config, release),
        auth_header=auth_header,
    )


def call_scheduler_delete(instance, auth_header=None):
    # Scheduler에 인스턴스 삭제를 요청한다
    return scheduler_request(
        "DELETE",
        f"/api/instances/{instance.instance_id}",
        body={},
        auth_header=auth_header,
    )


def call_scheduler_reset(instance, auth_header=None):
    # Scheduler에 인스턴스 초기화를 요청한다
    return scheduler_request(
        "POST",
        f"/api/instances/{instance.instance_id}/reset",
        body={},
        auth_header=auth_header,
    )


def call_scheduler_extend(instance, auth_header=None):
    # Scheduler에 인스턴스 시간 연장을 요청한다
    return scheduler_request(
        "POST",
        f"/api/instances/{instance.instance_id}/extend",
        body={"extend_minutes": settings.INSTANCE_EXTEND_MINUTES},
        auth_header=auth_header,
    )


def call_scheduler_detail(instance, auth_header=None):
    # Scheduler에서 인스턴스 단건 상태를 조회한다
    return scheduler_request(
        "GET",
        f"/api/instances/{instance.instance_id}",
        auth_header=auth_header,
    )


def call_scheduler_active(user, auth_header=None):
    # Scheduler에서 현재 사용자의 active instance를 조회한다
    return scheduler_request(
        "GET",
        "/api/instances/active",
        auth_header=auth_header,
        query={"user_id": str(user.user_id)},
    )


def update_instance_from_scheduler(instance, scheduler_data):
    # Scheduler 응답값으로 백엔드 인스턴스 상태를 갱신한다
    if not scheduler_data:
        return instance

    update_fields = ["updated_at"]

    instance.status = scheduler_data.get("status", instance.status)
    update_fields.append("status")

    has_service_url = "service_url" in scheduler_data
    has_endpoints = "endpoints" in scheduler_data
    endpoints = None
    if has_endpoints:
        endpoints = normalize_scheduler_endpoints(scheduler_data.get("endpoints"))
        instance.endpoints = endpoints
        update_fields.append("endpoints")

    if has_service_url or (has_endpoints and endpoints):
        service_url = scheduler_data.get("service_url")
        if not service_url and endpoints:
            service_url = endpoints[0]["service_url"]
        instance.host = service_url
        instance.ports = []
        update_fields.extend(["host", "ports"])

    if "expires_at" in scheduler_data:
        instance.expires_at = parse_scheduler_datetime(scheduler_data.get("expires_at"))
        update_fields.append("expires_at")

    if "hard_expires_at" in scheduler_data:
        instance.hard_expires_at = parse_scheduler_datetime(
            scheduler_data.get("hard_expires_at")
        )
        update_fields.append("hard_expires_at")

    instance.save(update_fields=update_fields)
    return instance


def validate_scheduler_instance_scope(instance, user, team, challenge):
    if instance is None:
        return

    if (
        instance.user_id != user.user_id
        or instance.team_id != team.team_id
        or instance.challenge_id != challenge.challenge_id
    ):
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "Scheduler 응답의 instance_id가 기존 인스턴스 소유 정보와 일치하지 않습니다.",
            503,
        )


def create_instance_from_scheduler(
    scheduler_data, user, team, challenge=None, replaced_instance=None, release=None
):
    # Scheduler가 발급한 instance_id로 백엔드 인스턴스 row를 만든다
    if challenge is None:
        challenge = Challenge.objects.filter(challenge_id=scheduler_data.get("challenge_id")).first()
    if release is None and challenge is not None:
        release = get_release_from_scheduler_data(challenge, scheduler_data)
    service_url, endpoints = scheduler_network_values(scheduler_data)

    with transaction.atomic():
        existing_instance = (
            Instance.objects
            .select_for_update()
            .filter(instance_id=scheduler_data["instance_id"])
            .first()
        )
        validate_scheduler_instance_scope(existing_instance, user, team, challenge)

        instance, _ = Instance.objects.update_or_create(
            instance_id=scheduler_data["instance_id"],
            defaults={
                "user": user,
                "team": team,
                "challenge": challenge,
                "status": scheduler_data.get("status", InstanceStatus.REQUESTED),
                "host": service_url,
                "ports": [],
                "endpoints": endpoints,
                "expires_at": parse_scheduler_datetime(scheduler_data.get("expires_at")),
                "hard_expires_at": parse_scheduler_datetime(scheduler_data.get("hard_expires_at")),
                "replaced_instance": replaced_instance,
                # 어떤 릴리스로 떴는지 추적하기 위한 생성 시점 스냅샷
                "release": release,
            },
        )
    return instance


def mark_instance_replaced(instance):
    if instance is None:
        return

    instance.status = InstanceStatus.STOPPING
    instance.delete_reason = DeleteReason.REPLACED_BY_NEW_INSTANCE
    instance.save(update_fields=["status", "delete_reason", "updated_at"])


def sync_instance_from_scheduler(instance, auth_header=None):
    # 최종 상태가 아닌 인스턴스를 Scheduler 최신 상태로 동기화한다
    if instance is None or instance.status in FINAL_INSTANCE_STATUSES:
        return instance

    scheduler_data = call_scheduler_detail(instance, auth_header)
    return update_instance_from_scheduler(instance, scheduler_data)
