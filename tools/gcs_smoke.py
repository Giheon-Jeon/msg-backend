"""Real storage -> backend HTTP downloads, using only disposable QA objects.

GCS is the default. --local-storage only validates the harness without GCP access.
Reports contain statuses and checksums, never credentials or response bodies.
"""

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from wsgiref.simple_server import WSGIRequestHandler, make_server


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def safe_error(error):
    result = {"type": type(error).__name__}
    message = str(error)
    for reason in (
        "attribute condition", "iam.serviceAccounts.getAccessToken",
        "storage.objects.get", "storage.objects.create", "storage.objects.delete",
        "invalid_grant", "invalid_target", "invalid_client", "unauthorized_client",
    ):
        if reason in message:
            result["reason"] = reason
            break
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-storage", action="store_true")
    parser.add_argument("--github-artifacts", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("reports/gcs-smoke.json"))
    args = parser.parse_args()
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "commit": os.getenv("GITHUB_SHA", "local"),
        "storage": "local" if args.local_storage else "gcs",
        "source": "synthetic participant ZIPs, no challenge secrets",
        "transport": "real loopback HTTP with JWT login",
        "checks": [],
        "result": "failed",
    }
    stage = "configuration"
    storage = runner = database_config = server = worker = None
    object_keys = []
    local_media = None
    cleanup_errors = []

    def check(name, condition):
        report["checks"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise RuntimeError(name)

    try:
        run_id = os.getenv("GCS_QA_RUN_ID", "")
        if args.local_storage:
            os.environ["GCS_ENABLED"] = "False"
            run_id = "0-" + str(secrets.randbelow(10**10))
        elif (os.getenv("GCS_ENABLED", "").lower() != "true"
              or os.getenv("GCS_OBJECT_PREFIX") != "dev"
              or not re.fullmatch(r"[0-9]+-[0-9]+", run_id)):
            raise RuntimeError("GCS requires the dev prefix and a unique numeric QA run ID")
        slug = "qa-gcs-smoke-" + run_id
        report["qa_slug"] = slug
        if args.github_artifacts:
            if args.local_storage or not os.getenv("RELEASE_POLL_GITHUB_TOKEN"):
                raise RuntimeError("Real artifact verification requires GCS and a GitHub read token")
            # Isolate real slugs without rewriting the signed artifact metadata.
            os.environ["GCS_OBJECT_PREFIX"] = "dev/participant-files/qa-github-" + run_id
            report["source"] = "synthetic ZIP checks followed by fixed real GitHub artifacts"

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        os.environ["DJANGO_SETTINGS_MODULE"] = "tools.gcs_smoke_settings"
        import django

        django.setup()
        from django.conf import settings
        from django.core.handlers.wsgi import WSGIHandler
        from django.core.files.storage import default_storage
        from django.db import connections
        from django.test.runner import DiscoverRunner

        from apps.accounts.models import Team, User
        from apps.board.models import Cell, TeamChallengeAccess
        from apps.challenge.models import Challenge
        from apps.instances.models import ChallengeRelease, ChallengeUserFileBundle
        from apps.instances.tests_releases import artifact_payload
        from apps.instances.tests_user_files import user_archive, user_artifact, user_manifest
        from apps.instances.user_files import register_user_files_bundle

        if args.local_storage:
            local_media = tempfile.TemporaryDirectory(prefix="msg-gcs-smoke-")
            settings.STORAGES = {
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": local_media.name},
                }
            }
        storage = default_storage
        if not args.local_storage:
            check("gcs_driver_selected", storage.__class__.__name__ == "GoogleCloudStorage")
            report["bucket"] = settings.GCS_BUCKET_NAME
            report["prefix"] = settings.GCS_OBJECT_PREFIX

        stage = "database_setup"
        runner = DiscoverRunner(verbosity=0, interactive=False)
        database_config = runner.setup_databases()
        password = secrets.token_urlsafe(32)
        team = Team.objects.create(team_name="GCS smoke team")
        outsider_team = Team.objects.create(team_name="GCS smoke locked team")
        for role in ("admin", "player", "outsider"):
            User.objects.create_user(
                login_id=f"gcs-{role}", password=password, nickname=f"GCS {role}",
                role="ADMIN" if role == "admin" else "PARTICIPANT",
                team={"player": team, "outsider": outsider_team}.get(role),
            )
        challenge = Challenge.objects.create(
            challenge_slug=slug, title="GCS smoke", category="WEB",
            difficulty="EASY", score=100, flag_hash="unused", is_published=True,
        )
        cell = Cell.objects.create(cell_index=1, type="CHALLENGE", difficulty="EASY", name="GCS smoke")
        TeamChallengeAccess.objects.create(team=team, challenge=challenge, source_cell=cell)
        server = make_server("127.0.0.1", 0, WSGIHandler(), handler_class=QuietHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path, body=None, token=None, key=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            if key:
                headers["Idempotency-Key"] = key
            req = Request(base + path, headers=headers, data=(
                json.dumps(body).encode() if body is not None else None
            ))
            try:
                # base is the loopback HTTP server created above, not an input URL.
                with urlopen(req, timeout=90) as response:  # nosec B310
                    return response.status, response.read(), response.headers
            except HTTPError as error:
                with error:
                    return error.code, error.read(), error.headers

        stage = "login"
        tokens = {}
        for role in ("admin", "player", "outsider"):
            status, raw, _ = request("/api/v1/auth/login", {
                "login_id": f"gcs-{role}", "password": password,
            })
            check(f"login_{role}", status == 200)
            tokens[role] = json.loads(raw)["data"]["access_token"]

        releases = []
        bundles = []
        admin_path = f"/api/v1/admin/challenges/{challenge.pk}"
        user_path = f"/api/v1/challenges/{challenge.pk}"
        for revision in (1, 2):
            stage = f"upload_revision_{revision}"
            payload = artifact_payload(revision=revision, slug=slug)
            status, raw, _ = request(admin_path + "/releases", payload, tokens["admin"])
            check(f"register_release_{revision}", status == 200)
            releases.append(json.loads(raw)["data"]["release_id"])
            archive = user_archive({"README.txt": f"GCS participant ZIP version {revision}\n".encode()})
            manifest = user_manifest(archive, revision=revision, slug=slug)
            key = f"participant-files/{slug}/{revision}/{manifest['source_sha']}/{manifest['user_files']['sha256']}.zip"
            check(f"new_object_{revision}", not storage.exists(key))
            object_keys.append(key)
            result, bundle = register_user_files_bundle(
                manifest, archive, user_artifact(revision, slug=slug)
            )
            check(f"upload_{revision}", result == "registered" and bundle.object_key == key)
            check(f"object_exists_{revision}", storage.exists(key))
            bundles.append(bundle)
            result, duplicate = register_user_files_bundle(
                manifest, archive, user_artifact(revision, slug=slug)
            )
            check(f"upload_retry_{revision}", result == "duplicate" and duplicate.pk == bundle.pk)
            path = admin_path + f"/files/{bundle.pk}/download"
            status, raw, headers = request(path, token=tokens["admin"])
            check(f"admin_download_{revision}", status == 200 and hashlib.sha256(raw).hexdigest() == bundle.sha256)
            check(f"download_headers_{revision}", headers.get("Content-Type") == "application/zip"
                  and "no-store" in headers.get("Cache-Control", "")
                  and "attachment" in headers.get("Content-Disposition", ""))
            with zipfile.ZipFile(io.BytesIO(raw)) as downloaded:
                check(f"zip_crc_{revision}", downloaded.testzip() is None)
            check(f"admin_download_auth_{revision}", request(path)[0] == 401
                  and request(path, token=tokens["player"])[0] == 403)
            if not args.local_storage:
                stage = f"object_privacy_{revision}"
                url = "https://storage.googleapis.com/" + quote(settings.GCS_BUCKET_NAME, safe="")
                url += "/" + quote(settings.GCS_OBJECT_PREFIX + "/" + key, safe="/")
                try:
                    # HTTPS host is fixed; bucket and object key are URL-quoted path components.
                    with urlopen(url, timeout=30) as response:  # nosec B310
                        anonymous_status = response.status
                except HTTPError as error:
                    anonymous_status = error.code
                    error.close()
                check(f"private_object_{revision}", anonymous_status in {401, 403, 404})

        stage = "version_selection"
        for index in (0, 1, 0):
            status, _, _ = request(admin_path + f"/releases/{releases[index]}/activate", {}, tokens["admin"])
            check(f"activate_version_{index + 1}", status == 200)
            selected, other = bundles[index], bundles[1 - index]
            path = user_path + f"/files/{selected.pk}/download"
            status, raw, _ = request(path, token=tokens["player"])
            check(f"participant_version_{index + 1}", status == 200
                  and hashlib.sha256(raw).hexdigest() == selected.sha256)
            check(f"unselected_version_{2 - index}_blocked",
                  request(user_path + f"/files/{other.pk}/download", token=tokens["player"])[0] == 404)
            check("anonymous_download_blocked", request(path)[0] == 401)
            check("unentitled_download_blocked", request(path, token=tokens["outsider"])[0] == 403)

        stage = "manual_file_selection"
        body = {"mode": "SELECTED", "file_id": str(bundles[1].pk)}
        path = admin_path + f"/releases/{releases[0]}/files"
        status, raw, _ = request(path, body, tokens["admin"], "gcs-smoke-file-selection")
        check("save_selected_file", status == 200)
        derived = json.loads(raw)["data"]["release_id"]
        repeated, repeated_raw, _ = request(path, body, tokens["admin"], "gcs-smoke-file-selection")
        check("save_retry", repeated == 200 and json.loads(repeated_raw) == json.loads(raw))
        status, selected_raw, _ = request(admin_path + "/files", token=tokens["admin"])
        check("save_does_not_activate", status == 200
              and json.loads(selected_raw)["data"]["current_file"]["file_id"] == str(bundles[0].pk))
        check("activate_selected_file", request(admin_path + f"/releases/{derived}/activate", {}, tokens["admin"])[0] == 200)
        status, raw, _ = request(user_path + f"/files/{bundles[1].pk}/download", token=tokens["player"])
        check("selected_file_download", status == 200 and hashlib.sha256(raw).hexdigest() == bundles[1].sha256)
        check("no_duplicate_rows", ChallengeUserFileBundle.objects.count() == 2 and ChallengeRelease.objects.count() == 3)
        stage = "missing_object"
        storage.delete(bundles[1].object_key)
        check("missing_object_returns_404", request(user_path + f"/files/{bundles[1].pk}/download", token=tokens["player"])[0] == 404)
        if args.github_artifacts:
            stage = "github_artifacts"
            from tools.gcs_artifact_smoke import verify_github_artifacts

            verify_github_artifacts(check, request, tokens, team, storage, object_keys, report)
        report["result"] = "passed"
    except Exception as error:
        report["failure"] = {"stage": stage, **safe_error(error)}
    finally:
        if server:
            server.shutdown()
            worker.join(timeout=10)
            server.server_close()
        for key in object_keys:
            try:
                if storage.exists(key):
                    storage.delete(key)
                if storage.exists(key):
                    raise RuntimeError("QA object remains")
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        report["cleanup"] = {"tracked_objects": len(object_keys), "errors": cleanup_errors}
        if database_config:
            try:
                connections.close_all()
                runner.teardown_databases(database_config)
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        if local_media:
            local_media.cleanup()
        if cleanup_errors:
            report["result"] = "failed"
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
