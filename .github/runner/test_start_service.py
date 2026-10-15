import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import start_service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(
            os.environ,
            {
                "UA_CI_ID": "a" * 24,
                "NATS_DOCKER_CONTAINER": "owned-nats",
                "UA_CI_POSTGRES": "owned-postgres",
                "UA_CI_CACHE": str(self.root),
                "GITHUB_ENV": str(self.root / "env"),
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def result(self, code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def test_service_has_explicit_restart_stable_port_and_owned_label(self):
        with patch.object(
            start_service, "docker", return_value=self.result(stdout="id")
        ) as docker:
            start_service.start("nats")
        args = docker.call_args.args
        binding = args[args.index("--publish") + 1]
        address, port, container_port = binding.split(":")
        self.assertEqual(address, "127.0.0.1")
        self.assertGreater(int(port), 0)
        self.assertEqual(container_port, "4222")
        self.assertIn("com.universalagents.ci.job=" + "a" * 24, args)
        self.assertEqual(
            (self.root / "env").read_text(),
            f"NATS_URL=nats://127.0.0.1:{port}\nNATS_SERVERS=nats://127.0.0.1:{port}\n"
        )
        self.assertTrue((self.root / "docker-used").exists())

    def test_port_race_retries_after_removing_only_owned_failed_container(self):
        replies = [
            self.result(1, stderr="address already in use"),
            self.result(stdout="a" * 24),
            self.result(),
            self.result(),
        ]
        with patch.object(start_service, "docker", side_effect=replies) as docker:
            start_service.start("postgres")
        self.assertEqual(
            docker.call_args_list[2].args,
            ("rm", "--force", "--volumes", "owned-postgres"),
        )
        self.assertEqual(docker.call_count, 4)
        self.assertIn("LIVE_TEST_POSTGRES_DSN=", (self.root / "env").read_text())

    def test_unowned_collision_is_never_removed(self):
        replies = [
            self.result(1, stderr="port is already allocated"),
            self.result(stdout="someone-else"),
        ]
        with (
            patch.object(start_service, "docker", side_effect=replies) as docker,
            self.assertRaises(RuntimeError),
        ):
            start_service.start("nats")
        self.assertEqual(docker.call_count, 2)

    def test_daemon_failures_are_not_retried_or_hidden(self):
        with (
            patch.object(
                start_service,
                "docker",
                return_value=self.result(1, stderr="daemon unavailable"),
            ) as docker,
            self.assertRaisesRegex(RuntimeError, "daemon unavailable"),
        ):
            start_service.start("nats")
        self.assertEqual(docker.call_count, 1)


if __name__ == "__main__":
    unittest.main()
