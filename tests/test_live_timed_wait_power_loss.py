"""NATS-backed JetStream file-storage power-loss tests for timed waits (P4, C01, C02, F2).

Each test starts an isolated ``nats-server`` subprocess with JetStream file
storage and release/0.10 partition mappings via ``tests.own_nats_server.own_nats_server_process``
(each server picks its own free port from its own log and shares only the
JetStream folder across the restart), connects a real ``UniversalAgent`` running
the real ``wait`` tool against a scripted Strands ``Model``, and then simulates
abrupt power loss:

1. The first agent runs in its own OS process
   (``tests/live_power_loss_first_agent.py``) with its own process run id,
   connected to the mapped server. Once its task is ``WAITING`` and the worker
   slot is free, ``SIGKILL`` ends that process and then ``nats-server``: a real
   process death, so nothing of the first agent (turn work, heartbeat, status
   writes) can run or write afterwards. An in-process stand-in that cancelled
   only the agent's tasks left paused-turn work writing its rows (F2);
2. The saved ``WAITING`` task, its wait record and single charge, and the first
   agent's status row (not exiting, still naming the killed process) are
   checked in SQLite, and the row is checked again right before the second
   agent starts;
3. Advance the controllable clock (``timed_waits.utc_now``);
4. Restart ``nats-server`` via ``own_nats_server_process`` on the SAME
   JetStream folder (picking a fresh ephemeral port), verify the prior
   file-backed work stream and consumers loaded from disk, and start a fresh
   ``UniversalAgent`` in the test process, under a new process run id, on the
   same SQLite database.

Covers all three power-loss scenarios:
1. Restart before ``resume_at``: stays ``WAITING``, never resumes early,
   resumes once at ``resume_at``, delivers durable note, finishes once.
2. Restart long after ``resume_at`` (3 hours): resumes promptly on startup
   with real waited time and exact UTC resume timestamp, finishes once.
3. Restart as a different ``agent_id`` of the same ``named_agent_type`` with
   a fresh ``UA_PROCESS_RUN_ID``: real ``agent_liveness.owner_liveness``
   decides ``PROCESS_GONE`` (logging ``event=owner_liveness_decided``), the new
   instance adopts the ``WAITING`` task transactionally, and resumes it once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from strands.models import Model

from custom_agents import timed_waits as timed_waits_rules
from custom_agents.strands_agent import UniversalAgent
from custom_agents.structured_llm_messages import (
    ModelAgentTurnDecision,
    ModelTaskResponseAction,
    ModelTaskResponsePayload,
)
from custom_agents.task_human_interaction_blockers import get_human_interaction_blocker
from custom_agents.task_manager import TaskManager
from custom_agents.timed_waits import WaitLedger
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from custom_tools.providers.timed_waits import wait
from custom_tools.runtime_context import set_agent_ref
from id_generation import new_id
from live_nats_client import LiveNatsClient
from runtime_hosting.process_run import (
    PROCESS_RUN_ID_ENV,
    PROCESS_RUN_PROVEN_ENV,
    current_process_identity,
)
from runtime_settings.projection import _project_transport
from runtime_settings.transport import TransportSettings
from storage import resolve_storage_settings
from test_live_multi_agent_state_boundaries import _start_agent, _stop_agents, _wait_for
from tests.own_nats_server import own_nats_server_process

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

BASE_TIME = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)


def _nats_server_binary() -> str:
    """Resolve ``nats-server`` from ``LIVE_TEST_NATS_SERVER`` or ``PATH``; fail loudly if absent."""

    configured = os.environ.get("LIVE_TEST_NATS_SERVER")
    if configured:
        if not (Path(configured).is_file() and os.access(configured, os.X_OK)):
            pytest.fail(f"LIVE_TEST_NATS_SERVER={configured} is not an executable file")
        return configured
    found = shutil.which("nats-server")
    if found is None:
        pytest.fail("Power-loss NATS tests require nats-server on PATH or LIVE_TEST_NATS_SERVER")
    return found


def _sigkill_nats_server(process: subprocess.Popen[bytes]) -> None:
    """Abruptly terminate ``nats-server`` with SIGKILL to simulate power loss."""

    try:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    finally:
        current_process_identity.cache_clear()


def _snapshot_runtime_rows(db_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT owner_agent_id, runtime_state_json FROM active_task_runtime ORDER BY owner_agent_id"
        ).fetchall()
    return [(str(owner), str(payload)) for owner, payload in rows]


def _saved_task(db_path: Path, owner_agent_id: str, task_id: str) -> dict[str, Any]:
    """The task as the owner last saved it (assembled the way recovery reads it)."""

    from storage.runtime_state_repository import SqliteRuntimeStateRepository

    rows = SqliteRuntimeStateRepository(str(db_path)).load_task_runtime_by_owner(owner_agent_id)
    assert len(rows) == 1, f"expected one saved runtime row for {owner_agent_id}, got {len(rows)}"
    payload = json.loads(rows[0].runtime_state_json)
    saved = next(item for item in payload["tasks"] if item["task"]["task_id"] == task_id)
    events = [(entry.get("details") or {}).get("event") for entry in saved["task"]["execution_history"]]
    return {
        "state": saved["state"],
        "blocker_kind": (payload["human_interaction_blockers"].get(task_id) or {}).get("kind"),
        "charges": events.count("timed_wait_started"),
    }


def _saved_agent_status(db_path: Path, agent_id: str) -> tuple[int, str | None]:
    """``(is_exiting, process_identity)`` from the agent's ``agent_states`` row."""

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT is_exiting, full_status_json FROM agent_states WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    assert row is not None, f"agent {agent_id} wrote no status row"
    return int(row[0]), json.loads(row[1]).get("process_identity")


async def _start_first_agent_process(tmp_path: Path, args: dict[str, str], env: dict[str, str]) -> tuple[Any, str]:
    """Start ``live_power_loss_first_agent.py``; return the process and its identity once its task is WAITING."""

    project_root = Path(__file__).resolve().parents[1]
    tests_dir = Path(__file__).resolve().parent
    log_path = tmp_path / "first-agent.log"
    log_handle = log_path.open("ab")
    child_env = dict(env)
    child_env["PYTHONPATH"] = os.pathsep.join([str(project_root), str(tests_dir)])
    child_env["RTK_DB_PATH"] = ":memory:"
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(tests_dir / "live_power_loss_first_agent.py"),
            json.dumps(args),
            cwd=str(project_root),
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=log_handle,
        )
    finally:
        log_handle.close()
    assert process.stdout is not None
    try:
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=60.0)
            if not line:
                await process.wait()
                pytest.fail(
                    f"First agent process exited ({process.returncode}) before its task was WAITING:\n"
                    + log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                )
            text = line.decode("utf-8", errors="replace").strip()
            if text.startswith("READY "):
                return process, str(json.loads(text[len("READY "):])["process_identity"])
    except asyncio.TimeoutError:
        await _sigkill_process(process)
        pytest.fail(
            "First agent process did not report a WAITING task within 60s:\n"
            + log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        )


async def _sigkill_process(process: Any) -> None:
    """SIGKILL the first agent's process and reap it (a real, abrupt process death)."""

    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await asyncio.wait_for(process.wait(), timeout=10.0)


async def _assert_restarted_jetstream_loaded_store(
    team_id: str,
    transport_cfg: TransportSettings,
    nats_url: str,
) -> None:
    """Verify the restarted nats-server loaded the prior file-backed work stream and its consumers."""
    from custom_transport.nats_adapter import NatsTransportAdapter
    from custom_transport.subjects import NatsSubjectMapper
    from live_nats_client import nats_transport_config_from_env

    config = nats_transport_config_from_env(transport=transport_cfg, servers=[nats_url], name="pl-store-check")
    mapper = NatsSubjectMapper(team_id=team_id, subject_prefix=config.subject_prefix)
    adapter = NatsTransportAdapter(config=config, mapper=mapper, logger=logging.getLogger("test.pl_store"))
    await adapter.connect(reconcile_topology=False)
    try:
        info = await adapter._js.stream_info(mapper.work_stream_name())
        assert str(info.config.storage).lower().endswith("file")
        assert info.state.consumer_count >= 1
    finally:
        await adapter.close()


def _prompt_text(messages: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for msg in messages or ():
        if msg.get("role") != "user":
            continue
        for block in msg.get("content") or ():
            text = block.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


def _model_response(answer: str) -> ModelAgentTurnDecision:
    return ModelAgentTurnDecision(
        actions=[ModelTaskResponseAction(task=ModelTaskResponsePayload(answer=answer))]
    )


class _PowerLossWaitModel(Model):
    """Calls real ``wait(5, reason)`` before power loss; after restart answers with the received resume note."""

    def __init__(self, *, reason: str, should_call_wait: bool) -> None:
        self.reason = reason
        self.should_call_wait = should_call_wait
        self.wait_calls = 0
        self.answer_calls = 0
        self.captured_prompts: list[str] = []
        self._last_messages: list[dict[str, Any]] = []

    def update_config(self, **config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted-power-loss-wait"}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        messages = prompt if isinstance(prompt, list) else self._last_messages
        decision = self._decision_for(messages)
        yield {"output": decision}

    def _decision_for(self, messages: list[dict[str, Any]]) -> ModelAgentTurnDecision:
        text = _prompt_text(messages) or _prompt_text(self._last_messages)
        self.captured_prompts.append(text)
        self.answer_calls += 1
        note_line = next(
            (line.strip() for line in text.splitlines() if "the task was resumed at" in line),
            "NO_RESUME_NOTE",
        )
        return _model_response(f"RESUMED_OK|{note_line}")

    async def stream(
        self, messages, tool_specs=None, system_prompt=None, *, structured_output_model=None, **kwargs
    ):
        self._last_messages = list(messages or ())
        text = _prompt_text(self._last_messages)
        if self.should_call_wait and self.wait_calls == 0 and "POWER_LOSS_WAIT" in text:
            self.wait_calls += 1
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "tu_power_loss", "name": "wait"}}}}
            yield {
                "contentBlockDelta": {
                    "delta": {"toolUse": {"input": json.dumps({"minutes": 5, "reason": self.reason})}}
                }
            }
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return

        decision = self._decision_for(self._last_messages)
        decision_tool = next(
            (
                spec["name"]
                for spec in (tool_specs or ())
                if isinstance(spec, dict) and str(spec.get("name", "")).endswith("TurnDecision")
            ),
            None,
        )
        if decision_tool:
            yield {"messageStart": {"role": "assistant"}}
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "tu_pl_decision", "name": decision_tool}}
                }
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": decision.model_dump_json()}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return

        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": decision.model_dump_json()}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def _power_loss_agent(
    *,
    agent_id: str,
    team_id: str,
    named_agent_type: str,
    db_path: Path,
    workspace_dir: Path,
    nats_url: str,
    same_role_takeover: bool = True,
) -> UniversalAgent:
    storage_settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db_path)}})
    transport = TransportSettings(nats_url=nats_url, nats_servers=[nats_url], storage="file")
    agent = UniversalAgent(
        id=agent_id,
        name=named_agent_type,
        named_agent_type=named_agent_type,
        team_id=team_id,
        is_team_lead=False,
        **_project_transport(transport_cfg=transport.model_dump()),
        task_manager=TaskManager(
            db_path=str(db_path),
            logger=logging.getLogger(f"TaskManager.{agent_id}"),
            storage_settings=storage_settings,
        ),
        workspace_dir=str(workspace_dir),
        instance_root=str(workspace_dir),
        memory_graph_enabled=False,
        activity_collector_base_url=None,
        subtask_watchdog_interval_seconds=3600,
        model_provider="openai",
        model_id="fake-power-loss-model",
    )
    agent.authorization_config = {"enabled": False}
    agent.tools = [wait]
    agent.same_role_runtime_takeover_enabled = same_role_takeover
    agent._strands_parts.replace_before_next_turn = True
    return agent


def _request_envelope(task_id: str, target_id: str, reply_queue: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(
            task_id=task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="POWER_LOSS_WAIT: wait 5 minutes then report.",
        ),
        msg_header=MessageHeader(
            from_agent_id="gateway",
            to_routing_path=target_id,
            response_routing_key=reply_queue,
        ),
        msg_meta=MessageMeta(
            from_agent_name="person",
            msg_correlation_id=f"corr_{task_id}",
            conversation_id="conv_power_loss",
            requester_type=RequesterType.USER_REQUESTER,
            is_response_requested=True,
            gateway_context=GatewayContext(canonical_user_id="owner", session_key="session_power_loss"),
        ),
    )


async def _wait_for_task_reply(queue: Any, task_id: str, *, timeout: float = 15.0) -> Task:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"Timed out waiting for response for {task_id}")
        envelope = await queue.get(timeout=remaining)
        content = envelope.msg_content
        if isinstance(content, Task) and content.task_id == task_id and content.task_type == MessageMetaType.RESPONSE:
            return content


async def _run_power_loss_scenario(
    tmp_path: Path,
    *,
    reason: str,
    restart_offset: timedelta,
    resume_offset: timedelta | None,
    different_instance_id: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Task, _PowerLossWaitModel, Path, str, str]:
    """Run one complete power-loss + restart scenario against isolated file-backed JetStream with 0.10 mappings."""

    now_box = [BASE_TIME]
    nats_binary = _nats_server_binary()
    nats_folder = tmp_path / "nats"
    nats_folder.mkdir(parents=True, exist_ok=True)
    team_id = new_id("team_pl")
    named_type = "WebResearchAgent"
    first_agent_id = new_id("worker_first")
    second_agent_id = new_id("worker_second") if different_instance_id else first_agent_id
    task_id = new_id("task_pl")
    reply_route = f"{team_id}_gateway_pl_reply_{new_id('r')}"
    db_path = tmp_path / "runtime.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # The first agent's process run id; real agent_liveness later finds this process gone.
    before_run_id = f"run-before-{new_id('p')}"

    first_process: Any = None
    second_model = _PowerLossWaitModel(reason=reason, should_call_wait=False)
    second_agent_tasks: list[tuple[UniversalAgent, asyncio.Task[Any]]] = []

    try:
        with patch.object(timed_waits_rules, "utc_now", side_effect=lambda: now_box[0]):
            # --- Phase 1: agent 1 in its own process (mapped server) calls real wait(5) and saves WAITING ---
            with own_nats_server_process(nats_binary, nats_folder) as (nats_url_1, proc1):
                first_env = dict(os.environ)
                first_env[PROCESS_RUN_ID_ENV] = before_run_id
                first_env[PROCESS_RUN_PROVEN_ENV] = "1"
                first_process, first_identity = await _start_first_agent_process(
                    tmp_path,
                    {
                        "nats_url": nats_url_1,
                        "db_path": str(db_path),
                        "workspace": str(workspace),
                        "team_id": team_id,
                        "named_agent_type": named_type,
                        "agent_id": first_agent_id,
                        "task_id": task_id,
                        "reply_route": reply_route,
                        "reason": reason,
                    },
                    first_env,
                )
                assert before_run_id in first_identity

                # --- Phase 2: power loss: SIGKILL agent 1's process, then nats-server ---
                await _sigkill_process(first_process)
                _sigkill_nats_server(proc1)
            assert _saved_task(db_path, first_agent_id, task_id) == {
                "state": ProgressState.WAITING.value,
                "blocker_kind": "timed_wait",
                "charges": 1,
            }
            assert _saved_agent_status(db_path, first_agent_id) == (0, first_identity), (
                "A power loss writes nothing: agent 1 is not exiting and its row names its killed process"
            )

            # --- Phase 3: advance clock, restart nats-server from SAME JetStream folder, start fresh agent ---
            now_box[0] = BASE_TIME + restart_offset
            monkeypatch.setenv(PROCESS_RUN_ID_ENV, f"run-after-{new_id('p')}")
            monkeypatch.setenv(PROCESS_RUN_PROVEN_ENV, "1")
            current_process_identity.cache_clear()

            with own_nats_server_process(nats_binary, nats_folder) as (nats_url_2, _proc2):
                transport_cfg_2 = TransportSettings(nats_url=nats_url_2, nats_servers=[nats_url_2], storage="file")
                # Prove the restarted nats-server loaded the prior file-backed work stream and consumers from disk.
                await _assert_restarted_jetstream_loaded_store(team_id, transport_cfg_2, nats_url_2)
                # Right before agent 2 starts, agent 1's row still names the killed process (nothing wrote since).
                assert _saved_agent_status(db_path, first_agent_id) == (0, first_identity)

                with patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=second_model):
                    agent2 = _power_loss_agent(
                        agent_id=second_agent_id,
                        team_id=team_id,
                        named_agent_type=named_type,
                        db_path=db_path,
                        workspace_dir=workspace,
                        nats_url=nats_url_2,
                        same_role_takeover=True,
                    )
                    set_agent_ref(agent2)
                    async with LiveNatsClient(team_id, transport=transport_cfg_2, servers=[nats_url_2]) as client2:
                        replies = await client2.subscribe_route(reply_route)
                        agent2_task = await _start_agent(agent2)
                        second_agent_tasks.append((agent2, agent2_task))

                        if resume_offset is not None:
                            # Restarted BEFORE resume_at: task must stay WAITING and not answer early.
                            await _wait_for(
                                lambda: (info := agent2.task_manager.get_task_info(task_id)) is not None
                                and info.state == ProgressState.WAITING
                                and (get_human_interaction_blocker(agent2.task_manager, task_id) or {}).get("kind")
                                == "timed_wait",
                                timeout=10.0,
                            )
                            await asyncio.sleep(0.4)
                            assert second_model.answer_calls == 0
                            assert agent2.task_manager.get_task_info(task_id).state == ProgressState.WAITING
                            if different_instance_id:
                                owners = [owner for owner, _ in _snapshot_runtime_rows(db_path)]
                                assert first_agent_id not in owners and second_agent_id in owners

                            # Move clock past resume_at and wake the real scheduler loop.
                            now_box[0] = BASE_TIME + resume_offset
                            if agent2._scheduler_wake is not None:
                                agent2._scheduler_wake.set()

                        reply = await _wait_for_task_reply(replies, task_id, timeout=15.0)
                        await _wait_for(lambda: agent2.task_manager.get_task_info(task_id) is None, timeout=10.0)
                        # Confirm no duplicate reply arrives.
                        with pytest.raises(asyncio.TimeoutError):
                            await replies.get(timeout=0.4)

                        archived = agent2.task_manager.get_task_from_db(task_id)
                        assert archived is not None and archived["status"] == ProgressState.COMPLETED.value
                        # Reconstruct budget from archived execution history: still charged once (5 minutes).
                        assert WaitLedger.from_history(archived["main_task"]).total_minutes == 5
                        assert second_model.wait_calls == 0
                        assert second_model.answer_calls == 1
                        # Stop agent2 before leaving own_nats_server_process so its graceful shutdown talks to live NATS.
                        await _stop_agents(second_agent_tasks)
                        second_agent_tasks.clear()
                        return reply, second_model, db_path, first_agent_id, second_agent_id
    finally:
        current_process_identity.cache_clear()
        if first_process is not None:
            with contextlib.suppress(Exception):
                await _sigkill_process(first_process)
        with contextlib.suppress(Exception):
            await _stop_agents(second_agent_tasks)
        set_agent_ref(None)


async def test_power_loss_restart_before_resume_at_waits_and_resumes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P4 (1): power loss before resume_at -> restarted agent keeps task WAITING, then resumes once at resume_at."""

    reply, model, _db_path, _first_id, _second_id = await _run_power_loss_scenario(
        tmp_path,
        reason="Portainer redeploy",
        restart_offset=timedelta(minutes=2),
        resume_offset=timedelta(minutes=5, seconds=12),
        different_instance_id=False,
        monkeypatch=monkeypatch,
    )
    answer = str(reply.answer or "")
    assert answer.startswith("RESUMED_OK|")
    assert "5-minute wait (Portainer redeploy) ended" in answer
    assert "after 5.2 minutes" in answer
    assert (BASE_TIME + timedelta(minutes=5, seconds=12)).isoformat() in answer
    assert model.answer_calls == 1


async def test_power_loss_restart_long_after_resume_at_resumes_promptly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P4 (2): power loss + 3-hour outage -> restarted agent resumes promptly once with real waited time."""

    reply, model, _db_path, _first_id, _second_id = await _run_power_loss_scenario(
        tmp_path,
        reason="DB migration",
        restart_offset=timedelta(hours=3),
        resume_offset=None,
        different_instance_id=False,
        monkeypatch=monkeypatch,
    )
    answer = str(reply.answer or "")
    assert answer.startswith("RESUMED_OK|")
    assert "5-minute wait (DB migration) ended" in answer
    assert "after 180 minutes" in answer
    assert (BASE_TIME + timedelta(hours=3)).isoformat() in answer
    assert model.answer_calls == 1


async def test_power_loss_different_instance_id_adopts_waiting_task_and_resumes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """P4 (3) & C02: power loss + fresh instance id -> real owner_liveness proves old process gone, adopts WAITING task, resumes once."""

    with caplog.at_level(logging.INFO):
        reply, model, db_path, first_id, second_id = await _run_power_loss_scenario(
            tmp_path,
            reason="container rollout",
            restart_offset=timedelta(minutes=2),
            resume_offset=timedelta(minutes=5, seconds=6),
            different_instance_id=True,
            monkeypatch=monkeypatch,
        )

    assert first_id != second_id
    assert "event=owner_liveness_decided" in caplog.text
    assert f"owner_agent_id={first_id}" in caplog.text
    assert "evidence=process_gone" in caplog.text
    answer = str(reply.answer or "")
    assert answer.startswith("RESUMED_OK|")
    assert "5-minute wait (container rollout) ended" in answer
    assert "after 5.1 minutes" in answer
    assert model.answer_calls == 1
    assert first_id not in [owner for owner, _ in _snapshot_runtime_rows(db_path)]
