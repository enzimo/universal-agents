"""The manager's share of people's stored data (custom_agents.storage_usage_runtime).

The manager measures conversation records, completed tasks, the files saved
beside conversations and people's folders, and writes per-person totals to
the shared table the gateway reads (``storage.person_storage_usage_repository``).
Real SQLite stores and real folders; the agent is a stand-in carrying the
same attributes the team lead has.

Covered: every kind adds up per person, a person with no data gets zeros,
another person's data is never counted, links are never followed; only active
people are written and people who left lose their rows; a runtime that cannot
measure writes nothing and says so (the gateway then reports the totals as
out of date); the team lead's measuring task starts once from the scheduler
pass, measures at start, keeps running across passes, is started again if it
ended, stops with the agent, and no other agent starts one.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_agents.storage_usage_runtime as runtime
from custom_agents.storage_usage_runtime import (
    keep_storage_usage_measured,
    measure_manager_side,
    record_manager_usage,
)
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from runtime_settings.instance_layout import InstanceLayout, person_folder_name
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from storage.conversation_repository import create_conversation_repository
from storage.person_storage_usage_repository import PersonStorageUsageRepository, StorageSide
from storage.task_archive_repository import create_task_archive_repository

ALICE = "usr_alice"
BOB = "usr_bob"
TEAM = "team_storage"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
WIDE = "Grüße 日本語 ✓"  # 26 bytes in UTF-8


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


class _Tasks:
    def stored_text_bytes_by_owner(self) -> dict[str, int]:
        return {ALICE: 70, BOB: 5}


def _settings(tmp_path: Path):
    return resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})


def test_every_kind_adds_up_per_person_and_a_person_without_data_gets_zeros(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    conversations = create_conversation_repository(_settings(tmp_path), artifacts_dir=layout.conversation_artifacts_dir)
    for conversation_id, owner in (("conv_a", ALICE), ("conv_b", BOB)):
        conversations.ensure_thread(
            conversation_id=conversation_id, team_id=TEAM, owner_agent_id="lead", owner_agent_name="Nova",
            requester_name=None, requester_id=None, root_task_id=f"task_{owner}", owner_canonical_user_id=owner,
        )
    conversations.offload_large_text(
        conversation_id="conv_a", task_id="task_a", artifact_type="tool_output", raw_text=WIDE * 100, summary_text="s"
    )
    conversations.offload_large_text(
        conversation_id="conv_b", task_id="task_b", artifact_type="tool_output", raw_text="b" * 5000, summary_text="s"
    )
    alice_folder = layout.users_dir / person_folder_name(ALICE)
    _write(alice_folder / "research" / "notes.md", 4000)
    _write(alice_folder / "python_envs" / "env" / "lib" / "module.py", 600)
    _write(tmp_path / "elsewhere" / "big.bin", 10_000)
    os.symlink(tmp_path / "elsewhere", alice_folder / "linked")  # never followed
    os.symlink(tmp_path / "elsewhere" / "big.bin", alice_folder / "linked.bin")

    measured = measure_manager_side([ALICE, "usr_carol"], conversations=conversations, tasks=_Tasks(), layout=layout)

    alice_texts = conversations.stored_text_bytes_by_owner()[ALICE]
    assert measured == {
        ALICE: {"tasks": alice_texts + 70, "conversation_files": 100 * len(WIDE.encode()), "folder": 4600},
        "usr_carol": {"tasks": 0, "conversation_files": 0, "folder": 0},
    }


def test_a_person_folder_replaced_by_a_link_counts_nothing_and_is_logged(tmp_path: Path, caplog) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    _write(tmp_path / "outside" / "big.bin", 10_000)
    layout.users_dir.mkdir(parents=True)
    os.symlink(tmp_path / "outside", layout.users_dir / person_folder_name(ALICE))
    conversations = SimpleNamespace(stored_text_bytes_by_owner=dict, conversation_owners=dict)

    with caplog.at_level("WARNING", logger="custom_agents.storage_usage_runtime"):
        measured = measure_manager_side([ALICE], conversations=conversations, tasks=_Tasks(), layout=layout)

    assert measured[ALICE]["folder"] == 0
    assert "event=storage_usage_folder_unmeasured person_folder=usr_alice reason=link" in caplog.text


def _agent(tmp_path: Path, **overrides) -> SimpleNamespace:
    """The team lead's attributes the measurement reads."""

    settings = _settings(tmp_path)
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.workspaces_dir.mkdir(parents=True, exist_ok=True)
    access = AccessRepository(settings)
    for person, standing in ((ALICE, PersonStanding.ACTIVE), (BOB, PersonStanding.ACTIVE), ("usr_dana", PersonStanding.DISABLED)):
        access.record_person(TeamPersonV1(team_id=TEAM, person_id=person, standing=standing, changed_at_utc=NOW))
    values = dict(
        id="lead-1",
        team_id=TEAM,
        is_team_lead=True,
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        _conversation_store=create_conversation_repository(settings, artifacts_dir=layout.conversation_artifacts_dir),
        task_manager=SimpleNamespace(task_archive_repository=create_task_archive_repository(settings), storage_settings=settings),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_the_manager_writes_its_share_for_active_people_and_drops_people_who_left(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    shared = PersonStorageUsageRepository(agent.task_manager.storage_settings)
    shared.record_side(TEAM, StorageSide.MANAGER, {"usr_gone": {"folder": 9}}, measured_at=NOW)
    _write(Path(agent.workspace_dir) / "users" / person_folder_name(ALICE) / "a.txt", 1234)
    agent.task_manager.task_archive_repository.save_completed_task(
        archive_id="arch_a", task_id="task_a", owner_agent_id="lead", owner_agent_name="Nova", team_id=TEAM,
        task_json="{}" * 10, status="completed", owner_canonical_user_id=ALICE,
    )

    assert record_manager_usage(agent, now=NOW) == 2

    rows = shared.side_usage(TEAM, StorageSide.MANAGER)
    assert set(rows) == {ALICE, BOB}  # not the disabled person, and not someone who left
    assert rows[ALICE].bytes_by_kind == {"tasks": 20, "conversation_files": 0, "folder": 1234}
    assert rows[ALICE].measured_at == NOW and rows[BOB].total == 0


@pytest.mark.parametrize("place", ["person", "nested", "artifacts"])
def test_an_unreadable_folder_preserves_the_prior_totals_until_they_are_reported_stale(
    tmp_path: Path, caplog, place: str
) -> None:
    from assistant_gateway.storage_usage import StorageUsageNotices

    agent = _agent(tmp_path)
    layout = InstanceLayout.at(Path(agent.instance_root))
    agent._conversation_store.ensure_thread(
        conversation_id="conv_a", team_id=TEAM, owner_agent_id="lead", owner_agent_name="Nova",
        requester_name=None, requester_id=None, root_task_id="task_a", owner_canonical_user_id=ALICE,
    )
    personal = layout.users_dir / person_folder_name(ALICE)
    nested = personal / "nested"
    artifacts = layout.conversation_artifacts_dir / "conv_a"
    for folder in (nested, artifacts):
        folder.mkdir(parents=True)
        with (folder / "sparse.bin").open("wb") as stream:
            stream.truncate(6_000_000_000)
    shared = PersonStorageUsageRepository(agent.task_manager.storage_settings)
    assert record_manager_usage(agent, now=NOW) == 2
    before = shared.side_usage(TEAM, StorageSide.MANAGER)
    blocked = {"person": personal, "nested": nested, "artifacts": layout.conversation_artifacts_dir}[place]
    blocked.chmod(0)
    later = NOW + timedelta(days=4)
    try:
        with caplog.at_level(logging.ERROR, logger="custom_agents.storage_usage_runtime"):
            assert record_manager_usage(agent, now=later) is None
    finally:
        blocked.chmod(0o700)
    assert shared.side_usage(TEAM, StorageSide.MANAGER) == before
    assert "event=storage_usage_failed" in caplog.text
    notices = StorageUsageNotices(
        team_id=TEAM, step_gb=5,
        people=lambda: [TeamPersonV1(team_id=TEAM, person_id=ALICE, standing=PersonStanding.ACTIVE, changed_at_utc=NOW)],
        measure=dict, store=lambda: shared, send_notice_once=lambda notice: True, now=lambda: later,
    )
    assert notices.stale_finding() is not None


@pytest.mark.parametrize("missing", ["workspace_dir", "_conversation_store", "task_manager"])
def test_a_runtime_that_cannot_measure_writes_nothing_and_says_so(tmp_path: Path, caplog, missing: str) -> None:
    agent = _agent(tmp_path, **{missing: SimpleNamespace(storage_settings=_settings(tmp_path)) if missing == "task_manager" else None})
    if missing == "workspace_dir":
        agent.workspace_dir = str(tmp_path / "somewhere_else")

    with caplog.at_level("WARNING", logger="custom_agents.storage_usage_runtime"):
        assert record_manager_usage(agent, now=NOW) is None

    assert "event=storage_usage_skipped" in caplog.text
    assert PersonStorageUsageRepository(_settings(tmp_path)).side_usage(TEAM, StorageSide.MANAGER) == {}


def test_the_team_lead_measures_at_start_and_keeps_its_task_across_passes(tmp_path: Path, monkeypatch, caplog) -> None:
    measured: list[str] = []
    monkeypatch.setattr(runtime, "record_manager_usage", lambda agent: measured.append(agent.id))
    monkeypatch.setattr(runtime, "_LOOPS", {})

    async def scenario() -> None:
        lead = SimpleNamespace(id="lead-1", team_id=TEAM, is_team_lead=True, _should_exit=asyncio.Event())
        worker = SimpleNamespace(id="worker-1", team_id=TEAM, is_team_lead=False, _should_exit=asyncio.Event())
        keep_storage_usage_measured(worker)
        keep_storage_usage_measured(lead)
        first = runtime._LOOPS["lead-1"]
        await asyncio.sleep(0.05)
        keep_storage_usage_measured(lead)  # the next scheduler pass
        assert runtime._LOOPS["lead-1"] is first and measured == ["lead-1"]  # measured once, at start
        first.cancel()  # the task ended for some reason
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING, logger="custom_agents.storage_usage_runtime"):
            keep_storage_usage_measured(lead)
        await asyncio.sleep(0.05)
        assert runtime._LOOPS["lead-1"] is not first and measured == ["lead-1", "lead-1"]
        lead._should_exit.set()  # the agent stops
        await asyncio.wait_for(runtime._LOOPS["lead-1"], timeout=2)
        assert "worker-1" not in runtime._LOOPS

    asyncio.run(scenario())

    assert "event=storage_usage_restarted agent_id=lead-1" in caplog.text


def test_a_failed_measurement_is_logged_and_tried_again(tmp_path: Path, monkeypatch, caplog) -> None:
    calls: list[int] = []

    def broken(agent) -> None:
        calls.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(runtime, "record_manager_usage", broken)

    async def scenario() -> None:
        lead = SimpleNamespace(id="lead-1", team_id=TEAM, is_team_lead=True, _should_exit=asyncio.Event())
        task = asyncio.create_task(runtime.storage_usage_loop(lead, interval=runtime.timedelta(milliseconds=10)))
        await asyncio.sleep(0.1)
        lead._should_exit.set()
        await asyncio.wait_for(task, timeout=2)

    with caplog.at_level(logging.ERROR, logger="custom_agents.storage_usage_runtime"):
        asyncio.run(scenario())

    assert len(calls) >= 2 and "event=storage_usage_failed agent_id=lead-1" in caplog.text
