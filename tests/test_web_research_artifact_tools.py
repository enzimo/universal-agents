from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_tools.runtime_context import set_agent_ref
from runtime_settings.instance_layout import InstanceLayout


class _FakeTaskManager:
    current_task_id = "task_web_test"

    def get_task_info(self, task_id: str):
        return SimpleNamespace(meta=SimpleNamespace(conversation_id="conversation_test"))


class _FakeAgent:
    """An agent in an instance layout whose current task works for no person.

    Authorization is off (single-person mode), so only the folder rules apply.
    """

    authorization_config = {"enabled": False}

    instance_root: str
    workspace_dir: str
    team_id = "team_test"
    id = "agent_web_test"
    named_agent_type = "WebResearchAgent"
    task_manager = _FakeTaskManager()

    def __init__(self, layout: InstanceLayout) -> None:
        self.instance_root = str(layout.root)
        self.workspace_dir = str(layout.workspaces_dir)


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    yield layout
    set_agent_ref(None)


def _task_research_dir(layout: InstanceLayout) -> Path:
    """Research files of the fake task: the task's own folder, then research/.

    Work for no person keeps them in ``workspaces/agents/research``.
    """

    return layout.person_research_dir(None) / "task_web_test"


def _payload(result):
    return json.loads(result["content"][0]["text"])


def test_web_artifact_inspect_bounds_reads_and_rejects_non_artifact_paths(tmp_path, layout):
    from custom_tools.web_research_artifact_tools import web_artifact_inspect

    set_agent_ref(_FakeAgent(layout))
    artifact = _task_research_dir(layout) / "artifact" / "page.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("\n".join(f"line {index} needle" for index in range(50)), encoding="utf-8")

    stat = _payload(web_artifact_inspect(str(artifact), action="stat"))
    assert stat["file_bytes"] > 0
    assert stat["line_count"] == 50

    grep = _payload(web_artifact_inspect(str(artifact), action="grep", query="needle", max_lines=3, max_chars=100))
    assert grep["status"] == "success"
    assert len(grep["matches"]) <= 3
    assert grep["truncated"] is True

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    rejected = _payload(web_artifact_inspect(str(outside), action="head"))
    assert rejected["failure_reason"] == "path_outside_artifacts"

    # A file in the task's folder but outside its research folder is refused too.
    workspace_file = layout.agents_dir / "notes.txt"
    workspace_file.write_text("not research", encoding="utf-8")
    rejected = _payload(web_artifact_inspect(str(workspace_file), action="head"))
    assert rejected["failure_reason"] == "path_outside_artifacts"


def test_web_artifact_inspect_extracts_links_without_reading_full_html(layout):
    from custom_tools.web_research_artifact_tools import web_artifact_inspect

    set_agent_ref(_FakeAgent(layout))
    artifact = _task_research_dir(layout) / "artifact" / "pages" / "001_travel_main.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        """
        <html><body>
          <a href="/flights/status/atl-sfo/" title="ATL to SFO">ATL to SFO Flight Status</a>
          <a href="/stays/oceanfront/">Oceanfront Stays</a>
        </body></html>
        """,
        encoding="utf-8",
    )
    artifact.with_name("001_travel_main.metadata.json").write_text(
        json.dumps({"url": "https://example.com/travel/"}),
        encoding="utf-8",
    )

    payload = _payload(web_artifact_inspect(str(artifact), action="extract_links", query="flight status", max_items=10))

    assert payload["status"] == "success"
    assert payload["item_count"] == 1
    assert payload["items"][0]["absolute_url"] == "https://example.com/flights/status/atl-sfo/"
    assert payload["items"][0]["text"] == "ATL to SFO Flight Status"


def test_web_artifact_inspect_scores_generic_content_candidates(layout):
    from custom_tools.web_research_artifact_tools import web_artifact_inspect

    set_agent_ref(_FakeAgent(layout))
    artifact = _task_research_dir(layout) / "artifact" / "sitemap.xml"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "<urlset><url><loc>https://example.com/stays/oceanfront-vrbo-reviews/</loc></url>"
        "<url><loc>https://example.com/stays/mountain-cabin-reviews/</loc></url>"
        "<url><loc>https://example.com/flights/status/</loc></url></urlset>",
        encoding="utf-8",
    )

    payload = _payload(
        web_artifact_inspect(
            str(artifact),
            action="extract_content_candidates",
            query="vrbo reviews",
            content_keywords=["oceanfront"],
            max_items=10,
        )
    )

    assert payload["status"] == "success"
    returned_urls = [item["url"] for item in payload["items"]]
    assert "https://example.com/stays/oceanfront-vrbo-reviews/" in returned_urls
    assert "https://example.com/stays/mountain-cabin-reviews/" in returned_urls
    assert "https://example.com/flights/status/" not in returned_urls


def test_web_artifact_inspect_previews_and_extracts_markdown(layout):
    from custom_tools.web_research_artifact_tools import web_artifact_inspect

    set_agent_ref(_FakeAgent(layout))
    artifact = _task_research_dir(layout) / "artifact" / "pages" / "001_example_listing.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        """
        <html><head>
          <title>Oceanfront Stay</title>
          <meta name="description" content="Guest reviews and availability for an oceanfront stay.">
        </head><body>
          <h1>Oceanfront Stay Reviews</h1>
          <p>Guests mention clean rooms, quiet nights, and reliable Wi-Fi.</p>
          <ul><li>4.8 average rating</li><li>Walkable beach access</li></ul>
          <a href="/availability">Check availability</a>
        </body></html>
        """,
        encoding="utf-8",
    )
    artifact.with_name("001_example_listing.metadata.json").write_text(
        json.dumps({"url": "https://example.com/stays/oceanfront/"}),
        encoding="utf-8",
    )

    preview = _payload(web_artifact_inspect(str(artifact), action="preview", max_items=5, max_chars=1000))
    assert preview["title"] == "Oceanfront Stay"
    assert preview["description"] == "Guest reviews and availability for an oceanfront stay."
    assert preview["headings"][0] == {"level": 1, "text": "Oceanfront Stay Reviews"}
    assert preview["links_preview"][0]["absolute_url"] == "https://example.com/availability"

    markdown = _payload(web_artifact_inspect(str(artifact), action="extract_markdown", max_chars=1000))
    assert "# Oceanfront Stay Reviews" in markdown["text"]
    assert "- 4.8 average rating" in markdown["text"]
    assert "Check availability (https://example.com/availability)" in markdown["text"]


def _plant_research_link(layout: InstanceLayout) -> Path:
    """Replace the task's research folder with a link to the control folder, as sandboxed code could."""

    research = layout.person_research_dir(None)
    if research.exists():
        shutil.rmtree(research)
    research.parent.mkdir(parents=True, exist_ok=True)
    research.symlink_to(layout.control_dir, target_is_directory=True)
    return research


def test_a_planted_research_link_does_not_expose_control_files(layout):
    from custom_tools.web_research_artifact_tools import web_artifact_inspect

    set_agent_ref(_FakeAgent(layout))
    secret = layout.control_dir / "secret.txt"
    secret.write_text("control secret", encoding="utf-8")
    _plant_research_link(layout)

    for requested in (str(secret), str(layout.person_research_dir(None) / "secret.txt")):
        payload = _payload(web_artifact_inspect(requested, action="head"))
        assert payload["status"] == "failed"
        assert "control secret" not in json.dumps(payload)


def test_a_planted_research_link_does_not_redirect_research_writes(layout):
    from custom_tools import web_research_artifact_tools as tools
    from custom_tools.path_policy import PathPolicyError

    set_agent_ref(_FakeAgent(layout))
    _plant_research_link(layout)

    with pytest.raises(PathPolicyError):
        tools._create_artifact_dir("search", "query")
    assert list(layout.control_dir.iterdir()) == []


def test_research_writes_refuse_a_link_swapped_in_after_the_folder_was_made(layout):
    from custom_tools import web_research_artifact_tools as tools
    from custom_tools.path_policy import PathPolicyError

    set_agent_ref(_FakeAgent(layout))
    artifact_dir, _artifact_id, _identity = tools._create_artifact_dir("search", "query")
    target = layout.control_dir / "moved"
    target.mkdir()
    shutil.rmtree(artifact_dir)
    artifact_dir.symlink_to(target, target_is_directory=True)

    with pytest.raises(PathPolicyError):
        tools._write_text(artifact_dir / "page.md", "hello")
    assert list(target.iterdir()) == []
