"""Run an in-page browser probe against the onboarding aesthetic preview.

The probes (``tests/*_browser_probe.mjs``) check behavior that only a real
layout engine shows; they also run as-is through the collaborative browser.
Here they run in headless Chromium through ``browser_probe_runner.cjs``,
which needs Node Playwright and Chromium, so the tests using this are opt-in
(``RUN_CHROMIUM_TESTS=1``).
"""

from __future__ import annotations

import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading

import pytest

from scripts.run_onboarding_aesthetic_preview import DEFAULT_ASSET_ROOT, build_handler

RUNNER = Path(__file__).with_name("browser_probe_runner.cjs")

requires_chromium = pytest.mark.skipif(
    os.environ.get("RUN_CHROMIUM_TESTS") != "1",
    reason="set RUN_CHROMIUM_TESTS=1 with Node Playwright and Chromium installed",
)


def run_browser_probe(page_path: str, probe_file: str, export_name: str) -> list[str]:
    """Serve the preview, run the probe on ``page_path`` and return the cases it checked."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(DEFAULT_ASSET_ROOT))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                "rtk", "proxy", "node", str(RUNNER),
                f"http://127.0.0.1:{server.server_port}", page_path, probe_file, export_name,
            ],
            capture_output=True, text=True, timeout=90, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
