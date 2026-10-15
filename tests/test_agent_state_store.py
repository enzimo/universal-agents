import logging
import os
import tempfile
from datetime import datetime, timezone

import pytest

from custom_agents.agent_state_store import AgentStateStore
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.enums import ControlType, MessageMetaType
from id_generation import new_id


@pytest.fixture
def temp_db_path():
    """Create a temporary database file for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def store(temp_db_path):
    """Fixture providing an AgentStateStore instance."""
    logger = logging.getLogger("test_agent_state_store")
    logger.setLevel(logging.DEBUG)
    return AgentStateStore(db_path=temp_db_path, logger=logger)


def create_mock_agent_status(
    agent_id: str,
    name: str = "TestAgent",
    agent_type: str = "CodeWritingAgent",
    is_executing: bool = False,
    team_id: str = "test_team",
) -> AgentStatus:
    return AgentStatus(
        self_id=agent_id,
        name=name,
        named_agent_type=agent_type,
        team_id=team_id,
        is_executing=is_executing,
        has_execution_capacity=not is_executing,
        is_busy=is_executing,
        top_level_queue_depth=0,
        waiting_top_level_task_count=0,
        deferred_message_count=0,
        is_paused=False,
        is_team_lead=False,
        agent_role="Test Role",
        tools=["test_tool"],
        last_seen_at=datetime.now(timezone.utc).isoformat(),
        status_type=MessageMetaType.RESPONSE,
        info_type=ControlType.STATUS_REQUEST_BASIC,
    )


def test_remove(store: AgentStateStore):
    """Verify an agent can be removed from the store."""
    agent_id = new_id("agent")
    status = create_mock_agent_status(agent_id=agent_id, name="AgentToBeRemoved")

    store.upsert(status)
    assert store.get_by_id(agent_id) is not None

    removed = store.remove(agent_id)
    assert removed is True

    assert store.get_by_id(agent_id) is None


def test_get_all(store: AgentStateStore):
    """Verify get_all returns all persisted agents."""
    agent1 = create_mock_agent_status(agent_id="id1", name="Agent1")
    agent2 = create_mock_agent_status(agent_id="id2", name="Agent2")

    store.upsert(agent1)
    store.upsert(agent2)

    all_agents = store.get_all()
    assert len(all_agents) == 2
    ids = {a.self_id for a in all_agents}
    assert "id1" in ids
    assert "id2" in ids


def test_query_filtering(store: AgentStateStore):
    """Verify query correctly filters by provided criteria."""
    a1 = create_mock_agent_status("id1", name="AlphaOne", agent_type="TypeA", is_executing=False, team_id="team1")
    a2 = create_mock_agent_status("id2", name="AlphaTwo", agent_type="TypeA", is_executing=True, team_id="team2")
    a3 = create_mock_agent_status("id3", name="BetaOne", agent_type="TypeB", is_executing=False, team_id="team1")

    store.upsert(a1)
    store.upsert(a2)
    store.upsert(a3)

    # Filter by name
    res_name = store.query({"agent_name": "Alpha"})
    assert len(res_name) == 2

    # Filter by type
    res_type = store.query({"named_agent_type": "TypeA"})
    assert len(res_type) == 2

    # Filter by execution state
    res_executing = store.query({"is_executing": True})
    assert len(res_executing) == 1
    assert res_executing[0].self_id == "id2"

    # Filter by team ID
    res_team = store.query({"team_id": "team1"})
    assert len(res_team) == 2

    # Multiple filters
    res_multi = store.query({"named_agent_type": "TypeA", "has_execution_capacity": True})
    assert len(res_multi) == 1
    assert res_multi[0].self_id == "id1"
