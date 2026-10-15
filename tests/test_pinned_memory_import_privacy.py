"""Transferred facts keep storage provenance without removing useful agent metadata."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.memory_plugins.base import MemoryPluginContext
from custom_agents.memory_plugins.hydration import (
    CorememHydrationPlugin,
    UsermemHydrationPlugin,
)
from custom_agents.memory_review_runtime import enqueue_pinned_memory_audit_job
from custom_agents.memory_review_store import MemoryReviewStore
from custom_agents.pinned_memory_runtime import pinned_memory_record_digest
from custom_tools.providers.memory import memory
from custom_tools.runtime_context import set_agent_ref
from storage.pinned_memory_repository import (
    COREMEM_BANK,
    USERMEM_BANK,
    create_pinned_memory_repository,
)
from tests.change_approval_support import SignedTaskManager, use_signed_contexts
from tests.test_team_transfer_pinned_memory import TEAM, _old_instance, _transfer

PERSON = "usr_ana"
CREATOR = {
    "kind": "agent",
    "id": "private_original_creator",
    "display_name": "Private Original Creator",
}
PROGRESS = {
    "kind": "onboarding_guide_progress",
    "resume_section": "step_3",
    "completed": ["step_1", "step_2"],
}
SOURCE_KEY = "onboarding:guide_progress:v1"


@pytest.mark.parametrize("bank", [COREMEM_BANK, USERMEM_BANK])
def test_transferred_fact_keeps_agent_progress_without_import_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bank: str
) -> None:
    use_signed_contexts(monkeypatch)
    old_path = _old_instance(tmp_path)
    old_config = GatewayInstanceConfig.load(old_path)
    old_repository = create_pinned_memory_repository(
        old_config.manager_storage_settings
    )
    create = (
        old_repository.create_coremem
        if bank == COREMEM_BANK
        else old_repository.create_usermem
    )
    scope = {"team_id": TEAM} if bank == COREMEM_BANK else {"canonical_user_id": PERSON}
    create(
        **scope,
        fact_text="Guide progress saved.",
        capacity=10,
        tags=["onboarding_guide"],
        metadata=PROGRESS,
        source_key=SOURCE_KEY,
        actor=CREATOR,
    )
    _plan, target = _transfer(tmp_path, old_path)
    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    repository = create_pinned_memory_repository(config.manager_storage_settings)
    rows = (
        repository.list_coremem(team_id=TEAM)
        if bank == COREMEM_BANK
        else repository.list_usermem(canonical_user_id=PERSON)
    )
    row = next(fact for fact in rows if fact.source_key == SOURCE_KEY)
    assert row.metadata["imported_from"]["created_by_id"] == CREATOR["id"]
    assert (
        row.metadata["imported_from"]["created_by_display_name"]
        == CREATOR["display_name"]
    )
    memory_config = {
        "review": {"enabled": True},
        bank: {"automatic_creation_enabled": True},
    }
    agent = SimpleNamespace(
        id="agent_import_view",
        team_id=TEAM,
        name="TeamLead",
        named_agent_type="TeamArchitect",
        authorization_config={"enabled": False},
        memory_config=memory_config,
        _pinned_memory_repository=repository,
        task_manager=SignedTaskManager(
            team_id=TEAM,
            person_id=PERSON,
            storage_settings=config.manager_storage_settings,
        ),
    )
    set_agent_ref(agent)
    result = json.loads(memory(action="list", bank=bank))
    assert result["ok"] is True
    fact = next(fact for fact in result["facts"] if fact["source_key"] == SOURCE_KEY)
    assert fact["metadata"] == PROGRESS
    assert fact["record_digest"] == pinned_memory_record_digest(row)
    assert fact["memory_id"] == row.memory_id

    job = enqueue_pinned_memory_audit_job(
        memory_review_store=MemoryReviewStore(
            storage_settings=config.manager_storage_settings
        ),
        pinned_memory_repository=repository,
        team_id=TEAM,
        named_agent_type="TeamLead",
        agent_id=agent.id,
        memory_config=memory_config,
        bank=bank,
        canonical_user_id=PERSON,
    )
    assert job is not None
    audit_facts = (
        job.payload["facts"]
        if bank == COREMEM_BANK
        else job.payload["facts"][0]["facts"]
    )
    audit_fact = next(fact for fact in audit_facts if fact["source_key"] == SOURCE_KEY)
    assert audit_fact["metadata"] == PROGRESS
    assert audit_fact["record_digest"] == fact["record_digest"]
    context = MemoryPluginContext(
        agent_id=agent.id,
        team_id=TEAM,
        named_agent_type="TeamLead",
        task=None,
        msg_meta=None,
        canonical_user_id=PERSON,
        request_timezone="UTC",
        current_utc=datetime.now(timezone.utc).isoformat(),
        conversation_id=None,
        gateway_context=None,
        pinned_memory_repository=repository,
    )
    plugin = (
        CorememHydrationPlugin() if bank == COREMEM_BANK else UsermemHydrationPlugin()
    )
    hydration = plugin.hydrate(context)
    assert "Guide progress saved." in hydration[0].text
    assert "step_3" in hydration[0].text
    assert "onboarding_guide" in hydration[0].text
    for output in (json.dumps(result), json.dumps(job.payload), hydration[0].text):
        assert "imported_from" not in output
        assert CREATOR["id"] not in output
        assert CREATOR["display_name"] not in output
    # Read projections must not change the original record or its revision token.
    stored_rows = (
        repository.list_coremem(**scope)
        if bank == COREMEM_BANK
        else repository.list_usermem(**scope)
    )
    stored = next(fact for fact in stored_rows if fact.memory_id == row.memory_id)
    assert stored.metadata == row.metadata
    assert pinned_memory_record_digest(stored) == fact["record_digest"]
