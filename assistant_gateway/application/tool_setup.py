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
page opens too (``credential_handoff``), and each page's card stays until that
page ends, whatever happens to the others (``page_status``), or until a
gateway restart (cards live only in memory). The run itself
tells the person about its workflow page in one chat message before the pages
open (``tool_setup_run_runtime``): a card here shows only its own reason, and
channels without cards show only its text. Chat history keeps each page's
receipt, never its address or the task's reason. An ``external_step`` is a
step the person does outside Universal Agents; the gateway shows the
catalog's words for it (``SetupGuide``, never words from the request), keeps
its title, instructions and link in chat history, opens no broker page, and
waits until the person marks it done on the workflow page
(``external_step_done``); it does not expire.

When a page ends, its record keeps the task's result (``result_json``,
without any address) in the same conditional update that ends the wait, and
the result is sent from there (``resend_kept_results``): to the team lead
known at that moment, under a message id naming that lead
(``secure_results``). If no lead is known, or the publish fails (NATS
disconnected), the result stays kept and the chat request is left as it was,
so the task's other pages keep their cards and later pages still open. Kept
results are sent on every team-lead-available transition and whenever the
team's records confirm the lead chats already go to (``manager_transitions``,
from the lead follower, ``team_view.TeamLeadFollower``), at start by a
gateway without a team view, and, after a failed publish while a lead is known,
by a retry with backoff; one pass at a time sends them, oldest first. With a
team view they go only to a lead that team records the follower read named
(``lead_from_records``): until then a pass, a page's ending included, keeps
them and stops with no retry, because a route saved by the previous run may
name a lead that is gone. A
result that can never be sent (its chat no longer exists, or the kept copy
cannot be read) is marked ``unsendable`` and logged once. Rationale (review
of the 2026-11-14 incident fixes): the record was marked finished before the
send and the result was not kept, so a page that ended while the team lead
restarted never reached its task, and the publisher marked the still-running
chat request ``manager_unavailable``, which removed the other pages' cards.

Two other ways end a wait: the person finishing the same setup on their own
(``/link <tool>`` or ``/auth <provider> team_client``; ``link_results`` calls
``person_finished``), and a gateway restart, after which nobody watches the
page any more (``recover_unwatched`` reports the page as unwatched, and
TeamLead checks the setup again before asking). Finishing a record is a
conditional update, so the task gets exactly one result. Both the person's own
``/link`` and Mark Done send a ``person_action`` result; their words differ.

When a page ends, its card closes with one line in the person's chat
(``_post_closing``). The code that ended the page writes that line
(``PageEnding.closing``, or else the task's ``message``). The line names the
page by kind (``_what``) and the tool by the name its page showed: the sign-in
name ("GitHub") for sign-ins and the team's sign-in app, and the catalog's
display name for account links and outside steps. For example: "The GitHub
sign-in is done (account exampleuser).", "You finished the account link for
Portainer yourself, so this page is no longer needed.", or, after Mark Done,
"You marked the Gitea setup step 'Create a Gitea token' done." Rationale (an
incident on 2026-11-14): the line used to be rebuilt from the result's source,
so Mark Done read as an account link with the raw tool id.

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

from pydantic import ValidationError

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
    SECURE_RESULT_RETRY_MAX_SECONDS,
    SECURE_RESULT_RETRY_MIN_SECONDS,
    SecureResultPublisher,
    SendOutcome,
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
from assistant_gateway.domain.request_state import (
    TOOL_SETUP_RESULT_SENT,
    TOOL_SETUP_RESULT_UNSENDABLE,
    TOOL_SETUP_STATUS_WAITING,
    ToolSetupRecord,
)
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
SETUP_STATUS_WAITING = TOOL_SETUP_STATUS_WAITING
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
# Kinds whose page names the tool by its sign-in name ("Sign in to GitHub"), not the catalog's ("GitHub (gh)").
_SIGN_IN_KINDS = frozenset({TOOL_SETUP_SIGN_IN, TOOL_SETUP_TEAM_SIGN_IN_APP})
# How long an external step's chat action stays live (it has no broker page):
# a non-time-sensitive wait, like approvals.
EXTERNAL_STEP_ACTION_HOURS = 48
REASON_STEP_UNKNOWN = "tool_setup_step_unknown"
# Why a kept result can never be sent (event=gateway_tool_setup_result_unsendable reason=...).
UNSENDABLE_SESSION_MISSING = "session_missing"
UNSENDABLE_UNREADABLE = "unreadable"
# The retry stops after this many passes in a row in which a kept result
# failed with an unexpected error (about two minutes with its backoff), so a
# row that fails on every pass does not keep it going; the next start or lead
# transition tries again. A pass that could not even list the kept results (the
# database is down) does not count: it is retried like a failed publish.
RESEND_ERROR_PASSES_LIMIT = 8


class SetupGuide(Protocol):
    """Trusted words and links for setup pages (``assistant_gateway.tool_setup_guide``)."""

    def external_step(self, tool_id: str, step_id: str) -> tuple[str, ExternalSetupStepV1] | None: ...

    def display_name(self, tool_id: str) -> str | None: ...

    def workflow_url(self, task_id: str) -> str | None: ...


def _sentence(text: str) -> str:
    words = " ".join(str(text or "").split())
    return words if not words or words.endswith((".", "!", "?")) else f"{words}."


def _what(kind: str, tool_name: str, step_title: str = "") -> str:
    """The setup page in plain words; the one place each kind's phrase is written.

    "the GitHub sign-in", "the team's Google sign-in app", "the account link
    for Portainer", "the Gitea setup step 'Create a Gitea token'" ("the Gitea
    setup step" when the step's title is not known). Records hold only kinds
    the ``ToolSetupKind`` contract accepted, so any other kind is a
    programming error, never a reason to borrow another kind's words.
    """

    if kind == TOOL_SETUP_SIGN_IN:
        return f"the {tool_name} sign-in"
    if kind == TOOL_SETUP_TEAM_SIGN_IN_APP:
        return f"the team's {tool_name} sign-in app"
    if kind == TOOL_SETUP_ACCOUNT_LINK:
        return f"the account link for {tool_name}"
    if kind == TOOL_SETUP_EXTERNAL_STEP:
        return f"the {tool_name} setup step '{step_title}'" if step_title else f"the {tool_name} setup step"
    raise ValueError(f"Unknown tool setup kind {kind!r}.")


# Lines several page endings share; ``what`` is always a ``_what`` phrase.


def _capitalized(text: str) -> str:
    """``text`` with its first letter in upper case, for a sentence that starts with it."""

    return f"{text[0].upper()}{text[1:]}" if text else text


def _is_done(what: str, account_label: str | None = None) -> str:
    return f"{_capitalized(what)} is done" + (f" (account {account_label})." if account_label else ".")


def _did_not_finish(what: str, why: str) -> str:
    return f"{_capitalized(what)} did not finish. {why}".strip()


def _unconfirmed(what: str) -> str:
    return f"Nobody could confirm whether {what} finished."


def _form_expired(what: str) -> str:
    return f"The form for {what} expired before it was used."


@dataclass(frozen=True)
class ResendPass:
    """What one pass over the kept results did (``GatewayToolSetups.resend_kept_results``).

    ``stopped`` is why the pass stopped before the end (no team lead known, or
    a failed publish); the results after that stay kept for the next pass.
    ``failed`` says an unexpected error (logged) kept at least one result, or
    stopped the pass when the kept results could not be listed
    (``listing_failed`` too).
    """

    sent: int
    stopped: SendOutcome | None = None
    failed: bool = False
    listing_failed: bool = False

    @property
    def result_failed(self) -> bool:
        """A kept result failed with an unexpected error (what the retry's give-up counts)."""

        return self.failed and not self.listing_failed

    @property
    def reason(self) -> str:
        """Why results stayed kept, for log lines: a ``SendOutcome`` value, ``error`` or ``-``."""

        if self.stopped is not None:
            return self.stopped.value
        return "error" if self.failed else "-"


@dataclass(frozen=True)
class SetupRefused(Exception):
    """A request the gateway will not open a page for; ``message`` says why in plain words."""

    message: str
    reason_code: str
    needs_administrator: bool = False


@dataclass(frozen=True)
class PageEnding:
    """How a page ended: ``message`` is what the task hears.

    ``closing`` is the line the person's chat shows when it differs from
    ``message`` (empty means the same line). The code that ends a page writes
    both, so the line always names what actually ended (``_post_closing``).
    """

    status: str
    message: str
    reason_code: str | None = None
    account_label: str | None = None
    link_id: str | None = None
    needs_administrator: bool = False
    closing: str = field(default="", kw_only=True)


@dataclass(frozen=True)
class _OpenedPage:
    """One opened page: what the person sees and how the gateway watches it."""

    label: str
    receipt: str
    url: str
    expires_at_utc: str
    watch: Callable[[], Awaitable[PageEnding]] = field(repr=False)
    # An external step's words (what to do, where, and how to finish it).
    instructions: str = ""


def sign_in_page_ending(outcome: BrokerSessionOutcome, *, what: str) -> PageEnding:
    """A sign-in or team sign-in app page's ending."""

    if outcome.ending == WATCH_EXPIRED:
        return PageEnding(TOOL_SETUP_EXPIRED, f"The page for {what} expired before it was finished.")
    status = str(outcome.payload.get("status") or "")
    if outcome.ending != WATCH_FINISHED or status not in _SIGN_IN_FINAL:
        return PageEnding(TOOL_SETUP_UNAVAILABLE, _unconfirmed(what), reason_code=REASON_PAGE_UNCONFIRMED)
    if status == SIGN_IN_STATUS_ERROR:
        why = _sentence(str(outcome.payload.get("message") or ""))[:300]
        return PageEnding(
            TOOL_SETUP_UNAVAILABLE,
            _did_not_finish(what, why),
            reason_code=str(outcome.payload.get("failure_reason") or REASON_SIGN_IN_FAILED),
        )
    label = " ".join(str(outcome.payload.get("username") or "").split())[:200] or None
    return PageEnding(TOOL_SETUP_COMPLETED, _is_done(what, label), account_label=label)


def account_link_page_ending(
    outcome: BrokerSessionOutcome, *, what: str, tool_id: str, owner: str
) -> PageEnding:
    """An account link form's ending; a created link for another tool or owner is never reported as linked."""

    if outcome.ending == WATCH_EXPIRED:
        return PageEnding(TOOL_SETUP_EXPIRED, _form_expired(what))
    if outcome.ending != WATCH_FINISHED:
        return PageEnding(TOOL_SETUP_UNAVAILABLE, _unconfirmed(what), reason_code=REASON_PAGE_UNCONFIRMED)
    result = outcome.payload.get("result")
    result = result if isinstance(result, dict) else {}
    status = str(result.get("status") or outcome.payload.get("status") or "")
    if status == ACCOUNT_LINK_STATUS_CREATED:
        link_id = str(result.get("link_id") or "") or None
        if link_id and result.get("tool_id") == tool_id and result.get("owner_kind") == owner:
            return PageEnding(TOOL_SETUP_COMPLETED, _is_done(what), link_id=link_id)
        return PageEnding(TOOL_SETUP_UNAVAILABLE, _unconfirmed(what), reason_code=REASON_LINK_MISMATCH)
    if status == ACCOUNT_LINK_STATUS_DECLINED:
        return PageEnding(TOOL_SETUP_DECLINED, f"The form for {what} was closed without linking anything.")
    if result.get("reason_code") == REASON_SESSION_EXPIRED:
        return PageEnding(TOOL_SETUP_EXPIRED, _form_expired(what))
    # The broker refused the form (its plain reason follows); name the page like every other ending.
    why = _sentence(str(result.get("error") or ""))[:300]
    return PageEnding(
        TOOL_SETUP_UNAVAILABLE,
        _did_not_finish(what, why),
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
        lead_from_records: Callable[[], bool] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._assistant = assistant_display_name
        # With a team view: False while a lead is routed that no team record the lead follower read names
        # (GatewayManagerClient.lead_from_records); kept results wait then, because a route saved by the previous
        # run may name a lead that is gone. With no route at all it is True, so the publisher logs no_route. None
        # without a team view: the saved route is used as it is.
        self._lead_from_records = lead_from_records
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
        # Every send of a page result is one pass over the kept results, one
        # pass at a time (resend_kept_results), so a result is never sent from
        # two places at once.
        self._send_lock = asyncio.Lock()
        # The retry after a failed publish while a team lead is known; kept
        # apart from the page watches, which pending_count counts.
        self._retries = BrokerSessionWatches(owner="tool_setup_results", logger=self._logger)
        self._retrying = False
        # Set by close(): no retry starts after the gateway began to stop.
        self._closed = False

    @property
    def pending_count(self) -> int:
        return self._watches.count

    async def close(self) -> None:
        self._closed = True
        await self._watches.close()
        await self._retries.close()

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
        # For the request's own task, the chat request now waits on its pages;
        # its sibling pages may open too. A delegated task's page changes neither
        # the request's status nor its task: a request with a run state keeps its
        # own task (update_request_coordination), so the person's guidance and
        # its files stay on the request
        # (tests/test_guidance_files_after_setup_page.py).
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

    def page_status(self, setup_request_id: str) -> str | None:
        """The page's status: ``waiting`` until it ends, then how it ended; None when no page has that id.

        A page's chat card follows this, not the chat request its task's pages
        share (``credential_handoff.ToolSetupHandoffs``).
        """

        record = self._setups.get_tool_setup(setup_request_id)
        return record.status if record is not None else None

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
        instructions = f"{_sentence(step.instructions)}{where}{check}{done}"
        return _OpenedPage(
            label=f"{tool_name}: {step.title}",
            # Chat history keeps the whole step, so a refresh never loses what to
            # do or where. Only catalog words and links an administrator approved
            # (checked by ExternalSetupStepV1) and the workflow page, never the
            # task's own reason.
            receipt=f"Setup step for {tool_name} shown: {_sentence(step.title)} {instructions}",
            url=step.url or workflow or "",
            expires_at_utc=expires.isoformat(),
            watch=never,
            instructions=instructions,
        )

    async def external_step_done(self, setup_request_id: str, *, person_id: str, step_title: str) -> bool:
        """The person marked an external step done (the workflow page); False when it is not theirs or not waiting.

        ``step_title`` is the step's title the workflow page stores, which
        trusted code wrote from the catalog when the page opened (never words
        from the browser); it is the row the person clicked. Marking a step
        done is a ``person_action`` result like the person's own ``/link``,
        with its own words: "You marked the Gitea setup step 'Create a Gitea
        token' done."
        """

        record = self._setups.get_tool_setup(setup_request_id)
        if (
            record is None
            or record.kind != TOOL_SETUP_EXTERNAL_STEP
            or record.person_id != person_id
            or record.status != SETUP_STATUS_WAITING
        ):
            return False
        title = " ".join(step_title.split())
        if not title:
            self._logger.warning(
                "An outside setup step was marked done without its stored title; its line names only the tool. "
                "event=gateway_tool_setup_step_title_missing setup_request_id=%s tool_id=%s",
                setup_request_id,
                record.tool_id,
            )
        what = self._page_what(record, title)
        ending = PageEnding(TOOL_SETUP_COMPLETED, f"The person marked {what} done.", closing=f"You marked {what} done.")
        return await self._finish(setup_request_id, ending, attempt=1, source=TOOL_SETUP_SOURCE_PERSON)

    def _page_what(self, record: ToolSetupRecord, step_title: str = "") -> str:
        """The record's page in plain words, with the tool named as its page named it."""

        return _what(record.kind, self._tool_name(record), step_title)

    def _tool_name(self, record: ToolSetupRecord) -> str:
        """The tool's name as the page showed it; the raw tool id, logged, when no name is known.

        Sign-in pages use the sign-in name (``tool_sign_in``); account link
        forms and outside steps use the catalog's display name, which the
        broker named the form with and ``_external_page`` showed. The raw id
        stands in only when the tool has left the catalog since the page opened.
        """

        if record.kind in _SIGN_IN_KINDS:
            sign_in = tool_sign_in(record.tool_id)
            name = sign_in.display_name if sign_in else None
        else:
            name = self.guide.display_name(record.tool_id) if self.guide else None
        if name:
            return name
        self._logger.warning(
            "A setup page's tool has no display name; its line names the tool id. "
            "event=gateway_tool_setup_name_unknown setup_request_id=%s tool_id=%s kind=%s",
            record.setup_request_id,
            record.tool_id,
            record.kind,
        )
        return record.tool_id

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
            what = self._page_what(record)
            ending = PageEnding(
                TOOL_SETUP_COMPLETED,
                f"The person finished {what} on their own" + (f" (account {account_label})." if account_label else "."),
                account_label=account_label,
                # The /link or /auth result already told the person how it went.
                closing=f"You finished {what} yourself, so this page is no longer needed.",
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
        """After a gateway start: report every page still marked waiting as unwatched.

        Each record is reported on its own, so a record that cannot be
        reported never stops the records after it. Its failure is logged with
        the record's stored status: ``waiting`` when it failed before it was
        claimed (a kind this version does not know), so the next start tries
        again; a finished status when it failed after the claim, which already
        kept its result for the task (``resend_kept_results``).
        """

        recovered = 0
        for record in self._setups.list_tool_setups(status=SETUP_STATUS_WAITING):
            if record.kind == TOOL_SETUP_EXTERNAL_STEP:
                continue  # nothing watches it; the person still marks it done
            try:
                if await self._report_unwatched(record):
                    recovered += 1
            except Exception:  # noqa: BLE001 - one bad record must not keep every later page waiting
                self._logger.exception(
                    "Could not report a setup page nobody watched after the gateway started "
                    "(status waiting: the next start tries again; a finished status: its result is kept). "
                    "event=gateway_tool_setup_recovery_failed setup_request_id=%s task_id=%s tool_id=%s kind=%s "
                    "status=%s",
                    record.setup_request_id,
                    record.parent_task_id,
                    record.tool_id,
                    record.kind,
                    self._stored_status(record.setup_request_id),
                )
        if recovered:
            self._logger.warning(
                "Reported setup pages nobody watched after the gateway started. "
                "event=gateway_tool_setup_recovered count=%s",
                recovered,
            )
        return recovered

    def _stored_status(self, setup_request_id: str) -> str:
        """A record's stored status for a failure log line; never raises, so the first failure stays visible."""

        try:
            record = self._setups.get_tool_setup(setup_request_id)
        except Exception:  # noqa: BLE001 - the failure being logged matters more than this read
            return "unreadable"
        return record.status if record is not None else "missing"

    async def _report_unwatched(self, record: ToolSetupRecord) -> bool:
        """Tell the record's task that nobody saw how its page ended; False when another path finished it."""

        ending = PageEnding(
            TOOL_SETUP_UNAVAILABLE,
            f"The gateway restarted while the page for {self._page_what(record)} was open, "
            "so nobody could see how it ended.",
            reason_code=REASON_PAGE_NOT_WATCHED,
        )
        return await self._finish(record.setup_request_id, ending, attempt=None, source=TOOL_SETUP_SOURCE_TASK)

    async def _finish(
        self,
        setup_request_id: str,
        ending: PageEnding,
        *,
        attempt: int | None,
        source: str,
    ) -> bool:
        """Claim the waiting record with its one result and send it; False when another path finished it.

        The result is built before the claim and stored by it, so a page that
        ends while no team lead can be reached keeps its result in the record.
        """

        record = self._setups.get_tool_setup(setup_request_id)
        if record is None or record.status != SETUP_STATUS_WAITING:
            return False
        result = self._result_for(record, ending, attempt=attempt, source=source)
        if not self._setups.finish_tool_setup(setup_request_id, status=ending.status, result_json=result.model_dump_json()):
            return False
        if ending.status == TOOL_SETUP_COMPLETED and source == TOOL_SETUP_SOURCE_TASK:
            self.record_completed(
                person_id=record.person_id, tool_id=record.tool_id, kind=record.kind,
                owner=record.owner_kind, account_label=ending.account_label, source=SOURCE_SETUP_PAGE,
            )
        sending = await self.resend_kept_results()
        self._log_if_kept(record, sending)
        await self._post_closing(record, ending, source)
        return True

    def _result_for(
        self, record: ToolSetupRecord, ending: PageEnding, *, attempt: int | None, source: str
    ) -> ToolSetupResultV1:
        """The task's result for a page that ended, without any address (it is addressed when sent).

        It never carries the broker session id: for an account link that id is
        the form's bearer, and the record keeps this result.
        """

        if attempt is None:
            attempt = sum(
                1
                for item in self._setups.list_tool_setups(parent_task_id=record.parent_task_id, tool_id=record.tool_id)
                if item.kind == record.kind
            )
        return ToolSetupResultV1(
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
        )

    async def _post_closing(self, record: ToolSetupRecord, ending: PageEnding, source: str) -> None:
        """One plain line in the page's chat, under the page's own id, so its card closes (logged).

        Every way a page ends passes here once (its watcher, the person's own
        ``/link`` or ``/auth``, Mark Done, restart recovery), because only the
        call that claimed the waiting record posts. The line is the ending's
        own (``closing``, else ``message``), written where the page ended:
        ``source`` alone cannot say what ended, because the person ends a page
        both with ``/link`` and with Mark Done (the 2026-11-14 incident, where
        Mark Done read as an account link with the raw tool id).
        """

        if self._event_output is None:
            return
        session = self._sessions.get_session(record.session_key)
        if session is None:
            return
        text = ending.closing or ending.message
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
        # A refusal has no page record, so a refusal that is not sent is not
        # kept: the task asked a moment ago, so a team lead was there to ask.
        try:
            outcome = await self._publish(result, session, correlation_id, conversation_id)
        except Exception:  # noqa: BLE001 - the person is still told why below
            self._logger.exception(
                "A tool setup refusal could not be sent to the task. event=gateway_tool_setup_refusal_send_failed "
                "setup_request_id=%s correlation_id=%s",
                request.setup_request_id,
                correlation_id,
            )
        else:
            if outcome is not SendOutcome.SENT:
                self._logger.warning(
                    "A tool setup refusal could not be sent to the task. "
                    "event=gateway_tool_setup_refusal_send_failed setup_request_id=%s correlation_id=%s reason=%s",
                    request.setup_request_id,
                    correlation_id,
                    outcome.value,
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

    async def _publish(
        self, result: ToolSetupResultV1, session: SessionRecord, correlation_id: str, conversation_id: str | None
    ) -> SendOutcome:
        """One send of a result to the team lead known now (``SecureResultPublisher``)."""

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
        return await self._publisher.publish(
            result,
            session=session,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            request_kind=REQUEST_KIND_TOOL_SETUP_RESULT,
            request_text=result.message,
            parent_task_id=result.parent_task_id,
        )

    # -- sending kept results ----------------------------------------------------------

    async def resend_kept_results(self) -> ResendPass:
        """Send every kept page result to the team lead known now, oldest first; one pass at a time.

        The one way page results are sent: when a page ends, on every
        team-lead-available transition and lead confirmation
        (``manager_transitions``), at start by a gateway without a team view,
        and from the retry after a failed publish. Each pass reads the kept results
        afresh under the lock, so a result is never sent twice at once. A pass
        stops at the first result it cannot send for want of a lead or because
        the publish failed; a result that can never be sent is marked and
        skipped. A failed publish or an unexpected error while a lead is known
        starts the retry.
        """

        async with self._send_lock:
            sending = await self._send_pass()
        if self._needs_retry(sending):
            self._retry_later()
        return sending

    def _needs_retry(self, sending: ResendPass) -> bool:
        """A pass that stopped on a failed publish or an unexpected error while a lead is known.

        Without a lead the next team-lead-available transition sends the kept
        results; with one, nothing else would until the next page ends, so the
        retry does (critique C09).
        """

        return (sending.stopped is SendOutcome.PUBLISH_FAILED or sending.failed) and self._publisher.route_known

    async def _send_pass(self) -> ResendPass:
        """One pass; it never raises, so a page's ending still posts its closing line (critique C07).

        A missing lead or a failed publish stops the pass: the rest would fail
        the same way. An unexpected error on one result (a database error, for
        example) is logged with its ids and that result stays kept, but the
        pass goes on, so one result that fails every time never holds back the
        results kept after it (critique C11); the pass then counts as failed,
        which the retry follows.
        """

        try:
            records = self._setups.list_unsent_tool_setup_results()
        except Exception as error:  # noqa: BLE001 - the results stay kept; the next pass reads them again
            self._log_resend_failed(None, error)
            return ResendPass(sent=0, failed=True, listing_failed=True)
        sent, failed = 0, False
        for record in records:
            try:
                outcome = await self._send_kept(record)
            except Exception as error:  # noqa: BLE001 - this result stays kept; the next pass sends it
                self._log_resend_failed(record, error)
                failed = True
                continue
            if outcome is SendOutcome.SENT:
                sent += 1
            elif outcome is not None:
                return ResendPass(sent=sent, stopped=outcome, failed=failed)
        return ResendPass(sent=sent, failed=failed)

    def _log_resend_failed(self, record: ToolSetupRecord | None, error: Exception) -> None:
        self._logger.error(
            "A kept setup page result could not be sent because of an error; it stays kept for the next pass. "
            "event=gateway_tool_setup_resend_failed setup_request_id=%s task_id=%s error_type=%s",
            record.setup_request_id if record is not None else "-",
            record.parent_task_id if record is not None else "-",
            type(error).__name__,
            exc_info=error,
        )

    async def _send_kept(self, record: ToolSetupRecord) -> SendOutcome | None:
        """Send one kept result; None when it can never be sent (marked, logged once)."""

        if self._lead_from_records is not None and not self._lead_from_records():
            # Stops the pass with no retry: the lead follower's transition or confirmation sends the kept results
            # once read records name the lead (team_view.TeamLeadFollower).
            self._logger.info(
                "Kept setup page results wait until the team's records name the team lead. "
                "event=gateway_tool_setup_resend_waits_for_records setup_request_id=%s task_id=%s",
                record.setup_request_id,
                record.parent_task_id,
            )
            return SendOutcome.LEAD_NOT_FROM_RECORDS
        try:
            result = ToolSetupResultV1.model_validate_json(record.result_json or "")
        except ValidationError:
            self._mark_unsendable(record, UNSENDABLE_UNREADABLE)
            return None
        session = self._sessions.get_session(record.session_key)
        if session is None:
            self._mark_unsendable(record, UNSENDABLE_SESSION_MISSING)
            return None
        outcome = await self._publish(result, session, record.correlation_id, record.conversation_id)
        if outcome is SendOutcome.SENT and not self._setups.mark_tool_setup_result(
            record.setup_request_id, state=TOOL_SETUP_RESULT_SENT
        ):
            self._logger.warning(
                "A sent setup page result was no longer waiting to be sent; another sender may have sent it too. "
                "event=gateway_tool_setup_result_already_marked setup_request_id=%s task_id=%s",
                record.setup_request_id,
                record.parent_task_id,
            )
        return outcome

    def _mark_unsendable(self, record: ToolSetupRecord, reason: str) -> None:
        """A kept result that can never be sent leaves the kept results, logged once with why."""

        self._setups.mark_tool_setup_result(record.setup_request_id, state=TOOL_SETUP_RESULT_UNSENDABLE)
        self._logger.error(
            "A setup page's result can never be sent to its task, so it is no longer kept. "
            "event=gateway_tool_setup_result_unsendable setup_request_id=%s task_id=%s tool_id=%s kind=%s reason=%s",
            record.setup_request_id,
            record.parent_task_id,
            record.tool_id,
            record.kind,
            reason,
        )

    def _log_if_kept(self, record: ToolSetupRecord, sending: ResendPass) -> None:
        """Log once, when its page ends, that a result is kept for later and why; never raises."""

        try:
            stored = self._setups.get_tool_setup(record.setup_request_id)
        except Exception:  # noqa: BLE001 - the page already ended; its closing line still follows
            self._logger.exception(
                "Could not read whether a setup page's result was sent. event=gateway_tool_setup_result_unreadable "
                "setup_request_id=%s task_id=%s",
                record.setup_request_id,
                record.parent_task_id,
            )
            return
        if stored is None or not stored.result_unsent:
            return
        self._logger.warning(
            "A setup page ended but its result could not be sent to the task; it is kept and sent when a team "
            "lead can be reached. event=gateway_tool_setup_result_kept setup_request_id=%s task_id=%s tool_id=%s "
            "kind=%s status=%s reason=%s",
            record.setup_request_id,
            record.parent_task_id,
            record.tool_id,
            record.kind,
            stored.status,
            sending.reason,
        )

    def _retry_later(self) -> None:
        """Start the retry after a failed send, unless it already runs or the gateway is stopping."""

        if self._closed:
            # A pass still running while the gateway stops (a lead announced
            # itself during close) must not start a retry that outlives it.
            self._logger.info(
                "Kept setup page results are not retried because the gateway is stopping; the next start sends "
                "them. event=gateway_tool_setup_retry_skipped_closed"
            )
            return
        if self._retrying:
            return
        self._retrying = True
        self._retries.start(self._retry_kept_results(), name="gateway_tool_setup_result_retry")

    async def _retry_kept_results(self) -> None:
        """Send the kept results again with backoff while passes fail with a lead known (``_needs_retry``).

        Without a lead the next team-lead-available transition sends them; a
        NATS outage, or a database error, that ends without a lead change
        fires no transition, so this retry sends them then. Failed publishes
        and failed listings (the database is down) are retried for as long as
        they last; passes in which a kept result failed with an unexpected
        error only ``RESEND_ERROR_PASSES_LIMIT`` times in a row, then the retry
        gives up with one line and the results stay kept for the next start or
        transition.
        """

        delay = max(SECURE_RESULT_RETRY_MIN_SECONDS, self._poll_interval_seconds())
        error_passes = 0
        try:
            while True:
                await asyncio.sleep(delay)
                async with self._send_lock:
                    sending = await self._send_pass()
                if not self._needs_retry(sending):
                    return
                error_passes = error_passes + 1 if sending.result_failed else 0
                if error_passes >= RESEND_ERROR_PASSES_LIMIT:
                    self._logger.error(
                        "Stopped retrying the kept setup page results after repeated errors; they stay kept for the "
                        "next gateway start or team lead transition. event=gateway_tool_setup_resend_gave_up "
                        "error_passes=%s",
                        error_passes,
                    )
                    return
                delay = min(SECURE_RESULT_RETRY_MAX_SECONDS, delay * 2)
        finally:
            self._retrying = False

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
