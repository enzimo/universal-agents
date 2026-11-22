"""The ``/link`` chat command: the one command for every tool's account.

``/link <tool_id>`` does what the tool needs:

- a tool a person signs in to (``cli:github``, ``cli:google``, ``cli:x``;
  ``secure_tools.auth_methods.SIGN_IN_TOOLS``) opens that tool's sign-in page
  in the secure broker for the person who typed it. A third word picks another
  way to sign in where the tool has one (``/link cli:github web``,
  ``/link cli:google json_token``).
- a tool that uses a linked account (``http:*``, ``mcp:*``,
  ``cli:huggingface``) opens the broker's account link form where the person
  chooses which stored credential the tool uses: ``me`` (the default) for
  their own account, ``team`` for the team's, which the broker allows only for
  team administrators and owners.
- ``/link <sign-in tool> team`` explains that sign-ins are personal, except for
  a sign-in tool that also takes the team's account as a fallback (X), which
  opens the team's link form.

The reply carries the page's link. The gateway then watches the page and
posts one message in the same chat when it ends: linked, closed without
linking, expired, or not confirmable (``link_results``). When the page
finished, a task of the same person waiting on that setup for that tool
resumes too. In Web Chat the chat history keeps a secret-free line
("Account link form opened for ...") in place of the card, and the result
message removes the card. Neither a sign-in nor a link grants anybody access.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from assistant_gateway.application.account_links import (
    ACCOUNT_LINK_FORM_ACTION_LABEL,
    ACCOUNT_LINK_NO_ACCESS_NOTE,
    ACCOUNT_LINK_OWNER_PERSON,
    ACCOUNT_LINK_OWNER_TEAM,
    ACCOUNT_LINK_PURPOSE_ACCOUNT,
    ACCOUNT_LINK_PURPOSE_GATE,
    GATE_LINK_FORM_ACTION_LABEL,
    GATE_LINK_NO_ACCESS_NOTE,
    AccountLinkOwner,
    AccountLinkPurpose,
    AccountLinkUnavailable,
    GatewayAccountLinks,
    account_link_owner_label,
    account_link_restart_command,
)
from assistant_gateway.application.broker_sign_in import BrokerSignInPage, GatewayBrokerSignIns
from assistant_gateway.application.commands.context import (
    GatewayActorContext,
    GatewayCommandActorResolver,
    resolve_actor_canonical_user_id,
)
from assistant_gateway.application.link_results import (
    GatewayLinkResultWatches,
    LinkResultTarget,
)
from assistant_gateway.application.transient_presentation import (
    ACCOUNT_LINK_COMMAND_ACTION_KIND,
    GatewayTransientAction,
)
from id_generation import new_id
from secure_tools.auth_methods import ToolSignIn, sign_in_instead_text, tool_sign_in

LOGGER = logging.getLogger(__name__)

ACCOUNT_LINK_COMMAND = "/link"
ACCOUNT_LINK_COMMAND_USAGE = (
    "Usage: /link <tool_id> [me|team|gate]\n"
    "Signs you in to a tool, or links the stored credential it uses. "
    "GitHub, Google and X use your own sign-in: /link cli:github, /link cli:google, /link cli:x. "
    "Other tools use a linked account, for you (me, the default) or for the whole team "
    "(team, administrators only), or a team gate credential (gate, administrators only, for gated web API tools). "
    "Example: /link http:portainer"
)
# Shown as the reason on the broker's form.
_COMMAND_REASON = "You asked for this with /link."
_OWNER_WORDS: dict[str, AccountLinkOwner] = {
    "me": ACCOUNT_LINK_OWNER_PERSON,
    "team": ACCOUNT_LINK_OWNER_TEAM,
}


@dataclass(frozen=True)
class AccountLinkCommandRequest:
    """Input for one ``/link`` command.

    ``session_key`` is the chat session where it ran and ``correlation_id``
    the command's id there (Web Chat's request id); the result message goes to
    that session with that id. Without a session nobody can be told, so the
    page is not watched.
    """

    command: str
    actor: GatewayActorContext
    session_key: str | None = None
    correlation_id: str | None = None


class GatewayAccountLinkActorResolver(GatewayCommandActorResolver, Protocol):
    """Resolve the person running ``/link`` and their current team roles."""

    def team_roles_for_person(self, canonical_user_id: str) -> tuple[str, ...]:
        """Return the person's current active roles in this gateway's team."""


@runtime_checkable
class GatewayAccountLinkCommands(Protocol):
    """Execute typed ``/link`` commands."""

    async def execute(
        self, request: AccountLinkCommandRequest
    ) -> str | GatewayTransientAction:
        """Execute one account link command."""


def is_account_link_command(command: str) -> bool:
    return command == ACCOUNT_LINK_COMMAND or command.startswith(
        f"{ACCOUNT_LINK_COMMAND} "
    )


def sign_in_usage(sign_in: ToolSignIn) -> str:
    ways = " | ".join(sorted(sign_in.methods))
    return (
        f"Usage: {sign_in.command} [{ways}]\n"
        f"Signs you in to {sign_in.display_name} with your own account; plain {sign_in.command} "
        f"uses {sign_in.default_method}."
    )


class AccountLinkCommandHandler:
    """Open the sign-in page or account link form for the person who ran ``/link``."""

    def __init__(
        self,
        *,
        identity: GatewayAccountLinkActorResolver,
        account_links: GatewayAccountLinks,
        sign_ins: GatewayBrokerSignIns,
        link_results: GatewayLinkResultWatches,
    ) -> None:
        self._identity = identity
        self._account_links = account_links
        self._sign_ins = sign_ins
        self._link_results = link_results

    async def execute(
        self, request: AccountLinkCommandRequest
    ) -> str | GatewayTransientAction:
        parts = request.command.split()
        if len(parts) not in {2, 3} or parts[1].lower() == "help":
            return ACCOUNT_LINK_COMMAND_USAGE
        tool_id = parts[1]
        option = parts[2].lower() if len(parts) == 3 else ""
        person_id = resolve_actor_canonical_user_id(request.actor, self._identity)
        if not person_id:
            return "Account links are available only to registered users."
        if option == ACCOUNT_LINK_PURPOSE_GATE:
            return await self._open_link_form(
                tool_id,
                ACCOUNT_LINK_OWNER_TEAM,
                person_id,
                request,
                purpose=ACCOUNT_LINK_PURPOSE_GATE,
            )
        sign_in = tool_sign_in(tool_id)
        if sign_in is not None and option != "team":
            return await self._sign_in(sign_in, option, person_id, request)
        if sign_in is not None and not sign_in.team_link:
            return sign_in_instead_text(sign_in, team=True)
        owner = _OWNER_WORDS.get(option or "me")
        if owner is None:
            return ACCOUNT_LINK_COMMAND_USAGE
        return await self._open_link_form(tool_id, owner, person_id, request)

    async def _sign_in(
        self, sign_in: ToolSignIn, option: str, person_id: str, request: AccountLinkCommandRequest
    ) -> str | GatewayTransientAction:
        """Start the tool's sign-in for the person (``me`` or a way to sign in)."""

        method = sign_in.default_method if option in {"", "me"} else option
        if method not in sign_in.methods:
            return sign_in_usage(sign_in)
        if self._sign_ins.unavailable_message:
            return self._sign_ins.unavailable_message
        restart = sign_in.command if method == sign_in.default_method else f"{sign_in.command} {method}"
        page = await self._sign_ins.open_page(
            provider=sign_in.provider,
            method=method,
            context=self._sign_ins.person_context(person_id),
            restart_command=restart,
        )
        if not isinstance(page, BrokerSignInPage):
            return page
        target = result_target(request, person_id)
        if target is not None:
            self._link_results.watch_sign_in(
                page=page, restart_command=restart, target=target, tool_id=sign_in.tool_id
            )
        return replace(page.action, receipt=f"{page.display_name} sign-in page opened.")

    async def _open_link_form(
        self,
        tool_id: str,
        owner: AccountLinkOwner,
        person_id: str,
        request: AccountLinkCommandRequest,
        *,
        purpose: AccountLinkPurpose = ACCOUNT_LINK_PURPOSE_ACCOUNT,
    ) -> str | GatewayTransientAction:
        gate = purpose == ACCOUNT_LINK_PURPOSE_GATE
        try:
            form = await self._account_links.open_form(
                request_id=new_id("acct_link_cmd"),
                person_id=person_id,
                person_roles=self._identity.team_roles_for_person(person_id),
                tool_id=tool_id,
                owner=owner,
                reason=_COMMAND_REASON,
                purpose=purpose,
            )
        except AccountLinkUnavailable as exc:
            kind_words = "team gate link form" if gate else "account link form"
            return f"Could not open the {kind_words} for {tool_id}. {exc.message}"
        target = result_target(request, person_id)
        if target is not None:
            self._link_results.watch_account_link(
                form=form,
                tool_id=tool_id,
                owner=owner,
                restart_command=account_link_restart_command(tool_id, owner, purpose=purpose),
                target=target,
                purpose=purpose,
            )
        if gate:
            return GatewayTransientAction(
                kind=ACCOUNT_LINK_COMMAND_ACTION_KIND,
                label=GATE_LINK_FORM_ACTION_LABEL,
                description=(
                    f"Link the team's gate credential to {tool_id}. "
                    "Open the form to choose or save the gate credential sent on every request "
                    f"to this tool's hosts. {GATE_LINK_NO_ACCESS_NOTE}"
                ),
                url=form.form_url,
                expires_at_utc=form.expires_at_utc,
                receipt=f"Team gate link form opened for {tool_id}.",
                broker_page=True,
            )
        return GatewayTransientAction(
            kind=ACCOUNT_LINK_COMMAND_ACTION_KIND,
            label=ACCOUNT_LINK_FORM_ACTION_LABEL,
            description=(
                f"Link an account to {tool_id} ({account_link_owner_label(owner)}). "
                "Open the form to choose which stored credential it uses. "
                f"{ACCOUNT_LINK_NO_ACCESS_NOTE}"
            ),
            url=form.form_url,
            expires_at_utc=form.expires_at_utc,
            receipt=(
                f"Account link form opened for {tool_id} "
                f"({account_link_owner_label(owner)})."
            ),
            broker_page=True,
        )


def result_target(request: Any, person_id: str) -> LinkResultTarget | None:
    """Where a ``/link`` or ``/auth`` page's result goes; ``None`` outside a session.

    ``request`` carries ``session_key``, ``correlation_id`` and ``actor``
    (``AccountLinkCommandRequest``, ``AuthenticationCommandRequest``).
    """

    if not request.session_key:
        LOGGER.warning(
            "A page command ran outside a chat session, so no message will say how its page ended. "
            "event=gateway_link_result_no_session channel_name=%s",
            request.actor.channel_name or "-",
        )
        return None
    return LinkResultTarget(
        session_key=request.session_key,
        correlation_id=request.correlation_id or new_id("link_cmd"),
        person_id=person_id,
    )
