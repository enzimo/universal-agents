"""Every Universal Agents page shows the team's icon in the browser tab.

Browsers ask each site for ``/favicon.ico`` even when a page names no icon, and
use that file for tabs, bookmarks and history. The servers that serve files
answer it; the broker's sign-in pages and the activity dashboard carry the icon
inside the page (runtime_hosting.page_icon).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.interactive_apps.website_support import ONBOARDING_ASSET_ROOT
from runtime_hosting.page_icon import FAVICON_PATH, favicon_link_tag
from secure_tool_broker.auth_pages import render_broker_auth_page
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp, _config, _FakeGateway

REPO = Path(__file__).resolve().parents[1]
ICO_HEADER = b"\x00\x00\x01\x00"


@pytest.mark.parametrize("web_chat_enabled", [True, False])
@pytest.mark.asyncio
async def test_the_gateway_website_serves_the_icon(tmp_path: Path, web_chat_enabled: bool) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = web_chat_enabled
    app = InternalWebsiteInteractiveApp(config, gateway=_FakeGateway(), bridge=SimpleNamespace())

    async with TestClient(TestServer(app.build_app())) as client:
        response = await client.get("/favicon.ico")
        body = await response.read()

    assert response.status == 200
    assert body.startswith(ICO_HEADER) and body == FAVICON_PATH.read_bytes()


@pytest.mark.parametrize(
    ("page", "href"),
    [
        *((ONBOARDING_ASSET_ROOT / name, "/favicon.ico") for name in ("chat.html", "config_editor.html", "index.html", "workflow.html", "team_import.html")),
        (REPO / "team_transfer" / "review_assets" / "review.html", "/assets/favicon.ico"),
    ],
    ids=lambda value: value.name if isinstance(value, Path) else value,
)
def test_served_pages_name_the_icon(page: Path, href: str) -> None:
    assert f'<link rel="icon" href="{href}" sizes="any">' in page.read_text(encoding="utf-8")


def test_the_docs_use_the_same_icon() -> None:
    mkdocs = yaml.load((REPO / "mkdocs.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert mkdocs["theme"]["favicon"] == "assets/favicon.ico"
    assert (REPO / "user_docs_source" / "assets" / "favicon.ico").read_bytes() == FAVICON_PATH.read_bytes()


def test_pages_on_other_servers_carry_the_icon_inline() -> None:
    tag = favicon_link_tag()

    assert tag.startswith('<link rel="icon" href="data:image/x-icon;base64,')
    assert tag in render_broker_auth_page("Sign in", "<p>Body</p>")
