import re

from django.db.models import Max

from apps.instances.models import ChallengeRelease, IsolationProfile, ReleaseContainer
from apps.instances.services import isoformat_z

# 공급망 발행 명명과 동일한 digest 고정 GHCR 참조만 허용한다
IMAGE_REF_PATTERN = re.compile(
    r"^ghcr\.io/msg-ctf/challenges/"
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?/"
    r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?"
    r"@sha256:[0-9a-f]{64}$"
)
CONTAINER_NAME_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)

SUPPORTED_SCHEMA_VERSION = "2.0"
MAX_CONTAINERS = 8
MAX_PORTS_PER_CONTAINER = 8
MAX_EXPOSED_PORTS = 8
MAX_NOTE_LENGTH = 500
WRITABLE_MIB_PER_CONTAINER = 64


class ReleaseValidationError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


def has_sufficient_container_resources(release, container_count):
    return (
        release.cpu_millicores >= container_count
        and release.memory_mib >= container_count
        and release.ephemeral_storage_mib
        >= WRITABLE_MIB_PER_CONTAINER * container_count
    )


def _require_string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ReleaseValidationError(f"{field} 값이 올바르지 않습니다")
    return value.strip()


def _require_positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReleaseValidationError(f"{field} 값은 양의 정수여야 합니다")
    return value


def _validate_isolation_profile(value):
    profile = _require_string(value, "isolation_profile")
    if profile not in IsolationProfile.values:
        raise ReleaseValidationError("isolation_profile 값이 올바르지 않습니다")
    return profile


def _validate_ports(raw_ports, container_name):
    if not isinstance(raw_ports, list) or not raw_ports:
        raise ReleaseValidationError(
            f"{container_name} 컨테이너의 ports 값이 올바르지 않습니다"
        )

    ports = []
    seen = set()
    for entry in raw_ports:
        if not isinstance(entry, dict):
            raise ReleaseValidationError(
                f"{container_name} 컨테이너의 ports 항목 형식이 올바르지 않습니다"
            )
        port = entry.get("port")
        public = entry.get("public")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ReleaseValidationError(
                f"{container_name} 컨테이너의 port 값이 올바르지 않습니다"
            )
        if not isinstance(public, bool):
            raise ReleaseValidationError(
                f"{container_name} 컨테이너의 public 값이 올바르지 않습니다"
            )
        if port in seen:
            raise ReleaseValidationError(
                f"{container_name} 컨테이너에 중복 port가 있습니다"
            )
        seen.add(port)
        ports.append({"port": port, "public": public})
    return ports


def validate_container_image(image_ref, challenge_slug, container_name):
    if not isinstance(image_ref, str) or not IMAGE_REF_PATTERN.fullmatch(image_ref):
        raise ReleaseValidationError("컨테이너 image가 digest 고정 GHCR 형식이 아닙니다")
    expected = f"ghcr.io/msg-ctf/challenges/{challenge_slug}/{container_name}@sha256:"
    if not image_ref.startswith(expected):
        raise ReleaseValidationError("image 경로가 문제 slug 및 컨테이너 이름과 일치하지 않습니다")


def _validate_containers(raw_containers, challenge_slug):
    if not isinstance(raw_containers, list) or not raw_containers:
        raise ReleaseValidationError("workload.containers 값이 올바르지 않습니다")
    if len(raw_containers) > MAX_CONTAINERS:
        raise ReleaseValidationError("컨테이너 수가 허용 한도를 초과했습니다")

    containers = []
    names = set()
    for raw in raw_containers:
        if not isinstance(raw, dict):
            raise ReleaseValidationError("workload.containers 항목 형식이 올바르지 않습니다")
        name = _require_string(raw.get("name"), "container.name")
        if not CONTAINER_NAME_PATTERN.fullmatch(name):
            raise ReleaseValidationError(
                "container.name은 63자 이하의 DNS label 형식이어야 합니다"
            )
        if name in names:
            raise ReleaseValidationError("컨테이너 이름이 중복됩니다")
        names.add(name)

        image_ref = _require_string(raw.get("image"), f"{name}.image")
        validate_container_image(image_ref, challenge_slug, name)

        containers.append(
            {
                "name": name,
                "image_ref": image_ref,
                "ports": _validate_ports(raw.get("ports"), name),
            }
        )
    return containers


def validate_healthcheck(healthcheck, container_ports):
    if healthcheck is None:
        return
    if not isinstance(healthcheck, dict):
        raise ReleaseValidationError("healthcheck 값이 올바르지 않습니다")
    container = healthcheck.get("container")
    port = healthcheck.get("port")
    path = healthcheck.get("path")
    if not isinstance(container, str) or container not in container_ports:
        raise ReleaseValidationError("healthcheck.container가 선언된 컨테이너와 일치하지 않습니다")
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
        or port not in container_ports[container]
    ):
        raise ReleaseValidationError("healthcheck.port가 컨테이너의 선언된 포트와 일치하지 않습니다")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        # Kotlin/JVM String.length counts UTF-16 code units.
        or len(path.encode("utf-16-le", errors="surrogatepass")) // 2 > 1024
        or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in path)
    ):
        raise ReleaseValidationError("healthcheck.path 값이 올바르지 않습니다")


def validate_release_payload(body):
    # 등록 요청 body에서 artifact 한 벌을 검증해 정제된 값으로 돌려준다
    if not isinstance(body, dict):
        raise ReleaseValidationError("요청 body 형식이 올바르지 않습니다")

    artifact = body.get("artifact")
    if not isinstance(artifact, dict):
        raise ReleaseValidationError("artifact 값이 올바르지 않습니다")

    note = body.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise ReleaseValidationError("note 값은 문자열이어야 합니다")
        note = note.strip() or None
        if note and len(note) > MAX_NOTE_LENGTH:
            raise ReleaseValidationError(f"note 값은 {MAX_NOTE_LENGTH}자 이하여야 합니다")

    if artifact.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ReleaseValidationError("지원하지 않는 schema_version입니다")
    if artifact.get("scan_result") != "PASS":
        raise ReleaseValidationError("scan_result가 PASS인 릴리스만 등록할 수 있습니다")

    runtime_type = _require_string(artifact.get("runtime_type"), "runtime_type")
    if runtime_type not in {"KUBERNETES", "DOCKER", "VM"}:
        raise ReleaseValidationError("runtime_type 값이 올바르지 않습니다")

    architecture = _require_string(artifact.get("architecture"), "architecture")
    if architecture not in {"AMD64", "ARM64"}:
        raise ReleaseValidationError("architecture 값이 올바르지 않습니다")

    resource_profile = artifact.get("resource_profile")
    if not isinstance(resource_profile, dict):
        raise ReleaseValidationError("resource_profile 값이 올바르지 않습니다")

    healthcheck = artifact.get("healthcheck")
    if healthcheck is None:
        workload = artifact.get("workload")
        if isinstance(workload, dict):
            healthcheck = workload.get("healthcheck")
    workload = artifact.get("workload")
    if not isinstance(workload, dict):
        raise ReleaseValidationError("workload 값이 올바르지 않습니다")
    challenge_slug = _require_string(artifact.get("challenge_slug"), "challenge_slug")
    containers = _validate_containers(workload.get("containers"), challenge_slug)
    validate_healthcheck(
        healthcheck,
        {container["name"]: [entry["port"] for entry in container["ports"]] for container in containers},
    )

    registry_revision = _require_positive_int(
        artifact.get("registry_revision"), "registry_revision"
    )
    legacy_revision = artifact.get("revision")
    if legacy_revision is not None:
        legacy_revision = _require_positive_int(legacy_revision, "revision")
        if legacy_revision != registry_revision:
            raise ReleaseValidationError(
                "revision과 registry_revision이 일치하지 않습니다"
            )

    return {
        "challenge_slug": challenge_slug,
        "registry_revision": registry_revision,
        "runtime_type": runtime_type,
        "architecture": architecture,
        "isolation_profile": _validate_isolation_profile(
            artifact.get("isolation_profile")
        ),
        "cpu_millicores": _require_positive_int(
            resource_profile.get("cpu_millicores"), "resource_profile.cpu_millicores"
        ),
        "memory_mib": _require_positive_int(
            resource_profile.get("memory_mib"), "resource_profile.memory_mib"
        ),
        "ephemeral_storage_mib": _require_positive_int(
            resource_profile.get("ephemeral_storage_mib"),
            "resource_profile.ephemeral_storage_mib",
        ),
        "healthcheck": healthcheck,
        "source_ref": _require_string(artifact.get("source_ref"), "source_ref"),
        "containers": containers,
        "note": note,
    }


def check_slug_consistency(challenge, challenge_slug):
    if challenge.challenge_slug and challenge.challenge_slug != challenge_slug:
        raise ReleaseValidationError(
            "challenge_slug가 등록된 문제의 challenge_slug와 일치하지 않습니다"
        )

    # 첫 등록이 slug를 정하고, 이후 등록은 같은 slug만 허용한다. 백필 릴리스는 제외한다
    existing_slug = (
        ChallengeRelease.objects
        .filter(challenge=challenge)
        .exclude(challenge_slug="")
        .values_list("challenge_slug", flat=True)
        .first()
    )
    if existing_slug is not None and existing_slug != challenge_slug:
        raise ReleaseValidationError(
            "challenge_slug가 이 문제의 기존 릴리스와 일치하지 않습니다"
        )


def next_release_version(challenge):
    current_max = (
        ChallengeRelease.objects
        .filter(challenge=challenge)
        .aggregate(Max("version"))["version__max"]
    )
    return (current_max or 0) + 1


def create_release(challenge, validated, created_by):
    release = ChallengeRelease.objects.create(
        challenge=challenge,
        version=next_release_version(challenge),
        registry_revision=validated["registry_revision"],
        challenge_slug=validated["challenge_slug"],
        runtime_type=validated["runtime_type"],
        architecture=validated["architecture"],
        isolation_profile=validated["isolation_profile"],
        cpu_millicores=validated["cpu_millicores"],
        memory_mib=validated["memory_mib"],
        ephemeral_storage_mib=validated["ephemeral_storage_mib"],
        healthcheck=validated["healthcheck"],
        source_ref=validated["source_ref"],
        note=validated["note"],
        created_by=created_by,
    )
    ReleaseContainer.objects.bulk_create(
        [
            ReleaseContainer(
                release=release,
                name=container["name"],
                image_ref=container["image_ref"],
                ports=container["ports"],
            )
            for container in validated["containers"]
        ]
    )
    return release


def public_container(release):
    # 현 Scheduler 계약이 전달할 수 있는 대표 컨테이너를 고른다
    for container in release.containers.all():
        public_ports = [entry["port"] for entry in container.ports if entry.get("public")]
        if public_ports:
            return container, public_ports
    return None, []


def is_deployable(release):
    # Scheduler의 exposed_ports 및 격리 프로필별 공개 규칙을 확인한다
    if release.registry_revision <= 0:
        return False

    containers = list(release.containers.all())
    if not 1 <= len(containers) <= 8:
        return False
    if not has_sufficient_container_resources(release, len(containers)):
        return False

    try:
        for container in containers:
            validate_container_image(container.image_ref, release.challenge_slug, container.name)
        validate_healthcheck(
            release.healthcheck,
            {container.name: [entry["port"] for entry in container.ports] for container in containers},
        )
    except ReleaseValidationError:
        return False

    exposed_port_count = 0
    exposed_containers = []
    for container in containers:
        if not 1 <= len(container.ports) <= MAX_PORTS_PER_CONTAINER:
            return False
        public_ports = [entry["port"] for entry in container.ports if entry.get("public")]
        if public_ports:
            exposed_containers.append((container, public_ports))
        exposed_port_count += len(public_ports)

    if not 1 <= exposed_port_count <= MAX_EXPOSED_PORTS:
        return False

    if release.isolation_profile == IsolationProfile.PWN:
        return (
            len(exposed_containers) == 1
            and len(exposed_containers[0][0].ports) == 1
            and len(exposed_containers[0][1]) == 1
        )

    return True


def serialize_release(release, current_release_id=None):
    return {
        "release_id": str(release.release_id),
        "challenge_id": str(release.challenge_id),
        "version": release.version,
        "registry_revision": release.registry_revision,
        "challenge_slug": release.challenge_slug,
        "runtime_type": release.runtime_type,
        "architecture": release.architecture,
        "containers": [
            {
                "name": container.name,
                "image_ref": container.image_ref,
                "ports": container.ports,
            }
            for container in release.containers.all()
        ],
        "is_current": release.release_id == current_release_id,
        "is_deployable": is_deployable(release),
        "note": release.note,
        "created_at": isoformat_z(release.created_at),
    }
