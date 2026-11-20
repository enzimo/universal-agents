"""Web Chat's thread list in headless Chromium, against the aesthetic preview.

``web_chat_thread_list_probe.cjs`` checks what only a real browser shows: the
layout and how many rows fit (A1), the menu by right-click, keyboard and long
press with Rename and Delete (A8), the hover preview (A9), and the narrow
layout. It needs Node Playwright and Chromium, so it is opt-in like the other
browser checks (``RUN_CHROMIUM_TESTS=1``); screenshots go to the test's folder.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from scripts.run_onboarding_aesthetic_preview import DEFAULT_ASSET_ROOT, build_handler
from tests.browser_probe import requires_chromium

PROBE = Path(__file__).with_name("web_chat_thread_list_probe.cjs")


@requires_chromium
def test_thread_list_layout_menu_and_hover_preview_in_chromium(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(DEFAULT_ASSET_ROOT))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            ["rtk", "proxy", "node", str(PROBE), f"http://127.0.0.1:{server.server_port}", str(tmp_path)],
            capture_output=True, text=True, timeout=180, check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    checked = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(checked) == 7
    assert {path.name for path in tmp_path.glob("*.png")} == {
        "thread-list-1280x880.png", "thread-menu.png", "thread-hover.png", "thread-status-failure.png",
        "thread-list-390x844.png",
    }
