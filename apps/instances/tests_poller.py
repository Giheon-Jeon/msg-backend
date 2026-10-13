import io
import json
import zipfile
from unittest.mock import patch
from urllib.request import HTTPRedirectHandler

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.challenge.models import Challenge
from apps.challenge.services import hash_flag
from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    PollerArtifact,
)
from apps.instances.poller import (
    github_request,
    list_bundle_artifacts,
    poll_once,
    register_bundle,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
SHA_A = "1" * 40
SHA_B = "2" * 40


def bundle(
    revision=1,
    slug="web-basic",
    name="Web Basic",
    container_name="app",
    digest=DIGEST_A,
    scan_result="PASS",
    challenge_id=None,
    source_ref="refs/heads/main",
    source_sha=SHA_A,
):
    data = {
        "schema_version": "2.0",
        "challenge_slug": slug,
        "revision": revision,
        "registry_revision": revision,
        "name": name,
        "category": "web",
        "runtime_type": "KUBERNETES",
        "architecture": "AMD64",
        "isolation_profile": "WEB",
        "workload": {
            "containers": [
                {
                    "name": container_name,
                    "image": f"ghcr.io/msg-ctf/challenges/{slug}/app@sha256:{digest}",
                    "ports": [{"port": 8080, "public": True}],
                }
            ]
        },
        "resource_profile": {
            "cpu_millicores": 500,
            "memory_mib": 512,
            "ephemeral_storage_mib": 1024,
        },
        "source_ref": source_ref,
        "source_sha": source_sha,
        "scan_result": scan_result,
    }
    if challenge_id is not None:
        data["challenge_id"] = str(challenge_id)
    return data


def publish_artifact(artifact_id, name, sha=SHA_A, branch="main", expired=False):
    return {
        "id": artifact_id,
        "name": name,
        "expired": expired,
        "workflow_run": {
            "id": artifact_id + 1000,
            "head_branch": branch,
            "head_sha": sha,
        },
    }


def workflow_run(artifact_id, sha=SHA_A, branch="main", status="completed", conclusion="success"):
    return json.dumps(
        {
            "id": artifact_id + 1000,
            "status": status,
            "conclusion": conclusion,
            "head_branch": branch,
            "head_sha": sha,
        }
    ).encode("utf-8")


def bundle_zip(artifact_data):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("artifact-v2.json", json.dumps(artifact_data))
        archive.writestr("registry-publish.json", "{}")
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_urlopen(responses):
    # 호출 순서대로 준비된 응답을 돌려주는 urlopen 대역
    queue = list(responses)

    def opener(request, timeout=10):
        return FakeResponse(queue.pop(0))

    return opener


class GithubRequestTests(TestCase):
    def test_does_not_forward_token_to_redirected_artifact_storage(self):
        requests = []

        def opener(request, timeout=10):
            requests.append(request)
            return FakeResponse(b"{}")

        with patch("apps.instances.poller.urlopen", opener):
            github_request("/test", token="test-token")

        request = requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")

        redirected = HTTPRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://artifact-storage.example/bundle.zip",
        )
        self.assertIsNone(redirected.get_header("Authorization"))


class ListBundleArtifactsTests(TestCase):
    def test_returns_new_artifacts_oldest_first(self):
        responses = [
            json.dumps(
                {
                    "artifacts": [
                        publish_artifact(12, "new-publish-bundle"),
                        publish_artifact(11, "old-publish-bundle"),
                    ]
                }
            ).encode("utf-8")
        ]

        with patch("apps.instances.poller.urlopen", fake_urlopen(responses)):
            artifacts = list_bundle_artifacts(token="test-token")

        self.assertEqual([artifact["id"] for artifact in artifacts], [11, 12])

    @override_settings(RELEASE_POLL_LIMIT=2)
    def test_lists_unexpired_publish_bundles_across_all_pages(self):
        responses = [
            json.dumps(
                {
                    "total_count": 4,
                    "artifacts": [
                        {"id": 1, "name": "test-output", "expired": False},
                        {
                            "id": 2,
                            "name": "web-a-publish-bundle",
                            "expired": False,
                        },
                    ],
                }
            ).encode("utf-8"),
            json.dumps(
                {
                    "total_count": 4,
                    "artifacts": [
                        {
                            "id": 3,
                            "name": "web-b-publish-bundle",
                            "expired": False,
                        },
                        {
                            "id": 4,
                            "name": "web-old-publish-bundle",
                            "expired": True,
                        },
                    ],
                }
            ).encode("utf-8"),
        ]

        with patch("apps.instances.poller.urlopen", fake_urlopen(responses)):
            artifacts = list_bundle_artifacts(token="test-token")

        self.assertEqual([artifact["id"] for artifact in artifacts], [2, 3])

    @override_settings(RELEASE_POLL_LIMIT=3)
    def test_stops_listing_at_processed_artifact_boundary(self):
        PollerArtifact.objects.create(
            artifact_id=10,
            kind=PollerArtifact.Kind.RELEASE,
            payload=publish_artifact(10, "old-publish-bundle"),
            processed_at=timezone.now(),
        )
        responses = [
            json.dumps(
                {
                    "total_count": 100,
                    "artifacts": [
                        publish_artifact(12, "new-publish-bundle"),
                        publish_artifact(11, "test-output"),
                        publish_artifact(10, "old-publish-bundle"),
                    ],
                }
            ).encode("utf-8")
        ]

        with patch("apps.instances.poller.urlopen", fake_urlopen(responses)):
            artifacts = list_bundle_artifacts(token="test-token")

        self.assertEqual([artifact["id"] for artifact in artifacts], [12])
        self.assertIsNone(PollerArtifact.objects.get(pk=12).processed_at)


@override_settings(CACHES=LOCMEM)
class PollerTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.challenge = Challenge.objects.create(
            title="Web Basic",
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=500,
            description="폴러 테스트 문제",
            flag_hash=hash_flag("MSG{flag}"),
            is_published=True,
        )


class RegisterBundleTests(PollerTestBase):
    def test_registers_first_bundle_by_challenge_slug(self):
        self.challenge.challenge_slug = "web-basic"
        self.challenge.save(update_fields=["challenge_slug"])

        status, release = register_bundle(bundle(revision=1))

        self.assertEqual(status, "registered")
        self.assertEqual(release.challenge_id, self.challenge.challenge_id)
        self.assertEqual(release.registry_revision, 1)

    def test_registers_new_bundle_by_challenge_id_match(self):
        # slug 이력이 없으면 bundle의 문제명으로 문제를 찾아 등록한다
        status, release = register_bundle(
            bundle(revision=1, challenge_id=self.challenge.challenge_id)
        )
        self.assertEqual(status, "registered")
        self.assertEqual(release.challenge_id, self.challenge.challenge_id)
        self.assertEqual(release.version, 1)
        self.assertEqual(release.created_by, "release-poller")

    def test_rejects_challenge_id_with_different_registered_slug(self):
        self.challenge.challenge_slug = "registered-slug"
        self.challenge.save(update_fields=["challenge_slug"])

        status, detail = register_bundle(
            bundle(
                revision=1,
                slug="different-slug",
                challenge_id=self.challenge.challenge_id,
            )
        )

        self.assertEqual(status, "invalid")
        self.assertIn("challenge_slug", detail)
        self.assertEqual(ChallengeRelease.objects.count(), 0)

    def test_matches_by_existing_release_slug_first(self):
        # 같은 slug의 릴리스가 있으면 제목과 무관하게 그 문제로 등록한다
        register_bundle(bundle(revision=1, challenge_id=self.challenge.challenge_id))
        self.challenge.title = "이름이 바뀐 문제"
        self.challenge.save(update_fields=["title"])

        status, release = register_bundle(bundle(revision=2, digest=DIGEST_B))
        self.assertEqual(status, "registered")
        self.assertEqual(release.challenge_id, self.challenge.challenge_id)
        self.assertEqual(release.version, 2)

    def test_skips_duplicate_revision(self):
        register_bundle(bundle(revision=1, challenge_id=self.challenge.challenge_id))
        status, detail = register_bundle(bundle(revision=1, digest=DIGEST_B))
        self.assertEqual(status, "duplicate")
        self.assertEqual(detail, 1)
        self.assertEqual(ChallengeRelease.objects.count(), 1)

    def test_unmatched_bundle_is_skipped(self):
        status, detail = register_bundle(bundle(slug="unknown-slug", name="없는 문제"))
        self.assertEqual(status, "unmatched")
        self.assertEqual(detail, "unknown-slug")
        self.assertEqual(ChallengeRelease.objects.count(), 0)

    def test_invalid_bundle_is_skipped(self):
        status, _ = register_bundle(bundle(scan_result="FAIL"))
        self.assertEqual(status, "invalid")
        self.assertEqual(ChallengeRelease.objects.count(), 0)

    def test_bundle_with_invalid_container_name_is_skipped(self):
        status, _ = register_bundle(bundle(container_name="web_app"))

        self.assertEqual(status, "invalid")
        self.assertEqual(ChallengeRelease.objects.count(), 0)

    def test_revision_mismatch_bundle_is_skipped(self):
        artifact_data = bundle(revision=1)
        artifact_data["registry_revision"] = 2

        status, _ = register_bundle(artifact_data)

        self.assertEqual(status, "invalid")
        self.assertEqual(ChallengeRelease.objects.count(), 0)

    def test_never_activates(self):
        # 자동 등록은 전환하지 않는다. 배포 버전 선택은 관리자 몫이다
        register_bundle(bundle(revision=1, challenge_id=self.challenge.challenge_id))
        config = ChallengeRuntimeConfig.objects.filter(challenge=self.challenge).first()
        self.assertTrue(config is None or config.current_release_id is None)


class PollOnceTests(PollerTestBase):
    def test_poll_once_registers_only_new_bundles(self):
        artifacts_page = json.dumps(
            {
                "artifacts": [
                    publish_artifact(11, "web-basic-100-1-x-publish-bundle"),
                    publish_artifact(12, "web-basic-101-1-x-publish-bundle"),
                    publish_artifact(13, "challenge-metadata-100-1-x"),
                    publish_artifact(14, "old-999-1-x-publish-bundle", expired=True),
                ]
            }
        ).encode("utf-8")
        responses = [
            artifacts_page,
            workflow_run(11),
            bundle_zip(bundle(revision=1, challenge_id=self.challenge.challenge_id)),
            workflow_run(12),
            bundle_zip(bundle(revision=1, challenge_id=self.challenge.challenge_id, digest=DIGEST_B)),
        ]
        with patch("apps.instances.poller.urlopen", fake_urlopen(responses)):
            summary = poll_once(token="test-token")

        self.assertEqual(summary["registered"], 1)
        self.assertEqual(summary["duplicate"], 1)
        self.assertEqual(summary["error"], 0)
        self.assertEqual(ChallengeRelease.objects.count(), 1)

    def test_poll_once_does_not_redownload_processed_artifact(self):
        artifact = publish_artifact(11, "web-basic-100-1-x-publish-bundle")
        artifacts_page = json.dumps({"artifacts": [artifact]}).encode("utf-8")
        responses = [
            artifacts_page,
            workflow_run(11),
            bundle_zip(bundle(revision=1, challenge_id=self.challenge.challenge_id)),
            artifacts_page,
        ]
        requested_urls = []

        def opener(request, timeout=10):
            requested_urls.append(request.full_url)
            return FakeResponse(responses.pop(0))

        with patch("apps.instances.poller.urlopen", opener):
            first = poll_once(token="test-token")
            second = poll_once(token="test-token")

        self.assertEqual(first["registered"], 1)
        self.assertEqual(second["registered"], 0)
        self.assertEqual(second["duplicate"], 0)
        self.assertEqual(len(requested_urls), 4)
        self.assertEqual(sum("/actions/runs/" in url for url in requested_urls), 1)
        self.assertEqual(sum("/zip" in url for url in requested_urls), 1)
        self.assertIsNotNone(PollerArtifact.objects.get(pk=11).processed_at)

    def test_poll_once_rejects_failed_workflow_run(self):
        artifacts_page = json.dumps(
            {"artifacts": [publish_artifact(11, "web-basic-100-1-x-publish-bundle")]}
        ).encode("utf-8")
        responses = [artifacts_page, workflow_run(11, conclusion="failure")]

        with patch("apps.instances.poller.urlopen", fake_urlopen(responses)):
            summary = poll_once(token="test-token")

        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["registered"], 0)
        self.assertEqual(ChallengeRelease.objects.count(), 0)
        self.assertIsNotNone(PollerArtifact.objects.get(pk=11).processed_at)

    def test_poll_once_retries_running_workflow_after_success(self):
        for offset, status in enumerate(("queued", "in_progress")):
            with self.subTest(status=status):
                artifact_id = 20 + offset
                revision = offset + 1
                sha = SHA_A if offset == 0 else SHA_B
                digest = DIGEST_A if offset == 0 else DIGEST_B
                artifact = publish_artifact(
                    artifact_id,
                    f"web-basic-{revision}-1-x-publish-bundle",
                    sha=sha,
                )
                responses = [
                    json.dumps({"artifacts": [artifact]}).encode("utf-8"),
                    workflow_run(
                        artifact_id,
                        sha=sha,
                        status=status,
                        conclusion=None,
                    ),
                    json.dumps({"artifacts": []}).encode("utf-8"),
                    workflow_run(artifact_id, sha=sha),
                    bundle_zip(
                        bundle(
                            revision=revision,
                            challenge_id=self.challenge.challenge_id,
                            source_sha=sha,
                            digest=digest,
                        )
                    ),
                ]

                with patch("apps.instances.poller.urlopen", fake_urlopen(responses)):
                    pending = poll_once(token="test-token")
                    record = PollerArtifact.objects.get(pk=artifact_id)
                    self.assertEqual(pending["registered"], 0)
                    self.assertEqual(pending["invalid"], 0)
                    self.assertIsNone(record.processed_at)

                    completed = poll_once(token="test-token")

                self.assertEqual(completed["registered"], 1)
                record.refresh_from_db()
                self.assertIsNotNone(record.processed_at)
                self.assertTrue(
                    ChallengeRelease.objects.filter(
                        challenge=self.challenge,
                        registry_revision=revision,
                    ).exists()
                )

    def test_poll_once_rejects_wrong_branch(self):
        artifacts_page = json.dumps(
            {
                "artifacts": [
                    publish_artifact(
                        11,
                        "web-basic-100-1-x-publish-bundle",
                        branch="feature/test",
                    )
                ]
            }
        ).encode("utf-8")
        responses = [artifacts_page, workflow_run(11, branch="feature/test")]

        with patch("apps.instances.poller.urlopen", fake_urlopen(responses)):
            summary = poll_once(token="test-token")

        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["registered"], 0)
        self.assertEqual(ChallengeRelease.objects.count(), 0)

    def test_poll_once_rejects_source_ref_mismatch(self):
        artifacts_page = json.dumps(
            {"artifacts": [publish_artifact(11, "web-basic-100-1-x-publish-bundle")]}
        ).encode("utf-8")
        responses = [
            artifacts_page,
            workflow_run(11),
            bundle_zip(
                bundle(
                    revision=1,
                    challenge_id=self.challenge.challenge_id,
                    source_ref="refs/heads/feature/test",
                )
            ),
        ]

        with patch("apps.instances.poller.urlopen", fake_urlopen(responses)):
            summary = poll_once(token="test-token")

        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["registered"], 0)
        self.assertEqual(ChallengeRelease.objects.count(), 0)

    def test_poll_once_rejects_source_sha_mismatch(self):
        artifacts_page = json.dumps(
            {"artifacts": [publish_artifact(11, "web-basic-100-1-x-publish-bundle")]}
        ).encode("utf-8")
        responses = [
            artifacts_page,
            workflow_run(11),
            bundle_zip(
                bundle(
                    revision=1,
                    challenge_id=self.challenge.challenge_id,
                    source_sha=SHA_B,
                )
            ),
        ]

        with patch("apps.instances.poller.urlopen", fake_urlopen(responses)):
            summary = poll_once(token="test-token")

        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["registered"], 0)
        self.assertEqual(ChallengeRelease.objects.count(), 0)

    def test_poll_once_survives_listing_failure(self):
        def broken_opener(request, timeout=10):
            raise TimeoutError("연결 실패")

        with patch("apps.instances.poller.urlopen", broken_opener):
            summary = poll_once(token="test-token")

        self.assertEqual(summary["error"], 1)
        self.assertEqual(summary["registered"], 0)
