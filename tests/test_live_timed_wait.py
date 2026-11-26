"""Opt-in live end-to-end test for a real 1-minute timed wait (A8).

Runs a real ``TeamArchitect`` ``UniversalAgent`` connected to a NATS server
with the real ``wait`` tool and a real configured model (no mocked model, no
patched clock). A one-time schedule due immediately instructs the agent to call
``wait(minutes=1, ...)`` and then report the ``now_utc`` timestamp returned by
``wait``. The test verifies that:

1. The scheduled run starts and pauses with a persisted ``timed_wait`` blocker.
2. The worker's execution slot is freed while waiting (``current_in_progress_task_id``
   is ``None``, ``can_start_new_task()`` and ``may_take_pool_task()`` are ``True``).
3. After 1 real minute passes, the scheduler resumes the paused turn in the
   same task and the final response reports a UTC time at least 1 minute after
   the wait started.

Run with ``RUN_LIVE_TIMED_WAIT=1`` against a private NATS server (``NATS_URL`` /
``NATS_SERVERS``). Configuration and model credentials are loaded only inside
the test process from ``TEAM_NATS_LIVE_CONFIG`` / ``../ua-private-config`` and
``TEAM_NATS_LIVE_ENV`` / ``../ua-private-config/*.env`` via ``ModelKeys``, never
printed or logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

from assistant_gateway.runtime_config import load_runtime_config_file
from config_loader import resolve_agent_type_settings
from custom_agents.agent_instructions import shared_agent_base_prompt, strip_access_prompt
from custom_agents.model_keys import ModelKeyError, ModelKeys
from custom_agents.pool_intake import may_take_pool_task
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
from custom_agents.task_human_interaction_blockers import get_human_interaction_blocker
from custom_agents.timed_waits import TIMED_WAIT_KIND, parse_utc
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType, ScheduleManagementClass
from custom_tools.providers.timed_waits import wait
from custom_tools.runtime_context import set_agent_ref
from id_generation import new_id
from model_providers.endpoints import openai_compatible_endpoint
from test_live_multi_agent_state_boundaries import (
    _agent,
    _connect_nats,
    _start_agent,
    _stop_agents,
    _wait_for,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_CONFIG_DIR = PROJECT_ROOT.parent / "ua-private-config"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.live,
    pytest.mark.external,
    pytest.mark.nats,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TIMED_WAIT") != "1",
        reason="Set RUN_LIVE_TIMED_WAIT=1 to run the live 1-minute timed wait test.",
    ),
]

_ISO_UTC_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)


def _find_existing_file(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved
    return None


def _load_live_env_values() -> dict[str, str]:
    explicit = os.getenv("TEAM_NATS_LIVE_ENV") or os.getenv("UA_LIVE_MODEL_ENV_FILE")
    candidates = (
        [Path(explicit)]
        if explicit
        else [
            PRIVATE_CONFIG_DIR / "hosted-model.env",
            PRIVATE_CONFIG_DIR / "team.env",
            PRIVATE_CONFIG_DIR / "debug.env",
            PROJECT_ROOT / "debug.env",
        ]
    )
    merged: dict[str, str] = {}
    for candidate in reversed(candidates):
        resolved = candidate.expanduser().resolve()
        if not resolved.is_file():
            continue
        for key, value in dotenv_values(resolved).items():
            if key and value:
                merged[str(key)] = str(value).strip()
    return merged


def _build_live_model() -> tuple[Any, dict[str, Any]]:
    explicit_cfg = (
        os.getenv("TEAM_NATS_LIVE_CONFIG")
        or os.getenv("UA_LIVE_CONFIG")
        or os.getenv("UA_LIVE_INSTANCE_CONFIG")
    )
    cfg_candidates = (
        [Path(explicit_cfg)]
        if explicit_cfg
        else [
            PRIVATE_CONFIG_DIR / "instance_config.yaml",
            PRIVATE_CONFIG_DIR / "config.yml",
            PRIVATE_CONFIG_DIR / "config.yaml",
            PROJECT_ROOT / "config.yaml",
        ]
    )
    config_path = _find_existing_file(cfg_candidates)
    if config_path is None:
        pytest.skip("No live instance config found in TEAM_NATS_LIVE_CONFIG or ../ua-private-config.")

    env_values = _load_live_env_values()

    def _env_lookup(name: str) -> str | None:
        return env_values.get(name) or str(os.getenv(name) or "").strip() or None

    config = load_runtime_config_file(config_path)
    settings = resolve_agent_type_settings(config, "TeamArchitect")
    provider = str(settings.get("model_provider") or "").strip()
    model_id = str(settings.get("model_id") or "").strip()
    if not provider or not model_id:
        pytest.skip("TeamArchitect model_provider or model_id is missing in live config.")

    model_keys = ModelKeys(None, env_lookup=_env_lookup)
    if provider == "litellm":
        api_base = str(settings.get("litellm_api_base") or "").strip()
        if not api_base:
            pytest.skip("Configured litellm_api_base is empty.")
        try:
            model_keys.resolve(openai_compatible_endpoint(api_base))
        except ModelKeyError:
            pytest.skip("No LITELLM_API_KEY available for the configured live endpoint.")

    factory_kwargs: dict[str, Any] = {
        "provider": provider,
        "model_id": model_id,
        "max_output_tokens": min(int(settings.get("max_output_tokens") or 1024), 2048),
        "request_timeout_seconds": 120,
        "stream_idle_timeout_seconds": 60,
        "model_call_timeout_seconds": 120,
        "model_keys": model_keys,
    }
    for key in (
        "litellm_api_base",
        "litellm_api_mode",
        "reasoning_effort",
        "reasoning_summary",
        "context_window_tokens",
    ):
        if settings.get(key) is not None:
            factory_kwargs[key] = settings[key]

    return build_strands_model(StrandsModelFactoryConfig(**factory_kwargs)), settings


def _parse_reported_utc(text: str) -> datetime | None:
    for match in _ISO_UTC_RE.finditer(text or ""):
        token = match.group(0).replace(" ", "T")
        parsed = parse_utc(token)
        if parsed is not None:
            return parsed
    return None


def _find_timed_wait_task(tasks: Any) -> tuple[str, dict[str, Any]] | None:
    blockers = dict(tasks.task_state.human_interaction_blockers or {})
    for task_id, record in blockers.items():
        if isinstance(record, dict) and record.get("kind") == TIMED_WAIT_KIND:
            return str(task_id), dict(record)
    return None


async def test_live_scheduled_one_time_job_waits_one_minute_and_resumes_in_same_task() -> None:
    """A8: real one-time scheduled job pauses 1 minute, frees the worker, resumes same task, reports UTC time."""

    live_model, settings = _build_live_model()
    team_id = new_id("team_live_tw_a8")
    manager_id = new_id("manager")
    schedule_id = new_id("sched_live_tw")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "workspace").mkdir()
        with patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=live_model):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=tmp_path / "manager.db",
                workspace_dir=tmp_path / "workspace",
                is_lead=True,
            )
            manager.authorization_config = {"enabled": False}
            manager.model_provider = str(settings["model_provider"])
            manager.model_id = str(settings["model_id"])
            manager.backstory = strip_access_prompt(shared_agent_base_prompt)
            manager.tools = [wait]
            manager._strands_parts.replace_before_next_turn = True

            agent_tasks: list[tuple[Any, asyncio.Task[Any]]] = []
            connection = await _connect_nats(team_id)
            set_agent_ref(manager)
            try:
                # Register a due one-time schedule before starting the agent so the real scheduler loop claims and fires it.
                due_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
                manager._ensure_schedule_store().add(
                    ScheduledMessage(
                        schedule_id=schedule_id,
                        schedule_name="Live 1-minute timed wait check",
                        management_class=ScheduleManagementClass.USER,
                        repeat_count=1,
                        next_fire_at=due_at,
                        payload=Task(
                            task_id=new_id("task_live_sched"),
                            task_type=MessageMetaType.REQUEST,
                            problem_statement=(
                                "You must call the `wait` tool once with minutes=1 and reason='live timed wait check'. "
                                "Do not finish before calling `wait`. After `wait` returns, finish with a task_response "
                                "whose answer states the exact `now_utc` ISO-8601 UTC timestamp returned by `wait`."
                            ),
                        ),
                        payload_header=MessageHeader(
                            from_agent_id="scheduler",
                            to_routing_path=manager_id,
                        ),
                        payload_meta=MessageMeta(
                            from_agent_name="scheduler",
                            to_named_agent_type="TeamArchitect",
                            conversation_id="conv_live_timed_wait",
                            requester_type=RequesterType.USER_REQUESTER,
                            is_response_requested=False,
                            gateway_context=GatewayContext(
                                canonical_user_id="owner",
                                session_key="session_live_timed_wait",
                            ),
                        ),
                    ),
                    claim_team_id=team_id,
                    claim_named_agent_type="TeamArchitect",
                )

                agent_tasks.append((manager, await _start_agent(manager)))
                if manager._scheduler_wake is not None:
                    manager._scheduler_wake.set()
                tasks = manager.task_manager

                # 1. Wait for the live model to call wait(1, ...) and persist the timed_wait blocker.
                await _wait_for(lambda: _find_timed_wait_task(tasks) is not None, timeout=60.0)
                found = _find_timed_wait_task(tasks)
                assert found is not None
                paused_task_id, blocker = found
                assert blocker["minutes"] == 1
                requested_at = parse_utc(blocker["requested_at"])
                resume_at = parse_utc(blocker["resume_at"])
                assert requested_at is not None and resume_at is not None
                assert resume_at - requested_at == timedelta(minutes=1)

                # 2. Verify the worker's execution slot is freed while waiting.
                await _wait_for(
                    lambda: tasks.task_state.current_in_progress_task_id is None
                    and tasks.can_start_new_task()
                    and may_take_pool_task(manager),
                    timeout=10.0,
                )

                # 3. Wait in real wall-clock time for the 1-minute timer to expire and the same task to archive as COMPLETED.
                await _wait_for(
                    lambda: tasks.get_task_from_db(paused_task_id, include_all_agents=True) is not None,
                    timeout=150.0,
                )
                completed_at = datetime.now(timezone.utc)
                archived = tasks.get_task_from_db(paused_task_id, include_all_agents=True)
                assert archived is not None
                assert archived["status"] == ProgressState.COMPLETED.value, archived
                reply_task = archived["main_task"]
                assert reply_task.task_id == paused_task_id

                # 4. Verify real elapsed time (>= 1 minute), clean blocker removal, and reported UTC timestamp.
                assert completed_at - requested_at >= timedelta(seconds=59)
                await _wait_for(
                    lambda: get_human_interaction_blocker(tasks, paused_task_id) is None,
                    timeout=10.0,
                )
                reported_utc = _parse_reported_utc(reply_task.answer or "")
                assert reported_utc is not None, f"Expected UTC timestamp in answer, got: {reply_task.answer!r}"
                assert reported_utc >= requested_at + timedelta(seconds=59), (
                    f"Reported time {reported_utc.isoformat()} was less than 1 minute after {requested_at.isoformat()}"
                )
            finally:
                with contextlib.suppress(Exception):
                    await _stop_agents(agent_tasks)
                set_agent_ref(None)
                await connection.close()
