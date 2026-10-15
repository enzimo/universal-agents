"""Re-check ready tool setups now and then, so TeamLead can trust them (owner's decision 2026-09-28).

TeamLead treats a ``ready`` tool setup state (``access_control.tool_setup_states``)
as a fact and does not check it before use. A sign-in can still expire without
anyone using the tool. This trusted job, not a model task, re-checks every
``ready`` state whose last use and last check are both older than
``security_layer.tool_setup_recheck_days`` (default 30; 0 turns it off):

- it runs in the broker's own long-running loop (``run_secure_tool_broker``),
  because only the broker can read sign-ins and linked accounts; this needs no
  new route and no signed task for work that has no task;
- it uses the same cheap, read-only checks as ``set_up_tools``
  (``tool_setup_status``): ``gh auth status`` for GitHub and Google's token
  check for Google really ask the service; an X sign-in and account links are
  checked for being stored and linked only (X is checked when an agent posts;
  a linked credential is checked when it is used);
- it skips people who are not active in the gateway's people copy;
- a state that is still ready gets a new check time; a check that cannot
  answer changes nothing;
- when a state that was ready now needs setup, the person gets one Inbox notice
  in plain words saying what to do. The notice's id comes from the person, the
  tool, the missing step and when the state changed, so it is sent once per
  problem until the tool is set up again.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from access_control.tool_setup_states import (
    SOURCE_PERIODIC_CHECK,
    STEP_ACCOUNT_LINK,
    STEP_SIGN_IN,
    STEP_TEAM_SIGN_IN_APP,
    ToolSetupStates,
)
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    AccessNoticeV1,
    PersonStanding,
    ToolSetupState,
    ToolSetupStateV1,
)
from secure_tool_broker.tool_setup_status import record_status, tool_status_entry
from secure_tools.auth_methods import tool_sign_in

LOGGER = logging.getLogger(__name__)

# How often the loop looks for states that are due.
RECHECK_LOOP_SECONDS: Final = 3600
_NOTICE_ID_PREFIX = "tool_setup_"


@dataclass(frozen=True)
class RecheckReport:
    checked: int = 0
    still_ready: int = 0
    now_needs_setup: int = 0
    notices: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _last_seen(state: ToolSetupStateV1) -> datetime:
    return max(filter(None, (state.last_worked_at_utc, state.checked_at_utc)))


def _active_people(store: Any, team_id: str) -> set[str] | None:
    """Active people from the gateway's people copy; None when the copy is not recorded yet (check everyone)."""

    if store.people_synced_at(team_id) is None:
        return None
    return {person.person_id for person in store.team_people(team_id) if person.standing is PersonStanding.ACTIVE}


def due_states(store: Any, team_id: str, *, every_days: int, now: datetime) -> list[ToolSetupStateV1]:
    """Ready states of active people that nobody used or checked for ``every_days`` days."""

    cutoff = now - timedelta(days=every_days)
    active = _active_people(store, team_id)
    return [
        state
        for state in ToolSetupStates(store).for_team(team_id)
        if state.state is ToolSetupState.READY
        and _last_seen(state) < cutoff
        and (active is None or state.person_id in active)
    ]


def _tool_name(gate: Any, tool_id: str) -> str:
    sign_in = tool_sign_in(tool_id)
    if sign_in is not None:
        return sign_in.display_name
    tool = gate.catalog.tools.get(tool_id)
    return tool.display_name if tool is not None else tool_id


def notice_text(gate: Any, state: ToolSetupStateV1) -> tuple[str, str]:
    """The notice's title and plain summary for a tool that needs setting up again."""

    name = _tool_name(gate, state.tool_id)
    ask = "or ask TeamLead to walk you through it."
    if state.missing_step == STEP_SIGN_IN:
        return f"Sign in to {name} again", f"Your {name} sign-in no longer works. Run /link {state.tool_id}, {ask}"
    if state.missing_step == STEP_TEAM_SIGN_IN_APP:
        sign_in = tool_sign_in(state.tool_id)
        command = sign_in.team_client_command if sign_in else "/auth"
        return (
            f"{name} needs the team's sign-in app",
            f"{name} stopped working for you because the team's {name} sign-in app is not set up. "
            f"A team administrator or owner can set it up with {command}.",
        )
    if state.missing_step == STEP_ACCOUNT_LINK:
        return (
            f"Link an account to {name} again",
            f"{name} has no account linked for your work any more. Run /link {state.tool_id}, {ask}",
        )
    return f"{name} needs setting up again", f"{name} needs setting up again. Ask TeamLead to walk you through it."


def _notice_id(state: ToolSetupStateV1) -> str:
    key = "|".join((state.team_id, state.person_id, state.tool_id, state.missing_step or "", state.changed_at_utc.isoformat()))
    return _NOTICE_ID_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:48]


def send_setup_notice(gate: Any, state: ToolSetupStateV1, *, now: datetime) -> bool:
    """One Inbox notice for this problem; False when it was already sent."""

    title, summary = notice_text(gate, state)
    sent = gate.links.create_notice_if_absent(
        AccessNoticeV1(
            notice_id=_notice_id(state),
            team_id=state.team_id,
            kind=AccessNoticeKind.TOOL_SETUP_NEEDED,
            recipient_person_id=state.person_id,
            title=title,
            summary=summary,
            created_at_utc=now,
        )
    )
    if sent:
        LOGGER.warning(
            "Told a person that a tool needs setting up again. event=tool_setup_notice_sent person_id=%s tool_id=%s "
            "missing_step=%s",
            state.person_id,
            state.tool_id,
            state.missing_step,
        )
    return sent


def _recheck_one(gate: Any, state: ToolSetupStateV1, now: datetime) -> tuple[bool, bool]:
    """(still ready, notice sent) for one due state."""

    entry = tool_status_entry(state.tool_id, state.person_id, gate)
    record_status(gate, state.person_id, entry, source=SOURCE_PERIODIC_CHECK)
    current = ToolSetupStates(gate.links).get(gate.team_id, state.person_id, state.tool_id)
    if current is None or current.state is ToolSetupState.READY:
        return True, False
    if current.state is ToolSetupState.NEEDS_SETUP:
        return False, send_setup_notice(gate, current, now=now)
    return False, False


def recheck_due(gate: Any, *, every_days: int, now: datetime | None = None) -> RecheckReport:
    """Re-check every due ready state once (module docstring); nothing when ``every_days`` is 0."""

    if every_days <= 0 or gate.links is None:
        return RecheckReport()
    moment = now or _now()
    due = due_states(gate.links, gate.team_id, every_days=every_days, now=moment)
    still_ready = needs_setup = notices = 0
    for state in due:
        try:
            ready, noticed = _recheck_one(gate, state, moment)
        except Exception as exc:  # noqa: BLE001 - one person's check must not stop the others
            LOGGER.warning(
                "A periodic tool setup check failed; the state is kept. event=tool_setup_recheck_failed "
                "person_id=%s tool_id=%s error=%s",
                state.person_id,
                state.tool_id,
                type(exc).__name__,
            )
            continue
        still_ready += int(ready)
        needs_setup += int(not ready)
        notices += int(noticed)
    report = RecheckReport(len(due), still_ready, needs_setup, notices)
    if due:
        LOGGER.info(
            "Re-checked ready tool setups. event=tool_setup_recheck_finished checked=%s still_ready=%s "
            "now_needs_setup=%s notices=%s",
            report.checked,
            report.still_ready,
            report.now_needs_setup,
            report.notices,
        )
    return report


async def run_rechecks(gate: Any, *, every_days: int, interval_seconds: float = RECHECK_LOOP_SECONDS) -> None:
    """The broker's loop: look for due states every ``interval_seconds``, forever."""

    if every_days <= 0:
        LOGGER.info(
            "Periodic tool setup checks are off (security_layer.tool_setup_recheck_days is 0). "
            "event=tool_setup_recheck_disabled"
        )
    while True:
        await asyncio.sleep(interval_seconds)
        if every_days > 0:
            await asyncio.to_thread(recheck_due, gate, every_days=every_days)


__all__ = [
    "RECHECK_LOOP_SECONDS",
    "RecheckReport",
    "due_states",
    "notice_text",
    "recheck_due",
    "run_rechecks",
    "send_setup_notice",
]
