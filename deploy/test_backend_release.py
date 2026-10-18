import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import backend_release as release
import send_backend_release as sender


def request(action="deploy"):
    return {
        "action": action,
        "source_sha": "a" * 40,
        "deployment_id": "12345-1",
        "pipeline_revision": release.CENTRAL_REVISION,
    }


class BackendReleaseTests(unittest.TestCase):
    def test_manifest_rejects_paths_commands_component_changes_and_unknown_fields(self):
        self.assertEqual(release.manifest(json.dumps(request()).encode()), request())
        for changes in [
            {"action": "shell"},
            {"source_sha": "main; id"},
            {"deployment_id": "../other"},
            {"pipeline_revision": "b" * 40},
            {"repository": "other"},
            {"helper": "/unapproved/helper.py"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(
                release.DeploymentError
            ):
                release.manifest(json.dumps({**request(), **changes}).encode())
        with self.assertRaises(release.DeploymentError):
            release.manifest(b"a" * 4097)

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and permission contract")
    def test_trust_requires_expected_revision_checksum_owner_and_nonwritable_files(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "deploy-development-vm.sh": b"reviewed shell",
                "backend_proxy_bridge.py": b"reviewed bridge",
            }
            for name, raw in files.items():
                (root / name).write_bytes(raw)
                (root / name).chmod(0o644)
            trust = {
                "revision": release.CENTRAL_REVISION,
                "checksums": {
                    k: hashlib.sha256(v).hexdigest() for k, v in files.items()
                },
            }
            (root / "trusted-central.json").write_text(json.dumps(trust))
            (root / "trusted-central.json").chmod(0o600)
            release.trusted_files(root, owner=os.getuid())
            (root / "backend_proxy_bridge.py").write_bytes(b"different")
            with self.assertRaises(release.DeploymentError):
                release.trusted_files(root, owner=os.getuid())
            (root / "backend_proxy_bridge.py").write_bytes(
                files["backend_proxy_bridge.py"]
            )
            (root / "backend_proxy_bridge.py").chmod(0o666)
            with self.assertRaises(release.DeploymentError):
                release.trusted_files(root, owner=os.getuid())
            (root / "backend_proxy_bridge.py").chmod(0o644)
            with self.assertRaises(release.DeploymentError):
                release.trusted_files(root, owner=os.getuid() + 1)

    def test_probe_verifies_installation_and_health_without_running_deploy(self):
        with patch.object(release, "trusted_files") as trust, patch.object(
            release, "healthy"
        ) as health, patch.object(release.subprocess, "run") as run:
            result = release.deploy(request("probe"))
            self.assertEqual(result["status"], "preflight_verified")
            trust.assert_called_once()
            health.assert_called_once()
            run.assert_not_called()

    def test_untrusted_installation_or_unhealthy_proxy_cannot_start_deployment(self):
        for stage in ["trusted_files", "healthy"]:
            with patch.object(release, "trusted_files"), patch.object(
                release, "healthy"
            ), patch.object(
                release, stage, side_effect=release.DeploymentError()
            ), patch.object(
                release.subprocess, "run"
            ) as run:
                with self.assertRaises(release.DeploymentError):
                    release.deploy(request())
                run.assert_not_called()

    def test_only_current_main_can_start_fixed_script(self):
        with patch.object(release, "trusted_files"), patch.object(
            release, "healthy"
        ), patch.object(
            release.subprocess,
            "run",
            return_value=Mock(stdout="b" * 40 + "\trefs/heads/main\n"),
        ) as run:
            with self.assertRaises(release.DeploymentError):
                release.deploy(request())
            self.assertEqual(run.call_count, 1)

    @unittest.skipUnless(os.name == "posix", "POSIX private release log")
    def test_success_records_private_log_and_uses_only_fixed_backend_arguments(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            release, "trusted_files"
        ), patch.object(release, "healthy"):
            state = Path(temporary)

            def run(args, **kwargs):
                if args[0] == "/usr/bin/git":
                    return Mock(stdout="a" * 40 + "\trefs/heads/main\n")
                self.assertEqual(
                    args[:4],
                    [
                        "/bin/bash",
                        str(release.ROOT / "deploy-development-vm.sh"),
                        "backend",
                        "MSG-CTF/msg-backend",
                    ],
                )
                self.assertNotIn("shell", kwargs)
                self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
                (state / "backend.sha").write_text("a" * 40)
                return Mock(returncode=0)

            with patch.object(release.subprocess, "run", side_effect=run):
                result = release.deploy(request(), state=state)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(
                (state / "restricted-deploy-12345-1.log").stat().st_mode & 0o777, 0o600
            )

    @unittest.skipUnless(os.name == "posix", "POSIX private release log")
    def test_failed_process_or_unverified_release_state_never_reports_success(self):
        for failed in (True, False):
            with tempfile.TemporaryDirectory() as temporary, patch.object(
                release, "trusted_files"
            ), patch.object(release, "healthy"):
                state = Path(temporary)
                responses = [
                    Mock(stdout="a" * 40 + "\trefs/heads/main\n"),
                    Mock(returncode=1 if failed else 0),
                ]
                with patch.object(release.subprocess, "run", side_effect=responses):
                    with self.assertRaises(
                        (release.DeploymentError, FileNotFoundError)
                    ):
                        release.deploy(request(), state=state)


class SenderTests(unittest.TestCase):
    def environment(self):
        return {
            "SOURCE_SHA": "a" * 40,
            "DEPLOYMENT_ID": "12345-1",
            "BACKEND_DEPLOY_HOST": "server.example.test",
            "BACKEND_DEPLOY_SSH_KEY": "fixture-key",
            "BACKEND_DEPLOY_KNOWN_HOSTS": "fixture-host-key",
            "PATH": os.environ.get("PATH", ""),
        }

    def test_sender_pins_host_and_sends_data_only_without_secret_process_environment(
        self,
    ):
        def run(args, **kwargs):
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertEqual(
                args[-2:], ["msg-backend-deploy@server.example.test", "deploy"]
            )
            self.assertEqual(json.loads(kwargs["input"]), request())
            self.assertFalse(
                any(k.startswith("BACKEND_DEPLOY_") for k in kwargs["env"])
            )
            return Mock(
                returncode=0,
                stdout=release.PREFIX
                + json.dumps(
                    {
                        "source_commit": "a" * 40,
                        "pipeline_revision": release.CENTRAL_REVISION,
                        "status": "verified",
                        "deployment_strategy": "verified_backend_proxy_bridge",
                        "private": "must-not-be-forwarded",
                    }
                ),
            )

        with patch.object(sender.subprocess, "run", side_effect=run):
            self.assertNotIn("private", sender.send("deploy", self.environment()))

    def test_sender_rejects_failed_mismatched_duplicate_or_missing_proof(self):
        valid = {
            "source_commit": "a" * 40,
            "pipeline_revision": release.CENTRAL_REVISION,
            "status": "verified",
            "deployment_strategy": "verified_backend_proxy_bridge",
        }
        for stdout, code in [
            ("", 0),
            (release.PREFIX + json.dumps(valid), 1),
            (release.PREFIX + json.dumps({**valid, "source_commit": "b" * 40}), 0),
            ((release.PREFIX + json.dumps(valid) + "\n") * 2, 0),
        ]:
            with self.subTest(code=code), patch.object(
                sender.subprocess,
                "run",
                return_value=Mock(returncode=code, stdout=stdout),
            ):
                with self.assertRaises(RuntimeError):
                    sender.send("deploy", self.environment())

    def test_invalid_destination_never_contacts_ssh(self):
        with patch.object(sender.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                sender.send(
                    "deploy",
                    {**self.environment(), "BACKEND_DEPLOY_HOST": "host; command"},
                )
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
