import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.board.models import Cell, TeamChallengeAccess
from apps.challenge.models import Challenge
from apps.challenge.services import hash_flag
from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    Instance,
    InstanceStatus,
    ReleaseContainer,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


INVALID_HEALTHCHECKS = [
    {},
    [],
    {"port": 8080, "path": "/health"},
    {"container": "app", "path": "/health"},
    {"container": "app", "port": 8080},
    {"container": "missing", "port": 8080, "path": "/health"},
    {"container": ["app"], "port": 8080, "path": "/health"},
    {"container": "app", "port": 9090, "path": "/health"},
    {"container": "app", "port": True, "path": "/health"},
    {"container": "app", "port": "8080", "path": "/health"},
    *[{"container": "app", "port": 8080, "path": path} for path in (
        "", "health", "/" + "x" * 1024, "/health check", "/health\n", "/health\x00", "/health\x7f",
        "/" + "\U0001f680" * 512,
    )],
]


def artifact_payload(revision=1, slug="web-basic", containers=None, note=None, **overrides):
    # 공급망 artifact-v2.json 형식의 등록 요청 body를 만든다
    if containers is None:
        containers = [
            {
                "name": "app",
                "image": f"ghcr.io/msg-ctf/challenges/{slug}/app@sha256:{DIGEST_A}",
                "ports": [{"port": 8080, "public": True}],
            }
        ]
    artifact = {
        "schema_version": "2.0",
        "challenge_slug": slug,
        "revision": revision,
        "registry_revision": revision,
        "name": "Web Basic",
        "category": "web",
        "runtime_type": "KUBERNETES",
        "architecture": "AMD64",
        "isolation_profile": "WEB",
        "workload": {"containers": containers},
        "resource_profile": {
            "cpu_millicores": 500,
            "memory_mib": 512,
            "ephemeral_storage_mib": 1024,
        },
        "source_ref": "refs/heads/main",
        "scan_result": "PASS",
    }
    artifact.update(overrides)
    body = {"artifact": artifact}
    if note is not None:
        body["note"] = note
    return body


@override_settings(CACHES=LOCMEM)
class ReleaseTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="릴리스팀")
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.admin = User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자",
            team=None, role=Role.ADMIN,
        )
        self.challenge = Challenge.objects.create(
            title="Web Basic",
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=500,
            description="릴리스 테스트 문제",
            flag_hash=hash_flag("MSG{flag}"),
            is_published=True,
        )
        self.base_url = f"/api/v1/admin/challenges/{self.challenge.challenge_id}/releases"

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def register(self, **kwargs):
        return self.client.post(self.base_url, artifact_payload(**kwargs), format="json")

    def activate(self, release_id):
        return self.client.post(f"{self.base_url}/{release_id}/activate")

    def create_legacy_release(self):
        release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=1,
            registry_revision=0,
            challenge_slug="",
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
            isolation_profile="WEB",
            source_ref="backfill",
        )
        ReleaseContainer.objects.create(
            release=release,
            name="app",
            image_ref=f"ghcr.io/msg-ctf/challenges/web-basic/app@sha256:{DIGEST_A}",
            ports=[{"port": 8080, "public": True}],
        )
        return release


class ReleaseRegisterTests(ReleaseTestBase):
    def test_register_rejects_image_slug_or_container_mismatch(self):
        self.auth("root")
        for image_path in ("other-problem/app", "web-basic/other"):
            with self.subTest(image_path=image_path):
                body = artifact_payload()
                body["artifact"]["workload"]["containers"][0]["image"] = (
                    f"ghcr.io/msg-ctf/challenges/{image_path}@sha256:{DIGEST_A}"
                )
                response = self.client.post(self.base_url, body, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "RELEASE_INVALID")
                self.assertFalse(ChallengeRelease.objects.exists())

    def test_register_accepts_utf16_healthcheck_length_limit(self):
        self.auth("root")
        healthcheck = {"container": "app", "port": 8080, "path": "/" + "\U0001f680" * 511 + "x"}
        response = self.register(healthcheck=healthcheck)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["data"]["is_deployable"])
        self.assertEqual(self.activate(response.data["data"]["release_id"]).status_code, 200)

    def test_register_rejects_invalid_healthcheck_without_saving_release(self):
        self.auth("root")
        for location in ("artifact", "workload"):
            for healthcheck in INVALID_HEALTHCHECKS:
                with self.subTest(location=location, healthcheck=healthcheck):
                    body = artifact_payload()
                    target = body["artifact"] if location == "artifact" else body["artifact"]["workload"]
                    target["healthcheck"] = healthcheck
                    response = self.client.post(self.base_url, body, format="json")
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.data["code"], "RELEASE_INVALID")
                    self.assertFalse(ChallengeRelease.objects.exists())

    def test_register_accepts_private_healthcheck_port_and_path_length_limit(self):
        self.auth("root")
        healthcheck = {"container": "app", "port": 9090, "path": "/" + "x" * 1023}
        response = self.register(
            healthcheck=healthcheck,
            containers=[{
                "name": "app",
                "image": f"ghcr.io/msg-ctf/challenges/web-basic/app@sha256:{DIGEST_A}",
                "ports": [{"port": 8080, "public": True}, {"port": 9090, "public": False}],
            }],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ChallengeRelease.objects.get().healthcheck, healthcheck)

    def test_register_creates_version_one(self):
        # 첫 등록은 version 1로 만들어지고 배포에는 영향이 없다
        self.auth("root")
        res = self.register(note="첫 릴리스")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["code"], "SUCCESS")
        data = res.data["data"]
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["registry_revision"], 1)
        self.assertFalse(data["is_current"])
        self.assertTrue(data["is_deployable"])
        self.assertEqual(data["note"], "첫 릴리스")
        self.assertEqual(len(data["containers"]), 1)

    def test_register_increments_version(self):
        # 등록할 때마다 문제별 version이 1씩 늘어난다
        self.auth("root")
        self.register(revision=1)
        res = self.register(
            revision=2,
            containers=[
                {
                    "name": "app",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/app@sha256:{DIGEST_B}",
                    "ports": [{"port": 8080, "public": True}],
                }
            ],
        )
        self.assertEqual(res.data["data"]["version"], 2)

    def test_register_uses_artifact_isolation_profile(self):
        self.auth("root")
        res = self.register(category="pwn", isolation_profile="PWN")

        self.assertEqual(res.status_code, 200)
        release = ChallengeRelease.objects.get(
            release_id=res.data["data"]["release_id"]
        )
        self.assertEqual(release.isolation_profile, "PWN")

    def test_register_rejects_missing_isolation_profile(self):
        self.auth("root")
        body = artifact_payload()
        del body["artifact"]["isolation_profile"]

        res = self.client.post(self.base_url, body, format="json")

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_INVALID")

    def test_register_rejects_invalid_isolation_profile(self):
        self.auth("root")
        res = self.register(isolation_profile="LINUX")

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_INVALID")

    def test_register_multi_container_is_deployable(self):
        # 공개 포트가 하나인 멀티 컨테이너 릴리스는 Scheduler 계약으로 배포할 수 있다
        self.auth("root")
        res = self.register(
            containers=[
                {
                    "name": "web",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/web@sha256:{DIGEST_A}",
                    "ports": [{"port": 8080, "public": True}],
                },
                {
                    "name": "db",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/db@sha256:{DIGEST_B}",
                    "ports": [{"port": 5432, "public": False}],
                },
            ]
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["data"]["is_deployable"])

    def test_register_rejects_tag_reference(self):
        # digest가 아닌 태그 참조는 거절한다
        self.auth("root")
        res = self.register(
            containers=[
                {
                    "name": "app",
                    "image": "ghcr.io/msg-ctf/challenges/web-basic/app:latest",
                    "ports": [{"port": 8080, "public": True}],
                }
            ]
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_INVALID")

    def test_register_rejects_foreign_registry(self):
        # 허용 외 레지스트리는 거절한다
        self.auth("root")
        res = self.register(
            containers=[
                {
                    "name": "app",
                    "image": f"docker.io/msg-ctf/challenges/web-basic/app@sha256:{DIGEST_A}",
                    "ports": [{"port": 8080, "public": True}],
                }
            ]
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_INVALID")

    def test_register_rejects_invalid_container_names(self):
        self.auth("root")

        for name in ("web_app", "Web", "-web", "web-", "a" * 64):
            with self.subTest(name=name):
                res = self.register(
                    containers=[
                        {
                            "name": name,
                            "image": (
                                "ghcr.io/msg-ctf/challenges/web-basic/"
                                f"app@sha256:{DIGEST_A}"
                            ),
                            "ports": [{"port": 8080, "public": True}],
                        }
                    ]
                )

                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "RELEASE_INVALID")

        self.assertEqual(ChallengeRelease.objects.count(), 0)

    def test_register_accepts_63_character_container_name(self):
        self.auth("root")
        name = "a" * 63

        res = self.register(
            containers=[
                {
                    "name": name,
                    "image": (
                        "ghcr.io/msg-ctf/challenges/web-basic/"
                        f"{name}@sha256:{DIGEST_A}"
                    ),
                    "ports": [{"port": 8080, "public": True}],
                }
            ]
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["containers"][0]["name"], name)

    def test_register_rejects_failed_scan(self):
        # scan_result가 PASS가 아니면 거절한다
        self.auth("root")
        res = self.register(scan_result="FAIL")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_INVALID")

    def test_register_rejects_wrong_schema_version(self):
        self.auth("root")
        res = self.register(schema_version="1.0")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_INVALID")

    def test_register_rejects_missing_registry_revision(self):
        self.auth("root")
        body = artifact_payload()
        del body["artifact"]["registry_revision"]

        res = self.client.post(self.base_url, body, format="json")

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_INVALID")

    def test_register_rejects_mismatched_revision_fields(self):
        self.auth("root")

        res = self.register(registry_revision=2)

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_INVALID")

    def test_register_accepts_registry_revision_without_legacy_revision(self):
        self.auth("root")
        body = artifact_payload(revision=7)
        del body["artifact"]["revision"]

        res = self.client.post(self.base_url, body, format="json")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["registry_revision"], 7)

    def test_register_rejects_slug_mismatch(self):
        # 첫 등록이 slug를 정하고 이후 다른 slug는 거절한다
        self.auth("root")
        self.register(revision=1, slug="web-basic")
        res = self.register(
            revision=2,
            slug="other-slug",
            containers=[
                {
                    "name": "app",
                    "image": f"ghcr.io/msg-ctf/challenges/other-slug/app@sha256:{DIGEST_B}",
                    "ports": [{"port": 8080, "public": True}],
                }
            ],
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_INVALID")

    def test_register_rejects_duplicated_revision(self):
        # 같은 registry_revision 재등록은 409로 거절한다
        self.auth("root")
        self.register(revision=3)
        res = self.register(revision=3)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "RELEASE_DUPLICATED")

    def test_register_requires_admin(self):
        self.auth("player")
        res = self.register()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "FORBIDDEN")

    def test_register_unknown_challenge(self):
        self.auth("root")
        res = self.client.post(
            f"/api/v1/admin/challenges/{uuid.uuid4()}/releases",
            artifact_payload(),
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "CHALLENGE_NOT_FOUND")


class ReleaseActivateTests(ReleaseTestBase):
    def test_activate_rejects_invalid_stored_image_and_healthcheck(self):
        self.auth("root")
        release_id = self.register().data["data"]["release_id"]
        container = ReleaseContainer.objects.get(release_id=release_id)
        original_image = container.image_ref
        for image_path in ("other-problem/app", "web-basic/other"):
            with self.subTest(image_path=image_path):
                container.image_ref = f"ghcr.io/msg-ctf/challenges/{image_path}@sha256:{DIGEST_A}"
                container.save(update_fields=["image_ref"])
                response = self.activate(release_id)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "RELEASE_NOT_DEPLOYABLE")
                self.assertFalse(ChallengeRuntimeConfig.objects.exists())
        container.image_ref = original_image
        container.save(update_fields=["image_ref"])
        ChallengeRelease.objects.filter(pk=release_id).update(
            healthcheck={"container": "app", "port": 8080, "path": "/" + "\U0001f680" * 512},
        )
        self.assertEqual(self.activate(release_id).status_code, 400)
        self.assertFalse(ChallengeRuntimeConfig.objects.exists())

    def test_activate_switches_current(self):
        # 전환하면 runtime_config 포인터가 바뀌고 직전 릴리스가 응답에 남는다
        self.auth("root")
        first = self.register(revision=1).data["data"]["release_id"]
        second = self.register(
            revision=2,
            containers=[
                {
                    "name": "app",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/app@sha256:{DIGEST_B}",
                    "ports": [{"port": 8080, "public": True}],
                }
            ],
        ).data["data"]["release_id"]

        res = self.activate(first)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["data"]["previous_release_id"])

        res = self.activate(second)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["previous_release_id"], first)

        config = ChallengeRuntimeConfig.objects.get(challenge=self.challenge)
        self.assertEqual(str(config.current_release_id), second)

    def test_activate_old_release_is_rollback(self):
        # 옛 릴리스를 지정하면 그대로 롤백된다
        self.auth("root")
        first = self.register(revision=1).data["data"]["release_id"]
        second = self.register(
            revision=2,
            containers=[
                {
                    "name": "app",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/app@sha256:{DIGEST_B}",
                    "ports": [{"port": 8080, "public": True}],
                }
            ],
        ).data["data"]["release_id"]
        self.activate(second)

        res = self.activate(first)
        self.assertEqual(res.status_code, 200)
        config = ChallengeRuntimeConfig.objects.get(challenge=self.challenge)
        self.assertEqual(str(config.current_release_id), first)

    def test_activate_is_idempotent(self):
        # 이미 현재인 릴리스를 다시 전환하면 에러 없이 previous가 null이다
        self.auth("root")
        release_id = self.register(revision=1).data["data"]["release_id"]
        self.activate(release_id)

        res = self.activate(release_id)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["data"]["previous_release_id"])

    def test_activate_accepts_multi_container_release(self):
        # 공개 포트가 하나인 멀티 컨테이너 릴리스도 현재 릴리스로 전환할 수 있다
        self.auth("root")
        release_id = self.register(
            containers=[
                {
                    "name": "web",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/web@sha256:{DIGEST_A}",
                    "ports": [{"port": 8080, "public": True}],
                },
                {
                    "name": "db",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/db@sha256:{DIGEST_B}",
                    "ports": [{"port": 5432, "public": False}],
                },
            ]
        ).data["data"]["release_id"]

        res = self.activate(release_id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["release_id"], release_id)

    def test_activate_rejects_insufficient_ephemeral_storage_for_container_count(self):
        self.auth("root")
        containers = [
            {
                "name": "web",
                "image": f"ghcr.io/msg-ctf/challenges/web-basic/web@sha256:{DIGEST_A}",
                "ports": [{"port": 8080, "public": True}],
            },
            {
                "name": "db",
                "image": f"ghcr.io/msg-ctf/challenges/web-basic/db@sha256:{DIGEST_B}",
                "ports": [{"port": 5432, "public": False}],
            },
        ]

        for revision, ephemeral_storage_mib in ((10, 64), (11, 127)):
            with self.subTest(ephemeral_storage_mib=ephemeral_storage_mib):
                registered = self.register(
                    revision=revision,
                    containers=containers,
                    resource_profile={
                        "cpu_millicores": 500,
                        "memory_mib": 512,
                        "ephemeral_storage_mib": ephemeral_storage_mib,
                    },
                )

                self.assertEqual(registered.status_code, 200)
                self.assertFalse(registered.data["data"]["is_deployable"])
                activated = self.activate(registered.data["data"]["release_id"])
                self.assertEqual(activated.status_code, 400)
                self.assertEqual(activated.data["code"], "RELEASE_NOT_DEPLOYABLE")

    def test_activate_accepts_minimum_ephemeral_storage_for_container_count(self):
        self.auth("root")
        registered = self.register(
            revision=12,
            containers=[
                {
                    "name": "web",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/web@sha256:{DIGEST_A}",
                    "ports": [{"port": 8080, "public": True}],
                },
                {
                    "name": "db",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/db@sha256:{DIGEST_B}",
                    "ports": [{"port": 5432, "public": False}],
                },
            ],
            resource_profile={
                "cpu_millicores": 2,
                "memory_mib": 2,
                "ephemeral_storage_mib": 128,
            },
        )

        self.assertEqual(registered.status_code, 200)
        self.assertTrue(registered.data["data"]["is_deployable"])
        activated = self.activate(registered.data["data"]["release_id"])
        self.assertEqual(activated.status_code, 200)

    def test_activate_rejects_cpu_or_memory_below_container_count(self):
        self.auth("root")
        containers = [
            {
                "name": "web",
                "image": f"ghcr.io/msg-ctf/challenges/web-basic/web@sha256:{DIGEST_A}",
                "ports": [{"port": 8080, "public": True}],
            },
            {
                "name": "db",
                "image": f"ghcr.io/msg-ctf/challenges/web-basic/db@sha256:{DIGEST_B}",
                "ports": [{"port": 5432, "public": False}],
            },
        ]
        cases = (
            (13, 1, 512),
            (14, 500, 1),
        )

        for revision, cpu_millicores, memory_mib in cases:
            with self.subTest(
                cpu_millicores=cpu_millicores,
                memory_mib=memory_mib,
            ):
                registered = self.register(
                    revision=revision,
                    containers=containers,
                    resource_profile={
                        "cpu_millicores": cpu_millicores,
                        "memory_mib": memory_mib,
                        "ephemeral_storage_mib": 128,
                    },
                )

                self.assertEqual(registered.status_code, 200)
                self.assertFalse(registered.data["data"]["is_deployable"])
                activated = self.activate(registered.data["data"]["release_id"])
                self.assertEqual(activated.status_code, 400)
                self.assertEqual(activated.data["code"], "RELEASE_NOT_DEPLOYABLE")

    def test_activate_accepts_multiple_exposed_containers_and_ports(self):
        self.auth("root")
        registered = self.register(
            containers=[
                {
                    "name": "web",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/web@sha256:{DIGEST_A}",
                    "ports": [
                        {"port": 8080, "public": True},
                        {"port": 8443, "public": True},
                    ],
                },
                {
                    "name": "pwn",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/pwn@sha256:{DIGEST_B}",
                    "ports": [{"port": 31337, "public": True}],
                },
            ]
        )
        self.assertEqual(registered.status_code, 200)
        self.assertTrue(registered.data["data"]["is_deployable"])

        res = self.activate(registered.data["data"]["release_id"])

        self.assertEqual(res.status_code, 200)

    def test_activate_accepts_mixed_public_and_private_ports_for_web(self):
        self.auth("root")
        registered = self.register(
            containers=[
                {
                    "name": "web",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/web@sha256:{DIGEST_A}",
                    "ports": [
                        {"port": 8080, "public": True},
                        {"port": 9090, "public": False},
                    ],
                }
            ]
        )
        self.assertEqual(registered.status_code, 200)
        self.assertTrue(registered.data["data"]["is_deployable"])

        res = self.activate(registered.data["data"]["release_id"])

        self.assertEqual(res.status_code, 200)

    def test_activate_rejects_multiple_public_containers_for_pwn(self):
        self.auth("root")
        registered = self.register(
            isolation_profile="PWN",
            containers=[
                {
                    "name": "pwn",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/pwn@sha256:{DIGEST_A}",
                    "ports": [{"port": 31337, "public": True}],
                },
                {
                    "name": "admin",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/admin@sha256:{DIGEST_B}",
                    "ports": [{"port": 8080, "public": True}],
                },
            ],
        )

        self.assertEqual(registered.status_code, 200)
        self.assertFalse(registered.data["data"]["is_deployable"])
        res = self.activate(registered.data["data"]["release_id"])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_NOT_DEPLOYABLE")

    def test_activate_rejects_multiple_declared_ports_on_public_pwn_container(self):
        self.auth("root")
        registered = self.register(
            isolation_profile="PWN",
            containers=[
                {
                    "name": "pwn",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/pwn@sha256:{DIGEST_A}",
                    "ports": [
                        {"port": 31337, "public": True},
                        {"port": 9000, "public": False},
                    ],
                }
            ],
        )

        self.assertEqual(registered.status_code, 200)
        self.assertFalse(registered.data["data"]["is_deployable"])
        res = self.activate(registered.data["data"]["release_id"])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_NOT_DEPLOYABLE")

    def test_activate_accepts_private_helper_container_for_pwn(self):
        self.auth("root")
        registered = self.register(
            isolation_profile="PWN",
            containers=[
                {
                    "name": "pwn",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/pwn@sha256:{DIGEST_A}",
                    "ports": [{"port": 31337, "public": True}],
                },
                {
                    "name": "db",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/db@sha256:{DIGEST_B}",
                    "ports": [{"port": 5432, "public": False}],
                },
            ],
        )

        self.assertEqual(registered.status_code, 200)
        self.assertTrue(registered.data["data"]["is_deployable"])
        res = self.activate(registered.data["data"]["release_id"])
        self.assertEqual(res.status_code, 200)

    def test_activate_rejects_more_than_eight_public_ports(self):
        self.auth("root")
        registered = self.register(
            containers=[
                {
                    "name": "web",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/web@sha256:{DIGEST_A}",
                    "ports": [
                        {"port": port, "public": True}
                        for port in range(8001, 8010)
                    ],
                }
            ]
        )
        self.assertEqual(registered.status_code, 200)
        self.assertFalse(registered.data["data"]["is_deployable"])

        res = self.activate(registered.data["data"]["release_id"])

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_NOT_DEPLOYABLE")

    def test_activate_unknown_release(self):
        self.auth("root")
        res = self.activate(uuid.uuid4())
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "RELEASE_NOT_FOUND")

    def test_activate_rejects_legacy_registry_revision(self):
        release = self.create_legacy_release()

        self.auth("root")
        res = self.activate(release.release_id)

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_NOT_DEPLOYABLE")


class ReleaseListTests(ReleaseTestBase):
    def test_list_returns_versions_desc_with_current(self):
        # 이력은 최신 버전부터 내려가고 현재 버전이 표시된다
        self.auth("root")
        first = self.register(revision=1).data["data"]["release_id"]
        self.register(
            revision=2,
            containers=[
                {
                    "name": "app",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/app@sha256:{DIGEST_B}",
                    "ports": [{"port": 8080, "public": True}],
                }
            ],
        )
        self.activate(first)

        res = self.client.get(self.base_url)
        self.assertEqual(res.status_code, 200)
        data = res.data["data"]
        self.assertEqual(data["total_count"], 2)
        self.assertEqual(data["current_release_id"], first)
        self.assertEqual([row["version"] for row in data["releases"]], [2, 1])
        self.assertEqual(
            [row["is_current"] for row in data["releases"]], [False, True]
        )

    def test_list_empty_challenge(self):
        self.auth("root")
        res = self.client.get(self.base_url)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["data"]["current_release_id"])
        self.assertEqual(res.data["data"]["total_count"], 0)


@override_settings(CACHES=LOCMEM, SCHEDULER_API_TOKEN="test-scheduler-token")
class ReleaseInstanceCreateTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.player_url = "/api/v1/instances"
        self.challenge_cell = Cell.objects.create(
            cell_index=1,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="release-instance-challenge",
        )
        TeamChallengeAccess.objects.create(
            team=self.team,
            challenge=self.challenge,
            source_cell=self.challenge_cell,
        )

    @patch("apps.instances.services.scheduler_request")
    def test_create_preserves_registered_healthcheck(self, scheduler_request):
        healthcheck = {"container": "app", "port": 8080, "path": "/health"}
        cases = (
            ("artifact", healthcheck),
            ("workload", healthcheck),
            ("artifact", None),
        )
        for revision, (location, expected) in enumerate(cases, start=1):
            with self.subTest(location=location, healthcheck=expected):
                self.auth("root")
                payload = artifact_payload(revision=revision)
                target = payload["artifact"]
                if location == "workload":
                    target = target["workload"]
                if expected is not None:
                    target["healthcheck"] = expected
                elif revision == 3:
                    target["healthcheck"] = None

                registered = self.client.post(self.base_url, payload, format="json")
                self.assertEqual(registered.status_code, 200)
                release_id = registered.data["data"]["release_id"]
                release = ChallengeRelease.objects.get(pk=release_id)
                self.assertEqual(release.healthcheck, expected)
                self.assertEqual(self.activate(release_id).status_code, 200)

                scheduler_request.return_value = {
                    "instance_id": str(uuid.uuid4()),
                    "status": "REQUESTED",
                }
                self.auth("player")
                response = self.client.post(
                    self.player_url,
                    {"challenge_id": str(self.challenge.pk)},
                    format="json",
                )

                self.assertEqual(response.status_code, 202)
                body = scheduler_request.call_args.kwargs["body"]
                self.assertEqual(body["registry_revision"], revision)
                if expected is None:
                    self.assertNotIn("healthcheck", body)
                else:
                    self.assertEqual(body["healthcheck"], expected)

    @patch("apps.instances.services.scheduler_request")
    def test_create_rejects_invalid_stored_healthcheck_before_scheduler(self, scheduler_request):
        self.auth("root")
        release_id = self.register().data["data"]["release_id"]
        self.assertEqual(self.activate(release_id).status_code, 200)
        self.auth("player")
        for healthcheck in INVALID_HEALTHCHECKS:
            # PostgreSQL JSONB cannot store NUL; registration rejection covers it.
            if isinstance(healthcheck, dict) and "\x00" in healthcheck.get("path", ""):
                continue
            with self.subTest(healthcheck=healthcheck):
                ChallengeRelease.objects.filter(pk=release_id).update(healthcheck=healthcheck)
                response = self.client.post(
                    self.player_url, {"challenge_id": str(self.challenge.pk)}, format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "RELEASE_NOT_DEPLOYABLE")
                scheduler_request.assert_not_called()
                self.assertFalse(Instance.objects.exists())

    @patch("apps.instances.services.scheduler_request")
    def test_create_rejects_invalid_stored_image_before_scheduler(self, scheduler_request):
        self.auth("root")
        release_id = self.register().data["data"]["release_id"]
        self.assertEqual(self.activate(release_id).status_code, 200)
        self.auth("player")
        for image_path in ("other-problem/app", "web-basic/other"):
            with self.subTest(image_path=image_path):
                ReleaseContainer.objects.filter(release_id=release_id).update(
                    image_ref=f"ghcr.io/msg-ctf/challenges/{image_path}@sha256:{DIGEST_A}",
                )
                response = self.client.post(
                    self.player_url, {"challenge_id": str(self.challenge.pk)}, format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "RELEASE_NOT_DEPLOYABLE")
                scheduler_request.assert_not_called()
                self.assertFalse(Instance.objects.exists())

    def test_create_without_current_release_fails(self):
        # 활성 릴리스가 없으면 인스턴스를 만들 수 없다
        self.auth("root")
        self.register(revision=1)

        self.auth("player")
        res = self.client.post(
            self.player_url,
            {"challenge_id": str(self.challenge.challenge_id)},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "RUNTIME_CONFIG_NOT_FOUND")

    @patch("apps.instances.services.scheduler_request")
    def test_create_rejects_legacy_current_release_before_scheduler(self, scheduler_request):
        legacy_release = self.create_legacy_release()
        ChallengeRuntimeConfig.objects.create(
            challenge=self.challenge,
            current_release=legacy_release,
        )

        self.auth("player")
        res = self.client.post(
            self.player_url,
            {"challenge_id": str(self.challenge.challenge_id)},
            format="json",
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "RELEASE_NOT_DEPLOYABLE")
        scheduler_request.assert_not_called()

    @patch("apps.instances.services.scheduler_request")
    def test_create_revalidates_container_resources_before_scheduler(self, scheduler_request):
        self.auth("root")
        release_id = self.register(
            revision=15,
            containers=[
                {
                    "name": "web",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/web@sha256:{DIGEST_A}",
                    "ports": [{"port": 8080, "public": True}],
                },
                {
                    "name": "db",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/db@sha256:{DIGEST_B}",
                    "ports": [{"port": 5432, "public": False}],
                },
            ],
        ).data["data"]["release_id"]
        self.activate(release_id)
        release = ChallengeRelease.objects.get(release_id=release_id)
        release.ephemeral_storage_mib = 127
        release.save(update_fields=["ephemeral_storage_mib"])

        self.auth("player")
        response = self.client.post(
            self.player_url,
            {"challenge_id": str(self.challenge.challenge_id)},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "RELEASE_NOT_DEPLOYABLE")
        scheduler_request.assert_not_called()

    @patch("apps.instances.services.scheduler_request")
    def test_legacy_transition_requires_registration_and_activation(self, scheduler_request):
        legacy = self.create_legacy_release()
        config = ChallengeRuntimeConfig.objects.create(
            challenge=self.challenge, current_release=legacy,
            ttl_minutes=45, hard_timeout_minutes=90,
        )
        payload = {"challenge_id": str(self.challenge.pk)}
        self.auth("player")
        response = self.client.post(self.player_url, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "RELEASE_NOT_DEPLOYABLE")
        scheduler_request.assert_not_called()

        self.auth("root")
        image = f"ghcr.io/msg-ctf/challenges/web-basic/app@sha256:{DIGEST_B}"
        response = self.register(
            revision=7, isolation_profile="PWN", architecture="ARM64",
            containers=[{"name": "app", "image": image,
                         "ports": [{"port": 31337, "public": True}]}],
        )
        self.assertEqual(response.status_code, 200)
        release_id = response.data["data"]["release_id"]
        self.assertEqual(response.data["data"]["version"], 2)
        config.refresh_from_db()
        self.assertEqual(config.current_release_id, legacy.pk)

        self.auth("player")
        response = self.client.post(self.player_url, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "RELEASE_NOT_DEPLOYABLE")
        scheduler_request.assert_not_called()

        self.auth("root")
        response = self.activate(release_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["previous_release_id"], str(legacy.pk))
        config.refresh_from_db()
        self.assertEqual(str(config.current_release_id), release_id)
        self.assertEqual((config.ttl_minutes, config.hard_timeout_minutes), (45, 90))

        instance_id = uuid.uuid4()
        scheduler_request.return_value = {
            "instance_id": str(instance_id), "team_id": str(self.team.pk),
            "user_id": str(self.player.pk), "challenge_id": str(self.challenge.pk),
            "registry_revision": 7, "status": "REQUESTED",
            "expires_at": "2026-11-08T10:00:00Z",
            "hard_expires_at": "2026-11-08T11:00:00Z",
        }
        self.auth("player")
        response = self.client.post(self.player_url, payload, format="json")
        self.assertEqual(response.status_code, 202)
        scheduler_request.assert_called_once()
        body = scheduler_request.call_args.kwargs.get("body")
        if body is None:
            body = scheduler_request.call_args.args[2]
        self.assertEqual(body["registry_revision"], 7)
        self.assertEqual(body["isolation_profile"], "PWN")
        self.assertEqual(body["architecture"], "ARM64")
        self.assertEqual(body["containers"], [
            {"name": "app", "image": image, "ports": [31337], "exposed_ports": [31337]}
        ])
        self.assertEqual((body["ttl_minutes"], body["hard_timeout_minutes"]), (45, 90))
        self.assertEqual(str(Instance.objects.get(pk=instance_id).release_id), release_id)
        legacy.refresh_from_db()
        self.assertEqual(legacy.registry_revision, 0)
        self.assertEqual(legacy.containers.get().image_ref,
                         f"ghcr.io/msg-ctf/challenges/web-basic/app@sha256:{DIGEST_A}")

    @patch("apps.instances.services.scheduler_request")
    def test_create_uses_current_release_and_saves_snapshot(self, scheduler_request):
        # 생성 요청은 현재 릴리스의 멀티 컨테이너 값으로 나가고 인스턴스에 릴리스가 스냅샷된다
        self.auth("root")
        release_id = self.register(
            revision=1,
            containers=[
                {
                    "name": "web",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/web@sha256:{DIGEST_A}",
                    "ports": [
                        {"port": 8080, "public": True},
                        {"port": 8443, "public": False},
                    ],
                },
                {
                    "name": "db",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/db@sha256:{DIGEST_B}",
                    "ports": [{"port": 5432, "public": False}],
                },
                {
                    "name": "pwn",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/pwn@sha256:{DIGEST_C}",
                    "ports": [{"port": 31337, "public": True}],
                },
            ],
        ).data["data"]["release_id"]
        self.activate(release_id)

        instance_id = uuid.uuid4()
        scheduler_request.return_value = {
            "instance_id": str(instance_id),
            "team_id": str(self.team.team_id),
            "user_id": str(self.player.user_id),
            "challenge_id": str(self.challenge.challenge_id),
            "status": "REQUESTED",
            "expires_at": "2026-11-08T10:00:00Z",
            "hard_expires_at": "2026-11-08T11:00:00Z",
        }

        self.auth("player")
        res = self.client.post(
            self.player_url,
            {"challenge_id": str(self.challenge.challenge_id)},
            format="json",
        )
        self.assertEqual(res.status_code, 202)

        body = scheduler_request.call_args.kwargs.get("body")
        if body is None:
            body = scheduler_request.call_args.args[2]
        self.assertEqual(
            scheduler_request.call_args.kwargs["auth_header"],
            "Bearer test-scheduler-token",
        )
        release = ChallengeRelease.objects.get(release_id=release_id)
        self.assertEqual(body["registry_revision"], release.registry_revision)
        self.assertEqual(body["isolation_profile"], "WEB")
        self.assertEqual(body["architecture"], "AMD64")
        self.assertNotIn("internal_connections", body)
        self.assertTrue(all("expose" not in container for container in body["containers"]))
        self.assertEqual(
            body["containers"],
            [
                {
                    "name": "db",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/db@sha256:{DIGEST_B}",
                    "ports": [5432],
                    "exposed_ports": [],
                },
                {
                    "name": "pwn",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/pwn@sha256:{DIGEST_C}",
                    "ports": [31337],
                    "exposed_ports": [31337],
                },
                {
                    "name": "web",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/web@sha256:{DIGEST_A}",
                    "ports": [8080, 8443],
                    "exposed_ports": [8080],
                },
            ],
        )

        from apps.instances.models import Instance

        instance = Instance.objects.get(instance_id=instance_id)
        self.assertEqual(instance.release_id, release.release_id)

    def test_create_from_scheduler_restores_release_by_registry_revision(self):
        self.auth("root")
        release_id = self.register(revision=1).data["data"]["release_id"]
        self.register(
            revision=2,
            containers=[
                {
                    "name": "app",
                    "image": f"ghcr.io/msg-ctf/challenges/web-basic/app@sha256:{DIGEST_B}",
                    "ports": [{"port": 8080, "public": True}],
                }
            ],
        )

        from apps.instances.services import create_instance_from_scheduler

        instance_id = uuid.uuid4()
        instance = create_instance_from_scheduler(
            {
                "instance_id": str(instance_id),
                "challenge_id": str(self.challenge.challenge_id),
                "registry_revision": 1,
                "status": "RUNNING",
                "service_url": "https://instance.example",
                "expires_at": "2026-11-08T10:00:00Z",
                "hard_expires_at": "2026-11-08T11:00:00Z",
            },
            user=self.player,
            team=self.team,
            challenge=self.challenge,
        )

        self.assertEqual(str(instance.release_id), release_id)
        self.assertEqual(instance.host, "https://instance.example")
        self.assertEqual(instance.endpoints, [])

    def test_create_from_scheduler_rejects_missing_registry_revision(self):
        self.auth("root")
        release_id = self.register(revision=1).data["data"]["release_id"]
        self.activate(release_id)

        from apps.instances.services import SchedulerError, create_instance_from_scheduler

        with self.assertRaises(SchedulerError) as caught:
            create_instance_from_scheduler(
                {
                    "instance_id": str(uuid.uuid4()),
                    "challenge_id": str(self.challenge.challenge_id),
                    "status": "RUNNING",
                    "service_url": "https://instance.example",
                    "expires_at": "2026-11-08T10:00:00Z",
                    "hard_expires_at": "2026-11-08T11:00:00Z",
                },
                user=self.player,
                team=self.team,
                challenge=self.challenge,
            )

        self.assertEqual(caught.exception.code, "SCHEDULER_UNAVAILABLE")

    def test_create_from_scheduler_rejects_unknown_registry_revision(self):
        self.auth("root")
        self.register(revision=1)

        from apps.instances.services import SchedulerError, create_instance_from_scheduler

        with self.assertRaises(SchedulerError) as caught:
            create_instance_from_scheduler(
                {
                    "instance_id": str(uuid.uuid4()),
                    "challenge_id": str(self.challenge.challenge_id),
                    "registry_revision": 9,
                    "status": "RUNNING",
                    "service_url": "https://instance.example",
                    "expires_at": "2026-11-08T10:00:00Z",
                    "hard_expires_at": "2026-11-08T11:00:00Z",
                },
                user=self.player,
                team=self.team,
                challenge=self.challenge,
            )

        self.assertEqual(caught.exception.code, "SCHEDULER_UNAVAILABLE")

    def test_create_from_scheduler_rejects_existing_instance_scope_mismatch(self):
        self.auth("root")
        release_id = self.register(revision=1).data["data"]["release_id"]
        release = ChallengeRelease.objects.get(release_id=release_id)
        other_user = User.objects.create_user(
            login_id="other-player",
            password="pw1234",
            nickname="다른 참가자",
            team=self.team,
        )
        other_team = Team.objects.create(team_name="다른 팀")
        other_challenge = Challenge.objects.create(
            title="Other Basic",
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=500,
            description="다른 문제",
            flag_hash=hash_flag("MSG{other}"),
            is_published=True,
        )
        ChallengeRelease.objects.create(
            challenge=other_challenge,
            version=1,
            registry_revision=1,
            challenge_slug="other-basic",
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
            isolation_profile="WEB",
            source_ref="refs/heads/main",
        )

        from apps.instances.services import SchedulerError, create_instance_from_scheduler

        cases = [
            ("user", other_user, self.team, self.challenge),
            ("team", self.player, other_team, self.challenge),
            ("challenge", self.player, self.team, other_challenge),
        ]
        for field_name, user, team, challenge in cases:
            with self.subTest(field_name=field_name):
                instance_id = uuid.uuid4()
                existing = Instance.objects.create(
                    instance_id=instance_id,
                    user=self.player,
                    team=self.team,
                    challenge=self.challenge,
                    status=InstanceStatus.RUNNING,
                    release=release,
                )

                with self.assertRaises(SchedulerError) as caught:
                    create_instance_from_scheduler(
                        {
                            "instance_id": str(instance_id),
                            "challenge_id": str(challenge.challenge_id),
                            "registry_revision": 1,
                            "status": "RUNNING",
                            "service_url": "https://instance.example",
                            "expires_at": "2026-11-08T10:00:00Z",
                            "hard_expires_at": "2026-11-08T11:00:00Z",
                        },
                        user=user,
                        team=team,
                        challenge=challenge,
                    )

                self.assertEqual(caught.exception.code, "SCHEDULER_UNAVAILABLE")
                existing.refresh_from_db()
                self.assertEqual(existing.user_id, self.player.user_id)
                self.assertEqual(existing.team_id, self.team.team_id)
                self.assertEqual(existing.challenge_id, self.challenge.challenge_id)

    @patch("apps.instances.views.call_scheduler_active")
    def test_my_instance_recovery_rejects_missing_registry_revision(self, scheduler_active):
        self.auth("root")
        release_id = self.register(revision=1).data["data"]["release_id"]
        self.activate(release_id)
        scheduler_active.return_value = {
            "instance_id": str(uuid.uuid4()),
            "challenge_id": str(self.challenge.challenge_id),
            "status": "RUNNING",
            "service_url": "https://instance.example",
            "expires_at": "2026-11-08T10:00:00Z",
            "hard_expires_at": "2026-11-08T11:00:00Z",
        }

        self.auth("player")
        res = self.client.get("/api/v1/teams/me/instances")

        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data["code"], "SCHEDULER_UNAVAILABLE")
