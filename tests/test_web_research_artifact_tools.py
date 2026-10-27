from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.task_work_support import bind_task, set_agent_ref
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

    from custom_tools.research_identity import task_folder_name

    return layout.person_research_dir(None) / task_folder_name("task_web_test")


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


FIXED_STAMP = "20261016T120000Z"


def test_two_result_folders_made_in_the_same_second_for_the_same_query_differ(layout, monkeypatch):
    from custom_tools import web_research_artifact_tools as tools

    set_agent_ref(_FakeAgent(layout))
    monkeypatch.setattr(tools, "_now_stamp", lambda: FIXED_STAMP)

    first_dir, first_id, _identity = tools._create_artifact_dir("search", "query")
    second_dir, second_id, _identity = tools._create_artifact_dir("search", "query")

    assert first_dir != second_dir and first_id != second_id
    assert first_dir.is_dir() and second_dir.is_dir()
    assert first_dir.parent == second_dir.parent == _task_research_dir(layout)
    for artifact_id in (first_id, second_id):
        readable, _, random_part = artifact_id.rpartition("_")
        assert readable == f"{FIXED_STAMP}_search_query"
        assert len(random_part) == 12 and int(random_part, 16) >= 0


def _brave_answers(monkeypatch, tools, *titles: str) -> None:
    """Answer each Brave request with the next title (the broker is faked here;
    tests/test_broker_web_tools.py runs the real one)."""

    from custom_tools.broker_web_requests import BrokerWebResponse

    pending = list(titles)

    def fake_send(method, url, **_kwargs):
        title = pending.pop(0)
        answer = {"web": {"results": [{"title": title, "url": f"https://example.com/{title}"}]}}
        return BrokerWebResponse(http_status=200, content_type="application/json", json=answer, text="", truncated=False)

    monkeypatch.setattr(tools, "send_web_request", fake_send)


def test_two_brave_searches_in_the_same_second_keep_their_own_files(layout, monkeypatch):
    """The same query with another offset in the same second used to share a
    folder, and the second search overwrote the first one's files."""

    from custom_tools import web_research_artifact_tools as tools

    set_agent_ref(_FakeAgent(layout))
    monkeypatch.setattr(tools, "_now_stamp", lambda: FIXED_STAMP)
    _brave_answers(monkeypatch, tools, "first-page", "second-page")

    first = _payload(tools.brave_web_search_artifact("agent frameworks", offset=0))
    second = _payload(tools.brave_web_search_artifact("agent frameworks", offset=1))

    assert first["artifact_dir"] != second["artifact_dir"]
    for payload, title, offset in ((first, "first-page", 0), (second, "second-page", 1)):
        files = payload["files"]
        assert all(Path(path).parent == Path(payload["artifact_dir"]) for path in files.values())
        normalized = json.loads(Path(files["normalized_json"]).read_text(encoding="utf-8"))
        assert [item["title"] for item in normalized] == [title]
        manifest = json.loads(Path(files["manifest"]).read_text(encoding="utf-8"))
        assert manifest["artifact_id"] == payload["artifact_id"]
        assert manifest["request"]["offset"] == offset


def test_task_id_matching_another_tasks_folder_keeps_research_separate(layout, monkeypatch):
    from custom_tools import web_research_artifact_tools as tools
    from custom_tools.research_identity import task_folder_name

    agent = _FakeAgent(layout)
    agent.task_manager = _FakeTaskManager()
    set_agent_ref(agent)
    monkeypatch.setattr(tools, "_now_stamp", lambda: FIXED_STAMP)
    _brave_answers(monkeypatch, tools, "first-task", "second-task")
    task_ids = ("Report-A", task_folder_name("Report-A"))
    assert task_ids[0] != task_ids[1]

    results = []
    for task_id in task_ids:
        agent.task_manager.current_task_id = task_id
        bind_task(task_id)  # the next call works for this task (tests.task_work_support)
        results.append(_payload(tools.brave_web_search_artifact("agent frameworks")))

    assert Path(results[0]["artifact_dir"]).parent != Path(results[1]["artifact_dir"]).parent
    for result, task_id, title in zip(results, task_ids, ("first-task", "second-task"), strict=True):
        manifest = json.loads(Path(result["files"]["manifest"]).read_text(encoding="utf-8"))
        normalized = json.loads(Path(result["files"]["normalized_json"]).read_text(encoding="utf-8"))
        assert manifest["task_id"] == task_id
        assert [item["title"] for item in normalized] == [title]


def test_ordinary_task_ids_also_get_stable_digest_folder_names():
    from custom_tools.research_identity import NO_ACTIVE_TASK, task_folder_name

    for task_id in ("task_web_test", "task_id_0123456789ab", "task_id_0123456789ab_sub_1", NO_ACTIVE_TASK, "t" * 48):
        name = task_folder_name(task_id)
        readable, _, digest = name.rpartition("-")
        assert name != task_id
        assert readable == task_id[:35].rstrip("-._")
        assert len(name) <= 48
        assert len(digest) == 12 and int(digest, 16) >= 0
        assert task_folder_name(task_id) == name


def test_long_task_ids_that_share_a_beginning_get_different_folders():
    """Subtask ids grow as <parent>_sub_<n>; a few levels down they pass 48
    characters, and cutting them to 48 used to give siblings one folder."""

    from custom_tools.research_identity import safe_slug, task_folder_name

    parent = "task_id_0123456789ab_sub_1_sub_1_sub_1_sub_1"
    first, second = f"{parent}_sub_1", f"{parent}_sub_2"
    assert safe_slug(first) == safe_slug(second), "the old name was the same for both"

    names = {task_folder_name(first), task_folder_name(second)}

    assert len(names) == 2
    for name in names:
        assert len(name) <= 48
        assert safe_slug(name) == name, "the name is itself a safe folder name"
        assert name.startswith("task_id_0123456789ab_sub_1_sub_1")
    assert task_folder_name(first) == task_folder_name(first), "a task always finds its own folder again"


def test_task_ids_that_differ_only_in_case_or_punctuation_get_different_folders():
    from custom_tools.research_identity import task_folder_name

    names = [task_folder_name(task_id) for task_id in ("report-a", "Report-A", "report a", "report-a_")]

    assert len(set(names)) == len(names)
    assert all(name.startswith("report-a-") for name in names)


@pytest.mark.parametrize("task_id", ["task_web_test", "task_id_0123456789ab" + "_sub_1" * 6])
def test_saved_artifacts_remain_readable_after_task_folder_names_change(layout, task_id):
    from custom_tools import web_research_artifact_tools as tools
    from custom_tools.research_identity import safe_slug

    agent = _FakeAgent(layout)
    agent.task_manager = _FakeTaskManager()
    agent.task_manager.current_task_id = task_id
    set_agent_ref(agent)
    old_task_folder = layout.person_research_dir(None) / safe_slug(task_id)
    old_file = old_task_folder / "saved-result" / "page.md"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("Saved before the naming change", encoding="utf-8")

    new_dir, _artifact_id, _identity = tools._create_artifact_dir("search", "new result")

    assert new_dir.parent != old_task_folder
    inspected = _payload(tools.web_artifact_inspect(str(old_file), action="head"))
    assert inspected["status"] == "success"
    assert inspected["text"] == "Saved before the naming change"


def test_a_long_task_id_puts_research_files_in_its_own_folder(tmp_path):
    from custom_tools.research_identity import ResearchIdentity, task_folder_name

    task_id = "task_id_0123456789ab" + "_sub_1" * 6
    identity = ResearchIdentity(
        research_root=tmp_path / "research",
        team_id=None,
        agent_id=None,
        named_agent_type=None,
        task_id=task_id,
        conversation_id=None,
    )

    assert identity.task_folder == tmp_path / "research" / task_folder_name(task_id)
    assert identity.manifest_fields()["task_id"] == task_id, "manifests keep the full task id"
