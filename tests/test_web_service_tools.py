from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import requests

from custom_tools.runtime_context import set_agent_ref


class _FakeTaskManager:
    current_task_id = "task_web_service_test"

    def get_task_info(self, task_id: str):
        return SimpleNamespace(
            meta=SimpleNamespace(conversation_id="conversation_test")
        )


class _FakeAgent:
    """An agent in single-person mode (authorization off), so only folder rules apply."""

    team_id = "team_test"
    id = "agent_web_service_test"
    named_agent_type = "WebResearchAgent"
    task_manager = _FakeTaskManager()
    authorization_config = {"enabled": False}

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = str(workspace_dir)


class _Response:
    def __init__(
        self,
        payload=None,
        *,
        text: str = "",
        status_code: int = 200,
        headers=None,
        content: bytes = b"",
        lines=None,
    ) -> None:
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self._lines = list(lines or [])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self.content

    def iter_lines(self, decode_unicode: bool = False):
        del decode_unicode
        yield from self._lines


def _payload(result):
    return json.loads(result["content"][0]["text"])


def _set_service_environment(monkeypatch) -> None:
    monkeypatch.setenv("CRAWL4AI_BASE_URL", "http://crawl4ai.test:11235")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng.test:8080")
    monkeypatch.setenv("CRAWL4AI_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("SEARXNG_TIMEOUT_SECONDS", "5")


def _broker_answers(monkeypatch, tools, payload, captured=None) -> None:
    """Answer the screenshot tool's broker request (tests/test_broker_web_tools.py runs the real broker)."""

    from custom_tools.broker_web_requests import BrokerWebResponse

    def fake_send(method, url, **kwargs):
        assert (method, url) == ("POST", "http://crawl4ai.test:11235/crawl")
        if captured is not None:
            captured.update(kwargs)
        return BrokerWebResponse(http_status=200, content_type="application/json", json=payload, text="", truncated=False)

    monkeypatch.setattr(tools, "send_web_request", fake_send)


def _refuse_requests(monkeypatch, tools, message: str) -> list:
    called: list = []

    def fake_send(*args, **kwargs):
        called.append(args)
        raise AssertionError(message)

    monkeypatch.setattr(tools, "send_web_request", fake_send)
    return called


def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)


def test_searxng_search_preserves_typed_results_and_response_metadata(
    tmp_path, monkeypatch
):
    from custom_tools import web_service_tools as tools

    set_agent_ref(_FakeAgent(tmp_path))
    _set_service_environment(monkeypatch)
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        assert url == "http://searxng.test:8080/search"
        return _Response(
            {
                "query": "agent search",
                "number_of_results": 42,
                "answers": ["direct answer"],
                "corrections": ["agentic search"],
                "suggestions": ["agent search framework"],
                "infoboxes": [{"infobox": "Agent"}],
                "unresponsive_engines": [["engine-a", "timeout"]],
                "results": [
                    {
                        "title": "Image result",
                        "url": "https://example.com/image",
                        "content": "typed result",
                        "engine": "images",
                        "category": "images",
                        "template": "images.html",
                        "img_src": "https://example.com/image.png",
                        "thumbnail_src": "https://example.com/thumb.png",
                        "resolution": "1600x900",
                    }
                ],
            }
        )

    monkeypatch.setattr(tools.requests, "post", fake_post)

    result = tools.searxng(
        operation="search",
        query="agent search",
        categories="images,science",
        engines="images",
        time_range="month",
        max_results=5,
        options={"enabled_plugins": "Hash_plugin", "image_proxy": True},
    )
    payload = _payload(result)

    assert payload["status"] == "success"
    assert payload["service_reported_result_count"] == 42
    assert payload["answers"] == ["direct answer"]
    assert payload["unresponsive_engines"] == [["engine-a", "timeout"]]
    assert payload["results"][0]["details"]["img_src"].endswith("image.png")
    assert payload["results"][0]["details"]["resolution"] == "1600x900"
    assert captured["data"]["enabled_plugins"] == "Hash_plugin"
    assert captured["data"]["image_proxy"] is True
    assert Path(payload["raw_file"]).is_file()
    assert Path(payload["normalized_file"]).is_file()
    assert Path(payload["manifest"]).is_file()


FIXED_STAMP = "20261016T120000000000Z"


def test_two_service_result_folders_made_at_the_same_moment_differ(tmp_path, monkeypatch):
    from custom_tools import web_service_tools as tools

    set_agent_ref(_FakeAgent(tmp_path))
    monkeypatch.setattr(tools, "_now_stamp", lambda: FIXED_STAMP)

    first_dir, first_id, _identity = tools._create_artifact_dir("searxng", "search", "agent search")
    second_dir, second_id, _identity = tools._create_artifact_dir("searxng", "search", "agent search")

    assert first_dir != second_dir and first_id != second_id
    assert first_dir.is_dir() and second_dir.is_dir()
    assert first_dir.parent == second_dir.parent
    for artifact_id in (first_id, second_id):
        readable, _, random_part = artifact_id.rpartition("_")
        assert readable == f"{FIXED_STAMP}_searxng_search_agent-search"
        assert len(random_part) == 12 and int(random_part, 16) >= 0


def test_two_searxng_searches_at_the_same_moment_keep_their_own_files(tmp_path, monkeypatch):
    """The same query at the same moment (parallel calls in one turn) used to
    share a folder, and the second search overwrote the first one's files."""

    from custom_tools import web_service_tools as tools

    set_agent_ref(_FakeAgent(tmp_path))
    _set_service_environment(monkeypatch)
    monkeypatch.setattr(tools, "_now_stamp", lambda: FIXED_STAMP)
    pending = ["first-page", "second-page"]

    def fake_post(url, **kwargs):
        title = pending.pop(0)
        return _Response({"query": "agent search", "results": [{"title": title, "url": f"https://example.com/{title}"}]})

    monkeypatch.setattr(tools.requests, "post", fake_post)

    first = _payload(tools.searxng(operation="search", query="agent search"))
    second = _payload(tools.searxng(operation="search", query="agent search", page=2))

    assert first["artifact_dir"] != second["artifact_dir"]
    for payload, title in ((first, "first-page"), (second, "second-page")):
        for key in ("raw_file", "normalized_file", "manifest"):
            assert Path(payload[key]).parent == Path(payload["artifact_dir"])
        raw = json.loads(Path(payload["raw_file"]).read_text(encoding="utf-8"))
        assert [item["title"] for item in raw["results"]] == [title]
        manifest = json.loads(Path(payload["manifest"]).read_text(encoding="utf-8"))
        assert manifest["artifact_id"] == payload["artifact_id"]


def test_searxng_disabled_format_returns_actionable_error(tmp_path, monkeypatch):
    from custom_tools import web_service_tools as tools

    set_agent_ref(_FakeAgent(tmp_path))
    _set_service_environment(monkeypatch)
    monkeypatch.setattr(
        tools.requests,
        "post",
        lambda *args, **kwargs: _Response(status_code=403),
    )

    result = tools.searxng(
        operation="search", query="agent search", output_format="rss"
    )
    payload = _payload(result)

    assert result["status"] == "error"
    assert payload["failure_reason"] == "operation_forbidden"
    assert "enable output format 'rss'" in payload["mitigation_hints"][0]


def test_capture_web_screenshot_uses_exact_viewport_and_saves_png(
    tmp_path, monkeypatch
):
    from custom_tools import web_service_tools as tools

    set_agent_ref(_FakeAgent(tmp_path))
    _set_service_environment(monkeypatch)
    png = _png_header(1600, 1600)
    captured = {}
    _broker_answers(
        monkeypatch,
        tools,
        {
            "success": True,
            "server_processing_time_s": 0.5,
            "results": [
                {
                    "url": "https://example.com/",
                    "success": True,
                    "status_code": 200,
                    "screenshot": base64.b64encode(png).decode(),
                }
            ],
        },
        captured,
    )

    payload = _payload(
        tools.capture_web_screenshot(
            url="https://example.com/",
            width_px=1600,
            height_px=1600,
            wait_seconds=1.5,
            wait_for_images=True,
        )
    )

    assert "headers" in captured and "Authorization" not in captured["headers"], "the broker adds the token"
    browser_params = captured["json_body"]["browser_config"]["params"]
    crawler_params = captured["json_body"]["crawler_config"]["params"]
    assert browser_params == {
        "headless": True,
        "viewport_width": 1600,
        "viewport_height": 1600,
        "text_mode": False,
    }
    assert crawler_params == {
        "cache_mode": "bypass",
        "force_viewport_screenshot": True,
        "screenshot": True,
        "screenshot_wait_for": 1.5,
        "wait_for_images": True,
    }
    assert payload["status"] == "success"
    assert payload["requested_dimensions"] == {
        "width_px": 1600,
        "height_px": 1600,
    }
    assert payload["actual_dimensions"] == {
        "width_px": 1600,
        "height_px": 1600,
    }
    capture_path = Path(payload["path"])
    assert capture_path.name == "screenshot_1600x1600.png"
    assert capture_path.read_bytes() == png
    stored_response = Path(
        payload["artifact_dir"], "crawl4ai_response.json"
    ).read_text(encoding="utf-8")
    assert base64.b64encode(png).decode() not in stored_response
    assert payload["manifest"] == str(Path(payload["artifact_dir"], "manifest.json"))


def test_capture_web_screenshot_rejects_invalid_dimensions_without_request(
    tmp_path, monkeypatch
):
    from custom_tools import web_service_tools as tools

    set_agent_ref(_FakeAgent(tmp_path))
    _set_service_environment(monkeypatch)
    called = _refuse_requests(monkeypatch, tools, "invalid dimensions must not reach Crawl4AI")

    for width in (0, 4001, True, "1600"):
        payload = _payload(
            tools.capture_web_screenshot(
                url="https://example.com/", width_px=width, height_px=800
            )
        )
        assert payload["failure_reason"] == "invalid_viewport_dimensions"

    assert called == []


def test_capture_web_screenshot_rejects_invalid_render_options_without_request(
    tmp_path, monkeypatch
):
    from custom_tools import web_service_tools as tools

    set_agent_ref(_FakeAgent(tmp_path))
    _set_service_environment(monkeypatch)
    called = _refuse_requests(monkeypatch, tools, "invalid render options must not reach Crawl4AI")

    invalid_wait = _payload(
        tools.capture_web_screenshot(
            url="https://example.com/", wait_seconds=float("inf")
        )
    )
    invalid_images = _payload(
        tools.capture_web_screenshot(
            url="https://example.com/", wait_for_images="false"
        )
    )

    assert invalid_wait["failure_reason"] == "invalid_wait_seconds"
    assert invalid_images["failure_reason"] == "invalid_wait_for_images"
    assert called == []


def test_capture_web_screenshot_rejects_credentialed_url_without_leaking_secret(
    tmp_path, monkeypatch
):
    from custom_tools import web_service_tools as tools

    set_agent_ref(_FakeAgent(tmp_path))
    _set_service_environment(monkeypatch)

    payload = _payload(
        tools.capture_web_screenshot(
            url="https://user:secret@example.com/", width_px=800, height_px=600
        )
    )

    assert payload["failure_reason"] == "invalid_url"
    manifest_text = Path(payload["artifact_dir"], "manifest.json").read_text(
        encoding="utf-8"
    )
    assert "secret" not in manifest_text
    assert "user" not in Path(payload["artifact_dir"]).name


def test_capture_web_screenshot_rejects_dimension_mismatch_and_keeps_diagnostic(
    tmp_path, monkeypatch
):
    from custom_tools import web_service_tools as tools

    set_agent_ref(_FakeAgent(tmp_path))
    _set_service_environment(monkeypatch)
    png = _png_header(1080, 600)
    _broker_answers(
        monkeypatch,
        tools,
        {
            "success": True,
            "results": [
                {
                    "url": "https://example.com/",
                    "success": True,
                    "status_code": 200,
                    "screenshot": base64.b64encode(png).decode(),
                }
            ],
        },
    )

    payload = _payload(
        tools.capture_web_screenshot(
            url="https://example.com/", width_px=1600, height_px=1600
        )
    )

    assert payload["failure_reason"] == "screenshot_dimension_mismatch"
    assert "1080x600" in payload["message"]
    screenshot_files = list(Path(payload["artifact_dir"]).glob("*.png"))
    assert len(screenshot_files) == 1
    assert screenshot_files[0].read_bytes() == png


def test_catalog_exposes_screenshot_and_searxng_but_not_a_custom_crawl_tool():
    from custom_tools.registry import WEB_RESEARCH_PROVIDER
    from custom_tools.web_service_tools import capture_web_screenshot

    names = [
        getattr(item, "tool_name", getattr(item, "__name__", ""))
        for item in WEB_RESEARCH_PROVIDER.tools
    ]

    assert "crawl4ai" not in names
    assert names.count("capture_web_screenshot") == 1
    assert names.count("searxng") == 1
    assert "web_fetch_artifact" not in names
    assert "web_search_artifact" not in names
    screenshot_schema = capture_web_screenshot.tool_spec["inputSchema"]["json"]
    assert screenshot_schema["properties"]["width_px"]["type"] == "integer"
    assert "1 through 4000" in screenshot_schema["properties"]["width_px"]["description"]
    assert "crawl4ai_browse" not in names
    assert "searxng_search" not in names


def test_capture_web_screenshot_names_the_redirect_and_the_setting_that_fixes_it(tmp_path, monkeypatch):
    """A fresh instance's base URL was http:// and the service redirects to https://; the tool only said http_308."""

    from custom_tools import web_service_tools as tools
    from custom_tools.broker_web_requests import BrokerWebResponse

    set_agent_ref(_FakeAgent(tmp_path))
    _set_service_environment(monkeypatch)
    monkeypatch.setattr(
        tools,
        "send_web_request",
        lambda method, url, **kwargs: BrokerWebResponse(
            http_status=308, content_type="", json=None, text="", truncated=False,
            redirect_location="https://crawl4ai.test/crawl",
        ),
    )

    payload = _payload(tools.capture_web_screenshot(url="https://example.com/", width_px=800, height_px=600))

    assert payload["failure_reason"] == "redirected"
    assert payload["status_code"] == 308
    assert "a redirect to https://crawl4ai.test/crawl" in payload["message"]
    assert payload["mitigation_hints"] == [
        "an administrator sets web_services.crawl4ai.base_url in instance_config.yaml to https://crawl4ai.test "
        "and removes any CRAWL4AI_BASE_URL override, then restarts"
    ]
