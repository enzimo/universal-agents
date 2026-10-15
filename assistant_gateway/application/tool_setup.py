"""Open the setup page a task asked for, watch it, and tell the task how it ended.

TeamLead asks the person to set a tool up with one ``ToolSetupRequestV1``
(owner's decision 2026-09-28): their own sign-in (``sign_in``: GitHub,
Google, X), an account link that chooses the stored credential a tool uses
(``account_link``, the person's own or the team's), or the team's sign-in app
people sign in through (``team_sign_in_app``, administrators only). This
module:

1. checks the request against what the tool uses and what this task already
   offered: a page that expired may be offered again, up to
   ``TOOL_SETUP_OFFER_LIMIT`` pages of one kind per task and tool; after the
   person closed a page for a tool without finishing it, no further page for
   that tool opens in the task. The team's sign-in app is refused for a
   person who is not an administrator, with a plain explanation of who can
   do it. A refusal is reported to the task as an ``unavailable`` result;
2. opens the page in the chat of the person the task works for, through the
   same broker calls as ``/link`` and ``/auth ... team_client``
   (``GatewayBrokerSignIns``, ``GatewayAccountLinks``), and records the wait
   (``ToolSetupRecord``);
3. watches the page with the shared broker watcher and sends one
   ``ToolSetupResultV1`` (completed, declined, expired or unavailable) to the
   waiting task.

Setup pages come from ``set_up_tools`` runs (``custom_agents.tool_setup_run_runtime``),
several at once for one task: a sibling page of a task that already waits on a
page opens too (``credential_handoff``). The run itself tells the person about
its workflow page in one chat message before the pages open
(``tool_setup_run_runtime``): a card here shows only its own reason, and
channels without cards show only its text. An ``external_step`` is a step the
person does outside Universal Agents; the gateway shows the catalog's words
for it (``SetupGuide``, never words from the request), opens no broker page,
and waits until the person marks it done on the workflow page
(``external_step_done``); it does not expire.

Two other ways end a wait: the person finishing the same setup on their own
(``/link <tool>`` or ``/auth <provider> team_client``; ``link_results`` calls
``person_finished``), and a gateway restart, after which nobody watches the
page any more (``recover_unwatched`` reports the page as unwatched, and
TeamLead checks the setup again before asking). Finishing a record is a
conditional update, so the task gets exactly one result.

Nothing here grants access: signing in or linking an account only makes a
credential available; grants decide who may use the tool.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

from access_control.people import is_administrator
from assistant_gateway.application.account_links import (
    ACCOUNT_LINK_NO_ACCESS_NOTE,
    ACCOUNT_LINK_STATUS_CREATED,
    ACCOUNT_LINK_STATUS_DECLINED,
    REASON_SESSION_EXPIRED,
    REASON_TEAM_ADMIN_REQUIRED,
    AccountLinkForm,
    AccountLinkUnavailable,
    GatewayAccountLinks,
    account_link_status_is_final,
)
from assistant_gateway.application.broker_session_watch import (
    WATCH_EXPIRED,
    WATCH_FINISHED,
    BrokerSessionOutcome,
    BrokerSessionWatches,
    watch_broker_session,
    watch_deadline,
)
from assistant_gateway.application.broker_sign_in import BrokerSignInPage, GatewayBrokerSignIns
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.application.ports import GatewayClock, GatewayEventOutput, GatewaySecureBroker
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.application.secure_results import (
    REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
    SecureResultPublisher,
)
from assistant_gateway.application.transient_presentation import (
    TOOL_SETUP_ACTION_KIND,
    GatewayTransientAction,
)
from assistant_gateway.domain.message_types import (
    TOOL_SETUP_MESSAGE_TYPE,
    TOOL_SETUP_RESULT_MESSAGE_TYPE,
    TOOL_SETUP_UNAVAILABLE_MESSAGE_TYPE,
)
from assistant_gateway.domain.request_state import ToolSetupRecord
from assistant_gateway.domain.sessions import SessionRecord
from access_control.tool_setup_states import (
    HOW_OWN_LINK,
    HOW_SIGNED_IN,
    HOW_TEAM_LINK,
    SOURCE_PERSON_ACTION,
    SOURCE_SETUP_PAGE,
    STEP_ACCOUNT_LINK,
    STEP_TEAM_SIGN_IN_APP,
    ToolSetupStates,
    safely,
)
from assistant_gateway.ports.repositories import SessionRepository, ToolSetupRepository
from custom_messages.contracts.credentials_v1 import (
    TOOL_SETUP_ACCOUNT_LINK,
    TOOL_SETUP_COMPLETED,
    TOOL_SETUP_DECLINED,
    TOOL_SETUP_EXPIRED,
    TOOL_SETUP_EXTERNAL_STEP,
    TOOL_SETUP_OFFER_LIMIT,
    TOOL_SETUP_SIGN_IN,
    TOOL_SETUP_SOURCE_PERSON,
    TOOL_SETUP_SOURCE_TASK,
    TOOL_SETUP_TEAM_SIGN_IN_APP,
    TOOL_SETUP_UNAVAILABLE,
    ToolSetupRequestV1,
    ToolSetupResultV1,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.tool_setup_spec_v1 import ExternalSetupStepV1
from secure_tools.auth_methods import (
    TEAM_CLIENT_METHOD,
    ToolSignIn,
    team_client_command,
    tool_sign_in,
)

# A waiting setup record; finished records keep the result status.
SETUP_STATUS_WAITING = "waiting"
REQUEST_KIND_TOOL_SETUP_RESULT = "tool_setup_result"

# Why a request was refused or a page could not be used (reason_code).
REASON_OFFER_LIMIT = "tool_setup_offer_limit"
REASON_DECLINED_BEFORE = "tool_setup_declined_before"
REASON_ADMIN_REQUIRED = "tool_setup_admin_required"
REASON_KIND_MISMATCH = "tool_setup_kind_mismatch"
REASON_PERSON_UNKNOWN = "tool_setup_person_unknown"
REASON_CHAT_NOT_WAITING = "tool_setup_chat_not_waiting"
REASON_PAGE_REFUSED = "tool_setup_page_refused"
REASON_PAGE_UNCONFIRMED = "tool_setup_page_unconfirmed"
REASON_SIGN_IN_FAILED = "tool_setup_sign_in_failed"
REASON_LINK_MISMATCH = "tool_setup_link_mismatch"
# The gateway restarted while the page was open (custom_agents.tool_setup_runtime).
REASON_PAGE_NOT_WATCHED = "tool_setup_page_not_watched"

SIGN_IN_STATUS_SUCCESS = "success"
SIGN_IN_STATUS_ERROR = "error"
_SIGN_IN_FINAL = frozenset({SIGN_IN_STATUS_SUCCESS, SIGN_IN_STATUS_ERROR})
_BROKER_NOTE = "What you enter goes straight to the secure broker, never to the agent."

TeamRolesResolver = Callable[[str], tuple[str, ...]]
# How long an external step's chat action stays live (it has no broker page):
# a non-time-sensitive wait, like approvals.
EXTERNAL_STEP_ACTION_HOURS = 48
REASON_STEP_UNKNOWN = "tool_setup_step_unknown"


class SetupGuide(Protocol):
    """Trusted words and links for setup pages (``assistant_gateway.tool_setup_guide``)."""

    def external_step(self, tool_id: str, step_id: str) -> tuple[str, ExternalSetupStepV1] | None: ...

    def workflow_url(self, task_id: str) -> str | None: ...


def _sentence(text: str) -> str:
    words = " ".join(str(text or "").split())
    return words if not words or words.endswith((".", "!", "?")) else f"{words}."


def _what(kind: str, tool_words: str) -> str:
    """The setup in plain words: "the GitHub sign-in", "the account link for Portainer"."""

    if kind == TOOL_SETUP_SIGN_IN:
        return f"the {tool_words} sign-in"
    if kind == TOOL_SETUP_TEAM_SIGN_IN_APP:
        return f"the team's {tool_words} sign-in app"
    return f"the account link for {tool_words}"


@dataclass(frozen=True)
class SetupRefused(Exception):
    """A request the gateway will not open a page for; ``message`` says why in plain words."""

    message: str
    reason_code: str
    needs_administrator: bool = False


@dataclass(frozen=True)
class PageEnding:
    """How a page ended, as the task hears it."""

    status: str
    message: str
    reason_code: str | None = None
    account_label: str | None = None
    link_id: str | None = None
    needs_administrator: bool = False


@dataclass(frozen=True)
class _OpenedPage:
    """One opened page: what the person sees and how the gateway watches it."""

    label: str
    receipt: str
    url: str
    expires_at_utc: str
    watch: Callable[[], Awaitable[PageEnding]] = field(repr=False)
    broker_session_id: str | None = None
    # An external step's words (what to do, where, and how to finish it).
    instructions: str = ""


def sign_in_page_ending(outcome: BrokerSessionOutcome, *, what: str) -> PageEnding:
    """A sign-in or team sign-in app page's ending."""

    if outcome.ending == WATCH_EXPIRED:
        return PageEnding(TOOL_SETUP_EXPIRED, f"The page for {what} expired before it was finished.")
    status = str(outcome.payload.get("status") or "")
    if outcome.ending != WATCH_FINISHED or status not in _SIGN_IN_FINAL:
        return PageEnding(
            TOOL_SETUP_UNAVAILABLE,
            f"Nobody could confirm whether {what} finished.",
            reason_code=REASON_PAGE_UNCONFIRMED,
        )
    if status == SIGN_IN_STATUS_ERROR:
        why = _sentence(str(outcome.payload.get("message") or ""))[:300]
        return PageEnding(
            TOOL_SETUP_UNAVAILABLE,
            f"{what[0].upper()}{what[1:]} did not finish. {why}".strip(),
            reason_code=str(outcome.payload.get("failure_reason") or REASON_SIGN_IN_FAILED),
        )
    label = " ".join(str(outcome.payload.get("username") or "").split())[:200] or None
    done = f"{what[0].upper()}{what[1:]} is done" + (f" (account {label})." if label else ".")
    return PageEnding(TOOL_SETUP_COMPLETED, done, account_label=label)


def account_link_page_ending(
    outcome: BrokerSessionOutcome, *, what: str, tool_id: str, owner: str
) -> PageEnding:
    """An account link form's ending; a created link for another tool or owner is never reported as linked."""

    if outcome.ending == WATCH_EXPIRED:
        return PageEnding(TOOL_SETUP_EXPIRED, f"The form for {what} expired before it was used.")
    if outcome.ending != WATCH_FINISHED:
        return PageEnding(
            TOOL_SETUP_UNAVAILABLE, f"Nobody could confirm whether {what} finished.", REASON_PAGE_UNCONFIRMED
        )
    result = outcome.payload.get("result")
    result = result if isinstance(result, dict) else {}
    status = str(result.get("status") or outcome.payload.get("status") or "")
    if status == ACCOUNT_LINK_STATUS_CREATED:
        link_id = str(result.get("link_id") or "") or None
        if link_id and result.get("tool_id") == tool_id and result.get("owner_kind") == owner:
            return PageEnding(TOOL_SETUP_COMPLETED, f"{what[0].upper()}{what[1:]} is done.", link_id=link_id)
        return PageEnding(
            TOOL_SETUP_UNAVAILABLE, f"Nobody could confirm whether {what} finished.", REASON_LINK_MISMATCH
        )
    if status == ACCOUNT_LINK_STATUS_DECLINED:
        return PageEnding(TOOL_SETUP_DECLINED, f"The form for {what} was closed without linking anything.")
    if result.get("reason_code") == REASON_SESSION_EXPIRED:
        return PageEnding(TOOL_SETUP_EXPIRED, f"The form for {what} expired before it was used.")
    why = _sentence(str(result.get("error") or ""))[:300]
    return PageEnding(
        TOOL_SETUP_UNAVAILABLE,
        f"Nothing was linked. {why}".strip(),
        reason_code=str(result.get("reason_code") or REASON_PAGE_REFUSED),
    )


class GatewayToolSetups:
    """Open, watch and report the setup pages tasks ask for (module docstring)."""

    def __init__(
        self,
        *,
        assistant_display_name: str,
        poll_interval_seconds: Callable[[], float],
        broker: GatewaySecureBroker,
        account_links: GatewayAccountLinks,
        sign_ins: GatewayBrokerSignIns,
        team_roles: TeamRolesResolver,
        publisher: SecureResultPublisher,
        sessions: SessionRepository,
        setups: ToolSetupRepository,
        request_service: GatewayRequestService,
        clock: GatewayClock,
        states: ToolSetupStates | None = None,
        team_id: str = "",
        event_output: GatewayEventOutput | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._assistant = assistant_display_name
        # People's last known tool setup states; a finished setup makes the tool ready.
        self._states = states
        self._team_id = team_id
        # Read at each watch so tests can speed it up after construction.
        self._poll_interval_seconds = poll_interval_seconds
        self._broker = broker
        self._account_links = account_links
        self._sign_ins = sign_ins
        self._team_roles = team_roles
        self._publisher = publisher
        self._sessions = sessions
        self._setups = setups
        self._requests = request_service
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._watches = BrokerSessionWatches(owner="tool_setup", logger=self._logger)
        # Set by the gateway's access Inbox (assistant_gateway.tool_setup_guide).
        self.guide: SetupGuide | None = None
        # Posts the line that closes a page's card when the page ends (_post_closing).
        self._event_output = event_output

    @property
    def pending_count(self) -> int:
        return self._watches.count

    async def close(self) -> None:
        await self._watches.close()

    # -- starting a page ---------------------------------------------------------

    async def start(
        self,
        *,
        request_content: ToolSetupRequestV1,
        request_envelope: MessageEnvelope,
        correlation_id: str,
        conversation_id: str | None,
        session_key: str,
    ) -> GatewayResponse:
        """Open the page for the person the task works for, or report why not."""

        session = self._require_session(session_key)
        person_id = str(session.owner_canonical_user_id or "").strip()
        try:
            if not person_id:
                raise SetupRefused(
                    "Setting up a tool needs a registered person, and this conversation has none.",
                    REASON_PERSON_UNKNOWN,
                )
            roles = self._team_roles(person_id)
            if request_content.kind == TOOL_SETUP_EXTERNAL_STEP:
                # No broker page and no expiry, so no offer limit.
                attempt, page = 1, self._external_page(request_content, roles)
            else:
                attempt = self._next_attempt(request_content)
                page = await self._open(request_content, person_id, roles)
        except SetupRefused as refused:
            return await self._refuse(
                request_content, refused, session=session, correlation_id=correlation_id,
                conversation_id=conversation_id, envelope=request_envelope,
            )
        self._setups.record_tool_setup(
            ToolSetupRecord(
                setup_request_id=request_content.setup_request_id,
                parent_task_id=request_content.parent_task_id,
                person_id=person_id,
                tool_id=request_content.tool_id,
                kind=request_content.kind,
                owner_kind=request_content.owner,
                session_key=session.session_key,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                status=SETUP_STATUS_WAITING,
                created_at=self._clock.now_utc().isoformat(),
            )
        )
        # The chat request now waits on this task's pages; its sibling pages may open too.
        self._requests.update_coordination(
            correlation_id, status=REQUEST_STATUS_WAITING_FOR_CREDENTIAL, active_task_id=request_content.parent_task_id
        )
        if request_content.kind != TOOL_SETUP_EXTERNAL_STEP:
            self._watches.start(
                self._watch(request_content, page, attempt),
                name=f"gateway_tool_setup_{request_content.setup_request_id[:24]}",
            )
        self._logger.info(
            "Opened a tool setup page for a waiting task. event=gateway_tool_setup_opened "
            "setup_request_id=%s task_id=%s tool_id=%s kind=%s owner=%s person_id=%s attempt=%s",
            request_content.setup_request_id,
            request_content.parent_task_id,
            request_content.tool_id,
            request_content.kind,
            request_content.owner,
            person_id,
            attempt,
        )
        return self._page_response(request_content, page, correlation_id, conversation_id, request_envelope)

    def is_new_sibling_page(self, request: ToolSetupRequestV1) -> bool:
        """A page never opened before, for a task that already waits on another page (it opens beside it)."""

        if self._setups.get_tool_setup(request.setup_request_id) is not None:
            return False
        return bool(self._setups.list_tool_setups(status=SETUP_STATUS_WAITING, parent_task_id=request.parent_task_id))

    def _external_page(self, request: ToolSetupRequestV1, roles: tuple[str, ...]) -> _OpenedPage:
        """A step outside Universal Agents, in the catalog's words; the person marks it done."""

        found = self.guide.external_step(request.tool_id, request.step_id or "") if self.guide else None
        if found is None:
            raise SetupRefused(
                f"{request.tool_id} has no setup step {request.step_id!r} in the tool catalog.", REASON_STEP_UNKNOWN
            )
        tool_name, step = found
        if step.who == "administrator" and not is_administrator(roles):
            raise SetupRefused(
                f"{step.title} for {tool_name} is a team administrator's step.",
                REASON_ADMIN_REQUIRED,
                needs_administrator=True,
            )
        workflow = self.guide.workflow_url(request.parent_task_id) if self.guide else None
        where = f" Open: {step.url}" if step.url else ""
        check = f" How to tell it worked: {step.verify}" if step.verify else ""
        done = (
            f" When it is done, choose Mark done on the workflow page: {workflow}"
            if workflow
            else " When it is done, tell me in chat."
        )

        async def never() -> PageEnding:  # pragma: no cover - external steps are not watched
            raise RuntimeError("External steps end when the person marks them done.")

        expires = self._clock.now_utc() + timedelta(hours=EXTERNAL_STEP_ACTION_HOURS)
        return _OpenedPage(
            label=f"{tool_name}: {step.title}",
            receipt=f"Setup step for {tool_name} shown: {step.title}.",
            url=step.url or workflow or "",
            expires_at_utc=expires.isoformat(),
            watch=never,
            instructions=f"{_sentence(step.instructions)}{where}{check}{done}",
        )

    async def external_step_done(self, setup_request_id: str, *, person_id: str) -> bool:
        """The person marked an external step done (the workflow page); False when it is not theirs or not waiting."""

        record = self._setups.get_tool_setup(setup_request_id)
        if (
            record is None
            or record.kind != TOOL_SETUP_EXTERNAL_STEP
            or record.person_id != person_id
            or record.status != SETUP_STATUS_WAITING
        ):
            return False
        ending = PageEnding(TOOL_SETUP_COMPLETED, "The setup step was marked done.")
        return await self._finish(setup_request_id, ending, attempt=1, source=TOOL_SETUP_SOURCE_PERSON)

    def _next_attempt(self, request: ToolSetupRequestV1) -> int:
        """This page's number among the task's pages of its kind for the tool; refuses past the limit."""

        earlier = self._setups.list_tool_setups(parent_task_id=request.parent_task_id, tool_id=request.tool_id)
        if any(record.status == TOOL_SETUP_DECLINED for record in earlier):
            raise SetupRefused(
                f"You closed the setup page for {request.tool_id} earlier in this request without "
                f"finishing it, so it is not offered again here. You can set it up any time with "
                f"/link {request.tool_id}.",
                REASON_DECLINED_BEFORE,
            )
        offered = sum(1 for record in earlier if record.kind == request.kind)
        if offered >= TOOL_SETUP_OFFER_LIMIT:
            raise SetupRefused(
                f"The setup page for {request.tool_id} was already offered {TOOL_SETUP_OFFER_LIMIT} times "
                f"in this request. You can set it up any time with /link {request.tool_id}.",
                REASON_OFFER_LIMIT,
            )
        return offered + 1

    async def _open(self, request: ToolSetupRequestV1, person_id: str, roles: tuple[str, ...]) -> _OpenedPage:
        sign_in = tool_sign_in(request.tool_id)
        if request.kind == TOOL_SETUP_SIGN_IN:
            if sign_in is None:
                raise SetupRefused(
                    f"{request.tool_id} does not use a personal sign-in; it uses a linked account.",
                    REASON_KIND_MISMATCH,
                )
            return await self._open_sign_in(sign_in, person_id, method=sign_in.default_method, roles=None)
        if request.kind == TOOL_SETUP_TEAM_SIGN_IN_APP:
            command = sign_in.team_client_command if sign_in else None
            if sign_in is None or command is None:
                raise SetupRefused(f"{request.tool_id} has no team sign-in app.", REASON_KIND_MISMATCH)
            if not is_administrator(roles):
                raise SetupRefused(
                    f"Setting up the team's {sign_in.display_name} sign-in app is an administrator's step. "
                    f"A team administrator or owner can do it with {command}.",
                    REASON_ADMIN_REQUIRED,
                    needs_administrator=True,
                )
            return await self._open_sign_in(sign_in, person_id, method=TEAM_CLIENT_METHOD, roles=roles)
        if sign_in is not None and (request.owner != "team" or not sign_in.team_link):
            raise SetupRefused(
                f"{sign_in.display_name} uses each person's own sign-in, not a linked account.",
                REASON_KIND_MISMATCH,
            )
        return await self._open_account_link(request, person_id, roles)

    async def _open_sign_in(
        self, sign_in: ToolSignIn, person_id: str, *, method: str, roles: tuple[str, ...] | None
    ) -> _OpenedPage:
        team_app = method == TEAM_CLIENT_METHOD
        what = _what(TOOL_SETUP_TEAM_SIGN_IN_APP if team_app else TOOL_SETUP_SIGN_IN, sign_in.display_name)
        if self._sign_ins.unavailable_message:
            raise SetupRefused(self._sign_ins.unavailable_message, REASON_PAGE_REFUSED)
        page = await self._sign_ins.open_page(
            provider=sign_in.provider,
            method=method,
            context=self._sign_ins.person_context(person_id, roles),
            restart_command=team_client_command(sign_in.provider) if team_app else sign_in.command,
        )
        if not isinstance(page, BrokerSignInPage):
            raise SetupRefused(_sentence(page), REASON_PAGE_REFUSED)
        if not page.expires_at_utc:
            raise SetupRefused(f"The page for {what} has no expiry, so it cannot be watched.", REASON_PAGE_REFUSED)

        async def watch() -> PageEnding:
            outcome = await watch_broker_session(
                read_status=lambda: self._broker.get_sign_in_status(status_url=page.status_url),
                is_final=lambda payload: str(payload.get("status") or "") in _SIGN_IN_FINAL,
                deadline=watch_deadline(page.expires_at_utc),
                clock=self._clock,
                poll_interval_seconds=self._poll_interval_seconds(),
                on_read_failed=lambda error: self._log_read_failed(what, error),
            )
            return sign_in_page_ending(outcome, what=what)

        name = sign_in.display_name
        if team_app:
            label, receipt = f"Set up the team's {name} sign-in app", f"Page for the team's {name} sign-in app opened."
        else:
            label, receipt = f"Sign in to {name}", f"{name} sign-in page opened."
        return _OpenedPage(
            label=label,
            receipt=receipt,
            url=page.action.url,
            expires_at_utc=page.expires_at_utc,
            watch=watch,
        )

    async def _open_account_link(self, request: ToolSetupRequestV1, person_id: str, roles: tuple[str, ...]) -> _OpenedPage:
        try:
            form = await self._account_links.open_form(
                request_id=request.setup_request_id,
                person_id=person_id,
                person_roles=roles,
                tool_id=request.tool_id,
                owner=request.owner,
                reason=request.reason,
            )
        except AccountLinkUnavailable as exc:
            admin = exc.reason_code == REASON_TEAM_ADMIN_REQUIRED
            message = _sentence(exc.message)
            if admin:
                message = (
                    f"{message} A team administrator or owner can link the team's account with "
                    f"/link {request.tool_id} team."
                )
            raise SetupRefused(message, exc.reason_code, needs_administrator=admin) from exc
        return self._account_link_page(request.tool_id, request.owner, form)

    def _account_link_page(self, tool_id: str, owner: str, form: AccountLinkForm) -> _OpenedPage:
        what = _what(TOOL_SETUP_ACCOUNT_LINK, form.tool_display_name)

        async def watch() -> PageEnding:
            outcome = await watch_broker_session(
                read_status=lambda: self._broker.get_account_link_status(
                    status_url=form.status_url, session_id=form.session_id
                ),
                is_final=account_link_status_is_final,
                deadline=watch_deadline(form.expires_at_utc),
                clock=self._clock,
                poll_interval_seconds=self._poll_interval_seconds(),
                on_read_failed=lambda error: self._log_read_failed(what, error),
            )
            return account_link_page_ending(outcome, what=what, tool_id=tool_id, owner=owner)

        whose = "the team's account" if owner == "team" else "your account"
        return _OpenedPage(
            label=f"Choose {whose} for {form.tool_display_name}",
            receipt=f"Account link form opened for {form.tool_display_name} ({whose}).",
            url=form.form_url,
            expires_at_utc=form.expires_at_utc,
            watch=watch,
            broker_session_id=form.session_id,
        )

    # -- account links an approved access proposal asks for -------------------------

    async def open_proposal_link(
        self,
        *,
        request_id: str,
        person_id: str,
        person_roles: tuple[str, ...],
        tool_id: str,
        owner: str,
        reason: str,
    ) -> AccountLinkForm:
        """Open one account link form an approved proposal asks for, for the approver."""

        return await self._account_links.open_form(
            request_id=request_id,
            person_id=person_id,
            person_roles=person_roles,
            tool_id=tool_id,
            owner=owner,  # type: ignore[arg-type]
            reason=reason,
        )

    def after_proposal_links(
        self,
        *,
        request_id: str,
        forms: list[tuple[AccountLinkForm, str, str]],
        then: Callable[[], Awaitable[None]],
    ) -> None:
        """Watch an approved proposal's forms ``(form, tool_id, owner)``; run ``then`` once all ended.

        The proposal's task waits inside ``propose_access_change``; ``then``
        sends its decision notice, and the tool reads which links exist.
        """

        pages = [self._account_link_page(tool_id, owner, form) for form, tool_id, owner in forms]
        self._watches.start(
            self._after_pages(request_id, pages, then),
            name=f"gateway_proposal_links_{request_id[:24]}",
        )

    async def _after_pages(
        self, request_id: str, pages: list[_OpenedPage], then: Callable[[], Awaitable[None]]
    ) -> None:
        try:
            endings = await asyncio.gather(*(page.watch() for page in pages))
            self._logger.info(
                "An approved proposal's account link forms ended. event=gateway_proposal_links_ended "
                "request_id=%s statuses=%s",
                request_id,
                ",".join(ending.status for ending in endings),
            )
        finally:
            await then()

    # -- ending a wait -------------------------------------------------------------

    async def _watch(self, request: ToolSetupRequestV1, page: _OpenedPage, attempt: int) -> None:
        ending = await page.watch()
        await self._finish(
            request.setup_request_id,
            ending,
            attempt=attempt,
            source=TOOL_SETUP_SOURCE_TASK,
            broker_session_id=page.broker_session_id,
        )

    async def person_finished(
        self,
        *,
        person_id: str,
        tool_id: str,
        kind: str,
        owner: str | None = None,
        account_label: str | None = None,
    ) -> int:
        """The person finished this setup on their own (``/link``, ``/auth``); end their waiting tasks' waits."""

        self.record_completed(
            person_id=person_id, tool_id=tool_id, kind=kind, owner=owner or "person",
            account_label=account_label, source=SOURCE_PERSON_ACTION,
        )
        finished = 0
        for record in self._setups.list_tool_setups(status=SETUP_STATUS_WAITING, person_id=person_id, tool_id=tool_id):
            if record.kind != kind or (owner is not None and record.owner_kind != owner):
                continue
            what = _what(kind, tool_id)
            ending = PageEnding(
                TOOL_SETUP_COMPLETED,
                f"The person finished {what} on their own" + (f" (account {account_label})." if account_label else "."),
                account_label=account_label,
            )
            if await self._finish(record.setup_request_id, ending, attempt=None, source=TOOL_SETUP_SOURCE_PERSON):
                finished += 1
        return finished

    def record_completed(
        self, *, person_id: str, tool_id: str, kind: str, owner: str, account_label: str | None, source: str
    ) -> None:
        """A finished setup makes the tool ready for the person (``access_control.tool_setup_states``).

        The team's sign-in app, or the team's account, can also fix the tool for
        other people: their states that lacked it become ``unknown``, so
        TeamLead checks them before use instead of asking again.
        """

        states = self._states
        if states is None:
            return
        team_id = self._team_id

        def record() -> None:
            if kind == TOOL_SETUP_TEAM_SIGN_IN_APP:
                _forget_missing_step(states, team_id, tool_id, STEP_TEAM_SIGN_IN_APP, source)
                return
            if owner == "team":
                _forget_missing_step(states, team_id, tool_id, STEP_ACCOUNT_LINK, source)
            how = HOW_SIGNED_IN if kind == TOOL_SETUP_SIGN_IN else (HOW_TEAM_LINK if owner == "team" else HOW_OWN_LINK)
            states.note_ready(
                team_id, person_id, tool_id, how=how, account_label=account_label, source=source, worked=False
            )

        safely(record, what="setup_finished", tool_id=tool_id, kind=kind, person_id=person_id)

    async def recover_unwatched(self) -> int:
        """After a gateway start: report every page still marked waiting as unwatched."""

        recovered = 0
        for record in self._setups.list_tool_setups(status=SETUP_STATUS_WAITING):
            if record.kind == TOOL_SETUP_EXTERNAL_STEP:
                continue  # nothing watches it; the person still marks it done
            ending = PageEnding(
                TOOL_SETUP_UNAVAILABLE,
                "The gateway restarted while the setup page was open, so nobody could see how it ended.",
                reason_code=REASON_PAGE_NOT_WATCHED,
            )
            if await self._finish(record.setup_request_id, ending, attempt=None, source=TOOL_SETUP_SOURCE_TASK):
                recovered += 1
        if recovered:
            self._logger.warning(
                "Reported setup pages nobody watched after the gateway started. "
                "event=gateway_tool_setup_recovered count=%s",
                recovered,
            )
        return recovered

    async def _finish(
        self,
        setup_request_id: str,
        ending: PageEnding,
        *,
        attempt: int | None,
        source: str,
        broker_session_id: str | None = None,
    ) -> bool:
        """Claim the waiting record and send its one result; False when another path finished it."""

        record = self._setups.get_tool_setup(setup_request_id)
        if record is None or not self._setups.finish_tool_setup(setup_request_id, status=ending.status):
            return False
        if ending.status == TOOL_SETUP_COMPLETED and source == TOOL_SETUP_SOURCE_TASK:
            self.record_completed(
                person_id=record.person_id, tool_id=record.tool_id, kind=record.kind,
                owner=record.owner_kind, account_label=ending.account_label, source=SOURCE_SETUP_PAGE,
            )
        if attempt is None:
            attempt = sum(
                1
                for item in self._setups.list_tool_setups(parent_task_id=record.parent_task_id, tool_id=record.tool_id)
                if item.kind == record.kind
            )
        result = ToolSetupResultV1(
            setup_request_id=record.setup_request_id,
            status=ending.status,
            parent_task_id=record.parent_task_id,
            tool_id=record.tool_id,
            kind=record.kind,
            owner=record.owner_kind,
            source=source,
            attempt=attempt,
            account_label=ending.account_label,
            needs_administrator=ending.needs_administrator,
            reason_code=ending.reason_code,
            message=ending.message,
            link_id=ending.link_id if ending.status == TOOL_SETUP_COMPLETED else None,
            broker_session_id=broker_session_id,
        )
        await self._send(result, record.session_key, record.correlation_id, record.conversation_id)
        await self._post_closing(record, ending, source)
        return True

    async def _post_closing(self, record: ToolSetupRecord, ending: PageEnding, source: str) -> None:
        """One plain line in the page's chat, under the page's own id, so its card closes (logged).

        Every way a page ends passes here once (its watcher, the person's own
        ``/link`` or ``/auth``, Mark Done, restart recovery), because only the
        call that claimed the waiting record posts. When the person finished it
        with ``/link``, the /link result already said how that went, so this
        line only says the page is no longer needed.
        """

        if self._event_output is None:
            return
        session = self._sessions.get_session(record.session_key)
        if session is None:
            return
        if source == TOOL_SETUP_SOURCE_PERSON:
            text = f"You finished {_what(record.kind, record.tool_id)} yourself, so this page is no longer needed."
        else:
            text = ending.message
        try:
            await self._event_output.emit(
                GatewayDeliveryEvent(
                    text=text,
                    session_key=session.session_key,
                    channel_name=session.channel_name,
                    user_id=session.user_id,
                    chat_id=session.chat_id,
                    thread_id=session.thread_id,
                    conversation_id=session.conversation_id,
                    scheduled_job_id=None,
                    correlation_id=record.setup_request_id,
                    original_message_id=None,
                    message_type=TOOL_SETUP_RESULT_MESSAGE_TYPE,
                )
            )
        except Exception:  # noqa: BLE001 - the task already has its result; the card only lingers
            self._logger.exception(
                "Could not post how a setup page ended. event=gateway_tool_setup_closing_failed "
                "setup_request_id=%s session_key=%s",
                record.setup_request_id,
                record.session_key,
            )
            return
        self._logger.info(
            "Posted how a setup page ended. event=gateway_tool_setup_closing_posted setup_request_id=%s "
            "status=%s source=%s",
            record.setup_request_id,
            ending.status,
            source,
        )

    async def _refuse(
        self,
        request: ToolSetupRequestV1,
        refused: SetupRefused,
        *,
        session: SessionRecord,
        correlation_id: str,
        conversation_id: str | None,
        envelope: MessageEnvelope,
    ) -> GatewayResponse:
        """Report a request no page opens for; the person reads the same plain reason."""

        self._logger.warning(
            "A tool setup request was refused. event=gateway_tool_setup_refused setup_request_id=%s "
            "task_id=%s tool_id=%s kind=%s owner=%s reason_code=%s",
            request.setup_request_id,
            request.parent_task_id,
            request.tool_id,
            request.kind,
            request.owner,
            refused.reason_code,
        )
        earlier = self._setups.list_tool_setups(parent_task_id=request.parent_task_id, tool_id=request.tool_id)
        result = ToolSetupResultV1(
            setup_request_id=request.setup_request_id,
            status=TOOL_SETUP_UNAVAILABLE,
            parent_task_id=request.parent_task_id,
            tool_id=request.tool_id,
            kind=request.kind,
            owner=request.owner,
            attempt=sum(1 for record in earlier if record.kind == request.kind),
            needs_administrator=refused.needs_administrator,
            reason_code=refused.reason_code,
            message=refused.message,
        )
        try:
            await self._send(result, session.session_key, correlation_id, conversation_id)
        except Exception:  # noqa: BLE001 - the person is still told why below
            self._logger.exception(
                "A tool setup refusal could not be sent to the task. event=gateway_tool_setup_refusal_send_failed "
                "setup_request_id=%s correlation_id=%s",
                request.setup_request_id,
                correlation_id,
            )
        return GatewayResponse(
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            task_id=request.parent_task_id,
            message_type=TOOL_SETUP_UNAVAILABLE_MESSAGE_TYPE,
            text=refused.message,
            raw_envelope=envelope,
            transcript_text=refused.message,
        )

    async def fail_unmatched(
        self,
        *,
        request_content: ToolSetupRequestV1,
        request_envelope: MessageEnvelope,
        correlation_id: str,
        conversation_id: str | None,
        session_key: str,
    ) -> None:
        """Tell the waiting task no chat can show its page."""

        await self._refuse(
            request_content,
            SetupRefused(
                "The chat that asked for it is no longer waiting for an answer. Ask again.",
                REASON_CHAT_NOT_WAITING,
            ),
            session=self._require_session(session_key),
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            envelope=request_envelope,
        )

    async def _send(self, result: ToolSetupResultV1, session_key: str, correlation_id: str, conversation_id: str | None) -> None:
        session = self._require_session(session_key)
        self._logger.info(
            "Sending a tool setup result. event=gateway_tool_setup_result setup_request_id=%s task_id=%s "
            "tool_id=%s kind=%s status=%s source=%s attempt=%s reason_code=%s",
            result.setup_request_id,
            result.parent_task_id,
            result.tool_id,
            result.kind,
            result.status,
            result.source,
            result.attempt,
            result.reason_code or "-",
        )
        await self._publisher.publish(
            result,
            session=session,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            request_kind=REQUEST_KIND_TOOL_SETUP_RESULT,
            request_text=result.message,
            parent_task_id=result.parent_task_id,
        )

    # -- presentation ----------------------------------------------------------------

    def _page_response(
        self,
        request: ToolSetupRequestV1,
        page: _OpenedPage,
        correlation_id: str,
        conversation_id: str | None,
        envelope: MessageEnvelope,
    ) -> GatewayResponse:
        """The reply the person sees: the reason and the page's Open action."""

        reason = _sentence(request.reason)
        if request.kind == TOOL_SETUP_EXTERNAL_STEP:
            note = page.instructions
            text = f"{reason} *{page.label}*: {page.instructions}"
        else:
            note = ACCOUNT_LINK_NO_ACCESS_NOTE if request.kind == TOOL_SETUP_ACCOUNT_LINK else _BROKER_NOTE
            text = f"{reason} Open *{page.label}*: {page.url}\n\n{note}"
        action = GatewayTransientAction(
            kind=TOOL_SETUP_ACTION_KIND,
            label=page.label,
            description=f"{reason}\n{note}",
            url=page.url,
            expires_at_utc=page.expires_at_utc,
            receipt=page.receipt,
            request_id=request.setup_request_id,
        )
        return GatewayResponse(
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            task_id=request.parent_task_id,
            message_type=TOOL_SETUP_MESSAGE_TYPE,
            text=text,
            raw_envelope=envelope,
            transcript_text=page.receipt,
            transient_action=action if page.url else None,
        )

    def _require_session(self, session_key: str) -> SessionRecord:
        session = self._sessions.get_session(session_key)
        if session is None:
            raise RuntimeError(f"Gateway session {session_key!r} is unavailable.")
        return session

    def _log_read_failed(self, what: str, error: Exception) -> None:
        self._logger.warning(
            "Could not read a tool setup page's status from the broker. "
            "event=gateway_tool_setup_read_failed page=%s error=%s",
            what,
            type(error).__name__,
        )


def _forget_missing_step(states: ToolSetupStates, team_id: str, tool_id: str, step: str, source: str) -> None:
    """Mark every state of the tool that lacked ``step`` as unknown (a team-wide fix)."""

    for state in states.for_team(team_id):
        if state.tool_id == tool_id and state.missing_step == step:
            states.note_unknown(team_id, state.person_id, tool_id, source=source)


__all__ = [
    "GatewayToolSetups",
    "PageEnding",
    "REASON_ADMIN_REQUIRED",
    "REASON_DECLINED_BEFORE",
    "REASON_KIND_MISMATCH",
    "REASON_OFFER_LIMIT",
    "REASON_PAGE_NOT_WATCHED",
    "SETUP_STATUS_WAITING",
    "account_link_page_ending",
    "sign_in_page_ending",
]
