"""Execute the production browser history handlers without external services."""

import subprocess
from pathlib import Path


def test_web_chat_history_loading():
    subprocess.run(
        [
            "rtk",
            "proxy",
            "node",
            "--test",
            str(Path(__file__).with_name("web_chat_history.test.cjs")),
            str(Path(__file__).with_name("chat_live_events.test.mjs")),
            str(Path(__file__).with_name("web_chat_thread_sync.test.cjs")),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
