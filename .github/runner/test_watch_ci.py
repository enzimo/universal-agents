import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("watch_ci", Path(__file__).parents[1] / "scripts/watch_ci.py")
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


class WatchTests(unittest.TestCase):
    def run_record(self):
        return {"name": "CI", "status": "completed", "conclusion": "success",
                "jobs": [{"status": "completed", "conclusion": "success"}]}

    def test_all_expected_workflows_must_finish(self):
        run = self.run_record()
        self.assertEqual(watch.outcome([run], {"CI", "Native"}), "waiting")
        self.assertEqual(watch.outcome([run], {"CI"}), "success")
        run["jobs"][0]["conclusion"] = None
        self.assertEqual(watch.outcome([run], {"CI"}), "waiting")

    def test_failure_is_reported_while_other_jobs_are_queued(self):
        run = self.run_record()
        run.update(status="in_progress", conclusion=None)
        run["jobs"].append({"status": "completed", "conclusion": "failure"})
        self.assertEqual(watch.outcome([run], {"CI"}), "failure")

    def test_all_skipped_is_not_validation(self):
        run = self.run_record()
        run["jobs"][0]["conclusion"] = "skipped"
        self.assertEqual(watch.outcome([run], {"CI"}), "failure")

    def test_log_propagation_delay_does_not_erase_job_status(self):
        def api(*args):
            url = args[-1]
            if url.endswith("/logs"):
                raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 404: log not ready")
            if "/jobs?" in url:
                return json.dumps({"jobs": [{"id": 9, "status": "completed", "conclusion": "success"}]})
            return json.dumps(dict(self.run_record(), id=7, head_sha="a" * 40))
        with tempfile.TemporaryDirectory() as directory, patch.object(watch, "gh", side_effect=api):
            result = watch.snapshot("owner/repo", [7], None, set(), Path(directory))
            self.assertEqual(watch.outcome(result, {"CI"}), "success")
            self.assertIn("log_pending", result[0]["jobs"][0])


if __name__ == "__main__":
    unittest.main()
