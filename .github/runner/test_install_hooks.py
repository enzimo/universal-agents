"""Exercise explicit public approval at the operator installation boundary."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class InstallationTests(unittest.TestCase):
    def install(self, public_repository, *extra):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        runner = root / "runner"
        runner.mkdir()
        (runner / ".runner").write_text(json.dumps({"workFolder": "_work"}))
        sccache = root / "sccache"
        sccache.touch()
        destination = root / "hooks"
        result = subprocess.run(
            ["rtk", "proxy", sys.executable, str(Path(__file__).with_name("install_hooks.py")),
             "--runner", str(runner), "--destination", str(destination),
             "--sccache", str(sccache), "--reserve-path", str(root),
             "--repository", "example/approved", "--public-repository", public_repository, *extra],
            check=False, capture_output=True, text=True,
        )
        return result, destination

    def test_explicit_public_approval_is_written_to_the_host_configuration(self):
        result, destination = self.install("example/approved")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads((destination / "config.json").read_text())
        self.assertEqual(config["public_repositories"], ["example/approved"])
        self.assertTrue((destination / "ci_hook.py").is_file())

    def test_public_approval_cannot_broaden_the_repository_allowlist(self):
        result, destination = self.install("example/unapproved")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Every public repository", result.stderr)
        self.assertFalse((destination / "config.json").exists())

    def test_host_cache_and_worker_limits_are_written_and_invalid_limits_are_rejected(self):
        result, destination = self.install(
            "example/approved", "--cache-gib", "128", "--compiler-cache-gib", "8", "--build-jobs", "1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads((destination / "config.json").read_text())
        self.assertEqual(config["build_cache"]["max_bytes"], 128 * 1024**3)
        self.assertEqual(config["build_cache"]["compiler_gib"], 8)
        self.assertEqual(config["build_jobs"], 1)
        result, destination = self.install("example/approved", "--cache-gib", "2")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
