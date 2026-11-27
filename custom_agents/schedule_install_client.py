"""Client helpers for installing schedules through ``schedules.install`` on ``svc.team``.

Agents never write new schedule definitions directly to ``ScheduleStore``: every user
schedule, turn-action schedule, Objective work schedule, and recovery checkpoint goes
through ``schedules.install`` carrying the active task's verified protocol 1
``AuthorityToken`` (and opaque ``delivery_ref`` when the task originated in a chat).
``team_services.schedules`` verifies the token, enforces 13C.5 vouching (``owner_vouched = 1``
only for gateway-signed tokens; scheduler-signed runs and permits install unvouched),
persists the row and its reply in one transaction, and synchronously runs initial permit
evaluation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from custom_agents.agent_requests import agent_blob_store, ask_team
from custom_agents.schedule_store import (
    SCHEDULE_INSTALLED,
    SCHEDULE_REPLACED,
    ScheduleWriteConflict,
    owner_canonical_user_id_for_schedule,
    schedule_record_digest,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.enums import ScheduleManagementClass
from custom_messages.wire.encoding import format_timestamp
from id_generation import new_id

logger = logging.getLogger(__name__)

INLINE_INSTRUCTION_MAX_CHARS = 32_768
INSTALL_TIMEOUT_SECONDS = 30.0
INSTALL_SEND_ATTEMPTS = 2


def logical_install_request_id(
    schedule_id: str,
    *,
    mode: str,
    authority_token: Optional[Mapping[str, Any]] = None,
    seen_version: Optional[str] = None,
) -> str:
    """One stable ``request_id`` per logical install so resends hit stored outcomes instead of duplicating (F5).

    Includes the token's ``person`` reference so two different people colliding on the same
    ``schedule_id`` never share a retry key in ``protocol1_operation_outcomes``.
    """

    person_ref = str((authority_token or {}).get("person") or "")
    if mode in ("create", "ensure"):
        digest = hashlib.sha256(f"{mode}:{person_ref}:{schedule_id}".encode("utf-8")).hexdigest()[:24]
        return f"sched_install_{digest}"
    if mode == "replace" and seen_version:
        digest = hashlib.sha256(f"{mode}:{person_ref}:{schedule_id}:{seen_version}".encode("utf-8")).hexdigest()[:24]
        return f"sched_install_{digest}"
    return new_id("sched_install")


class ScheduleInstallError(ValueError):
    """A typed client-side refusal from ``schedules.install`` carrying the wire outcome."""

    def __init__(self, outcome: str, schedule_id: str, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.schedule_id = schedule_id


def extract_task_delivery_ref(meta: Any) -> Optional[str]:
    """Return the active task's opaque ``delivery_ref`` (``dr_...``) when present, else None."""

    gateway_context = getattr(meta, "gateway_context", None)
    if gateway_context is None:
        return None
    ref = getattr(gateway_context, "delivery_ref", None)
    if not isinstance(ref, str):
        return None
    cleaned = ref.strip()
    return cleaned or None


def extract_task_authority_token(meta: Any) -> dict[str, Any]:
    """Return the active task's protocol 1 authority token as a wire dict, or raise ``PermissionError``."""

    gateway_context = getattr(meta, "gateway_context", None)
    if gateway_context is None:
        raise PermissionError("Current task carries no verified authority token to install a schedule.")
    token = getattr(gateway_context, "task_context", None)
    if token is None:
        schedule_run = getattr(gateway_context, "schedule_run", None)
        token = getattr(schedule_run, "permit", None) if schedule_run is not None else None
    if token is None:
        raise PermissionError("Current task carries no verified authority token to install a schedule.")
    if hasattr(token, "model_dump"):
        return dict(token.model_dump(mode="json", exclude_none=True))
    if isinstance(token, Mapping):
        return dict(token)
    raise PermissionError("Current task authority token has an unsupported format.")


def _wire_timestamp(value: Any) -> str:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return format_timestamp(moment)


def _wire_run_notes(schedule: ScheduledMessage) -> Optional[dict[str, Any]]:
    memory = schedule.schedule_memory
    if memory is None:
        return None
    policy: dict[str, Any] = {
        "notify": memory.policy.notify,
        "notify_on_failure": bool(memory.policy.notify_on_failure),
        "evidence_required": str(memory.policy.evidence_required or ""),
    }
    if memory.policy.custom:
        policy["custom_json"] = json.dumps(memory.policy.custom, ensure_ascii=False, separators=(",", ":"))
    state: dict[str, Any] = {
        "summary": str(memory.state.summary or ""),
        "confidence": str(memory.state.confidence or "unknown"),
    }
    if memory.state.last_observed_at:
        state["last_observed_at"] = _wire_timestamp(memory.state.last_observed_at)
    if memory.state.last_changed_at:
        state["last_changed_at"] = _wire_timestamp(memory.state.last_changed_at)
    if memory.state.domain:
        state["domain_json"] = json.dumps(memory.state.domain, ensure_ascii=False, separators=(",", ":"))
    notes: dict[str, Any] = {
        "schedule_kind": str(memory.schedule_kind or ""),
        "policy": policy,
        "state": state,
    }
    if memory.domain_schema_hint:
        notes["domain_schema_hint_json"] = json.dumps(
            memory.domain_schema_hint, ensure_ascii=False, separators=(",", ":")
        )
    return notes


def schedule_install_body(
    schedule: ScheduledMessage,
    *,
    mode: str,
    request_id: str,
    seen_version: Optional[str] = None,
    delivery_ref: Optional[str] = None,
    default_timezone: Optional[str] = None,
    instruction_content: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a contract-valid ``schedules.install`` request body from a ``ScheduledMessage``."""

    instruction_text = str(getattr(schedule.payload, "problem_statement", "") or "").strip()
    if instruction_content is None:
        instruction_content = {"type": "inline", "text": instruction_text}

    if schedule.interval_ms is not None:
        timing: dict[str, Any] = {"kind": "interval", "interval_ms": int(schedule.interval_ms)}
    elif schedule.cron_expression:
        zone = (
            schedule.cron_timezone
            or getattr(getattr(schedule, "payload_meta", None), "request_timezone", None)
            or default_timezone
            or "UTC"
        )
        timing = {"kind": "cron", "expression": schedule.cron_expression, "time_zone": str(zone)}
    else:
        when = schedule.next_fire_at or schedule.created_at
        timing = {"kind": "once", "at": _wire_timestamp(when)}

    body: dict[str, Any] = {
        "request_id": request_id,
        "schedule_id": schedule.schedule_id,
        "mode": mode,
        "management_class": schedule.management_class.value,
        "timing": timing,
        "enabled": bool(schedule.enabled),
        "action": {"type": "task", "instruction": instruction_content},
    }
    if seen_version:
        body["seen_version"] = seen_version
    if schedule.schedule_name:
        body["name"] = schedule.schedule_name
    if schedule.management_class == ScheduleManagementClass.SYSTEM and schedule.effective_system_schedule_kind is not None:
        body["system_schedule_kind"] = schedule.effective_system_schedule_kind.value
    if schedule.objective_id:
        body["objective_id"] = schedule.objective_id

    worker: dict[str, Any] = {}
    if schedule.claim_named_agent_type:
        worker["agent_type"] = schedule.claim_named_agent_type
    if schedule.claim_agent_id:
        worker["agent_id"] = schedule.claim_agent_id
    if worker:
        body["worker"] = worker

    if timing["kind"] == "once":
        body["repeat_count"] = 1
    else:
        body["repeat_count"] = 0 if schedule.repeat_count is None else int(schedule.repeat_count)

    if schedule.expires_at_utc:
        body["expires_at"] = _wire_timestamp(schedule.expires_at_utc)
    if schedule.paused_until:
        body["paused_until"] = _wire_timestamp(schedule.paused_until)

    run_notes = _wire_run_notes(schedule)
    if run_notes is not None:
        body["run_notes"] = run_notes

    if delivery_ref:
        deliver_to: dict[str, Any] = {"delivery_ref": delivery_ref}
        meta = schedule.payload_meta
        if meta is not None and meta.conversation_id:
            deliver_to["conversation_id"] = meta.conversation_id
        tz = (meta.request_timezone if meta is not None else None) or default_timezone
        if tz:
            deliver_to["time_zone"] = tz
        body["deliver_to"] = deliver_to
    return body


async def _instruction_content(
    agent: Any,
    text: str,
    *,
    authority_token: Mapping[str, Any],
    request_id: str,
    timeout: float,
) -> dict[str, Any]:
    if len(text) <= INLINE_INSTRUCTION_MAX_CHARS:
        return {"type": "inline", "text": text}
    blobs = agent_blob_store(agent, timeout=timeout)
    if blobs is None:
        raise RuntimeError(
            "Schedule instruction exceeds the 32,768-character inline limit and no blob store is available."
        )
    from custom_transport.protocol1.blobs import BlobOwner
    from custom_transport.protocol1.knowledge_blobs import encode_text_content

    person_ref = str(authority_token.get("person") or "")
    owner = BlobOwner(request_id=request_id, person_ref=person_ref)
    return await encode_text_content(text, blobs, owner, inline_limit=INLINE_INSTRUCTION_MAX_CHARS)


def _check_reply(schedule_id: str, reply: dict[str, Any]) -> dict[str, Any]:
    outcome = str(reply.get("outcome") or "")
    if outcome == "ok":
        return reply
    message = str(reply.get("message") or f"schedules.install refused with {outcome or 'unknown error'}.")
    if outcome == "changed_since_read":
        raise ScheduleWriteConflict("changed_since_read", schedule_id, message)
    if outcome == "not_allowed":
        raise PermissionError(message)
    if outcome in ("invalid_request", "id_in_use", "not_found", "not_system_managed"):
        raise ScheduleInstallError(outcome, schedule_id, message)
    raise RuntimeError(message)


def _is_retryable_no_answer(exc: Exception) -> bool:
    """Whether ``exc`` indicates a lost reply or temporary team-services unavailability worth one resend."""

    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    from assistant_gateway.application.team_commands import TeamServicesUnavailable
    from custom_transport.protocol1.live import ServiceUnavailable

    return isinstance(exc, (ServiceUnavailable, TeamServicesUnavailable))


def _stored_matches_install(
    stored: ScheduledMessage,
    schedule: ScheduledMessage,
    *,
    mode: str,
    seen_version: Optional[str],
    expected_team: str,
) -> Optional[str]:
    """Return the confirmed install state when ``stored`` matches this install, else None."""

    if expected_team and stored.claim_team_id and stored.claim_team_id != expected_team:
        return None
    expected_owner = owner_canonical_user_id_for_schedule(schedule)
    stored_owner = owner_canonical_user_id_for_schedule(stored)
    if expected_owner and stored_owner and stored_owner != expected_owner:
        return None
    if mode in ("create", "ensure"):
        return SCHEDULE_INSTALLED
    if mode != "replace":
        return None
    if seen_version and schedule_record_digest(stored) == seen_version:
        return None
    expected_instruction = str(getattr(schedule.payload, "problem_statement", "") or "").strip()
    stored_instruction = str(getattr(stored.payload, "problem_statement", "") or "").strip()
    same_def = (
        stored_instruction == expected_instruction
        and stored.interval_ms == schedule.interval_ms
        and stored.cron_expression == schedule.cron_expression
    )
    return SCHEDULE_REPLACED if same_def else None


async def _confirm_install_from_store(
    agent: Any,
    schedule: ScheduledMessage,
    *,
    mode: str,
    seen_version: Optional[str],
    request_id: str,
) -> Optional[dict[str, Any]]:
    """Re-read the local schedule store after a lost reply so committed installs are not treated as failed (F5)."""

    store = getattr(agent, "_schedule_store", None)
    if store is None or not hasattr(store, "get_scheduled_message"):
        return None
    try:
        stored = await asyncio.to_thread(store.get_scheduled_message, schedule.schedule_id)
    except Exception:
        return None
    if stored is None:
        return None
    expected_team = str(schedule.claim_team_id or getattr(agent, "team_id", "") or "")
    state = _stored_matches_install(stored, schedule, mode=mode, seen_version=seen_version, expected_team=expected_team)
    if state is None:
        return None
    is_vouched = bool(store.is_owner_vouched(stored.schedule_id)) if hasattr(store, "is_owner_vouched") else False
    logger.warning(
        "Schedule install got no reply from team services, but the schedule is present in the store; "
        "treating the install as committed. event=schedule_install_confirmed_from_store "
        "schedule_id=%s mode=%s request_id=%s",
        stored.schedule_id,
        mode,
        request_id,
    )
    return {
        "outcome": "ok",
        "state": state,
        "schedule_id": stored.schedule_id,
        "version": {
            "seen_version": schedule_record_digest(stored),
            "timing_revision": stored.timing_revision,
        },
        "next_fire_at": stored.next_fire_at,
        "owner_vouched": is_vouched,
    }


async def _send_install_with_resend(
    agent: Any,
    *,
    schedule_id: str,
    mode: str,
    req_id: str,
    body: dict[str, Any],
    authority_token: Mapping[str, Any],
    send_deadline: float,
) -> tuple[Optional[dict[str, Any]], Optional[Exception]]:
    """Send ``schedules.install`` up to ``INSTALL_SEND_ATTEMPTS`` times under one ``req_id`` and deadline."""

    loop = asyncio.get_running_loop()
    last_error: Optional[Exception] = None
    for attempt in range(1, INSTALL_SEND_ATTEMPTS + 1):
        remaining = send_deadline - loop.time()
        if remaining <= 0:
            last_error = last_error or TimeoutError(
                f"Schedule install {schedule_id} timed out before attempt {attempt}."
            )
            break
        attempt_timeout = remaining / (INSTALL_SEND_ATTEMPTS - attempt + 1)
        try:
            reply = await asyncio.wait_for(
                ask_team(agent, "schedules.install", body, authority=authority_token, timeout=attempt_timeout),
                timeout=attempt_timeout,
            )
            return _check_reply(schedule_id, reply), None
        except Exception as exc:
            if not _is_retryable_no_answer(exc):
                raise
            last_error = exc
            action = "resending with the same request id" if attempt < INSTALL_SEND_ATTEMPTS else "re-reading store"
            logger.warning(
                "Schedule install got no answer; %s. event=schedule_install_no_answer "
                "schedule_id=%s mode=%s request_id=%s attempt=%s of=%s error=%s",
                action,
                schedule_id,
                mode,
                req_id,
                attempt,
                INSTALL_SEND_ATTEMPTS,
                exc,
            )
    return None, last_error


async def install_schedule_via_team_services(
    agent: Any,
    schedule: ScheduledMessage,
    *,
    mode: str,
    authority_token: Mapping[str, Any],
    seen_version: Optional[str] = None,
    request_id: Optional[str] = None,
    delivery_ref: Optional[str] = None,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Send ``schedules.install`` to ``svc.team`` carrying ``authority_token`` and verify the reply.

    Uses one stable ``request_id`` per logical install, resends once under that same id on
    timeout or team-services unavailability within one overall ``timeout`` deadline, and
    re-reads the store before reporting the install as not created (F5).
    """

    req_id = request_id or logical_install_request_id(
        schedule.schedule_id,
        mode=mode,
        authority_token=authority_token,
        seen_version=seen_version,
    )
    resolved_delivery_ref = delivery_ref or extract_task_delivery_ref(schedule.payload_meta)
    instruction = str(getattr(schedule.payload, "problem_statement", "") or "").strip()

    loop = asyncio.get_running_loop()
    total_timeout = max(0.05, float(timeout))
    send_deadline = loop.time() + total_timeout - min(0.5, total_timeout * 0.1)

    remaining = send_deadline - loop.time()
    if remaining <= 0:
        raise TimeoutError(f"Schedule install {schedule.schedule_id} timed out before preparing instruction.")
    content = await asyncio.wait_for(
        _instruction_content(
            agent,
            instruction,
            authority_token=authority_token,
            request_id=req_id,
            timeout=remaining,
        ),
        timeout=remaining,
    )
    body = schedule_install_body(
        schedule,
        mode=mode,
        request_id=req_id,
        seen_version=seen_version,
        delivery_ref=resolved_delivery_ref,
        default_timezone=getattr(agent, "instance_timezone", None),
        instruction_content=content,
    )
    reply, last_error = await _send_install_with_resend(
        agent,
        schedule_id=schedule.schedule_id,
        mode=mode,
        req_id=req_id,
        body=body,
        authority_token=authority_token,
        send_deadline=send_deadline,
    )
    if reply is not None:
        return reply
    confirmed = await _confirm_install_from_store(
        agent,
        schedule,
        mode=mode,
        seen_version=seen_version,
        request_id=req_id,
    )
    if confirmed is not None:
        return confirmed
    assert last_error is not None
    raise last_error


def install_schedule_via_team_services_sync(
    agent: Any,
    schedule: ScheduledMessage,
    *,
    mode: str,
    authority_token: Mapping[str, Any],
    seen_version: Optional[str] = None,
    request_id: Optional[str] = None,
    delivery_ref: Optional[str] = None,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Synchronous wrapper for Strands tools calling ``schedules.install`` on ``svc.team``.

    Enforces one overall deadline ``timeout`` across blob upload, send/resend, and store
    confirmation, and cancels the submitted future when the deadline expires (F5 / CR-3).
    """

    total_timeout = max(0.05, float(timeout))
    coro = asyncio.wait_for(
        install_schedule_via_team_services(
            agent,
            schedule,
            mode=mode,
            authority_token=authority_token,
            seen_version=seen_version,
            request_id=request_id,
            delivery_ref=delivery_ref,
            timeout=total_timeout,
        ),
        timeout=total_timeout,
    )
    runtime_loop = getattr(agent, "_runtime_loop", None)
    if runtime_loop is not None and runtime_loop.is_running():
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not runtime_loop:
            future = asyncio.run_coroutine_threadsafe(coro, runtime_loop)
            try:
                return future.result(timeout=total_timeout)
            except BaseException:
                future.cancel()
                raise
    return asyncio.run(coro)


__all__ = [
    "INLINE_INSTRUCTION_MAX_CHARS",
    "INSTALL_SEND_ATTEMPTS",
    "INSTALL_TIMEOUT_SECONDS",
    "ScheduleInstallError",
    "extract_task_authority_token",
    "extract_task_delivery_ref",
    "install_schedule_via_team_services",
    "install_schedule_via_team_services_sync",
    "logical_install_request_id",
    "schedule_install_body",
]
