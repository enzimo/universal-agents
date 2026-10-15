"""The knowledge tools act for the person in the task's signed context, never a claimed one."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from custom_messages.contracts.routing_v1 import GatewayContext
from custom_tools.providers.knowledge import (
    knowledge_actor,
    knowledge_add_note,
    knowledge_archive,
    knowledge_history,
    knowledge_read,
    knowledge_search,
    knowledge_write,
)
from custom_tools.runtime_context import set_agent_ref
from storage.config import resolve_storage_settings
from storage.knowledge_repository import create_knowledge_repository
from storage.pinned_memory_repository import SqlitePinnedMemoryRepository
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts

TEAM = "team_test"


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    use_signed_contexts(monkeypatch)


class _TaskManager:
    current_task_id = "task_current"

    def __init__(self, *, signed_for: str | None, claimed: str) -> None:
        self._signed_for = signed_for
        self._claimed = claimed

    def get_task_info(self, task_id: str):
        signed = (
            context_signer().issue(team_id=TEAM, person_id=self._signed_for, person_roles=("member",))
            if self._signed_for
            else None
        )
        return SimpleNamespace(
            meta=SimpleNamespace(
                gateway_context=GatewayContext(
                    source="test", user_id=self._claimed, canonical_user_id=self._claimed, task_context=signed
                )
            )
        )


class _Agent:
    team_id = TEAM
    id = "agent_worker"
    name = "Worker"
    named_agent_type = "Researcher"
    authorization_config = {"enabled": False}
    knowledge_config: dict = {}

    def __init__(self, db_path, *, signed_for: str | None, claimed: str = "") -> None:
        settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db_path)}})
        self._knowledge_repository = create_knowledge_repository(settings)
        self._pinned_memory_repository = SqlitePinnedMemoryRepository(str(db_path))
        self.task_manager = _TaskManager(signed_for=signed_for, claimed=claimed or (signed_for or ""))
        self.task_manager.storage_settings = settings
        self.task_manager._db_path = str(db_path)


def _call(agent, tool, **kwargs) -> dict:
    set_agent_ref(agent)
    try:
        return json.loads(tool(**kwargs))
    finally:
        set_agent_ref(None)


def test_the_signed_person_wins_over_a_claimed_one(tmp_path):
    agent = _Agent(tmp_path / "kb.db", signed_for="alice", claimed="bob")
    actor = knowledge_actor(agent)
    assert actor.work_person_id == "alice" and actor.work_id


def test_without_a_signed_context_the_work_is_for_nobody(tmp_path):
    agent = _Agent(tmp_path / "kb.db", signed_for=None, claimed="bob")
    assert knowledge_actor(agent).work_person_id == ""
    refused = _call(agent, knowledge_write, page="personal/notes", title="Notes", body="Mine.")
    assert refused["ok"] is False and refused["error_code"] == "personal_space_unavailable"


def test_tools_write_read_search_and_never_name_people(tmp_path):
    db = tmp_path / "kb.db"
    system = _Agent(db, signed_for=None)
    alice = _Agent(db, signed_for="alice")
    saved = _call(system, knowledge_write, page="team/ci", title="CI", body="CI runs on push.", tags=["ci"])
    assert saved == {"ok": True, "status": "saved", "page": "team/ci", "revision": 1, "page_status": "active", "review_flags": []}
    proposed = _call(alice, knowledge_write, page="team/ci", body="CI runs nightly too.", base_revision=1)
    assert proposed["status"] == "pending_review" and proposed["contribution_id"].startswith("kb_contrib_")
    noted = _call(alice, knowledge_add_note, page="team/ci", note="Flaky on Sundays.")
    assert noted["status"] == "pending_review" and noted["change"] == "note"
    personal = _call(alice, knowledge_write, page="personal/ci-notes", title="My CI notes", body="I rerun flaky jobs.")
    assert personal["status"] == "saved"

    found = _call(alice, knowledge_search, query="ci")
    assert {result["page"] for result in found["results"]} == {"team/ci", "personal/ci-notes"}
    read = _call(alice, knowledge_read, page="team/ci")
    assert read["body"] == "CI runs on push." and len(read["pending_changes_from_this_work"]) == 2
    history = _call(system, knowledge_history, page="team/ci")
    assert history["revisions"][0]["by"] == "Researcher"
    outputs = json.dumps([saved, proposed, noted, found, read, history])
    assert "alice" not in outputs

    archived = _call(system, knowledge_archive, page="team/ci", base_revision=1, reason="Moved to team/build")
    assert archived["page_status"] == "archived"
    assert _call(system, knowledge_search, query="ci")["results"] == []


def test_refusals_explain_what_to_do(tmp_path):
    system = _Agent(tmp_path / "kb.db", signed_for=None)
    blocked = _call(system, knowledge_write, page="team/db", title="DB", body="password: hunter22")
    assert blocked["error_code"] == "sensitive_content" and "hunter22" not in json.dumps(blocked)
    missing = _call(system, knowledge_read, page="team/nothing")
    assert missing["error_code"] == "not_found" and "knowledge_search" in missing["message"]


def test_an_agent_without_the_store_is_told_it_is_unavailable(tmp_path):
    agent = _Agent(tmp_path / "kb.db", signed_for=None)
    agent._knowledge_repository = None
    result = _call(agent, knowledge_search, query="x")
    assert result["ok"] is False and result["error_code"] == "knowledge_unavailable"


def test_knowledge_write_keeps_an_omitted_summary_and_clears_an_explicit_empty_summary(tmp_path):
    alice = _Agent(tmp_path / "kb.db", signed_for="alice")
    _call(alice, knowledge_write, page="personal/guide", title="Guide", body="Read the guide.", summary="Private overview")
    _call(alice, knowledge_write, page="personal/guide", body="Read the updated guide.", base_revision=1)
    assert _call(alice, knowledge_read, page="personal/guide")["summary"] == "Private overview"
    cleared = _call(alice, knowledge_write, page="personal/guide", summary="", base_revision=2)
    assert cleared["ok"]
    assert _call(alice, knowledge_read, page="personal/guide")["summary"] == ""
