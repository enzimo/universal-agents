"""A new person's first-time setup: their own model and their own accounts for private services.

Owner's decisions (2026-10-08): when the team keeps something private, a
person an administrator adds is first shown what to set up for themselves:

- their own model, when they may not use the team's model keys and saved no
  model of their own (``access_control.person_settings``). It is needed: their
  requests do not run without it (the broker refuses the team's key);
- every service the team keeps private (``access_control.account_sharing``),
  including GitHub and Google, each optional and skippable, that someone in
  the team already uses: the team or a person linked an account for it, or a
  person signed in to it. A team that never uses GitHub does not send every
  new person to set it up; ``/link`` still works for any service, and the
  broker's refusal names it when a task needs one.

Trusted gateway code shows it, not an agent, because a person without a model
cannot reach an agent at all. ``GatewayPersonalSetup.mark_due`` records that a
newly added person has a first-time setup (``personal_setup_due`` in
``gateway_user_events``); on their first chat message ``before_request``
shows the list once (``personal_setup_shown``). When their model is still
missing, the list is the reply and the message is held back, because it could
not run; otherwise the list is posted in the chat and the message goes to the
team as usual. ``/setup`` shows the list again at any time
(``PersonalSetupSteps.text``). People who were in the team before this
existed have no setup due, so nothing changes for them.

The model step is read from the person's settings only. A person who saved a
personal key for the team's endpoint without "Use this as my model" also has
a working model, which the gateway cannot see (the key is the broker's), so
later messages are never held: the broker decides each call, and its refusal
names ``/setup`` (``custom_agents.model_keys``).

A service counts as set up when the person linked their own account (access
store) or the last known setup state says they signed in
(``access_control.tool_setup_states``, written by the broker and by the
gateway when a ``/link`` page finishes).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, Sequence

from access_catalog.catalog import ToolCatalog
from access_control.account_sharing import TeamAccountSharing
from access_control.person_settings import PersonSettings
from access_control.tool_setup_states import HOW_SIGNED_IN, ToolSetupStates
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.domain.message_types import SYSTEM_NOTIFICATION_MESSAGE_TYPE
from custom_messages.contracts.access_v2 import (
    MODEL_KEYS_SHARING_SUBJECT,
    AccountMode,
    AccountSharing,
    LinkOwnerKind,
    ToolDefinitionV1,
    ToolSetupState,
)
from id_generation import new_id
from secure_tools.auth_methods import tool_sign_in
from secure_tools.chat_commands import OWN_MODEL_COMMAND, SETUP_COMMAND, link_command
from storage.access_repository import AccessRepository

logger = logging.getLogger(__name__)

SETUP_DUE_EVENT = "personal_setup_due"
SETUP_SHOWN_EVENT = "personal_setup_shown"
MODEL_STEP_NAME = "Your own model"


@dataclass(frozen=True)
class SetupStep:
    """One thing a person sets up for themselves; ``command`` opens its secure page in their chat."""

    subject: str  # MODEL_KEYS_SHARING_SUBJECT or a tool id
    name: str
    command: str
    needed: bool
    done: bool


class GatewayUserEvents(Protocol):
    def record_gateway_user_event(
        self, *, event_type: str, canonical_user_id: str | None = None, payload: dict[str, Any] | None = None
    ) -> str: ...

    def gateway_user_event_types(self, *, canonical_user_id: str, event_types: Sequence[str]) -> set[str]: ...


class PersonalSetupSteps:
    """What one person still has to set up, and the list's text (module docstring)."""

    def __init__(self, *, store: Callable[[], AccessRepository], catalog: Callable[[], ToolCatalog], team_id: str) -> None:
        self._store = store
        self._catalog = catalog
        self._team_id = team_id

    def steps(self, person_id: str) -> list[SetupStep]:
        store = self._store()
        settings = PersonSettings(store, self._team_id)
        steps: list[SetupStep] = []
        if not settings.may_use_team_keys(person_id) and settings.own_model(person_id) is None:
            steps.append(SetupStep(MODEL_KEYS_SHARING_SUBJECT, MODEL_STEP_NAME, OWN_MODEL_COMMAND, needed=True, done=False))
        tools = self._catalog().tools
        states = ToolSetupStates(store)
        in_use = self._services_in_use(store, states)
        for row in TeamAccountSharing(store, self._team_id).overview(tools.values()):
            if row.sharing is not AccountSharing.PRIVATE or row.subject not in in_use:
                continue
            tool = tools[row.subject]
            steps.append(
                SetupStep(
                    tool.tool_id,
                    tool.display_name,
                    link_command(tool.tool_id),
                    needed=False,
                    done=self._has_own_account(store, states, tool, person_id),
                )
            )
        return steps

    def _services_in_use(self, store: AccessRepository, states: ToolSetupStates) -> set[str]:
        """The tools someone in the team has an account for: a linked one (any owner) or a sign-in (module docstring)."""

        linked = {link.tool_id for link in store.list_links(self._team_id)}
        signed_in = {
            state.tool_id
            for state in states.for_team(self._team_id)
            if state.state is ToolSetupState.READY and state.how == HOW_SIGNED_IN
        }
        return linked | signed_in

    def has_own_account(self, tool: ToolDefinitionV1, person_id: str) -> bool:
        """Whether the person set up their own account for the tool: signed in, or linked their own."""

        store = self._store()
        return self._has_own_account(store, ToolSetupStates(store), tool, person_id)

    def _has_own_account(self, store: AccessRepository, states: ToolSetupStates, tool: ToolDefinitionV1, person_id: str) -> bool:
        if tool_sign_in(tool.tool_id) is not None or tool.account is AccountMode.PERSONAL_LOGIN:
            state = states.get(self._team_id, person_id, tool.tool_id)
            return state is not None and state.state is ToolSetupState.READY and state.how == HOW_SIGNED_IN
        return store.active_link(self._team_id, tool.tool_id, LinkOwnerKind.PERSON, person_id) is not None

    def text(self, person_id: str, *, first_time: bool = False, held: bool = False) -> str:
        return setup_text(self.steps(person_id), first_time=first_time, held=held)


def setup_text(steps: Sequence[SetupStep], *, first_time: bool, held: bool) -> str:
    """The list in plain words; every step names the command that opens its page in this chat."""

    open_model = [step for step in steps if step.needed and not step.done]
    open_services = [step for step in steps if not step.needed and not step.done]
    done = [step for step in steps if step.done]
    something_open = bool(open_model or open_services)
    lines = [_heading(first_time=first_time, held=held, something_open=something_open)]
    lines += [
        f"- {step.name} (needed first: your requests do not run without it): send {step.command} "
        "and keep \"Use this as my model\" ticked."
        for step in open_model
    ]
    lines += _service_lines(open_services)
    if done and not first_time:
        lines.append("Already set up: " + ", ".join(step.name for step in done) + ".")
    if something_open:
        lines.append("Each command opens a secure page; how it ended comes back in this chat.")
    if held:
        lines.append("Your message was not sent to the team yet. Send it again once your model is set up.")
    lines.append(f"Send {SETUP_COMMAND} to see this list again.")
    return "\n".join(lines)


def _heading(*, first_time: bool, held: bool, something_open: bool) -> str:
    if first_time and held:
        return "Welcome to the team. Before your agents can work for you, set up your own model."
    if first_time:
        # The message goes on to the team right after this list (GatewayPersonalSetup).
        return "Welcome to the team. Your message is on its way to your agents. You can also set up your own accounts."
    return "Still to set up for your own work:" if something_open else "Everything you need is set up."


def _service_lines(open_services: Sequence[SetupStep]) -> list[str]:
    if not open_services:
        return []
    return [
        "These services are private in this team, so agents use only your own account. Each is optional:",
        *(f"- {step.name}: send {step.command}" for step in open_services),
    ]


def setup_summary(steps: Sequence[SetupStep]) -> str:
    """One line for the administrator's settings report, or empty when nothing is to set up."""

    model_needed = any(step.needed and not step.done for step in steps)
    services = [step.name for step in steps if not step.needed and not step.done]
    if not model_needed and not services:
        return ""
    parts = ["their own model (needed)"] if model_needed else []
    if services:
        parts.append("optionally their own " + ", ".join(services))
    return " and ".join(parts)


class GatewayPersonalSetup:
    """Show a newly added person their first-time setup on their first chat message (module docstring)."""

    def __init__(
        self,
        *,
        steps: PersonalSetupSteps,
        events: GatewayUserEvents,
        emit: Callable[[GatewayDeliveryEvent], Awaitable[None]],
    ) -> None:
        self._steps = steps
        self._events = events
        self._emit = emit
        # People whose answer can no longer change: shown already, or never
        # due (a setup is due only when someone is added, before they can
        # write). Kept in memory so later messages need no store read.
        self._settled: set[str] = set()

    @property
    def steps(self) -> PersonalSetupSteps:
        return self._steps

    def mark_due(self, person_id: str) -> None:
        """A newly added person has a first-time setup to see (recorded once)."""

        self._events.record_gateway_user_event(event_type=SETUP_DUE_EVENT, canonical_user_id=person_id)
        logger.info("A new person has a first-time setup to see. event=personal_setup_due person_id=%s", person_id)

    async def before_request(self, person_id: str, *, session: Any) -> str | None:
        """Before a chat message goes to the team: the reply to send instead, or None to send it (the list may be posted)."""

        try:
            return await self._before_request(person_id, session)
        except Exception:  # noqa: BLE001 - setup help never stops a message; the failure is logged
            logger.exception(
                "The first-time setup could not be shown; the message goes on. event=personal_setup_failed person_id=%s",
                person_id,
            )
            return None

    async def _before_request(self, person_id: str, session: Any) -> str | None:
        if person_id in self._settled:
            return None
        seen = await asyncio.to_thread(
            self._events.gateway_user_event_types,
            canonical_user_id=person_id,
            event_types=(SETUP_DUE_EVENT, SETUP_SHOWN_EVENT),
        )
        if SETUP_DUE_EVENT not in seen or SETUP_SHOWN_EVENT in seen:
            self._settled.add(person_id)
            return None
        steps = await asyncio.to_thread(self._steps.steps, person_id)
        open_steps = [step.subject for step in steps if not step.done]
        held = any(step.needed and not step.done for step in steps)
        if not open_steps:
            await self._record_shown(person_id, held=False, open_steps=open_steps)
            logger.info("A new person had nothing to set up. event=personal_setup_nothing person_id=%s", person_id)
            return None
        text = setup_text(steps, first_time=True, held=held)
        if held:
            await self._record_shown(person_id, held=True, open_steps=open_steps)
            return text
        await self._emit(
            GatewayDeliveryEvent(
                text=text,
                session_key=session.session_key,
                channel_name=session.channel_name,
                user_id=session.user_id,
                chat_id=session.chat_id,
                thread_id=session.thread_id,
                conversation_id=session.conversation_id,
                scheduled_job_id=None,
                correlation_id=new_id("personal_setup"),
                original_message_id=None,
                message_type=SYSTEM_NOTIFICATION_MESSAGE_TYPE,
            )
        )
        # Recorded once it reached the chat, so a failed post shows it next time.
        await self._record_shown(person_id, held=False, open_steps=open_steps)
        return None

    async def _record_shown(self, person_id: str, *, held: bool, open_steps: list[str]) -> None:
        await asyncio.to_thread(
            self._events.record_gateway_user_event,
            event_type=SETUP_SHOWN_EVENT,
            canonical_user_id=person_id,
            payload={"held": held, "steps": open_steps},
        )
        self._settled.add(person_id)
        logger.info(
            "Recorded a new person's first-time setup as shown. event=personal_setup_shown person_id=%s held=%s steps=%s",
            person_id,
            held,
            ",".join(open_steps),
        )


__all__ = [
    "GatewayPersonalSetup",
    "PersonalSetupSteps",
    "SETUP_DUE_EVENT",
    "SETUP_SHOWN_EVENT",
    "SetupStep",
    "setup_summary",
    "setup_text",
]
