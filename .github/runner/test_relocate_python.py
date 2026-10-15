import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("relocate", Path(__file__).with_name("relocate_python.py"))
relocate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relocate)


class RelocationTests(unittest.TestCase):
    def test_external_prefix_and_current_interpreter_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "tools"
            with patch.object(relocate.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    relocate.normalize(root, cache)
                prefix = cache / "Python/3.13/x64"
                (prefix / "bin").mkdir(parents=True)
                (prefix / "bin/python3.13").symlink_to(sys.executable)
                with self.assertRaises(ValueError):
                    relocate.normalize(prefix, cache)
                run.assert_not_called()

    def test_probe_does_not_inherit_loader_or_python_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            prefix = cache / "Python/3.13/x64"
            (prefix / "bin").mkdir(parents=True)
            (prefix / "bin/python3.13").touch()
            with patch.object(relocate.subprocess, "run") as run:
                relocate.normalize(prefix, cache)
                environment = run.call_args.kwargs["env"]
                self.assertNotIn("LD_LIBRARY_PATH", environment)
                self.assertNotIn("PYTHONPATH", environment)
