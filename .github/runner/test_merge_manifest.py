import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("merge_manifest", Path(__file__).with_name("merge_manifest.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "digests"
        self.root.mkdir()
        for arch, digit in [("amd64", "1"), ("arm64", "2")]:
            (self.root / f"{arch}.digest").write_text("sha256:" + digit * 64)
        self.image = "ghcr.io/example/runtime"
        self.tags = [self.image + ":0.4.5", self.image + ":latest"]
        self.calls = []

    def docker(self, *args):
        self.calls.append(args)
        if "--format" in args:
            return json.dumps({"digest": "sha256:" + "3" * 64})
        if args[0] == "inspect":
            arch = "amd64" if args[-1].endswith("1" * 64) else "arm64"
            platform = {"architecture": arch, "os": "linux"}
            if arch == "arm64":
                platform["variant"] = "v8"
            return json.dumps({"manifests": [
                {"platform": platform},
                {"platform": {"os": "unknown", "architecture": "unknown"},
                 "annotations": {"vnd.docker.reference.type": "attestation-manifest"}},
            ]})
        return "{}"

    def test_both_platforms_are_inspected_before_tags_are_published(self):
        with patch.object(module, "docker", side_effect=self.docker):
            self.assertEqual(module.merge(self.root, self.image, self.tags), "sha256:" + "3" * 64)
        self.assertEqual([call[0] for call in self.calls], ["inspect", "inspect", "create", "inspect"])
        self.assertEqual(self.calls[2][1:5], ("--tag", self.tags[0], "--tag", self.tags[1]))
        self.assertEqual(len([arg for arg in self.calls[2] if "@sha256:" in arg]), 2)

    def test_wrong_architecture_does_not_publish(self):
        wrong = json.dumps({"manifests": [{"platform": {"architecture": "amd64", "os": "linux"}}]})
        with patch.object(module, "docker", return_value=wrong) as docker:
            with self.assertRaisesRegex(ValueError, "Unexpected platform"):
                module.merge(self.root, self.image, self.tags)
        self.assertTrue(all(call.args[0] == "inspect" for call in docker.call_args_list))

    def test_missing_platform_does_not_contact_registry(self):
        (self.root / "arm64.digest").unlink()
        with patch.object(module, "docker") as docker:
            with self.assertRaises(ValueError):
                module.merge(self.root, self.image, self.tags)
        docker.assert_not_called()

    def test_foreign_output_tag_is_rejected(self):
        with patch.object(module, "docker") as docker:
            with self.assertRaises(ValueError):
                module.merge(self.root, self.image, ["ghcr.io/other/runtime:latest"])
        docker.assert_not_called()

    def test_dry_run_cannot_publish(self):
        with patch.object(module, "docker", side_effect=self.docker):
            self.assertIsNone(module.merge(self.root, self.image, self.tags, dry_run=True))
        self.assertIn("--dry-run", self.calls[-1])
        self.assertNotIn("--metadata-file", self.calls[-1])


if __name__ == "__main__":
    unittest.main()
