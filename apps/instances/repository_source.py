"""Read one configured repository revision without executing problem code."""
import base64
import json
import re
import subprocess
from functools import lru_cache
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings

from apps.instances.releases import ReleaseValidationError

MAX_FILE_BYTES = 262144
MAX_FILES = 5000
SHA = re.compile(r"^[0-9a-f]{40}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")


class RepositorySourceUnavailable(Exception):
    pass


@lru_cache(maxsize=256)
def _local_blob(directory, sha):
    try:
        return subprocess.check_output(["git", "--no-optional-locks", "-C", directory, "cat-file", "blob", sha],
                                       stderr=subprocess.DEVNULL, timeout=8).decode("utf-8-sig")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise RepositorySourceUnavailable("연결된 저장소의 UTF-8 설정 파일을 읽지 못했습니다") from None


def _github(path):
    # Host and repository are operator configuration, never request parameters.
    request = Request("https://api.github.com" + path, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "msg-repository-environment",
    })
    token = settings.RELEASE_POLL_GITHUB_TOKEN
    if token:
        request.add_unredirected_header("Authorization", "Bearer " + token)
    try:
        # The URL above fixes HTTPS and api.github.com; callers supply repository API paths only.
        with urlopen(request, timeout=8) as response:  # nosec B310
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError
        return json.loads(raw)
    except Exception:
        raise RepositorySourceUnavailable("문제 저장소를 읽지 못했습니다 연결과 읽기 권한을 확인해주세요") from None


@lru_cache(maxsize=256)
def _github_blob(repository, sha):
    raw = _github(f"/repos/{repository}/git/blobs/{sha}")
    try:
        if raw.get("encoding") != "base64" or raw.get("size", MAX_FILE_BYTES + 1) > MAX_FILE_BYTES:
            raise ValueError
        content = base64.b64decode(raw["content"])
        if len(content) > MAX_FILE_BYTES:
            raise ValueError
        return content.decode("utf-8-sig")
    except (KeyError, ValueError, UnicodeError):
        raise RepositorySourceUnavailable("읽을 수 없는 설정 파일이 있습니다") from None


class RepositorySnapshot:
    def __init__(self, slug, revision=None):
        if not isinstance(slug, str) or not SLUG.fullmatch(slug):
            raise ReleaseValidationError("문제 저장소의 폴더 이름이 필요합니다")
        if slug in settings.REPOSITORY_ENV_EXCLUDED_SLUGS:
            raise ReleaseValidationError("별도로 배포하는 문제는 가져오기 대상에서 제외됩니다")
        if revision is not None and (not isinstance(revision, str) or not SHA.fullmatch(revision)):
            raise ReleaseValidationError("저장소 버전을 다시 불러와주세요")
        self.slug = slug
        self.repository = settings.RELEASE_POLL_REPO
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise RepositorySourceUnavailable("문제 저장소 설정을 확인해주세요")
        self.local_path = settings.REPOSITORY_ENV_GIT_DIR
        self.cache = {}
        ref = settings.REPOSITORY_ENV_REF
        if self.local_path:
            self.revision = self._git("rev-parse", "--verify", (revision or ref) + "^{commit}").decode().strip()
            raw = self._git("ls-tree", "-r", "-l", "-z", self.revision, "--", slug)
            self.files = {}
            for entry in raw.split(b"\0"):
                if not entry:
                    continue
                meta, path = entry.split(b"\t", 1)
                mode, kind, blob, size = meta.decode().split()
                path = path.decode("utf-8")
                if mode in ("100644", "100755") and path.startswith(slug + "/"):
                    self.files[path[len(slug) + 1:]] = {"sha": blob, "size": int(size)}
        else:
            if revision is None:
                ref = ref.removeprefix("refs/heads/").removeprefix("origin/")
                revision = _github(f"/repos/{self.repository}/commits/{quote(ref, safe='')}").get("sha")
            if not isinstance(revision, str) or not SHA.fullmatch(revision):
                raise RepositorySourceUnavailable("문제 저장소 버전을 확인하지 못했습니다")
            self.revision = revision
            root = _github(f"/repos/{self.repository}/git/trees/{revision}")
            folder = next((item for item in root.get("tree", []) if item.get("path") == slug and item.get("type") == "tree"), None)
            if folder is None:
                raise ReleaseValidationError("문제 저장소에서 해당 폴더를 찾지 못했습니다")
            tree = _github(f"/repos/{self.repository}/git/trees/{folder['sha']}?recursive=1")
            if tree.get("truncated"):
                raise RepositorySourceUnavailable("문제 파일 목록이 너무 커서 모두 확인하지 못했습니다")
            self.files = {item["path"]: {"sha": item["sha"], "size": item.get("size", 0)}
                          for item in tree.get("tree", []) if item.get("mode") in ("100644", "100755")}
        if not SHA.fullmatch(self.revision) or not self.files or len(self.files) > MAX_FILES:
            raise RepositorySourceUnavailable("문제 파일 목록을 확인하지 못했습니다")
        self.published = not self.local_path or bool(self._git("for-each-ref", "--contains=" + self.revision, "--format=%(refname)", "refs/remotes/").strip())

    def _git(self, *args):
        try:
            return subprocess.check_output(["git", "--no-optional-locks", "-C", self.local_path, *args],
                                           stderr=subprocess.DEVNULL, timeout=8)
        except (OSError, subprocess.SubprocessError):
            raise RepositorySourceUnavailable("연결된 문제 저장소를 읽지 못했습니다") from None

    def read(self, path):
        if path not in self.files or self.files[path]["size"] > MAX_FILE_BYTES:
            raise RepositorySourceUnavailable("설정 파일이 없거나 읽을 수 있는 크기를 넘었습니다")
        if path not in self.cache:
            if self.local_path:
                self.cache[path] = _local_blob(self.local_path, self.files[path]["sha"])
            else:
                self.cache[path] = _github_blob(self.repository, self.files[path]["sha"])
        return self.cache[path]
