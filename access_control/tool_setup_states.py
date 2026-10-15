"""Each person's last known tool setup state, kept by trusted code (owner's decision 2026-09-28).

TeamLead should not re-check a tool's whole setup every time it uses it. Trusted
code therefore keeps, per team, person and tool that acts with an account, one
``ToolSetupStateV1``: ``ready`` (and how: signed in as an account, the person's
own or the team's linked account, and when it last worked), ``needs_setup``
(the missing step: ``sign_in``, ``account_link``, ``team_sign_in_app`` or
``access``, and who can do it), or ``unknown``. It never involves a model:

- the broker, after every call of an account tool: success means ready
  (written only when the state changes or the last write is over an hour old);
  a failure caused by setup (``SETUP_FAILURE_CODES``) means needs_setup with
  the step; any other failure (network, 5xx, rate limits) changes nothing;
- the broker's setup status check (``set_up_tools`` and the periodic
  re-check) records what it found;
- the gateway, when a setup page or the person's own ``/link`` or ``/auth ...
  team_client`` finishes;
- ``set_up_tools`` records the person's own missing access (``custom_tools.tool_setup_check``).

TeamLead gets the states of the person it works for as one runtime-written
line (``describe_states``) and treats ``ready`` as a fact. The state covers the
person's account and the person's own access, never an agent's access, which
the access checks decide at every call.

States live in the access store (``storage.access_repository``), which the
broker, the agents' runtime and the gateway open in both authorization modes,
and are deleted with a removed person's other access records.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from custom_messages.contracts.access_v2 import ToolSetupState, ToolSetupStateV1
from secure_tools.auth_methods import tool_sign_in

logger = logging.getLogger(__name__)

# The missing step of a tool that needs setup.
STEP_SIGN_IN: Final = "sign_in"
STEP_ACCOUNT_LINK: Final = "account_link"
STEP_TEAM_SIGN_IN_APP: Final = "team_sign_in_app"
STEP_ACCESS: Final = "access"
# Who can do the missing step.
FIXED_BY_PERSON: Final = "person"
FIXED_BY_ADMINISTRATOR: Final = "administrator"
# How a ready tool works.
HOW_SIGNED_IN: Final = "signed_in"
HOW_OWN_LINK: Final = "own_link"
HOW_TEAM_LINK: Final = "team_link"
# Who recorded a state.
SOURCE_TOOL_CALL: Final = "tool_call"
SOURCE_SETUP_CHECK: Final = "setup_check"
SOURCE_SETUP_PAGE: Final = "setup_page"
SOURCE_PERSON_ACTION: Final = "person_action"
SOURCE_PERIODIC_CHECK: Final = "periodic_check"

# A ready tool that keeps working is written again only this often.
WORKED_WRITE_INTERVAL = timedelta(hours=1)
# Broker error codes that mean the tool is not set up for the person (the step
# for sign-in tools; tools with a linked account need a link instead).
SETUP_FAILURE_CODES: Final[Mapping[str, str]] = {
    "auth_required": STEP_SIGN_IN,
    "x_account_missing": STEP_SIGN_IN,
    "google_refresh_token_invalid": STEP_SIGN_IN,
    "google_unauthorized_client": STEP_SIGN_IN,
    "google_token_rejected": STEP_SIGN_IN,
    "account_link_missing": STEP_ACCOUNT_LINK,
    "google_oauth_client_missing": STEP_TEAM_SIGN_IN_APP,
    "x_client_missing": STEP_TEAM_SIGN_IN_APP,
    "x_client_invalid": STEP_TEAM_SIGN_IN_APP,
}
_ADMINISTRATOR_STEPS = frozenset({STEP_TEAM_SIGN_IN_APP, STEP_ACCESS})
MAX_DESCRIBED_TOOLS = 8

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def step_for_failure(tool_id: str, error_code: str) -> str | None:
    """The missing setup step a broker error code means for this tool, or None when it is not about setup."""

    step = SETUP_FAILURE_CODES.get(str(error_code or "").strip())
    if step == STEP_SIGN_IN and tool_sign_in(tool_id) is None:
        return STEP_ACCOUNT_LINK
    return step


def fixed_by(step: str) -> str:
    return FIXED_BY_ADMINISTRATOR if step in _ADMINISTRATOR_STEPS else FIXED_BY_PERSON


def verdict_from_status(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """The state one broker setup status entry means (``tool_setup_status``); None when nothing to record.

    Returns the ``note_ready`` or ``note_needs_setup`` fields: ``state`` plus
    ``how``/``account_label`` or ``step``.
    """

    if not entry.get("known") or entry.get("account_mode") == "none":
        return None
    link = entry.get("link") or {}
    sign_in = entry.get("sign_in")
    if sign_in is not None:
        if sign_in.get("state") == "signed_in":
            return {"state": ToolSetupState.READY, "how": HOW_SIGNED_IN, "account_label": sign_in.get("account")}
        if link.get("team"):
            return {"state": ToolSetupState.READY, "how": HOW_TEAM_LINK, "account_label": None}
        if entry.get("team_sign_in_app") == "missing":
            return {"state": ToolSetupState.NEEDS_SETUP, "step": STEP_TEAM_SIGN_IN_APP}
        if sign_in.get("state") == "missing":
            return {"state": ToolSetupState.NEEDS_SETUP, "step": STEP_SIGN_IN}
        return {"state": ToolSetupState.UNKNOWN}
    if link.get("own"):
        return {"state": ToolSetupState.READY, "how": HOW_OWN_LINK, "account_label": None}
    if link.get("team"):
        return {"state": ToolSetupState.READY, "how": HOW_TEAM_LINK, "account_label": None}
    return {"state": ToolSetupState.NEEDS_SETUP, "step": STEP_ACCOUNT_LINK}


class ToolSetupStates:
    """Read and write last known states in the access store (module docstring)."""

    def __init__(self, store: Any, *, clock: Clock = _now) -> None:
        self._store = store
        self._clock = clock

    def get(self, team_id: str, person_id: str, tool_id: str) -> ToolSetupStateV1 | None:
        return self._store.tool_setup_state(team_id, person_id, tool_id)

    def for_person(self, team_id: str, person_id: str) -> list[ToolSetupStateV1]:
        return self._store.tool_setup_states(team_id, person_id=person_id)

    def for_team(self, team_id: str) -> list[ToolSetupStateV1]:
        return self._store.tool_setup_states(team_id)

    def note_ready(
        self,
        team_id: str,
        person_id: str,
        tool_id: str,
        *,
        how: str,
        source: str,
        account_label: str | None = None,
        worked: bool = True,
    ) -> bool:
        """The tool works (``worked``) or was just set up; True when a row was written.

        Writes when the state or how changed, or when the last write is older
        than ``WORKED_WRITE_INTERVAL``, so a busy tool is not written on every call.
        """

        now = self._clock()
        current = self.get(team_id, person_id, tool_id)
        label = account_label or (current.account_label if current and current.how == how else None)
        unchanged = (
            current is not None
            and current.state is ToolSetupState.READY
            and current.how == how
            and current.account_label == label
        )
        if unchanged and now - current.checked_at_utc < WORKED_WRITE_INTERVAL:
            return False
        state = ToolSetupStateV1(
            team_id=team_id,
            person_id=person_id,
            tool_id=tool_id,
            state=ToolSetupState.READY,
            how=how,
            account_label=label,
            source=source,
            last_worked_at_utc=now if worked else (current.last_worked_at_utc if current else None),
            checked_at_utc=now,
            changed_at_utc=current.changed_at_utc if unchanged and current else now,
        )
        return self._put(state, current)

    def note_needs_setup(self, team_id: str, person_id: str, tool_id: str, *, step: str, source: str) -> bool:
        """The tool lacks ``step`` for the person; True when a row was written (only on a change)."""

        now = self._clock()
        current = self.get(team_id, person_id, tool_id)
        if current is not None and current.state is ToolSetupState.NEEDS_SETUP and current.missing_step == step:
            return False
        state = ToolSetupStateV1(
            team_id=team_id,
            person_id=person_id,
            tool_id=tool_id,
            state=ToolSetupState.NEEDS_SETUP,
            missing_step=step,
            fixed_by=fixed_by(step),
            source=source,
            last_worked_at_utc=current.last_worked_at_utc if current else None,
            checked_at_utc=now,
            changed_at_utc=now,
        )
        return self._put(state, current)

    def note_unknown(self, team_id: str, person_id: str, tool_id: str, *, source: str) -> bool:
        """Nobody knows now (for example the team's sign-in app changed); True when written."""

        now = self._clock()
        current = self.get(team_id, person_id, tool_id)
        if current is not None and current.state is ToolSetupState.UNKNOWN:
            return False
        state = ToolSetupStateV1(
            team_id=team_id,
            person_id=person_id,
            tool_id=tool_id,
            state=ToolSetupState.UNKNOWN,
            source=source,
            last_worked_at_utc=current.last_worked_at_utc if current else None,
            checked_at_utc=now,
            changed_at_utc=now,
        )
        return self._put(state, current)

    def note_verdict(self, team_id: str, person_id: str, tool_id: str, verdict: Mapping[str, Any], *, source: str) -> bool:
        """Record a ``verdict_from_status`` result (a check, not a use: last worked stays).

        A check that could not tell (``unknown``, for example the service did
        not answer) never replaces a known state; it only fills a missing one.
        """

        state = verdict["state"]
        if state is ToolSetupState.UNKNOWN and self.get(team_id, person_id, tool_id) is not None:
            return False
        if state is ToolSetupState.READY:
            return self.note_ready(
                team_id, person_id, tool_id, how=verdict["how"], source=source,
                account_label=verdict.get("account_label"), worked=False,
            )
        if state is ToolSetupState.NEEDS_SETUP:
            return self.note_needs_setup(team_id, person_id, tool_id, step=verdict["step"], source=source)
        return self.note_unknown(team_id, person_id, tool_id, source=source)

    def _put(self, state: ToolSetupStateV1, current: ToolSetupStateV1 | None) -> bool:
        self._store.put_tool_setup_state(state)
        if current is None or current.state is not state.state or current.missing_step != state.missing_step:
            logger.info(
                "A tool's setup state changed. event=tool_setup_state_changed team_id=%s person_id=%s tool_id=%s "
                "from_state=%s to_state=%s missing_step=%s source=%s",
                state.team_id,
                state.person_id,
                state.tool_id,
                current.state.value if current else "-",
                state.state.value,
                state.missing_step or "-",
                state.source,
            )
        return True


def safely(action: Callable[[], Any], /, *, what: str, **ids: Any) -> None:
    """Record a state without ever failing the work it describes (logged)."""

    try:
        action()
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not break a tool call or a page result
        logger.warning(
            "Could not record a tool setup state. event=tool_setup_state_record_failed what=%s %s error=%s",
            what,
            " ".join(f"{key}={value}" for key, value in ids.items()),
            type(exc).__name__,
        )


# -- the line TeamLead reads ----------------------------------------------------------

_STEP_WORDS = {
    STEP_SIGN_IN: "needs the person's sign-in",
    STEP_ACCOUNT_LINK: "needs an account link",
    STEP_TEAM_SIGN_IN_APP: "needs the team's sign-in app (an administrator)",
    STEP_ACCESS: "the person has no access (an administrator)",
}
_HOW_WORDS = {HOW_SIGNED_IN: "signed in", HOW_OWN_LINK: "own account linked", HOW_TEAM_LINK: "team account"}


def _when(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return ""
    days = (now.date() - moment.astimezone(timezone.utc).date()).days
    if days <= 0:
        return "worked today"
    return "worked yesterday" if days == 1 else f"worked {days} days ago"


def _describe(state: ToolSetupStateV1, name: str, now: datetime) -> str:
    if state.state is ToolSetupState.READY:
        details = [f"as {state.account_label}" if state.account_label else _HOW_WORDS.get(state.how or "", "")]
        details.append(_when(state.last_worked_at_utc, now))
        words = ", ".join(part for part in details if part)
        return f"{name} ready ({words})" if words else f"{name} ready"
    if state.state is ToolSetupState.NEEDS_SETUP:
        return f"{name} {_STEP_WORDS.get(state.missing_step or '', 'needs setup')}"
    return f"{name} unknown"


def describe_states(states: Iterable[ToolSetupStateV1], names: Mapping[str, str], *, now: datetime | None = None) -> str:
    """One bounded line of a person's known states, for TeamLead's context; empty without states."""

    moment = now or _now()
    ordered = sorted(states, key=lambda item: item.changed_at_utc, reverse=True)[:MAX_DESCRIBED_TOOLS]
    if not ordered:
        return ""
    parts = [_describe(state, names.get(state.tool_id, state.tool_id), moment) for state in ordered]
    return (
        "Tool setup for the person you work for (kept by the runtime; ready means use the tool without "
        "checking; needs setup or unknown means call set_up_tools without asking): " + "; ".join(parts) + "."
    )


__all__ = [
    "FIXED_BY_ADMINISTRATOR",
    "FIXED_BY_PERSON",
    "HOW_OWN_LINK",
    "HOW_SIGNED_IN",
    "HOW_TEAM_LINK",
    "SETUP_FAILURE_CODES",
    "SOURCE_PERIODIC_CHECK",
    "SOURCE_PERSON_ACTION",
    "SOURCE_SETUP_CHECK",
    "SOURCE_SETUP_PAGE",
    "SOURCE_TOOL_CALL",
    "STEP_ACCESS",
    "STEP_ACCOUNT_LINK",
    "STEP_SIGN_IN",
    "STEP_TEAM_SIGN_IN_APP",
    "ToolSetupStates",
    "describe_states",
    "fixed_by",
    "safely",
    "step_for_failure",
    "verdict_from_status",
]
