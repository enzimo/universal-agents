import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_cache
import ci_hook


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        state = self.path / "hooks" / "state"
        (state / "cache").mkdir(parents=True)
        self.work = self.path / "work"
        self.work.mkdir()
        self.config = {
            "state": str(state), "work": str(self.work), "min_free_gib": 0,
            "repositories": ["owner/desktop", "owner/runtime"],
            "build_cache": {"max_bytes": 10000, "max_age_days": 7,
                            "sccache": "/tools/sccache", "server_port": 48227},
        }
        self.env = patch.dict(os.environ, {
            "GITHUB_REPOSITORY": "owner/desktop", "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "push",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def prepare(self):
        env = build_cache.prepare(self.config)
        return env, Path(env["SCCACHE_DIR"]).parent

    def test_reuse_and_repository_and_pr_isolation(self):
        _, main = self.prepare()
        (main / "compiler" / "object").write_bytes(b"compiled")
        _, again = self.prepare()
        self.assertEqual(again, main)
        self.assertEqual((again / "compiler" / "object").read_bytes(), b"compiled")
        os.environ["GITHUB_EVENT_NAME"] = "pull_request"
        os.environ["GITHUB_REF"] = "refs/pull/1/merge"
        _, pr = self.prepare()
        self.assertNotEqual(main, pr)
        self.assertFalse((pr / "compiler" / "object").exists())
        os.environ["GITHUB_REPOSITORY"] = "owner/runtime"
        self.assertNotEqual(self.prepare()[1], pr)
        os.environ["GITHUB_REF"] = "refs/heads/main"
        self.assertNotEqual(build_cache.scope(), "main")

    def test_total_budget_evicts_oldest_scope_and_keeps_neighbor(self):
        _, old = self.prepare()
        metadata = old / "entry.json"
        row = json.loads(metadata.read_text())
        row["last_used"] -= 60
        metadata.write_text(json.dumps(row))
        (old / "compiler" / "data").write_bytes(b"x" * 6000)
        os.environ["GITHUB_REF"] = "refs/heads/feature"
        _, new = self.prepare()
        (new / "pip" / "data").write_bytes(b"x" * 6000)
        neighbor = self.path / "personal"
        neighbor.write_text("keep")
        build_cache.prune(self.config, ci_hook.remove)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual(neighbor.read_text(), "keep")

    def test_expiry_and_oversized_single_entry(self):
        _, path = self.prepare()
        metadata = path / "entry.json"
        row = json.loads(metadata.read_text())
        row["last_used"] -= 8 * 86400
        metadata.write_text(json.dumps(row))
        build_cache.prune(self.config, ci_hook.remove)
        self.assertFalse(path.exists())
        _, path = self.prepare()
        (path / "pip" / "large").write_bytes(b"x" * 11000)
        build_cache.prune(self.config, ci_hook.remove)
        self.assertFalse(path.exists())

    def test_low_disk_evicts_cache_before_admission(self):
        _, path = self.prepare()
        with patch.object(build_cache, "low_disk", return_value=True):
            build_cache.prune(self.config, ci_hook.remove)
        self.assertFalse(path.exists())

    def test_long_paths_are_counted_in_the_budget(self):
        _, path = self.prepare()
        long_path = path / "pip" / ("a" * 120) / ("b" * 120)
        if os.name == "nt":
            long_path = Path("\\\\?\\" + str(long_path.absolute()))
        try:
            long_path.mkdir(parents=True)
            (long_path / "object").write_bytes(b"x" * 11000)
            self.assertGreaterEqual(build_cache.size(path), 11000)
            build_cache.prune(self.config, ci_hook.remove)
            self.assertFalse(path.exists())
        finally:
            ci_hook.remove(path)

    def test_unknown_root_is_not_adopted(self):
        path = Path(self.config["state"]).parent / "build-cache-v1"
        path.mkdir()
        (path / "personal").write_text("keep")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual((path / "personal").read_text(), "keep")

    def test_file_browser_metadata_is_removed_and_other_files_still_stop_the_job(self):
        # A .DS_Store in the cache root stopped the macOS release build (v0.8.4).
        _, path = self.prepare()
        root = path.parent
        for name in (".DS_Store", "._entry", "Thumbs.db", "desktop.ini"):
            (root / name).write_text("finder")
        with self.assertLogs("ua.ci.cache", "WARNING") as logs:
            build_cache.prune(self.config, ci_hook.remove)
        self.assertEqual(len(logs.records), 4)
        self.assertEqual(sorted(item.name for item in root.iterdir()), sorted([".owner", path.name]))
        (root / "notes.txt").write_text("keep")
        with self.assertRaisesRegex(ValueError, "Unexpected file"):
            build_cache.prune(self.config, ci_hook.remove)
        self.assertEqual((root / "notes.txt").read_text(), "keep")

    def test_npx_environment_is_disposable_but_downloads_are_retained(self):
        _, path = self.prepare()
        downloads = path / "npm" / "_cacache"
        downloads.mkdir()
        (downloads / "tarball").write_text("download")
        execution = path / "npm" / "_npx" / "package" / "node_modules" / ".bin"
        execution.mkdir(parents=True)
        outside = self.path / "personal"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        if os.name != "nt":
            (execution / "tauri").symlink_to(outside, target_is_directory=True)
        else:
            (execution / "tauri.cmd").write_text("temporary launcher")
        build_cache.prune(self.config, ci_hook.remove)
        self.assertFalse((path / "npm" / "_npx").exists())
        self.assertEqual((downloads / "tarball").read_text(), "download")
        self.assertEqual((outside / "keep").read_text(), "keep")

    @unittest.skipIf(os.name == "nt", "Windows junction checked by native probe")
    def test_link_escape_is_rejected_without_touching_target(self):
        _, path = self.prepare()
        outside = self.path / "personal"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        (path / "pip" / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            build_cache.prune(self.config, ci_hook.remove)
        self.assertEqual((outside / "keep").read_text(), "keep")

    def test_server_failure_is_not_silently_ignored(self):
        env, _ = self.prepare()
        with (patch.object(build_cache, "listening", return_value=True),
              self.assertRaises(RuntimeError)):
            build_cache.start(env)
        with (patch.object(build_cache, "listening", return_value=True),
              patch.object(build_cache, "command") as command):
            command.return_value.returncode = 1
            command.return_value.stderr = "failed to stop"
            with self.assertRaises(RuntimeError):
                build_cache.stop(env)


if __name__ == "__main__":
    unittest.main()
