"""Execute the broker's inline polling script with a controlled browser clock."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from secure_tool_broker.sign_in_pages import sign_in_wait_script


@pytest.mark.parametrize("terminal", ["success", "error", "gone"])
@pytest.mark.parametrize("provider", ["google", "github"])
def test_broker_auth_polling_preserves_controls_and_stops_at_completion(terminal, provider):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the broker auth page script")
    script = sign_in_wait_script().split("<script>", 1)[1].split("</script>", 1)[0]
    subprocess.run(
        [node, str(Path(__file__).with_name("broker_auth_page_probe.cjs"))],
        input=json.dumps({"script": script, "terminal": terminal, "provider": provider}),
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )


def test_broker_pages_embed_the_single_dark_renderer_and_its_settings(monkeypatch):
    """Broker pages work without the website, so they inline the renderer.

    The renderer comes from its one source file in the gateway assets, after
    the appearance and light-mesh scripts it depends on, and the page carries
    the saved dark settings as CSS variables for appearance.js to read.
    """

    from secure_tool_broker.auth_pages import BROKER_APPEARANCE_ENV, render_broker_auth_page

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv(BROKER_APPEARANCE_ENV, json.dumps({"dark": {"facet_size": 300, "detail": "basic"}}))
    page = render_broker_auth_page("Sign in", "<p>Body</p>")

    renderer = (root / "assistant_gateway" / "onboarding_web" / "assets" / "metallic_mesh.js").read_text(encoding="utf-8")
    appearance = (root / "user_docs_source" / "assets" / "appearance.js").read_text(encoding="utf-8")
    light_mesh = (root / "user_docs_source" / "assets" / "static-mesh.js").read_text(encoding="utf-8")
    assert f"<script>\n{renderer}\n</script>" in page
    assert page.index(appearance) < page.index(light_mesh) < page.index(renderer)
    assert "--ua-dark-facet-size: 300;" in page
    assert "--ua-dark-detail: basic;" in page
