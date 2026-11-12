import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import build_cache
import runner_metrics


class MetricsTests(unittest.TestCase):
    def test_sampler_finishes_and_retains_observations_after_job_cache_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            (state / "cache").mkdir(parents=True)
            job = {"id": "a" * 24, "job": "test", "run_id": "1", "attempt": "1",
                   "metrics": True, "started_at": time.time()}
            build_cache.write_json(state / "job.json", job)
            config = {"state": str(state), "metrics": {"interval_seconds": 0.1, "keep_days": 7}}
            runner_metrics.start(config, job)
            try:
                runner_metrics.finish(config, job, recovered=False)
                report = json.loads((state.parent / "metrics" / (job["id"] + ".json")).read_text())
                self.assertTrue(report["complete"])
                self.assertGreater(report["samples"], 0)
                self.assertGreater(report["sampled_peak_memory_used_bytes"], 0)
                self.assertGreaterEqual(report["elapsed_seconds"], 0)
                self.assertFalse(report["recovered"])
            finally:
                (state / "cache" / f"resources-{job['id']}.stop").touch()

    def test_stale_sampler_exits_without_writing_into_a_new_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "cache").mkdir()
            build_cache.write_json(state / "job.json", {"id": "new-job"})
            result = subprocess.run(
                ["rtk", "proxy", sys.executable, runner_metrics.__file__, str(state), "old-job", "0.1"],
                check=False, timeout=10,
            )
            self.assertEqual(result.returncode, 0)
            self.assertFalse((state / "cache" / "resources.json").exists())

    def test_delayed_old_reading_cannot_publish_after_job_identity_changes(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "cache").mkdir()
            build_cache.write_json(state / "job.json", {"id": "old-job"})
            entered, release = threading.Event(), threading.Event()

            def delayed(_previous):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Test reader did not resume")
                return {"cpu_percent": 99, "memory_used_bytes": 123}, None

            with patch.object(runner_metrics, "reading", side_effect=delayed):
                worker = threading.Thread(target=runner_metrics.sample, args=(state, "old-job", 0.1))
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    build_cache.write_json(state / "job.json", {"id": "new-job"})
                    (state / "cache" / "resources-new-job.stop").touch()
                finally:
                    release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
            self.assertFalse(list((state / "cache").glob("*.json")))

    def test_atomic_metadata_failure_preserves_existing_manifest(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.json"
            build_cache.write_json(path, {"id": "old"})
            with (patch.object(build_cache.os, "replace", side_effect=OSError("interrupted")),
                  self.assertRaises(OSError)):
                build_cache.write_json(path, {"id": "new"})
            self.assertEqual(json.loads(path.read_text()), {"id": "old"})
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
