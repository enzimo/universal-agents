import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import image_cache


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            "UA_CI_IMAGE_CACHE_VERSION": "1", "UA_CI_IMAGE_CACHE": str(self.root),
            "UA_CI_BUILDER": "test-builder", "UA_CI_ID": "test-job",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def export(self, command, **_kwargs):
        destination = next(arg.split("dest=", 1)[1].split(",")[0]
                           for arg in command if arg.startswith("type=local,dest="))
        path = Path(destination)
        path.mkdir()
        (path / "index.json").write_text('{"manifests": []}')
        (path / "new-blob").write_text("new snapshot")

    def build(self):
        image_cache.build("Dockerfile", "tools", "test:latest")

    def test_warm_build_imports_and_replaces_snapshot_without_old_blobs(self):
        with patch.object(image_cache.subprocess, "run", side_effect=self.export) as execute:
            self.build()
            current = next(self.root.iterdir())
            (current / "unreferenced-old-blob").write_text("obsolete")
            self.build()
        self.assertIn("--cache-from", execute.call_args.args[0])
        self.assertTrue((current / "new-blob").is_file())
        self.assertFalse((current / "unreferenced-old-blob").exists())
        self.assertEqual(list(self.root.iterdir()), [current])

    def test_failed_build_preserves_last_good_snapshot_and_removes_partial_export(self):
        with patch.object(image_cache.subprocess, "run", side_effect=self.export):
            self.build()
        current = next(self.root.iterdir())
        (current / "keep").write_text("last good")

        def failed(command, **kwargs):
            self.export(command, **kwargs)
            raise subprocess.CalledProcessError(1, command)

        with (patch.object(image_cache.subprocess, "run", side_effect=failed),
              self.assertRaises(subprocess.CalledProcessError)):
            self.build()
        self.assertEqual((current / "keep").read_text(), "last good")
        self.assertEqual(list(self.root.iterdir()), [current])

    def test_interrupted_promotion_recovers_previous_before_next_build(self):
        with patch.object(image_cache.subprocess, "run", side_effect=self.export):
            self.build()
        current = next(self.root.iterdir())
        current.rename(current.with_name(current.name + ".previous"))
        with patch.object(image_cache.subprocess, "run", side_effect=self.export) as execute:
            self.build()
        self.assertIn("--cache-from", execute.call_args.args[0])
        self.assertEqual(list(self.root.iterdir()), [current])

    @unittest.skipIf(os.name == "nt", "Native Windows junctions covered by cache tests")
    def test_symlink_export_boundary_is_refused_without_removing_target(self):
        with patch.object(image_cache.subprocess, "run", side_effect=self.export):
            self.build()
        current = next(self.root.iterdir())
        pending = current.with_name(current.name + ".next")
        pending.symlink_to(current, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.build()
        self.assertTrue((current / "index.json").is_file())


if __name__ == "__main__":
    unittest.main()
