"""The gateway's own wiring of Web Chat's agent trail (review F3).

The service has its own tests (tests/test_gateway_request_agents.py); these
check the connections the real ``UserGateway`` makes: the team view's member
changes reach the recorder (``_team_view_and_presence``), the writer runs as a
gateway background task, live records give who works now through the thread
list, display names come from the agent catalog, and a published request
records the team lead with the published task's id (``request_sent``).
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from assistant_gateway import gateway as gateway_module
from assistant_gateway.domain.request_state import REQUEST_KIND_INTERACTIVE, RUN_STATE_RUNNING
from tests.test_gateway_parallel_threads import _gateway as answering_gateway
from tests.test_gateway_team_view import Reader, _gateway as hosted_gateway, _publish_when_read, member, until

THREAD = "website:web-chat:trail"
OWNER = "user_trail"


def _agent_types(path: Path) -> dict[str, str | None]:
    with sqlite3.connect(path) as conn:
        return dict(conn.execute("SELECT active_task_id, agent_types_json FROM gateway_requests").fetchall())


@pytest.mark.asyncio
async def test_member_records_reach_the_stored_trail_and_who_works_through_the_real_gateway(tmp_path, monkeypatch):
    reader = Reader()
    gateway, _recorded = hosted_gateway(tmp_path, monkeypatch, reader)
    lead = member("lead_1", lead=True)
    publishing = asyncio.create_task(_publish_when_read(reader, lead))
    await gateway.connect(start_services=False)
    try:
        await publishing
        assert "gateway_request_agents" in {task.get_name() for task in gateway._background_tasks}
        repo = gateway.session_repository
        repo.create_session(session_key=THREAD, conversation_id="conv_trail", manager_agent_id="lead_1",
                            channel_name="website", user_id=OWNER, chat_id="web-chat", owner_canonical_user_id=OWNER)
        gateway.request_repository.record_request(
            correlation_id="corr_trail", session_key=THREAD, request_kind=REQUEST_KIND_INTERACTIVE,
            request_text="compare", conversation_id="conv_trail", followup_to_task_id=None,
            owner_canonical_user_id=OWNER, active_task_id="task_trail", run_state=RUN_STATE_RUNNING,
        )
        worker = replace(member("worker_1", state="busy"), agent_type="WebResearchAgent", requests=("task_trail",))
        reader.publish(lead, worker)
        await until(lambda: (_agent_types(tmp_path / "state.db").get("task_trail") or "").count("WebResearchAgent") == 1)
        threads = gateway.request_agents.working({THREAD: "task_trail"})
        assert [(agent.agent_type, agent.name) for agent in threads[THREAD]] == [
            ("WebResearchAgent", "Web Research Agent")]  # the catalog's display name (_agent_display_names)
    finally:
        await gateway.close()


def test_live_reports_without_a_team_view_are_none() -> None:
    assert gateway_module._live_agent_reports(None) == []


@pytest.mark.asyncio
async def test_a_published_request_records_the_team_lead_with_the_published_task(tmp_path: Path) -> None:
    gateway, transport = answering_gateway(tmp_path)
    route = {"channel_name": "cli", "user_id": "user", "chat_id": "cli"}
    session = gateway.application.start_new_session(**route)
    await gateway.application.send_user_message("Compare the databases", session_key=session.session_key, **route)
    [published] = transport.durable
    gateway.request_agents.start()
    gateway.request_agents.close()  # drains what the submission handed over
    recorded = _agent_types(tmp_path / "gateway.db")[published.msg_content.task_id]
    assert recorded is not None
    assert f'"agent_type":"{gateway.config.manager.named_agent_type}","lead":true' in recorded
