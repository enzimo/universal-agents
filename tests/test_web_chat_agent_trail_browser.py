"""Web Chat's agent trail in headless Chromium, against the aesthetic preview (A4-A7).

``web_chat_agent_trail_probe.cjs`` measures what only a real browser shows: each
triangle's contrast against the pixels drawn behind it in normal, hovered, active
and Needs you rows in both themes (at least 3:1), that those backgrounds stay in
the range ``agent_trail.mjs`` fits its colours to, that the pulse stops without
motion or with Interface Animation off, the row's words for who works, and the
hover card's footer. Opt-in like the other browser checks (``RUN_CHROMIUM_TESTS=1``);
screenshots of a working row and the hover card in both themes go to the test's folder.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from scripts.run_onboarding_aesthetic_preview import DEFAULT_ASSET_ROOT, build_handler
from tests.browser_probe import requires_chromium

PROBE = Path(__file__).with_name("web_chat_agent_trail_probe.cjs")


@requires_chromium
def test_agent_trail_contrast_motion_and_hover_footer_in_chromium(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(DEFAULT_ASSET_ROOT))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            ["rtk", "proxy", "node", str(PROBE), f"http://127.0.0.1:{server.server_port}", str(tmp_path)],
            capture_output=True, text=True, timeout=300, check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    checked = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(checked) == 5
    for scheme in ("light", "dark"):
        assert (tmp_path / f"agent-trail-working-row-{scheme}.png").exists()
        assert (tmp_path / f"agent-trail-hover-{scheme}.png").exists()
