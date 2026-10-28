import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ci_hook


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.original_cwd = Path.cwd()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "runner" / "_work"
        self.workspace = self.work / "universal-agents" / "universal-agents"
        self.workspace.mkdir(parents=True)
        self.state = self.root / "hooks" / "state"
        self.config = {
            "state": str(self.state),
            "work": str(self.work),
            "runner": str(self.work.parent),
            "min_free_gib": 0,
            "repositories": ["enzimo/universal-agents"],
            "public_repositories": ["enzimo/universal-agents"],
        }
        self.event = self.root / "event.json"
        self.event.write_text(json.dumps({"repository": {"private": False, "full_name": "enzimo/universal-agents"}}))
        self.env = patch.dict(
            os.environ,
            {
                "GITHUB_REPOSITORY": "enzimo/universal-agents",
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_WORKSPACE": str(self.workspace),
                "GITHUB_EVENT_PATH": str(self.event),
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_JOB": "tests",
                "RUNNER_NAME": "test",
                "GITHUB_ENV": str(self.root / "env"),
                "GITHUB_PATH": str(self.root / "path"),
            },
        )
        self.env.start()

    def tearDown(self):
        os.chdir(self.original_cwd)
        self.env.stop()
        self.temp.cleanup()

    def begin(self):
        ci_hook.begin(self.config)
        return json.loads((self.state / "job.json").read_text())

    def test_completion_removes_job_outputs_but_preserves_neighbor_and_account(self):
        self.begin()
        (self.workspace / "target").mkdir()
        (self.workspace / "target" / "large-output").write_bytes(b"artifact")
        neighbor = self.work / "other-repository"
        neighbor.mkdir()
        personal = self.root / "personal-data"
        personal.mkdir()
        (personal / "database").write_text("persistent")
        if os.name != "nt":
            (self.workspace / "link").symlink_to(personal, target_is_directory=True)
        ci_hook.cleanup(self.config)
        ci_hook.cleanup(self.config)
        self.assertFalse(self.workspace.exists())
        self.assertFalse((self.state / "cache").exists())
        self.assertTrue(neighbor.exists())
        self.assertEqual((personal / "database").read_text(), "persistent")

    def test_next_job_recovers_interrupted_job_and_gets_new_identity(self):
        first = self.begin()
        prepared_action = self.work / "_actions" / "checkout"
        prepared_action.mkdir(parents=True)
        (prepared_action / "index.js").write_text("incoming action")
        (self.workspace / "old.log").write_text("abandoned")
        second = self.begin()
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse((self.workspace / "old.log").exists())
        self.assertTrue(self.workspace.is_dir())
        self.assertTrue((prepared_action / "index.js").exists())
        env = (self.root / "env").read_text()
        self.assertIn("UA_CI_HOOK_VERSION=1", env)
        self.assertIn("UA_CI_PUBLIC_JOB_POLICY_VERSION=1", env)
        self.assertIn("DOCKER_CONFIG=" + str(self.state / "cache" / "docker"), env)

    def test_interrupted_compiler_cache_is_stopped_before_reuse(self):
        self.config["build_cache"] = {
            "sccache": "/tools/sccache", "max_bytes": 1024**2,
            "max_age_days": 7, "server_port": 48227,
        }
        with (patch.object(ci_hook.build_cache, "listening", return_value=False),
              patch.object(ci_hook.build_cache, "start") as start,
              patch.object(ci_hook.build_cache, "stop") as stop):
            first = self.begin()
            cache = Path(first["compiler_env"]["SCCACHE_DIR"])
            (cache / "object").write_text("compiled")
            second = self.begin()
            stop.assert_called_once_with(first["compiler_env"])
            self.assertEqual(start.call_count, 2)
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual((cache / "object").read_text(), "compiled")
            ci_hook.cleanup(self.config)
        self.assertFalse(self.workspace.exists())
        self.assertFalse(list(self.state.iterdir()))
        self.assertTrue((cache / "object").exists())

    def test_long_cargo_paths_are_reclaimed(self):
        self.begin()
        long_path = self.workspace / "target" / ("a" * 120) / ("b" * 120)
        if os.name == "nt":
            long_path = Path("\\\\?\\" + str(long_path.absolute()))
        long_path.mkdir(parents=True)
        (long_path / "build-output").write_text("artifact")
        ci_hook.cleanup(self.config)
        self.assertFalse(self.workspace.exists())

    def test_cleanup_accepts_a_file_removed_after_enumeration(self):
        self.begin()
        lock = self.state / "cache" / "vanishing.lock"
        lock.touch()
        unlink = os.unlink

        def disappear(name, *args, **kwargs):
            result = unlink(name, *args, **kwargs)
            if Path(name).name == lock.name:
                raise FileNotFoundError(str(name))
            return result

        with patch.object(ci_hook.os, "unlink", side_effect=disappear):
            ci_hook.cleanup(self.config)
        self.assertFalse((self.state / "job.json").exists())
        self.assertFalse((self.state / "cache").exists())

    def test_cleanup_still_refuses_a_file_it_cannot_remove(self):
        self.begin()
        locked = self.state / "cache" / "locked-file"
        locked.touch()
        unlink = os.unlink

        def denied(name, *args, **kwargs):
            if Path(name).name == locked.name:
                raise PermissionError(str(name))
            return unlink(name, *args, **kwargs)

        with patch.object(ci_hook.os, "unlink", side_effect=denied):
            with self.assertRaises(PermissionError):
                ci_hook.cleanup(self.config)
        self.assertTrue((self.state / "job.json").exists())
        self.assertTrue(locked.exists())

    def test_docker_cleanup_uses_only_exact_job_label_and_owned_builder(self):
        job = self.begin()
        (self.state / "cache" / "docker-used").touch()
        instances = self.state / "cache" / "docker" / "buildx" / "instances"
        instances.mkdir(parents=True)
        (instances / f"ua-ci-{job['id']}").write_text("{}")
        prefix = ("docker", "--config", str(self.state / "cache" / "docker"))

        def boundary(*args):
            if args[0] == "powershell":
                self.assertIn(f"UA-CI-{job['id']}-*", args[-1])
                return ""
            self.assertEqual(args[:3], prefix)
            command = args[3:]
            if command[:2] == ("ps", "-aq"):
                self.assertEqual(command[-1], f"label={ci_hook.LABEL}={job['id']}")
                return "owned-container"
            if command[:2] in (("volume", "ls"), ("network", "ls")):
                self.assertEqual(command[-1], f"label={ci_hook.LABEL}={job['id']}")
                return "owned-" + command[0]
            if command[:2] == ("image", "ls"):
                self.assertEqual(command[-1], f"label={ci_hook.LABEL}={job['id']}")
                return "owned-image"
            if command[:2] == ("buildx", "ls"):
                return f"default\nother-builder\nua-ci-{job['id']}"
            return ""

        with patch.object(ci_hook, "run", side_effect=boundary) as mocked:
            ci_hook.cleanup(self.config)
        calls = [
            call.args[3:] for call in mocked.call_args_list if call.args[0] == "docker"
        ]
        self.assertIn(("rm", "--force", "--volumes", "owned-container"), calls)
        self.assertIn(("volume", "rm", "owned-volume"), calls)
        self.assertIn(("network", "rm", "owned-network"), calls)
        self.assertIn(("buildx", "rm", "--force", f"ua-ci-{job['id']}"), calls)
        self.assertNotIn(("buildx", "rm", "--force", "other-builder"), calls)

    def test_non_build_docker_job_does_not_require_the_buildx_plugin(self):
        self.begin()
        (self.state / "cache" / "docker-used").touch()
        with patch.object(ci_hook, "run", return_value="") as command:
            ci_hook.cleanup(self.config)
        self.assertFalse(any("buildx" in call.args for call in command.call_args_list))

    def test_colima_trim_is_limited_to_the_configured_ci_profile(self):
        self.config["colima_profile"] = "ua-ci"
        self.begin()
        (self.state / "cache" / "docker-used").touch()
        with patch.object(ci_hook, "run", return_value="") as command:
            ci_hook.cleanup(self.config)
        trims = [call.args for call in command.call_args_list if call.args[0] == "colima"]
        self.assertEqual(trims, [("colima", "--profile", "ua-ci", "ssh", "--", "sudo", "fstrim", "/var/lib/docker")])
        trim_call = next(call for call in command.call_args_list if call.args[0] == "colima")
        # Colima forwards the caller cwd, but host checkouts are not VM mounts.
        self.assertEqual(trim_call.kwargs, {"cwd": "/"})

    def test_failed_cleanup_preserves_manifest_and_blocks_next_job(self):
        job = self.begin()
        (self.state / "cache" / "docker-used").touch()
        with (
            patch.object(
                ci_hook, "run", side_effect=subprocess.TimeoutExpired("docker", 120)
            ),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            ci_hook.begin(self.config)
        self.assertEqual(json.loads((self.state / "job.json").read_text()), job)
        self.assertTrue(self.workspace.exists())

    def test_outside_workspace_and_symlink_are_rejected(self):
        outside = self.root / "personal-data"
        outside.mkdir()
        os.environ["GITHUB_WORKSPACE"] = str(outside)
        with self.assertRaises(ValueError):
            self.begin()
        if os.name != "nt":
            self.workspace.rmdir()
            self.workspace.symlink_to(outside, target_is_directory=True)
            os.environ["GITHUB_WORKSPACE"] = str(self.workspace)
            with self.assertRaises(ValueError):
                self.begin()

    def test_fork_pr_is_refused_before_any_job_state_is_created(self):
        os.environ["GITHUB_EVENT_NAME"] = "pull_request"
        self.event.write_text(json.dumps({
            "repository": {"full_name": "enzimo/universal-agents"},
            "pull_request": {
                "head": {"repo": {"full_name": "fork/universal-agents"}},
                "base": {"repo": {"full_name": "enzimo/universal-agents"}},
            },
        }))
        with self.assertRaisesRegex(ValueError, "Fork"):
            self.begin()
        self.assertFalse((self.state / "job.json").exists())

    def test_same_repository_pr_can_use_the_public_repository_runner(self):
        os.environ["GITHUB_EVENT_NAME"] = "pull_request"
        self.event.write_text(json.dumps({
            "repository": {"full_name": "enzimo/universal-agents", "private": False},
            "pull_request": {
                side: {"repo": {"full_name": "enzimo/universal-agents"}}
                for side in ("head", "base")
            },
        }))
        self.begin()
        self.assertTrue((self.state / "job.json").exists())

    def test_missing_or_unsupported_provenance_is_refused(self):
        repository = {"full_name": "enzimo/universal-agents"}
        for name, event in (
            ("", {"repository": repository}),
            ("pull_request_target", {"repository": repository}),
            ("workflow_run", {"repository": repository}),
            ("repository_dispatch", {"repository": repository}),
            ("push", {"repository": {"full_name": "other/repository"}}),
            ("push", {"repository": repository, "pull_request": {}}),
            ("pull_request", {"repository": repository}),
            ("push", {}),
        ):
            with self.subTest(event_name=name, event=event):
                os.environ["GITHUB_EVENT_NAME"] = name
                self.event.write_text(json.dumps(event))
                with self.assertRaises(ValueError):
                    self.begin()
                self.assertFalse((self.state / "job.json").exists())

    def test_configured_repositories_accept_supported_non_pr_events(self):
        for repository in ("enzimo/universal-agents", "enzimo/universal-agents-desktop"):
            for private in (False, True):
                for name in ("push", "workflow_dispatch", "schedule"):
                    with self.subTest(repository=repository, private=private, event_name=name):
                        ci_hook.require_trusted_event(
                            {"repositories": [repository], "public_repositories": [repository]},
                            {"GITHUB_REPOSITORY": repository, "GITHUB_EVENT_NAME": name},
                            {"repository": {"full_name": repository, "private": private}},
                        )

    def test_public_repository_needs_explicit_host_approval(self):
        del self.config["public_repositories"]
        with self.assertRaisesRegex(ValueError, "not approved"):
            self.begin()
        self.assertFalse((self.state / "job.json").exists())

    def test_disk_reserve_blocks_new_work(self):
        self.config["min_free_gib"] = 10**9
        with self.assertRaises(RuntimeError):
            self.begin()
        self.assertFalse((self.state / "job.json").exists())

    def test_windows_backing_drive_reserve_blocks_wsl_work(self):
        backing_drive = self.root / "windows-drive"
        backing_drive.mkdir()
        self.config["reserve_paths"] = [str(backing_drive)]
        self.config["min_free_gib"] = 30
        real_usage = ci_hook.shutil.disk_usage(self.root)

        def usage(path):
            free = 29 * 1024**3 if Path(path) == backing_drive else 100 * 1024**3
            return type(real_usage)(total=200 * 1024**3, used=0, free=free)

        with (
            patch.object(ci_hook.shutil, "disk_usage", side_effect=usage),
            self.assertRaises(RuntimeError),
        ):
            self.begin()
        self.assertFalse((self.state / "job.json").exists())


if __name__ == "__main__":
    unittest.main()
