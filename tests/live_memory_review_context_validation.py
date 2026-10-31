# Direct execution bootstraps the repository path before project imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import tempfile
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - live utility fallback
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import nats

from access_control.identity import (
    ENV_VERIFY_KEYS,
    ISSUER_GATEWAY,
    ContextSigner,
    generate_signing_key,
    verify_keys_value,
)
from config_loader import load_config
from custom_agents import access_runtime
from custom_messages.contracts.runtime_v1 import MEM_REVIEW_AGENT_TYPE
from custom_agents.memory_review_runtime import enqueue_stale_conversation_review_jobs
from custom_agents.memory_review_store import MemoryReviewStore
from custom_agents.request_prompt_context import (
    build_request_conversation_context,
    build_request_time_context,
)
from custom_agents.scheduled_runtime import _append_suppressed_completion_instruction
from custom_agents.conversation_store import ConversationStore
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.enums import RequesterType
from custom_tools.providers.memory import resolve_memory_review_job
from tests.task_work_support import set_agent_ref
from live_storage_utils import (
    reset_postgres_storage,
    resolve_live_storage_settings,
    storage_identifier,
)
from storage.conversation_repository import TURN_MEMORY_REVIEW_IGNORED
from id_generation import new_id_suffix


RESULT_DIR = PROJECT_ROOT / "test_results" / "live_memory_review_context_validation"
logger = logging.getLogger("live_memory_review_context_validation")
# The person the live conversation belongs to; its review jobs are made for them.
LIVE_PERSON = "canonical_live_memory_review"


@contextmanager
def _task_for_person(storage_settings: Any, team_id: str, person_id: str) -> Iterator[Any]:
    """A running task signed for ``person_id`` with a key made for this check.

    Memory review tools act only on jobs made for the person the task works
    for, read from its signed context. The process trusts the temporary key
    only inside this block, then the previous verification keys come back.
    """

    private, public = generate_signing_key(ISSUER_GATEWAY)
    previous = os.environ.get(ENV_VERIFY_KEYS)
    os.environ[ENV_VERIFY_KEYS] = verify_keys_value([public])
    access_runtime._runtime_access.cache_clear()
    signed = ContextSigner.from_value(ISSUER_GATEWAY, private).issue(
        team_id=team_id, person_id=person_id, person_roles=("member",)
    )
    meta = SimpleNamespace(gateway_context=SimpleNamespace(task_context=signed, canonical_user_id=person_id))
    try:
        yield SimpleNamespace(
            storage_settings=storage_settings,
            current_task_id="live_memory_review_task",
            get_task_info=lambda _task_id: SimpleNamespace(meta=meta),
        )
    finally:
        if previous is None:
            os.environ.pop(ENV_VERIFY_KEYS, None)
        else:
            os.environ[ENV_VERIFY_KEYS] = previous
        access_runtime._runtime_access.cache_clear()


def _redact_url(raw_url: str) -> str:
    parts = urlsplit(raw_url)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, hostinfo = netloc.rsplit("@", 1)
        if ":" in userinfo:
            username, _password = userinfo.split(":", 1)
            userinfo = f"{username}:***"
        netloc = f"{userinfo}@{hostinfo}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def _verify_live_nats(config_path: Path) -> str:
    config = load_config(str(config_path))
    servers = config.get("transport.nats_servers") or [config.get("transport.nats_url", "nats://127.0.0.1:4222")]
    errors: list[str] = []
    for server in servers:
        parts = urlsplit(str(server))
        host = parts.hostname or "127.0.0.1"
        port = int(parts.port or 4222)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2)
            writer.close()
            await writer.wait_closed()
            kwargs: dict[str, Any] = {
                "servers": [str(server)],
                "connect_timeout": 2,
                "allow_reconnect": False,
                "max_reconnect_attempts": 0,
                "name": "live-memory-review-context-validation",
            }
            if os.getenv("NATS_USER"):
                kwargs["user"] = os.getenv("NATS_USER")
            if os.getenv("NATS_PASSWORD"):
                kwargs["password"] = os.getenv("NATS_PASSWORD")
            connection = await asyncio.wait_for(nats.connect(**kwargs), timeout=5)
            try:
                await connection.flush(timeout=2)
            finally:
                await connection.close()
            return _redact_url(str(server))
        except Exception as exc:
            errors.append(f"{_redact_url(str(server))}: {type(exc).__name__}: {exc}")
    raise ConnectionError("Configured NATS endpoint is not reachable: " + "; ".join(errors))


def _reset_sqlite_database(path: Path) -> None:
    for candidate in (path, path.with_suffix(path.suffix + "-wal"), path.with_suffix(path.suffix + "-shm")):
        if candidate.exists():
            candidate.unlink()


def _append_completed_turn(
    store: ConversationStore,
    *,
    conversation_id: str,
    task_id: str,
    followup_to_task_id: str | None,
    requester_type: str,
    problem_text: str,
    owner_agent_id: str,
    owner_named_agent_type: str,
    correlation_id: str,
) -> None:
    store.append_turn(
        conversation_id=conversation_id,
        task_id=task_id,
        followup_to_task_id=followup_to_task_id,
        owner_agent_id=owner_agent_id,
        owner_named_agent_type=owner_named_agent_type,
        requester_name="live_user" if requester_type == RequesterType.USER_REQUESTER.value else owner_named_agent_type,
        requester_id="live_user" if requester_type == RequesterType.USER_REQUESTER.value else owner_agent_id,
        requester_type=requester_type,
        correlation_id=correlation_id,
        problem_text=problem_text,
        gateway_context=(
            GatewayContext(
                gateway_instance_name="live_gateway",
                session_key="live_memory_review_context",
                channel_name="cli",
                user_id="live_user",
                canonical_user_id=LIVE_PERSON,
                chat_id="live_chat",
                delivery_mode="interactive",
            ).model_dump(mode="json")
            if requester_type == RequesterType.USER_REQUESTER.value
            else None
        ),
        task_status="accepted",
    )
    store.update_turn_completion(
        task_id=task_id,
        task_status="completed",
        final_answer_text=f"Live answer for {task_id}",
        turn_summary_text=f"Live summary for {task_id}",
        user_request_summary=problem_text,
        assistant_response_summary=f"Live answer for {task_id}",
        context_summary_text=f"Live context summary for {task_id}",
    )


def _exercise_memory_review_context(storage_settings: Any) -> dict[str, Any]:
    conversation_store = ConversationStore(db_path="", storage_settings=storage_settings, logger=logger, artifacts_dir=Path(tempfile.mkdtemp(prefix="ua-conversation-artifacts-")))
    memory_review_store = MemoryReviewStore(db_path="", storage_settings=storage_settings, logger=logger)
    suffix = new_id_suffix()
    conversation_id = f"conversation_live_memreview_{suffix}"
    team_id = "team_live_memreview"
    manager_agent_id = "manager_live_memreview"

    conversation_store.ensure_thread(
        conversation_id=conversation_id,
        team_id=team_id,
        owner_agent_id=manager_agent_id,
        owner_agent_name="live-manager",
        requester_name="live_user",
        requester_id="live_user",
        root_task_id=f"task_live_user_1_{suffix}",
        subject_preview="live memory review duplicate prevention",
        owner_canonical_user_id=LIVE_PERSON,
    )
    user_task_1 = f"task_live_user_1_{suffix}"
    memreview_task_between = f"task_live_memreview_between_{suffix}"
    user_task_2 = f"task_live_user_2_{suffix}"
    memreview_task_latest = f"task_live_memreview_latest_{suffix}"
    _append_completed_turn(
        conversation_store,
        conversation_id=conversation_id,
        task_id=user_task_1,
        followup_to_task_id=None,
        requester_type=RequesterType.USER_REQUESTER.value,
        problem_text="Live user turn one about durable memory.",
        owner_agent_id=manager_agent_id,
        owner_named_agent_type="TeamArchitect",
        correlation_id=f"corr_live_user_1_{suffix}",
    )
    _append_completed_turn(
        conversation_store,
        conversation_id=conversation_id,
        task_id=memreview_task_between,
        followup_to_task_id=user_task_1,
        requester_type=RequesterType.AGENT_REQUESTER.value,
        problem_text="Review the queued memory job below.\nLive internal MemReview prompt between user turns.",
        owner_agent_id="memreview_live_1",
        owner_named_agent_type=MEM_REVIEW_AGENT_TYPE,
        correlation_id=f"memory_review_between_{suffix}",
    )
    _append_completed_turn(
        conversation_store,
        conversation_id=conversation_id,
        task_id=user_task_2,
        followup_to_task_id=memreview_task_between,
        requester_type=RequesterType.USER_REQUESTER.value,
        problem_text="Live user turn two about durable memory.",
        owner_agent_id=manager_agent_id,
        owner_named_agent_type="TeamArchitect",
        correlation_id=f"corr_live_user_2_{suffix}",
    )
    _append_completed_turn(
        conversation_store,
        conversation_id=conversation_id,
        task_id=memreview_task_latest,
        followup_to_task_id=user_task_2,
        requester_type=RequesterType.AGENT_REQUESTER.value,
        problem_text="Review the queued memory job below.\nLive internal MemReview prompt as raw latest task.",
        owner_agent_id="memreview_live_1",
        owner_named_agent_type=MEM_REVIEW_AGENT_TYPE,
        correlation_id=f"memory_review_latest_{suffix}",
    )
    conversation_store.update_thread_compaction(
        conversation_id,
        latest_task_id=memreview_task_latest,
        status="completed",
    )
    with conversation_store._repository._connect() as conn:
        conn.execute(
            "UPDATE conversation_threads SET updated_at = ? WHERE conversation_id = ?",
            ("2026-01-01T00:00:00+00:00", conversation_id),
        )
        conn.commit()

    between_turn = conversation_store.get_turn_by_task_id(memreview_task_between)
    latest_turn = conversation_store.get_turn_by_task_id(memreview_task_latest)
    if between_turn["memory_review_status"] != TURN_MEMORY_REVIEW_IGNORED:
        raise AssertionError(f"Expected between MemReview turn to be ignored: {between_turn}")
    if latest_turn["memory_review_status"] != TURN_MEMORY_REVIEW_IGNORED:
        raise AssertionError(f"Expected latest MemReview turn to be ignored: {latest_turn}")

    window = conversation_store.get_memory_review_window(
        conversation_id=conversation_id,
        max_unreviewed_turns=2,
        allow_partial=False,
    )
    if window is None:
        raise AssertionError("Expected a memory review window for the two user-origin turns.")
    if window["unreviewed_turn_indexes"] != [1, 3]:
        raise AssertionError(f"Expected only user turns [1, 3] in the unreviewed window: {window}")
    if [turn["turn_index"] for turn in window["turns"]] != [1, 3]:
        raise AssertionError(f"Expected review payload source turns [1, 3], got {window['turns']!r}")

    candidates = conversation_store.list_conversation_memory_review_candidates(team_id=team_id)
    matching_candidates = [candidate for candidate in candidates if candidate["conversation_id"] == conversation_id]
    if len(matching_candidates) != 1:
        raise AssertionError(f"Expected one stale candidate for live conversation, got {matching_candidates!r}")
    candidate = matching_candidates[0]
    if candidate["last_unreviewed_turn"] != 3:
        raise AssertionError(f"Expected stale candidate to anchor to latest eligible user turn 3: {candidate}")

    jobs = enqueue_stale_conversation_review_jobs(
        memory_review_store=memory_review_store,
        conversation_store=conversation_store,
        team_id=team_id,
        named_agent_type="TeamArchitect",
        agent_id=manager_agent_id,
        memory_config={"review": {"periodic_turn_interval": 15, "stale_conversation_after_seconds": 60}},
        canonical_user_id=LIVE_PERSON,
        now_utc="2026-01-03T00:00:00+00:00",
        logger=logger,
    )
    if len(jobs) != 1:
        raise AssertionError(f"Expected one stale MemReview job, got {jobs!r}")
    job = memory_review_store.get_review_job(jobs[0].job_id)
    if job is None:
        raise AssertionError("Enqueued live MemReview job was not persisted.")
    payload = job.source_payload
    turn_indexes = [turn["turn_index"] for turn in payload.get("turns", [])]
    if payload.get("task_id") != user_task_2:
        raise AssertionError(f"Expected stale job to anchor to user task 2, got {payload.get('task_id')!r}")
    if turn_indexes != [1, 3]:
        raise AssertionError(f"Expected live MemReview payload to exclude internal turns, got {turn_indexes!r}")

    duplicate_jobs = enqueue_stale_conversation_review_jobs(
        memory_review_store=memory_review_store,
        conversation_store=conversation_store,
        team_id=team_id,
        named_agent_type="TeamArchitect",
        agent_id=manager_agent_id,
        memory_config={"review": {"periodic_turn_interval": 15, "stale_conversation_after_seconds": 60}},
        canonical_user_id=LIVE_PERSON,
        now_utc="2026-01-03T00:00:00+00:00",
        logger=logger,
    )
    if duplicate_jobs:
        raise AssertionError(f"Expected queued turns to prevent duplicate stale jobs, got {duplicate_jobs!r}")

    memory_review_store.claim_review_jobs_for_role(
        owner_team_id=team_id,
        owner_named_agent_type=MEM_REVIEW_AGENT_TYPE,
        claimer_agent_id="memreview_live_other",
        limit=1,
    )
    with _task_for_person(storage_settings, team_id, LIVE_PERSON) as task_manager:
        set_agent_ref(
            type(
                "LiveMemReviewAgentRef",
                (),
                {
                    "id": "memreview_live_current",
                    "team_id": team_id,
                    "named_agent_type": MEM_REVIEW_AGENT_TYPE,
                    "_memory_review_store": memory_review_store,
                    "_conversation_store": conversation_store,
                    "task_manager": task_manager,
                    "authorization_config": {"enabled": False},
                },
            )()
        )
        try:
            resolve_result = json.loads(resolve_memory_review_job(job.review_job_id))
        finally:
            set_agent_ref(None)
    if resolve_result.get("error_code") != "claimed_by_other_agent":
        raise AssertionError(f"Expected typed claimed_by_other_agent result, got {resolve_result!r}")

    return {
        "conversation_id": conversation_id,
        "review_job_id": job.review_job_id,
        "window_unreviewed_turn_indexes": window["unreviewed_turn_indexes"],
        "job_payload_turn_indexes": turn_indexes,
        "duplicate_job_count": len(duplicate_jobs),
        "resolve_error_code": resolve_result.get("error_code"),
    }


def _exercise_prompt_context(config: Any) -> dict[str, Any]:
    request_timezone = str(config.get("agent.default_request_timezone", "") or "America/Chicago")
    time_context = build_request_time_context(MessageMeta(request_timezone=request_timezone))
    if "Current requester-local weekday:" not in time_context:
        raise AssertionError(f"Requester-local weekday missing from time context: {time_context!r}")

    recall_text = "What did we talk about in previous conversations?"
    user_context = build_request_conversation_context(
        MessageMeta(conversation_id="conversation_live_user", requester_type=RequesterType.USER_REQUESTER),
        request_text=recall_text,
    )
    timer_context = build_request_conversation_context(
        MessageMeta(
            conversation_id="conversation_live_timer",
            requester_type=RequesterType.TIMER_REQUESTER,
            msg_correlation_id="schedule_corr_live",
        ),
        request_text=recall_text,
    )
    if "list_conversations" not in user_context:
        raise AssertionError("User-origin recall request did not receive the cross-conversation retrieval hint.")
    if "list_conversations" in timer_context:
        raise AssertionError("Timer-origin generated instructions received the user-only recall hint.")

    scheduled_prompt = _append_suppressed_completion_instruction("Live scheduled check.")
    if "suppress_outbound_dispatch=true" not in scheduled_prompt or "task.answer" not in scheduled_prompt:
        raise AssertionError(f"Scheduled completion contract missing from prompt: {scheduled_prompt!r}")

    return {
        "request_timezone": request_timezone,
        "weekday_line_present": True,
        "user_recall_hint_present": True,
        "timer_recall_hint_absent": True,
        "scheduled_completion_contract_present": True,
    }


def run_validation(*, config_path: Path) -> dict[str, Any]:
    config = load_config(str(config_path))
    nats_servers = asyncio.run(_verify_live_nats(config_path))
    storage_settings = resolve_live_storage_settings(
        sqlite_fallback_path=RESULT_DIR / "task_history.db",
        workspace_dir=PROJECT_ROOT,
        config_path=config_path,
    )
    if storage_settings.backend == "postgres":
        reset_postgres_storage(storage_settings)
    else:
        _reset_sqlite_database(Path(storage_settings.sqlite_path or RESULT_DIR / "task_history.db"))

    storage_check = _exercise_memory_review_context(storage_settings)
    prompt_check = _exercise_prompt_context(config)
    return {
        "status": "passed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "config_path": str(config_path.resolve()),
        "nats_servers": nats_servers,
        "storage_backend": storage_settings.backend,
        "storage_locator": storage_identifier(storage_settings),
        "memory_review_context": storage_check,
        "prompt_context": prompt_check,
    }


def _write_report(report: dict[str, Any], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate live MemReview context eligibility and prompt-context guards.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env", default="debug.env")
    parser.add_argument("--report-path", default=str(RESULT_DIR / "report.json"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    env_path = Path(args.env)
    config_path = Path(args.config)
    report_path = Path(args.report_path)
    if env_path.exists() and load_dotenv is not None:
        load_dotenv(env_path, override=True)
        logger.info("Loaded environment variables from %s", env_path)
    elif env_path.exists():
        logger.warning("python-dotenv is unavailable; %s was not loaded.", env_path)
    else:
        logger.warning("Requested env file does not exist: %s", env_path)
    if RESULT_DIR.exists():
        shutil.rmtree(RESULT_DIR)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        report = run_validation(config_path=config_path)
    except Exception as exc:
        report = {
            "status": "failed",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "config_path": str(config_path.resolve()),
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        _write_report(report, report_path)
        logger.exception("Live memory review context validation failed.")
        raise
    _write_report(report, report_path)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    logger.info("Live memory review context validation passed. Report written to %s", report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
