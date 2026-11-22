"""Tell a person how a page they opened with ``/link``, ``/auth ... team_client`` or ``/upgrade`` ended.

``/link`` opens one of the secure broker's pages for the person who typed it:
an account link form (``http:*``, ``mcp:*``, ``cli:huggingface``, and
``cli:x team``) or a sign-in page (``cli:github``, ``cli:google``,
``cli:x``). ``/auth google team_client`` and ``/auth x team_client`` open the
page that stores the team's sign-in app. ``/upgrade`` opens the team export
page (``team_exports`` says what its message holds). The page finishes in the person's
browser, so the gateway watches it with the shared broker watcher
(``broker_session_watch``) and posts one plain message in the chat session
where the command ran, through the gateway's normal delivery path
(``GatewayEventOutput``): Web Chat records it in the transcript and removes
the command's card with the same correlation id (``chat_delivery.js``);
Telegram, Discord and the other chats show it as a message.

When the page finished, a task of the same person that waits on that setup
for that tool is told too (``tool_setup.GatewayToolSetups.person_finished``),
so the person may use ``/link`` instead of the page the task offered.

Messages never carry credential keys, values or page addresses; they say what
happened and where to check (Settings → Access → By Tool for linked accounts,
``/auth status`` for sign-ins).

Watches live in gateway memory only. After a gateway restart nobody watches a
page that is still open: the page itself keeps working, but no message follows
(documented for people with ``/link``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from assistant_gateway.application.account_links import (
    ACCOUNT_LINK_NO_ACCESS_NOTE,
    ACCOUNT_LINK_PURPOSE_ACCOUNT,
    ACCOUNT_LINK_PURPOSE_GATE,
    ACCOUNT_LINK_STATUS_CREATED,
    ACCOUNT_LINK_STATUS_DECLINED,
    GATE_LINK_NO_ACCESS_NOTE,
    REASON_SESSION_EXPIRED,
    AccountLinkForm,
    AccountLinkOwner,
    AccountLinkPurpose,
    account_link_owner_label,
    account_link_status_is_final,
)
from assistant_gateway.application.broker_session_watch import (
    SIGN_IN_MAY_REMAIN_NOTE,
    WATCH_EXPIRED,
    WATCH_FINISHED,
    BrokerSessionOutcome,
    BrokerSessionWatches,
    watch_broker_session,
    watch_deadline,
)
from assistant_gateway.application.broker_sign_in import BrokerSignInPage
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.application.team_exports import team_export_result_text, team_export_status_is_final
from assistant_gateway.application.ports import (
    GatewayClock,
    GatewayEventOutput,
    GatewaySecureBroker,
    GatewaySecureBrokerRefusal,
)
from assistant_gateway.domain.message_types import LINK_RESULT_MESSAGE_TYPE
from assistant_gateway.ports.repositories import SessionRepository
from secure_tools.auth_methods import SIGN_IN_STATUS_COMMAND
from secure_tools.broker_pages import (
    SIGN_IN_FINAL_STATUSES,
    SIGN_IN_STATUS_DECLINED,
    SIGN_IN_STATUS_ERROR,
    SIGN_IN_STATUS_SUCCESS,
)
from custom_messages.contracts.credentials_v1 import (
    TOOL_SETUP_ACCOUNT_LINK,
    TOOL_SETUP_SIGN_IN,
    TOOL_SETUP_TEAM_SIGN_IN_APP,
)

ACCESS_PAGE_BY_TOOL = "Settings → Access → By Tool"
SIGN_IN_NO_ACCESS_NOTE = "Signing in gives nobody access by itself."
# Page kinds in logs.
_PAGE_ACCOUNT_LINK = "account_link"
_PAGE_SIGN_IN = "sign_in"
_PAGE_TEAM_EXPORT = "team_export"


@dataclass(frozen=True)
class LinkResultTarget:
    """Where the result goes: the session where the command ran and its correlation id.

    In Web Chat the correlation id is the chat request's id, which is also the
    command card's, so the result message removes the card. ``person_id`` is
    the person who ran it, whose waiting tasks hear about a finished setup.
    """

    session_key: str
    correlation_id: str
    person_id: str = ""


class PersonFinishedSetups(Protocol):
    """Tell tasks waiting on a setup that the person finished it themselves."""

    async def person_finished(
        self,
        *,
        person_id: str,
        tool_id: str,
        kind: str,
        owner: str | None = None,
        account_label: str | None = None,
    ) -> int:
        """End the matching waits; the number of tasks told."""


@runtime_checkable
class GatewayLinkResultWatches(Protocol):
    """Watch the pages ``/link`` opened and post how they ended."""

    def watch_account_link(
        self,
        *,
        form: AccountLinkForm,
        tool_id: str,
        owner: AccountLinkOwner,
        restart_command: str,
        target: LinkResultTarget,
        purpose: AccountLinkPurpose = ACCOUNT_LINK_PURPOSE_ACCOUNT,
    ) -> None:
        """Watch one account or gate link form until it is used or expires."""

    def watch_sign_in(
        self,
        *,
        page: BrokerSignInPage,
        restart_command: str,
        target: LinkResultTarget,
        tool_id: str,
        team_app: bool = False,
    ) -> None:
        """Watch one sign-in page (or, with ``team_app``, a team sign-in app page) until it ends."""

    def watch_team_export(self, *, page: BrokerSignInPage, target: LinkResultTarget) -> None:
        """Watch one ``/upgrade`` export page until its export ends or the page closes."""


def _sentence(text: str) -> str:
    words = " ".join(str(text or "").split())
    return words if not words or words.endswith((".", "!", "?")) else f"{words}."


def _form_result(outcome: BrokerSessionOutcome) -> dict[str, Any]:
    result = outcome.payload.get("result")
    return result if isinstance(result, dict) else {}


def created_link_mismatch(
    outcome: BrokerSessionOutcome,
    *,
    tool_id: str,
    owner: str,
    purpose: AccountLinkPurpose = ACCOUNT_LINK_PURPOSE_ACCOUNT,
) -> bool:
    """Whether the broker reports a created link for another tool, owner or purpose than the form's."""

    result = _form_result(outcome)
    reported_purpose = str(result.get("purpose") or ACCOUNT_LINK_PURPOSE_ACCOUNT)
    return (
        outcome.ending == WATCH_FINISHED
        and str(result.get("status") or "") == ACCOUNT_LINK_STATUS_CREATED
        and (
            result.get("tool_id") != tool_id
            or result.get("owner_kind") != owner
            or reported_purpose != purpose
        )
    )


def account_link_result_text(
    outcome: BrokerSessionOutcome,
    *,
    tool_words: str,
    tool_id: str,
    owner: str,
    restart_command: str,
    purpose: AccountLinkPurpose = ACCOUNT_LINK_PURPOSE_ACCOUNT,
) -> str:
    """The message for one ``/link`` account or gate link form; ``tool_words`` is "Crawl4AI (mcp:crawl4ai)"."""

    gate = purpose == ACCOUNT_LINK_PURPOSE_GATE
    what = "team gate link" if gate else "account link"
    no_access = GATE_LINK_NO_ACCESS_NOTE if gate else ACCOUNT_LINK_NO_ACCESS_NOTE
    expired = (
        f"The {what} form for {tool_words} expired; nothing was linked. "
        f"Run {restart_command} again."
    )
    if outcome.ending == WATCH_EXPIRED:
        return expired
    unconfirmed = (
        f"Couldn't confirm whether the {what} for {tool_words} finished. "
        f"Check {ACCESS_PAGE_BY_TOOL}."
    )
    if outcome.ending != WATCH_FINISHED:
        return unconfirmed
    result = _form_result(outcome)
    status = str(result.get("status") or outcome.payload.get("status") or "")
    if status == ACCOUNT_LINK_STATUS_CREATED:
        if created_link_mismatch(outcome, tool_id=tool_id, owner=owner, purpose=purpose):
            # Never tell a person a different or unnamed link exists.
            return unconfirmed
        return (
            f"Linked {account_link_owner_label(owner, purpose=purpose)} to {tool_words}. "
            f"{ACCESS_PAGE_BY_TOOL} lists it. {no_access}"
        )
    if status == ACCOUNT_LINK_STATUS_DECLINED:
        return f"The {what} form for {tool_words} was closed without linking anything."
    if result.get("reason_code") == REASON_SESSION_EXPIRED:
        return expired
    why = _sentence(str(result.get("error") or ""))
    return f"Nothing was linked to {tool_words}. {why or f'Run {restart_command} to try again.'}"


def sign_in_result_text(
    outcome: BrokerSessionOutcome, *, display_name: str, restart_command: str, team_app: bool = False
) -> str:
    """The message for one ``/link`` sign-in page or ``/auth ... team_client`` page."""

    what = f"team's {display_name} sign-in app" if team_app else f"{display_name} sign-in"
    if outcome.ending == WATCH_EXPIRED:
        return f"The {what} page expired before it was finished. Run {restart_command} again."
    status = str(outcome.payload.get("status") or "")
    if outcome.ending != WATCH_FINISHED or status not in SIGN_IN_FINAL_STATUSES:
        return f"Couldn't confirm whether the {what} finished. Check with {SIGN_IN_STATUS_COMMAND}."
    if status == SIGN_IN_STATUS_DECLINED:
        if outcome.sign_in_may_remain:
            return f"You declined the {what} page. {SIGN_IN_MAY_REMAIN_NOTE}"
        return f"You closed the {what} page without finishing. Run {restart_command} to try again."
    if status == SIGN_IN_STATUS_ERROR:
        page = "sign-in page" if not team_app else "page"
        return f"The {what} did not finish; the {page} says why. Run {restart_command} to try again."
    if team_app:
        return f"The team's {display_name} sign-in app is stored; people can now sign in with it."
    username = " ".join(str(outcome.payload.get("username") or "").split())
    signed_in = f"Signed in to {display_name} as {username}." if username else f"Signed in to {display_name}."
    return f"{signed_in} {SIGN_IN_NO_ACCESS_NOTE}"


def _page_completed(outcome: BrokerSessionOutcome, *, sign_in: bool) -> bool:
    """Whether a watched page finished with the setup done."""

    if outcome.ending != WATCH_FINISHED:
        return False
    if sign_in:
        return str(outcome.payload.get("status") or "") == SIGN_IN_STATUS_SUCCESS
    return str(_form_result(outcome).get("status") or "") == ACCOUNT_LINK_STATUS_CREATED


def _read_error_words(error: Exception) -> str:
    """How a status read failed, for logs, without the error text.

    Error text can hold the page's address, and a page address is a one-time
    key to the page.
    """

    if isinstance(error, GatewaySecureBrokerRefusal):
        return f"refused status={error.status}"
    return type(error).__name__


class GatewayLinkResults:
    """Watch ``/link`` pages and post one message per page when it ends."""

    def __init__(
        self,
        *,
        broker: GatewaySecureBroker,
        sessions: SessionRepository,
        event_output: GatewayEventOutput,
        clock: GatewayClock,
        tool_setups: PersonFinishedSetups | None = None,
        poll_interval_seconds: float = 2.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._broker = broker
        self._sessions = sessions
        self._event_output = event_output
        self._clock = clock
        # Tasks waiting on the same setup (assistant_gateway.application.tool_setup).
        self._tool_setups = tool_setups
        self._poll_interval_seconds = poll_interval_seconds
        self._logger = logger or logging.getLogger(__name__)
        self._watches = BrokerSessionWatches(owner="link_results", logger=self._logger)

    @property
    def pending_count(self) -> int:
        return self._watches.count

    async def close(self) -> None:
        await self._watches.close()

    def watch_account_link(
        self,
        *,
        form: AccountLinkForm,
        tool_id: str,
        owner: AccountLinkOwner,
        restart_command: str,
        target: LinkResultTarget,
        purpose: AccountLinkPurpose = ACCOUNT_LINK_PURPOSE_ACCOUNT,
    ) -> None:
        self._watches.start(
            self._finish_account_link(form, tool_id, owner, restart_command, target, purpose),
            name=f"gateway_link_result_{form.session_id[:12]}",
        )

    def watch_sign_in(
        self,
        *,
        page: BrokerSignInPage,
        restart_command: str,
        target: LinkResultTarget,
        tool_id: str,
        team_app: bool = False,
    ) -> None:
        if not page.expires_at_utc:
            self._logger.warning(
                "The broker gave the sign-in page no expiry, so its result is not watched. "
                "event=gateway_link_sign_in_not_watched display_name=%s session_key=%s correlation_id=%s",
                page.display_name,
                target.session_key,
                target.correlation_id,
            )
            return
        self._watches.start(
            self._finish_sign_in(
                page, restart_command, target, tool_id, TOOL_SETUP_TEAM_SIGN_IN_APP if team_app else TOOL_SETUP_SIGN_IN
            ),
            name=f"gateway_link_sign_in_result_{target.correlation_id[:24]}",
        )

    def watch_team_export(self, *, page: BrokerSignInPage, target: LinkResultTarget) -> None:
        self._watches.start(
            self._finish_team_export(page, target),
            name=f"gateway_team_export_result_{target.correlation_id[:24]}",
        )

    async def _finish_team_export(self, page: BrokerSignInPage, target: LinkResultTarget) -> None:
        outcome = await watch_broker_session(
            read_status=lambda: self._broker.get_sign_in_status(status_url=page.status_url),
            is_final=team_export_status_is_final,
            deadline=watch_deadline(page.expires_at_utc),
            clock=self._clock,
            poll_interval_seconds=self._poll_interval_seconds,
            on_read_failed=lambda error: self._log_read_failed(_PAGE_TEAM_EXPORT, target, error),
        )
        await self._post(
            target, team_export_result_text(outcome), page=_PAGE_TEAM_EXPORT, outcome=outcome, owner="team"
        )

    async def _finish_account_link(
        self,
        form: AccountLinkForm,
        tool_id: str,
        owner: AccountLinkOwner,
        restart_command: str,
        target: LinkResultTarget,
        purpose: AccountLinkPurpose = ACCOUNT_LINK_PURPOSE_ACCOUNT,
    ) -> None:
        outcome = await watch_broker_session(
            read_status=lambda: self._broker.get_account_link_status(
                status_url=form.status_url, session_id=form.session_id
            ),
            is_final=account_link_status_is_final,
            deadline=watch_deadline(form.expires_at_utc),
            clock=self._clock,
            poll_interval_seconds=self._poll_interval_seconds,
            on_read_failed=lambda error: self._log_read_failed(_PAGE_ACCOUNT_LINK, target, error),
        )
        if created_link_mismatch(outcome, tool_id=tool_id, owner=owner, purpose=purpose):
            self._logger.error(
                "The broker reported a link for another tool, owner or purpose than the /link form's. "
                "event=gateway_link_result_mismatch tool_id=%s owner=%s purpose=%s session_key=%s correlation_id=%s",
                tool_id,
                owner,
                purpose,
                target.session_key,
                target.correlation_id,
            )
        text = account_link_result_text(
            outcome,
            tool_words=f"{form.tool_display_name} ({tool_id})",
            tool_id=tool_id,
            owner=owner,
            restart_command=restart_command,
            purpose=purpose,
        )
        await self._post(target, text, page=_PAGE_ACCOUNT_LINK, outcome=outcome, owner=owner)
        if (
            purpose == ACCOUNT_LINK_PURPOSE_ACCOUNT
            and _page_completed(outcome, sign_in=False)
            and not created_link_mismatch(outcome, tool_id=tool_id, owner=owner, purpose=purpose)
        ):
            await self._tell_waiting_tasks(target, tool_id=tool_id, kind=TOOL_SETUP_ACCOUNT_LINK, owner=owner)

    async def _finish_sign_in(
        self, page: BrokerSignInPage, restart_command: str, target: LinkResultTarget, tool_id: str, kind: str
    ) -> None:
        outcome = await watch_broker_session(
            read_status=lambda: self._broker.get_sign_in_status(status_url=page.status_url),
            is_final=lambda payload: str(payload.get("status") or "") in SIGN_IN_FINAL_STATUSES,
            deadline=watch_deadline(page.expires_at_utc),
            clock=self._clock,
            poll_interval_seconds=self._poll_interval_seconds,
            on_read_failed=lambda error: self._log_read_failed(_PAGE_SIGN_IN, target, error),
        )
        team_app = kind == TOOL_SETUP_TEAM_SIGN_IN_APP
        text = sign_in_result_text(
            outcome, display_name=page.display_name, restart_command=restart_command, team_app=team_app
        )
        await self._post(target, text, page=_PAGE_SIGN_IN, outcome=outcome, owner="team" if team_app else "person")
        if _page_completed(outcome, sign_in=True):
            label = " ".join(str(outcome.payload.get("username") or "").split())[:200] or None
            await self._tell_waiting_tasks(target, tool_id=tool_id, kind=kind, account_label=label)

    async def _tell_waiting_tasks(
        self,
        target: LinkResultTarget,
        *,
        tool_id: str,
        kind: str,
        owner: str | None = None,
        account_label: str | None = None,
    ) -> None:
        """A task of this person waiting on the same setup hears that it is done."""

        if self._tool_setups is None or not target.person_id:
            return
        try:
            told = await self._tool_setups.person_finished(
                person_id=target.person_id, tool_id=tool_id, kind=kind, owner=owner, account_label=account_label
            )
        except Exception:
            self._logger.exception(
                "Could not tell waiting tasks that the person finished a setup. "
                "event=gateway_link_result_task_notice_failed tool_id=%s kind=%s session_key=%s",
                tool_id,
                kind,
                target.session_key,
            )
            return
        if told:
            self._logger.info(
                "Told waiting tasks that the person finished a setup themselves. "
                "event=gateway_link_result_tasks_told tool_id=%s kind=%s count=%s",
                tool_id,
                kind,
                told,
            )

    def _log_read_failed(self, page: str, target: LinkResultTarget, error: Exception) -> None:
        self._logger.warning(
            "Could not read a /link page's status from the broker. "
            "event=gateway_link_result_read_failed page=%s session_key=%s correlation_id=%s error=%s",
            page,
            target.session_key,
            target.correlation_id,
            _read_error_words(error),
        )

    async def _post(
        self,
        target: LinkResultTarget,
        text: str,
        *,
        page: str,
        outcome: BrokerSessionOutcome,
        owner: str,
    ) -> None:
        """Deliver the result to the chat session where ``/link`` ran."""

        details: dict[str, Any] = {
            "page": page,
            "ending": outcome.ending,
            "status": str(outcome.payload.get("status") or "-"),
            "owner": owner,
            "session_key": target.session_key,
            "correlation_id": target.correlation_id,
        }
        session = self._sessions.get_session(target.session_key)
        if session is None:
            self._logger.warning(
                "The chat that ran /link is gone, so its result was not posted. "
                "event=gateway_link_result_session_missing %s",
                _log_fields(details),
            )
            return
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
                    correlation_id=target.correlation_id,
                    original_message_id=None,
                    message_type=LINK_RESULT_MESSAGE_TYPE,
                )
            )
        except Exception:
            self._logger.exception(
                "Could not post a /link result. event=gateway_link_result_post_failed %s",
                _log_fields(details),
            )
            return
        self._logger.info(
            "Posted a /link result. event=gateway_link_result_posted %s", _log_fields(details)
        )


def _log_fields(details: dict[str, Any]) -> str:
    return " ".join(f"{key}={value}" for key, value in details.items())


__all__ = [
    "GatewayLinkResultWatches",
    "GatewayLinkResults",
    "LinkResultTarget",
    "PersonFinishedSetups",
    "account_link_result_text",
    "sign_in_result_text",
]
