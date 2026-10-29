from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_tools import workspace_scope
from tests.task_work_support import set_agent_ref
from runtime_settings.instance_layout import InstanceLayout


class _FakeTaskManager:
    current_task_id = "task_site_test"

    def get_task_info(self, task_id: str):
        if task_id != "task_site_test":
            return None
        return SimpleNamespace(
            meta=SimpleNamespace(
                conversation_id="conversation_site_test",
                gateway_context=SimpleNamespace(
                    session_key="cli:site-test",
                    channel_name="telegram",
                    chat_id="chat_site_test",
                    thread_id="topic_42",
                ),
            )
        )


class _FakeAgent:
    team_id = "team_site_test"
    id = "agent_site_test"
    named_agent_type = "TeamArchitect"
    task_manager = _FakeTaskManager()
    test_task_id = _FakeTaskManager.current_task_id  # the tool calls are this task's turn

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = str(workspace_dir)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _record(root: Path, site_id: str) -> dict:
    """The trusted record the site host serves the page through (state/published_sites)."""

    return json.loads((root / "state" / "published_sites" / f"{site_id}.json").read_text(encoding="utf-8"))


def test_publish_single_page_site_writes_escaped_checklist_site(tmp_path: Path, monkeypatch) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    set_agent_ref(_FakeAgent(tmp_path))
    monkeypatch.setenv("INTERNAL_WEBSITE_BASE_URL", "http://127.0.0.1:18500")
    monkeypatch.setenv("INTERNAL_WEBSITE_SITES_DIR", "artifacts/sites")
    spec = {
        "title": "Repair <Motor>",
        "summary": "Bench **repair** guide with `safe` markdown.",
        "audience": "Owner",
        "safety_notes": ["Disconnect power first."],
        "materials": [
            {
                "name": "<script>alert(1)</script>",
                "quantity": "1",
                "notes": "Match the nameplate.",
                "required": True,
            }
        ],
        "tools": [{"name": "Multimeter", "required": True}],
        "plans": [{"title": "Prep", "description": "Make the work area safe.", "items": ["Label wires."]}],
        "steps": [
            {
                "title": "Verify zero voltage",
                "details": "Test every conductor pair with `meter`.\n\n- Line to neutral\n- Line to ground",
                "warning": "Stop if **voltage** is present.",
                "verification": "Meter reads zero.",
            }
        ],
        "assumptions": ["Small single-phase motor."],
        "resources": [
            {"label": "Manual", "url": "https://example.test/manual"},
            {"label": "Unsafe link", "url": "javascript:alert(1)"},
        ],
    }

    result = publish_single_page_site(
        json.dumps(spec),
        site_slug="../../motor repair",
        site_kind="how_to",
    )
    payload = _payload(result)

    assert result["status"] == "success"
    assert payload["status"] == "success"
    assert payload["site_url"].startswith("http://127.0.0.1:18500/docs/sites/")
    assert f"/docs/sites/{payload['site_id']}/" in payload["site_url"]
    assert "Return the exact site_url" in payload["next_step_hint"]
    assert "interactive state is saved in the browser" in payload["next_step_hint"]
    assert "Do not invent or shorten generated-site URLs" in payload["next_step_hint"]
    assert "do not wrap a bare URL in Markdown emphasis" in payload["next_step_hint"]
    assert "thread-topic_42" in payload["site_id"]
    assert "Z-" in payload["site_id"]
    assert ".." not in payload["site_id"]
    # New ids end with 8 random hex characters, so page links are hard to guess.
    assert re.fullmatch(r".*-[0-9a-f]{8}", payload["site_id"])
    index_path = Path(payload["index_path"])
    html_text = index_path.read_text(encoding="utf-8")
    # Work for no person publishes into agents/sites/<site_id>/.
    assert index_path == tmp_path / "agents" / "sites" / payload["site_id"] / "index.html"
    record = _record(tmp_path, payload["site_id"])
    assert record["folder"] == f"agents/sites/{payload['site_id']}"
    assert record["owner_person_id"] is None
    assert record["files"] == {"index.html": hashlib.sha256(index_path.read_bytes()).hexdigest()}
    assert record["conversation_id"] == "conversation_site_test"
    assert record["session_key"] == "cli:site-test"
    assert record["channel_name"] == "telegram"
    assert record["chat_id"] == "chat_site_test"
    assert record["thread_id"] == "topic_42"
    assert record["site_kind"] == "how_to"
    assert payload["site_kind"] == "how_to"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text
    assert 'href="javascript:alert(1)"' not in html_text
    assert "<h2>Materials</h2>" in html_text
    assert "Materials To Buy" not in html_text
    assert 'data-check-key="material:0"' in html_text
    assert 'data-check-key="step:1"' in html_text
    assert "/docs/assets/ua-runtime-theme.css" in html_text
    assert "/docs/assets/static-mesh.js" in html_text
    assert "/docs/assets/appearance.js" in html_text
    assert "/docs/assets/metallic_mesh.js" in html_text
    assert "/docs/assets/chat_markdown.js" in html_text
    assert "/docs/assets/operational_ids.js" in html_text
    assert "UAWebChatMarkdown" in html_text
    assert "Audience:" not in html_text
    assert "Site:" not in html_text
    assert re.search(r"Generated: [A-Z][a-z]+ \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M UTC", html_text)
    assert 'id="chat-popout-button"' in html_text
    assert 'aria-label="Pop out chat"' in html_text
    assert ".chat.is-popped" in html_text
    assert ".chat.is-popped .chat-copy" in html_text
    assert "body.chat-popped" in html_text
    assert 'document.body.classList.toggle("chat-popped", popped)' in html_text
    assert "justify-self: end" in html_text
    assert "justify-self: start" in html_text
    assert "setChatPopped" in html_text
    assert 'data-markdown="Bench **repair** guide with `safe` markdown."' in html_text
    assert "Test every conductor pair with `meter`." in html_text
    assert "Line to neutral" in html_text
    assert "ua-doc-static-mesh" in html_text
    assert f"/api/sites/{payload['site_id']}/chat" in html_text
    assert "mirror_to_origin" in html_text


def test_publish_single_page_site_omits_empty_standard_sections(tmp_path: Path, monkeypatch) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    set_agent_ref(_FakeAgent(tmp_path))
    monkeypatch.setenv("INTERNAL_WEBSITE_SITES_DIR", "artifacts/sites")
    spec = {"title": "Minimal Guide", "steps": ["Do one focused thing."]}

    payload = _payload(publish_single_page_site(json.dumps(spec), site_kind="how_to"))
    html_text = Path(payload["index_path"]).read_text(encoding="utf-8")

    assert "<h2>Ordered Steps</h2>" in html_text
    assert "Do one focused thing." in html_text
    assert "Materials To Buy" not in html_text
    assert "<h2>Materials</h2>" not in html_text
    assert "<h2>Safety Notes</h2>" not in html_text
    assert "<h2>Tools</h2>" not in html_text
    assert "<h2>Plans</h2>" not in html_text
    assert "<h2>Assumptions And Limits</h2>" not in html_text
    assert "<h2>References</h2>" not in html_text
    assert "No materials supplied" not in html_text


def test_publish_single_page_site_defaults_to_article_without_checklist_ui(tmp_path: Path, monkeypatch) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    set_agent_ref(_FakeAgent(tmp_path))
    monkeypatch.setenv("INTERNAL_WEBSITE_SITES_DIR", "artifacts/sites")
    spec = {
        "title": "EV Charging Overview",
        "summary": "A reference page for home charging choices.",
        "highlights": [
            {"title": "Level 2", "body": "The usual fit for overnight home charging."},
            {"title": "Portable", "body": "Useful when flexibility matters."},
        ],
        "key_points": ["Check panel capacity.", "Compare connector compatibility."],
        "callouts": [{"title": "Worth checking", "body": "Permit rules vary by location."}],
        "sections": [{"title": "Context", "body": "Charging speed depends on the vehicle and circuit."}],
        "resources": [{"label": "Reference", "url": "https://example.test/charging"}],
    }

    payload = _payload(publish_single_page_site(json.dumps(spec)))
    html_text = Path(payload["index_path"]).read_text(encoding="utf-8")
    manifest = _record(tmp_path, payload["site_id"])

    assert payload["site_kind"] == "article"
    assert manifest["site_kind"] == "article"
    assert "Generated article" in html_text
    assert "<h2>Highlights</h2>" in html_text
    assert "<h2>Key Points</h2>" in html_text
    assert "<h2>Worth Noting</h2>" in html_text
    assert "<h2>Context</h2>" in html_text
    assert "<h2>References</h2>" in html_text
    assert 'data-check-key="' not in html_text
    assert "interactive state is saved in the browser" not in payload["next_step_hint"]


def test_publish_single_page_site_renders_comparison_elements_without_steps(tmp_path: Path, monkeypatch) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    set_agent_ref(_FakeAgent(tmp_path))
    monkeypatch.setenv("INTERNAL_WEBSITE_SITES_DIR", "artifacts/sites")
    spec = {
        "title": "Car A vs Car B",
        "summary": "A practical comparison for a daily commute.",
        "options": [
            {
                "name": "Car A",
                "summary": "Efficient and compact.",
                "best_for": "City driving",
                "pros": ["Lower energy use"],
                "cons": ["Less cargo room"],
            },
            {
                "name": "Car B",
                "summary": "Larger and more flexible.",
                "best_for": "Family trips",
                "pros": ["More cargo room"],
                "cons": ["Higher price"],
            },
        ],
        "comparison_rows": [
            {"criterion": "Range", "values": {"Car A": "280 miles", "Car B": "310 miles"}},
            {"criterion": "Cargo", "values": ["Small", "Large"], "notes": "Seats upright"},
        ],
        "recommendation": {
            "title": "Choose based on space",
            "summary": "Car A fits the commute; Car B earns its premium when cargo capacity matters.",
        },
        "resources": [{"label": "Specifications", "url": "https://example.test/cars"}],
    }

    payload = _payload(publish_single_page_site(json.dumps(spec), site_kind="comparison"))
    html_text = Path(payload["index_path"]).read_text(encoding="utf-8")

    assert payload["site_kind"] == "comparison"
    assert "Generated comparison" in html_text
    assert "<h2>Options</h2>" in html_text
    assert "<h2>Side-By-Side Comparison</h2>" in html_text
    assert '<table class="comparison-table">' in html_text
    assert "Car A" in html_text
    assert "Car B" in html_text
    assert "Best for:" in html_text
    assert "<h2>Recommendation</h2>" in html_text
    assert 'data-check-key="' not in html_text
    assert "<h2>Ordered Steps</h2>" not in html_text


def test_publish_single_page_site_renders_app_tracking_state(tmp_path: Path, monkeypatch) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    set_agent_ref(_FakeAgent(tmp_path))
    monkeypatch.setenv("INTERNAL_WEBSITE_SITES_DIR", "artifacts/sites")
    spec = {
        "title": "Weekly Workout Tracker",
        "summary": "Track this week's movement goals.",
        "stats": [
            {"label": "Weekly goal", "value": "4 sessions"},
            {"label": "Focus", "value": "Strength"},
        ],
        "progress_trackers": [
            {
                "id": "workouts",
                "label": "Completed workouts",
                "current": 1,
                "target": 4,
                "unit": "sessions",
                "step": 1,
            },
            {
                "label": "Training minutes",
                "current": 45,
                "target": 180,
                "unit": "minutes",
                "step": 15,
            },
        ],
        "habits": [
            {"name": "Warm up before training", "notes": "Five easy minutes."},
            {"name": "Log recovery notes"},
        ],
        "actions": [{"label": "Exercise library", "url": "https://example.test/exercises"}],
        "sections": [{"title": "Plan", "body": "Alternate strength and mobility sessions."}],
    }

    payload = _payload(publish_single_page_site(json.dumps(spec), site_kind="app"))
    html_text = Path(payload["index_path"]).read_text(encoding="utf-8")
    manifest = _record(tmp_path, payload["site_id"])

    assert payload["site_kind"] == "app"
    assert manifest["site_kind"] == "app"
    assert "Interactive app" in html_text
    assert "<h2>At A Glance</h2>" in html_text
    assert "<h2>Progress</h2>" in html_text
    assert "<h2>Trackable Items</h2>" in html_text
    assert "<h2>Actions And Links</h2>" in html_text
    assert 'data-progress-key="1-workouts"' in html_text
    assert 'data-progress-target="4"' in html_text
    assert 'data-check-key="app:0"' in html_text
    assert "function restoreProgressTrackers()" in html_text
    assert 'localStorage.setItem(key, String(value))' in html_text
    assert "interactive state is saved in the browser" in payload["next_step_hint"]


def test_publish_single_page_site_exposes_typed_site_kind_contract() -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    site_kind_schema = publish_single_page_site.tool_spec["inputSchema"]["json"]["properties"]["site_kind"]

    assert site_kind_schema["default"] == "article"
    assert site_kind_schema["enum"] == ["article", "comparison", "how_to", "app"]
    assert "These are choices, not requirements" in publish_single_page_site.tool_spec["description"]


def test_publish_single_page_site_rejects_unknown_site_kind(tmp_path: Path, monkeypatch) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    set_agent_ref(_FakeAgent(tmp_path))
    monkeypatch.setenv("INTERNAL_WEBSITE_SITES_DIR", "artifacts/sites")

    result = publish_single_page_site(json.dumps({"title": "Unknown", "site_kind": "dashboard"}))
    payload = _payload(result)

    assert result["status"] == "error"
    assert payload["failure_reason"] == "invalid_site_kind"
    assert payload["retryable"] is False


def test_publish_single_page_site_can_regenerate_existing_site(tmp_path: Path, monkeypatch) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    set_agent_ref(_FakeAgent(tmp_path))
    monkeypatch.setenv("INTERNAL_WEBSITE_SITES_DIR", "artifacts/sites")
    first = _payload(
        publish_single_page_site(
            json.dumps(
                {
                    "title": "Reusable Guide",
                    "steps": ["First version only."],
                }
            ),
            site_slug="reusable",
            site_kind="how_to",
        )
    )

    updated = _payload(
        publish_single_page_site(
            json.dumps(
                {
                    "title": "Reusable Guide",
                    "update_site_url": first["site_url"],
                    "steps": ["Updated version."],
                    "resources": [{"label": "Reference", "url": "https://example.test/reference"}],
                }
            ),
            site_slug="ignored-on-update",
            site_kind="how_to",
        )
    )

    assert updated["operation"] == "updated"
    assert updated["updated_existing"] is True
    assert updated["site_id"] == first["site_id"]
    assert updated["site_url"] == first["site_url"]
    assert updated["index_path"] == first["index_path"]
    html_text = Path(updated["index_path"]).read_text(encoding="utf-8")
    record = _record(tmp_path, first["site_id"])
    assert "Updated version." in html_text
    assert "First version only." not in html_text
    assert "https://example.test/reference" in html_text
    assert record["update_count"] == 1
    # The record is rewritten with the new file's digest.
    assert record["files"]["index.html"] == hashlib.sha256(html_text.encode("utf-8")).hexdigest()


def test_publish_single_page_site_rejects_unsafe_update_site_id(tmp_path: Path, monkeypatch) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    set_agent_ref(_FakeAgent(tmp_path))
    monkeypatch.setenv("INTERNAL_WEBSITE_SITES_DIR", "artifacts/sites")

    result = publish_single_page_site(
        json.dumps(
            {
                "title": "Bad Update",
                "update_site_id": "../outside",
                "steps": ["Do not write this."],
            }
        )
    )
    payload = _payload(result)

    assert result["status"] == "error"
    assert payload["failure_reason"] == "invalid_site_id"


def test_publish_single_page_site_rejects_invalid_json(tmp_path: Path, monkeypatch) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    set_agent_ref(_FakeAgent(tmp_path))
    monkeypatch.setenv("INTERNAL_WEBSITE_SITES_DIR", "artifacts/sites")

    result = publish_single_page_site("{not json")
    payload = _payload(result)

    assert result["status"] == "error"
    assert payload["status"] == "failed"
    assert payload["failure_reason"] == "invalid_json"


class _InstanceAgent(_FakeAgent):
    """An agent inside a real instance layout (root, state/, workspaces/)."""

    def __init__(self, layout: InstanceLayout) -> None:
        super().__init__(layout.workspaces_dir)
        self.instance_root = str(layout.root)


def _publish_as(monkeypatch: pytest.MonkeyPatch, person: str | None, spec: dict) -> dict:
    from custom_tools.single_page_site_tools import publish_single_page_site

    # The person comes from the task's signed context in production.
    monkeypatch.setattr(workspace_scope, "task_person_id", lambda _agent: person)
    return _payload(publish_single_page_site(json.dumps(spec), site_slug="guide", site_kind="how_to"))


def test_published_page_lives_in_the_persons_folder_with_a_trusted_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    set_agent_ref(_InstanceAgent(layout))
    monkeypatch.setenv("INTERNAL_WEBSITE_BASE_URL", "http://127.0.0.1:18500")

    payload = _publish_as(monkeypatch, "alice", {"title": "Alice Guide", "steps": ["One."]})

    site_dir = layout.person_sites_dir("alice") / payload["site_id"]
    assert Path(payload["index_path"]) == site_dir / "index.html"
    assert (site_dir / "index.html").is_file()
    assert not (layout.published_sites_dir / payload["site_id"]).exists()
    record = json.loads((layout.published_sites_dir / f"{payload['site_id']}.json").read_text(encoding="utf-8"))
    assert record["owner_person_id"] == "alice"
    assert record["folder"] == f"users/alice/sites/{payload['site_id']}"
    assert record["title"] == "Alice Guide"


def test_a_page_can_be_updated_only_for_the_person_it_was_made_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    set_agent_ref(_InstanceAgent(layout))

    first = _publish_as(monkeypatch, "alice", {"title": "Alice Guide", "steps": ["Alice only."]})
    refused = _publish_as(
        monkeypatch, "bob", {"title": "Takeover", "update_site_id": first["site_id"], "steps": ["Bob was here."]}
    )
    page = (layout.person_sites_dir("alice") / first["site_id"] / "index.html").read_text(encoding="utf-8")

    assert refused["failure_reason"] == "site_not_found"
    assert "Alice only." in page
    assert "Bob was here." not in page
    assert not (layout.person_sites_dir("bob") / first["site_id"]).exists()


def test_a_link_planted_in_the_page_folder_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    set_agent_ref(_InstanceAgent(layout))
    first = _publish_as(monkeypatch, "alice", {"title": "Guide", "steps": ["One."]})
    index = layout.person_sites_dir("alice") / first["site_id"] / "index.html"
    layout.soul_path.write_text("team soul", encoding="utf-8")
    index.unlink()
    index.symlink_to(layout.soul_path)

    refused = _publish_as(monkeypatch, "alice", {"title": "Guide", "update_site_id": first["site_id"], "steps": ["Two."]})

    assert refused["failure_reason"] == "site_folder_unsafe"
    assert layout.soul_path.read_text(encoding="utf-8") == "team soul"


def test_agent_supplied_values_cannot_end_the_page_script(tmp_path: Path, monkeypatch) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    set_agent_ref(_FakeAgent(tmp_path))
    spec = {"title": "Safe", "session_key": "</script><script>alert(document.cookie)</script>"}

    payload = _payload(publish_single_page_site(json.dumps(spec)))
    html_text = Path(payload["index_path"]).read_text(encoding="utf-8")

    assert "<script>alert(document.cookie)</script>" not in html_text
    assert "\\u003c/script\\u003e" in html_text
    assert '"signInUrl": "/webchat"' in html_text
