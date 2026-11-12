from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_tools import file_access, workspace_scope
from custom_tools.path_policy import (
    PERSON_WORKSPACE_DENIED,
    PROTECTED_RUNTIME_CONTROL_DENIED,
    WORKSPACE_PATH_DENIED,
)
from custom_tools.work_report import WORK_REPORT_CSS_PATH
from scripts import sync_work_report_renderer
from tests.task_work_support import set_agent_ref
from tests.test_file_access import BINARY as CEDAR_BINARY
from tests.test_file_access import _swap_after_check
from tests.test_workspace_scope_policy import TEAM, as_person, task_agent
from tests.test_workspace_scope_policy import layout as _people_layout
from runtime_settings.instance_layout import InstanceLayout

# A trimmed stats.json (schema 1) from the coder-critic-team sample run.
WORK_REPORT_STATS = Path(__file__).parent / "fixtures" / "work_report" / "stats.json"


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
    assert site_kind_schema["enum"] == ["article", "comparison", "how_to", "app", "report"]
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


def _work_report_stats() -> dict:
    return json.loads(WORK_REPORT_STATS.read_text(encoding="utf-8"))


def _publish_report_as(monkeypatch: pytest.MonkeyPatch, person: str | None, spec: dict) -> dict:
    from custom_tools.single_page_site_tools import publish_single_page_site

    monkeypatch.setattr(workspace_scope, "task_person_id", lambda _agent: person)
    result = publish_single_page_site(json.dumps(spec), site_kind="report")
    payload = _payload(result)
    # Every refusal is the tool's usual error result.
    assert result["status"] == ("success" if payload["status"] == "success" else "error")
    return payload


def _article_instead_of_stats(stats: dict) -> None:
    stats.clear()
    stats["title"] = "An article, not stats"


def _instance_layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    set_agent_ref(_InstanceAgent(layout))
    return layout


def test_a_work_report_publishes_with_its_own_design_and_a_trusted_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _instance_layout(tmp_path)
    monkeypatch.setenv("INTERNAL_WEBSITE_BASE_URL", "http://127.0.0.1:18500")
    stats = _work_report_stats()

    payload = _publish_report_as(monkeypatch, "alice", stats)

    assert payload["status"] == "success"
    assert payload["site_kind"] == "report"
    assert payload["chat_enabled"] is False
    assert payload["site_url"] == f"http://127.0.0.1:18500/docs/sites/{payload['site_id']}/"
    assert "retry-failed-webhook-deliveries" in payload["site_id"]
    index_path = layout.person_sites_dir("alice") / payload["site_id"] / "index.html"
    assert Path(payload["index_path"]) == index_path
    page = index_path.read_text(encoding="utf-8")
    title = stats["run"]["title"]
    assert f"<title>{title} · work report</title>" in page
    assert f'<h1 class="title">{title}</h1>' in page
    # The report's design, with its stylesheet passed in, not the runtime theme or chat.
    assert '<html lang="en" class="cct-page">' in page
    assert f"<style>{WORK_REPORT_CSS_PATH.read_text(encoding='utf-8')}</style>" in page
    assert "<script" not in page.lower()
    assert "/docs/assets/" not in page
    assert "site-chat" not in page
    assert "Decisions to check" in page
    record = _record(layout.root, payload["site_id"])
    assert record["site_kind"] == "report"
    assert record["title"] == title
    assert record["owner_person_id"] == "alice"
    assert record["files"] == {"index.html": hashlib.sha256(index_path.read_bytes()).hexdigest()}


def test_a_work_report_is_updated_in_place_through_top_level_update_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _instance_layout(tmp_path)
    first = _publish_report_as(monkeypatch, "alice", _work_report_stats())
    stats = _work_report_stats()
    stats["narrative"]["summary"] = "The fix round closed the last blocking finding."
    stats["update_site_id"] = first["site_id"]

    updated = _publish_report_as(monkeypatch, "alice", stats)
    refused = _publish_report_as(monkeypatch, "bob", stats)

    assert updated["operation"] == "updated"
    assert updated["site_id"] == first["site_id"]
    page = Path(updated["index_path"]).read_text(encoding="utf-8")
    assert "The fix round closed the last blocking finding." in page
    record = _record(layout.root, first["site_id"])
    assert record["update_count"] == 1
    assert record["files"]["index.html"] == hashlib.sha256(page.encode("utf-8")).hexdigest()
    assert refused["failure_reason"] == "site_not_found"


@pytest.mark.parametrize(
    ("change", "named"),
    [
        (lambda stats: stats["run"].pop("title"), "run.title is missing"),
        (lambda stats: stats.update(schema=3), "schema 1 or 2"),
        (lambda stats: stats["run"].update(status="merged"), "run.status is not one of"),
        (lambda stats: stats["item_list"][0].update(lens="vibes"), "item_list[0].lens is not one of"),
        (lambda stats: stats["run"].update(started_at="yesterday"), "run.started_at"),
        (_article_instead_of_stats, "schema 1"),
    ],
)
def test_malformed_work_report_stats_are_refused_and_nothing_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change, named: str
) -> None:
    layout = _instance_layout(tmp_path)
    stats = _work_report_stats()
    change(stats)

    payload = _publish_report_as(monkeypatch, "alice", stats)

    assert payload["status"] == "failed"
    assert payload["failure_stage"] == "input_validation"
    assert payload["failure_reason"] == "invalid_report_stats"
    assert named in payload["message"]
    assert payload["retryable"] is False
    assert list(layout.published_sites_dir.glob("*.json")) == []
    assert not layout.person_sites_dir("alice").exists() or not any(layout.person_sites_dir("alice").iterdir())


def test_hostile_strings_in_a_work_report_are_escaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _instance_layout(tmp_path)
    hostile = '<script>alert(1)</script><img src=x onerror="alert(2)">'
    stats = _work_report_stats()
    stats["run"]["title"] = "Retry </title></style>" + hostile
    stats["run"]["pr"]["url"] = "javascript:alert(3)"
    stats["narrative"]["summary"] = hostile
    stats["acceptance"][0]["evidence"] = hostile
    stats["item_list"][0]["title"] = hostile
    stats["diff"]["files_list"][0]["path"] = hostile
    issue = next(f for f in stats["review"]["findings"] if f["status"] == "issue_filed")
    issue["issue_url"] = "javascript:alert(4)"

    payload = _publish_report_as(monkeypatch, "alice", stats)
    page = Path(payload["index_path"]).read_text(encoding="utf-8")

    assert payload["status"] == "success"
    assert "<script" not in page.lower()
    assert "<img" not in page
    assert "javascript:" not in page
    assert page.count("</style>") == 1
    assert page.count("</title>") == 1
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert 'onerror=&quot;alert(2)&quot;' in page


def test_work_report_sync_check_refuses_a_stale_copy_or_a_missing_skill(tmp_path: Path) -> None:
    canonical = tmp_path / "coder-critic-team"
    (canonical / "scripts").mkdir(parents=True)
    (canonical / "assets").mkdir()
    (canonical / "SKILL.md").write_text("---\nname: coder-critic-team\n---\n", encoding="utf-8")
    copies = sync_work_report_renderer.DESTINATION
    (canonical / "scripts" / "render_report.py").write_bytes((copies / "render_report.py").read_bytes())
    (canonical / "assets" / "report.css").write_bytes((copies / "report.css").read_bytes() + b"\n")
    check = ["--check", "--canonical-root", str(canonical)]

    assert sync_work_report_renderer.main(check) == 1
    assert sync_work_report_renderer.stale_copies(canonical) == ["report.css"]
    (canonical / "assets" / "report.css").write_bytes((copies / "report.css").read_bytes())
    assert sync_work_report_renderer.main(check) == 0
    (canonical / "SKILL.md").unlink()
    assert sync_work_report_renderer.main(check) == 1


@pytest.mark.skipif(
    bool(sync_work_report_renderer.canonical_skill_missing(sync_work_report_renderer.DEFAULT_CANONICAL_ROOT)),
    reason="The canonical coder-critic-team skill is not in a sibling ua-skills checkout (CI checks out only this repository).",
)
def test_work_report_renderer_copies_match_the_canonical_skill() -> None:
    # Fails when ua-skills changed the renderer or stylesheet without a sync:
    # python -m scripts.sync_work_report_renderer
    assert sync_work_report_renderer.main(["--check"]) == 0


# Work reports published from their statistics file (site_spec_path). These
# tests use the folder-rule fixture from the workspace scope tests: alice and bob
# have folders, authorization is off unless a test turns it on, and the task's
# person comes from a signed context, as in production.
people_layout = _people_layout


def _write_stats(path: Path, stats: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats if stats is not None else _work_report_stats()), encoding="utf-8")
    return path


def _publish_from(path: str | Path, **arguments) -> dict:
    from custom_tools.single_page_site_tools import publish_single_page_site

    arguments.setdefault("site_kind", "report")
    result = publish_single_page_site(site_spec_path=str(path), **arguments)
    payload = _payload(result)
    assert result["status"] == ("success" if payload["status"] == "success" else "error")
    return payload


def _nothing_published(layout: InstanceLayout) -> bool:
    return not list(layout.published_sites_dir.glob("*.json")) and not list(layout.workspaces_dir.glob("**/sites/*"))


def _other_persons_file(layout: InstanceLayout, _monkeypatch) -> Path:
    return _write_stats(layout.person_dir("bob") / "coder-critic" / "run" / "stats.json")


def _file_outside_the_instance(layout: InstanceLayout, _monkeypatch) -> Path:
    return _write_stats(layout.root.parent / "elsewhere" / "stats.json")


def _control_file(layout: InstanceLayout, _monkeypatch) -> Path:
    return _write_stats(layout.control_dir / "stats.json")


def _link_to_another_persons_file(layout: InstanceLayout, _monkeypatch) -> Path:
    link = layout.person_dir("alice") / "coder-critic" / "run" / "stats.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(_other_persons_file(layout, _monkeypatch))
    return link


def _link_swapped_in_after_the_check(layout: InstanceLayout, monkeypatch) -> Path:
    runs = layout.person_dir("alice") / "coder-critic"
    _write_stats(runs / "stats.json")
    _write_stats(layout.person_dir("bob") / "coder-critic" / "stats.json")
    _swap_after_check(monkeypatch, runs, layout.person_dir("bob") / "coder-critic")
    return runs / "stats.json"


def _missing_file(layout: InstanceLayout, _monkeypatch) -> Path:
    return layout.person_dir("alice") / "coder-critic" / "run" / "stats.json"


def _folder(layout: InstanceLayout, _monkeypatch) -> Path:
    return _write_stats(layout.person_dir("alice") / "coder-critic" / "run" / "stats.json").parent


def _file_over_the_limit(layout: InstanceLayout, _monkeypatch) -> Path:
    from custom_tools.single_page_site_tools import REPORT_STATS_MAX_BYTES

    path = layout.person_dir("alice") / "coder-critic" / "run" / "stats.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b" " * (REPORT_STATS_MAX_BYTES + 1))
    return path


def _path_with_a_nul_character(_layout: InstanceLayout, _monkeypatch) -> str:
    return "coder-critic/stats\x00.json"


@pytest.mark.parametrize(
    ("place", "reason", "said"),
    [
        (_other_persons_file, PERSON_WORKSPACE_DENIED, "cannot be read"),
        (_file_outside_the_instance, WORKSPACE_PATH_DENIED, "outside this task's folders"),
        (_control_file, PROTECTED_RUNTIME_CONTROL_DENIED, "cannot be read"),
        (_link_to_another_persons_file, PERSON_WORKSPACE_DENIED, "cannot be read"),
        (_link_swapped_in_after_the_check, file_access.LINK_NOT_FOLLOWED, "link on this path is not followed"),
        (_missing_file, "file_not_found", "No statistics file is at that path"),
        (_folder, file_access.NOT_A_REGULAR_FILE, "not its folder"),
        (_file_over_the_limit, file_access.FILE_TOO_LARGE, "Statistics files can be up to 2 MB"),
        (_path_with_a_nul_character, "unreadable_spec_file", "cannot be read as a file"),
    ],
)
def test_a_statistics_file_the_task_may_not_read_is_refused_and_nothing_is_written(
    people_layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, place, reason: str, said: str
) -> None:
    as_person(people_layout, "alice")
    path = place(people_layout, monkeypatch)

    payload = _publish_from(path)

    assert payload["status"] == "failed"
    assert (payload["failure_stage"], payload["failure_reason"]) == ("read_spec_file", reason)
    assert said in payload["message"]
    assert payload["retryable"] is False
    assert _nothing_published(people_layout)


def test_a_link_to_the_persons_own_statistics_publishes_like_the_file_tools_read_it(
    people_layout: InstanceLayout,
) -> None:
    as_person(people_layout, "alice")
    stats = _write_stats(people_layout.person_dir("alice") / "coder-critic" / "run" / "stats.json")
    (people_layout.person_dir("alice") / "latest-stats.json").symlink_to(stats)

    payload = _publish_from("latest-stats.json")

    assert payload["status"] == "success"
    assert Path(payload["index_path"]).parent.parent == people_layout.person_sites_dir("alice")


@pytest.mark.skipif(not CEDAR_BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_a_statistics_file_in_a_folder_the_access_service_blocks_is_refused(people_layout: InstanceLayout) -> None:
    from datetime import datetime, timezone

    from access_control.entities import PersonPrincipal
    from custom_agents import folder_access
    from custom_agents.access_runtime import runtime_access
    from custom_messages.contracts.access_v2 import (
        AccessLevel,
        GrantKind,
        GrantV1,
        SubjectKind,
        SubjectRefV1,
        TargetKind,
        TargetRefV1,
    )

    agent = task_agent(people_layout, "alice")
    agent.authorization_config = {"enabled": True, "cedar_binary_path": str(CEDAR_BINARY)}
    agent.named_agent_type = "WebResearchAgent"
    set_agent_ref(agent)
    folder_access.forget_kept_answers()
    blocked = _write_stats(people_layout.team_dir / "finance" / "stats.json")
    own = _write_stats(people_layout.person_dir("alice") / "coder-critic" / "run" / "stats.json")
    runtime_access(agent).service.grant_by_person(
        PersonPrincipal("owner-1", TEAM, ("owner",)),
        GrantV1(
            grant_id="new",
            team_id=TEAM,
            kind=GrantKind.BLOCK,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance"),
            level=AccessLevel.FULL,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id="owner-1"),
            reason="Keep finance away from research.",
            created_at_utc=datetime.now(timezone.utc),
        ),
    )

    refused = _publish_from(blocked)
    assert (refused["failure_reason"], refused["failure_stage"]) == (file_access.ACCESS_DENIED, "read_spec_file")
    # The access service decided: its block's own reason, not a check that could not run.
    assert "only a person can remove the block" in refused["message"]
    assert _nothing_published(people_layout)
    assert _publish_from(own)["status"] == "success"


def test_statistics_of_at_least_170_kb_publish_from_a_relative_path(people_layout: InstanceLayout) -> None:
    as_person(people_layout, "alice")
    stats = _work_report_stats()
    template = stats["acceptance"][0]
    while len(json.dumps(stats)) < 170 * 1024:
        number = len(stats["acceptance"]) + 1
        stats["acceptance"].append({**template, "id": f"A{number}", "text": f"Criterion {number}: " + "x" * 200})
    path = _write_stats(people_layout.person_dir("alice") / "coder-critic" / "big-run" / "stats.json", stats)

    payload = _publish_from("coder-critic/big-run/stats.json")

    assert path.stat().st_size >= 170 * 1024
    assert payload["status"] == "success"
    page = Path(payload["index_path"]).read_text(encoding="utf-8")
    assert f"Criterion {len(stats['acceptance'])}: " in page


def test_the_file_form_is_in_the_tool_schema_and_description() -> None:
    from custom_tools.single_page_site_tools import REPORT_STATS_MAX_BYTES, publish_single_page_site

    schema = publish_single_page_site.tool_spec["inputSchema"]["json"]
    description = " ".join(publish_single_page_site.tool_spec["description"].split())  # one line

    assert {"site_spec_path", "update_site_id"} <= set(schema["properties"])
    assert not {"site_spec_json", "site_spec_path", "update_site_id"} & set(schema.get("required", []))
    assert REPORT_STATS_MAX_BYTES >= 1_000_000
    assert f"may be up to {REPORT_STATS_MAX_BYTES // (1024 * 1024)} MB" in description
    assert 'pass site_kind="report" and the file\'s path in site_spec_path' in description
    assert "works only for small statistics" in description
    # A report is published again with the parameter, never by editing its statistics.
    assert "update_site_id" in description and "do not edit the statistics file" in description
    assert "top level of the statistics" not in description


@pytest.mark.parametrize(
    ("arguments", "reason", "said"),
    [
        ({"site_spec_json": json.dumps({"schema": 1}), "site_spec_path": "stats.json"}, "site_spec_conflict", "not both"),
        ({}, "site_spec_missing", "site_spec_path"),
        ({"site_spec_json": "  "}, "site_spec_missing", "JSON object in site_spec_json"),
    ],
)
def test_one_spec_source_is_required(
    people_layout: InstanceLayout, arguments: dict, reason: str, said: str
) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    as_person(people_layout, "alice")

    payload = _payload(publish_single_page_site(site_kind="report", **arguments))

    assert (payload["failure_stage"], payload["failure_reason"]) == ("input_validation", reason)
    assert said in payload["message"]
    assert _nothing_published(people_layout)


@pytest.mark.parametrize("site_kind", ["article", "comparison", "how_to", "app"])
def test_site_spec_path_is_refused_for_other_kinds_before_anything_is_read(
    people_layout: InstanceLayout, site_kind: str
) -> None:
    as_person(people_layout, "alice")

    # The file does not exist: a read would answer file_not_found.
    payload = _publish_from("coder-critic/run/stats.json", site_kind=site_kind)

    assert payload["failure_reason"] == "site_spec_path_report_only"
    assert "Pass every other kind of page as a JSON object in site_spec_json" in payload["message"]
    assert _nothing_published(people_layout)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"{not json", "invalid_json"),
        (b"[1, 2]", "json_not_object"),
        ("café".encode("latin-1"), "invalid_json"),
        (json.dumps({"title": "An article"}).encode(), "invalid_report_stats"),
        (json.dumps({"site_kind": "article", "title": "An article"}).encode(), "site_spec_path_report_only"),
        (b"[" * 100_000 + b"]" * 100_000, "invalid_json"),
    ],
)
def test_a_file_that_is_not_statistics_is_refused_and_nothing_is_written(
    people_layout: InstanceLayout, content: bytes, reason: str
) -> None:
    as_person(people_layout, "alice")
    path = people_layout.person_dir("alice") / "coder-critic" / "run" / "stats.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    payload = _publish_from(path)

    assert payload["failure_reason"] == reason
    assert payload["retryable"] is False
    assert _nothing_published(people_layout)


def test_a_report_from_its_file_is_the_same_page_as_from_its_text(people_layout: InstanceLayout) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    as_person(people_layout, "alice")
    text = WORK_REPORT_STATS.read_text(encoding="utf-8")
    path = people_layout.person_dir("alice") / "coder-critic" / "run" / "stats.json"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")

    from_text = _payload(publish_single_page_site(text, site_kind="report"))
    from_file = _publish_from(path)

    assert from_text["status"] == from_file["status"] == "success"
    assert Path(from_file["index_path"]).read_bytes() == Path(from_text["index_path"]).read_bytes()
    text_record = _record(people_layout.root, from_text["site_id"])
    file_record = _record(people_layout.root, from_file["site_id"])
    for key in ("title", "site_kind", "owner_person_id", "files"):
        assert file_record[key] == text_record[key]


def test_a_report_from_its_file_is_published_again_in_place_only_for_its_person(
    people_layout: InstanceLayout,
) -> None:
    as_person(people_layout, "alice")
    path = _write_stats(people_layout.person_dir("alice") / "coder-critic" / "run" / "stats.json")
    first = _publish_from("coder-critic/run/stats.json")
    stats = _work_report_stats()
    stats["narrative"]["summary"] = "The fix round closed the last blocking finding."
    _write_stats(path, stats)  # cct.py stats ran again after the fix round
    written = path.read_bytes()

    # The page's URL names it as well as its id does.
    updated = _publish_from("coder-critic/run/stats.json", update_site_id=first["site_url"])

    assert updated["operation"] == "updated"
    for key in ("site_id", "site_url", "index_path"):
        assert updated[key] == first[key]
    page = Path(updated["index_path"]).read_bytes()
    assert "The fix round closed the last blocking finding." in page.decode("utf-8")
    record = _record(people_layout.root, first["site_id"])
    assert record["update_count"] == 1
    assert record["files"]["index.html"] == hashlib.sha256(page).hexdigest()
    assert path.read_bytes() == written  # the statistics file is left as cct.py wrote it

    as_person(people_layout, "bob")
    stats["narrative"]["summary"] = "Bob was here."
    _write_stats(people_layout.person_dir("bob") / "coder-critic" / "run" / "stats.json", stats)
    refused = _publish_from("coder-critic/run/stats.json", update_site_id=first["site_id"])

    assert refused["failure_reason"] == "site_not_found"
    assert Path(first["index_path"]).read_bytes() == page
    assert not people_layout.person_sites_dir("bob").exists()


def test_update_site_id_names_the_page_for_every_kind_and_must_agree_with_the_spec(
    people_layout: InstanceLayout,
) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    as_person(people_layout, "alice")
    first = _payload(publish_single_page_site(json.dumps({"title": "Guide", "key_points": ["One."]})))
    other = _payload(publish_single_page_site(json.dumps({"title": "Other", "key_points": ["Other."]})))

    updated = _payload(
        publish_single_page_site(
            json.dumps({"title": "Guide", "key_points": ["Two."]}), update_site_id=first["site_id"]
        )
    )
    refused = _payload(
        publish_single_page_site(
            json.dumps({"title": "Guide", "update_site_id": other["site_id"], "key_points": ["Three."]}),
            update_site_id=first["site_id"],
        )
    )

    assert (updated["operation"], updated["site_id"]) == ("updated", first["site_id"])
    assert (refused["failure_stage"], refused["failure_reason"]) == ("input_validation", "conflicting_update_site_id")
    assert "Pass the page to publish again only in update_site_id" in refused["message"]
    first_page = Path(first["index_path"]).read_text(encoding="utf-8")
    assert "Two." in first_page and "Three." not in first_page
    assert "Three." not in Path(other["index_path"]).read_text(encoding="utf-8")


def test_an_unreadable_path_is_refused_in_plain_words(people_layout: InstanceLayout) -> None:
    as_person(people_layout, "alice")

    payload = _publish_from("coder-critic/stats\x00.json")

    assert payload["message"] == (
        "The path cannot be read as a file. Pass the path of the stats.json that cct.py stats wrote."
    )


def _round_named_twice(stats: dict) -> None:
    stats["rounds"][0]["critic"]["items"].append("C01")


def _critique_in_many_rounds(stats: dict) -> None:
    # C17 already belongs to round cp3; each extra round names it once.
    for number in range(50):
        stats["rounds"].append(
            {**stats["rounds"][1], "key": f"cp{100 + number}", "critic": {"summary": "Again.", "items": ["C17"]}}
        )


def _round_key_twice(stats: dict) -> None:
    # Each copy would draw the plan column of the lens grid, with all its critique ids, again.
    for _ in range(300):
        stats["rounds"].append({**stats["rounds"][0], "critic": {"summary": "Again.", "items": []}})


def _intervention_in_two_rounds(stats: dict) -> None:
    stats["rounds"][0]["interventions"] = [17]  # 17 already belongs to round cp3


def _review_stage_twice(stats: dict) -> None:
    from custom_tools.work_report.render_report import upgrade

    upgraded = upgrade(stats)  # schema 2 lists its reviews
    stats.clear()
    stats.update(upgraded)
    stats["reviews"].append(dict(stats["reviews"][0]))


def _long_label_named_by_many_critiques(stats: dict) -> None:
    stats["rounds"][0]["label"] = "Plan " * 40_000
    template = stats["item_list"][1]  # rejected, so the decisions list names its round too
    for number in range(100):
        stats["item_list"].append({**template, "id": f"C{200 + number}"})  # in no round's critic.items


def _long_label_addressed_many_times(stats: dict) -> None:
    stats["rounds"][0]["label"] = "Plan " * 40_000
    stats["review"]["findings"][0]["addressed_in"] = ["plan"] * 100


def _short_label_addressed_very_many_times(stats: dict) -> None:
    # Each entry costs the file a few bytes and makes the page write the label again.
    stats["rounds"][0]["label"] = "Plan " * 20
    stats["review"]["findings"][0]["addressed_in"] = ["plan"] * 1_000


@pytest.mark.parametrize(
    ("repeat", "said"),
    [
        (_long_label_named_by_many_critiques, "characters of round labels, more than 65536"),
        (_long_label_addressed_many_times, "characters of round labels, more than 65536"),
        (_short_label_addressed_very_many_times, "characters of round labels, more than 65536"),
        (_round_key_twice, "Two rounds have the key plan"),
        (_round_named_twice, "The rounds name the critique C01 more than once"),
        (_critique_in_many_rounds, "The rounds name the critique C17 more than once"),
        (_intervention_in_two_rounds, "The rounds name the intervention 17 more than once"),
        (_review_stage_twice, "Two reviews have the stage after_pr"),
    ],
)
def test_statistics_that_mention_something_twice_are_refused_before_rendering(
    people_layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, repeat, said: str
) -> None:
    from custom_tools import single_page_site_tools

    def not_rendered(_stats: dict) -> str:
        raise AssertionError("statistics with repeated mentions were rendered")

    monkeypatch.setattr(single_page_site_tools, "render_work_report_page", not_rendered)
    as_person(people_layout, "alice")
    stats = _work_report_stats()
    repeat(stats)
    _write_stats(people_layout.person_dir("alice") / "coder-critic" / "run" / "stats.json", stats)

    payload = _publish_from("coder-critic/run/stats.json")

    assert (payload["failure_stage"], payload["failure_reason"]) == ("input_validation", "invalid_report_stats")
    assert said in payload["message"]
    assert _nothing_published(people_layout)


def test_a_page_larger_than_the_website_serves_is_refused_and_the_published_page_stays(
    people_layout: InstanceLayout,
) -> None:
    from user_docs.published_sites import MAX_SITE_FILE_BYTES

    as_person(people_layout, "alice")
    path = _write_stats(people_layout.person_dir("alice") / "coder-critic" / "run" / "stats.json")
    first = _publish_from("coder-critic/run/stats.json")
    page = Path(first["index_path"]).read_bytes()
    record = _record(people_layout.root, first["site_id"])
    # Each critique is named once, but '&' grows fivefold when escaped and a
    # rejected critique is drawn in its round and among the decisions to check.
    stats = _work_report_stats()
    template = stats["item_list"][1]  # rejected
    while len(json.dumps(stats)) < 1_900_000:
        item_id = f"C{len(stats['item_list']) + 100}"
        stats["item_list"].append({**template, "id": item_id, "detail": "&" * 10_000})
        stats["rounds"][0]["critic"]["items"].append(item_id)
    _write_stats(path, stats)
    assert path.stat().st_size < 2 * 1024 * 1024

    updated = _publish_from("coder-critic/run/stats.json", update_site_id=first["site_id"])
    new = _publish_from("coder-critic/run/stats.json")

    for refused in (updated, new):
        assert (refused["failure_stage"], refused["failure_reason"]) == ("render", "page_too_large")
        assert f"more than the {MAX_SITE_FILE_BYTES // (1024 * 1024)} MB the website serves" in refused["message"]
    assert "the published page stays as it was" in updated["message"]
    assert Path(first["index_path"]).read_bytes() == page
    assert _record(people_layout.root, first["site_id"]) == record
    assert [p.name for p in people_layout.published_sites_dir.glob("*.json")] == [f"{first['site_id']}.json"]


@pytest.mark.parametrize("in_team_folder", [False, True])
def test_a_statistics_file_cannot_choose_the_page_it_replaces(
    people_layout: InstanceLayout, in_team_folder: bool
) -> None:
    from custom_tools.single_page_site_tools import publish_single_page_site

    as_person(people_layout, "alice")
    guide = _payload(
        publish_single_page_site(json.dumps({"title": "Alice Guide", "steps": ["Alice only."]}), site_kind="how_to")
    )
    page = Path(guide["index_path"]).read_bytes()
    stats = _work_report_stats()
    if in_team_folder:
        # Anyone in the team can write team/; the file names alice's page by its link.
        stats["update_site_url"] = guide["site_url"]
        path = _write_stats(people_layout.team_dir / "coder-critic" / "run" / "stats.json", stats)
    else:
        stats["update_site_id"] = guide["site_id"]
        path = _write_stats(people_layout.person_dir("alice") / "coder-critic" / "run" / "stats.json", stats)

    payload = _publish_from(path)

    assert (payload["failure_stage"], payload["failure_reason"]) == ("input_validation", "update_key_in_spec_file")
    assert guide["site_id"] in payload["message"]
    assert "update_site_id parameter" in payload["message"]
    assert Path(guide["index_path"]).read_bytes() == page
    assert _record(people_layout.root, guide["site_id"])["site_kind"] == "how_to"
    assert [p.name for p in people_layout.published_sites_dir.glob("*.json")] == [f"{guide['site_id']}.json"]
