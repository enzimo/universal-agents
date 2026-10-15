"""The ``/auth`` chat command: sign-in status, team sign-in apps and password managers.

A person signs in to a tool with ``/link <tool>`` (``commands.account_link``),
the one command for every tool's account. ``/auth`` keeps what is not a
tool's account:

- ``/auth`` and ``/auth status``: the person's sign-in status for every
  provider the broker knows;
- ``/auth google team_client`` and ``/auth x team_client`` (administrators):
  the team's sign-in app that people sign in through with their own accounts.
  The gateway watches the page, posts how it ended in the same chat, and tells
  a task of the same person waiting on that setup (``link_results``);
- ``/auth model`` (administrators): the team's model provider keys, one per
  endpoint address. The gateway lists the endpoints the configuration uses
  (the broker cannot read it); the broker tests each key before storing it
  (``secure_tool_broker.model_key_page``). It needs no working model, so it
  is also how a rejected key is repaired;
- ``/auth bitwarden [server <url>]``, ``/auth onepassword``, ``/auth keepass``,
  ``/auth vault``: the password managers the broker reads saved credentials
  from. They are credential stores, not accounts a tool acts with.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from assistant_gateway.application.broker_sign_in import (
    GatewayBrokerSignIns,
    provider_display_name,
)
from assistant_gateway.application.broker_sign_in import BrokerSignInPage
from assistant_gateway.application.commands.account_link import result_target
from assistant_gateway.application.commands.context import (
    GatewayActorContext,
    GatewayCommandActorResolver,
    resolve_actor_canonical_user_id,
)
from assistant_gateway.application.link_results import GatewayLinkResultWatches
from assistant_gateway.application.ports import GatewaySecureBroker
from assistant_gateway.application.transient_presentation import GatewayTransientAction
from secure_tools.chat_commands import MODEL_KEY_COMMAND, OWN_MODEL_COMMAND
from assistant_gateway.application.model_keys import (
    MODEL_KEY_PROVIDER,
    PERSONAL_KEY_METHOD,
    ModelEndpointUses,
    model_key_page_method,
    model_key_page_options,
    model_key_status_text,
)
from secure_tools.auth_methods import (
    SIGN_IN_TOOL_BY_PROVIDER,
    TEAM_CLIENT_METHOD,
    TEAM_CLIENT_PROVIDERS,
    team_client_command,
)

LOGGER = logging.getLogger(__name__)

# Providers /auth status reports, in order.
_STATUS_PROVIDERS = ("github", "google", "x", "bitwarden", "onepassword", "keepass", "vault")
# Password managers the broker reads saved credentials from.
_CREDENTIAL_STORES = ("bitwarden", "onepassword", "keepass", "vault")
AUTH_COMMAND_USAGE = (
    "Usage: /auth [status | model [me] | google team_client | x team_client | bitwarden [server <url>] | "
    "onepassword | keepass | vault]\n"
    "To sign in to a tool or link its account, use /link <tool_id> (for example /link cli:github)."
)


class GatewayAuthenticationActorResolver(GatewayCommandActorResolver, Protocol):
    """Resolve the person running ``/auth`` and, for team settings, their roles."""

    def team_roles_for_person(self, canonical_user_id: str) -> tuple[str, ...]:
        """Return the person's current active roles in this gateway's team."""


@dataclass(frozen=True)
class AuthenticationCommandRequest:
    """Input for one ``/auth`` command.

    ``session_key`` and ``correlation_id`` say where the result of a
    ``team_client`` page goes, as for ``/link``.
    """

    command: str
    actor: GatewayActorContext
    session_key: str | None = None
    correlation_id: str | None = None


@runtime_checkable
class GatewayAuthenticationCommands(Protocol):
    """Execute typed broker-authentication commands."""

    async def execute(
        self, request: AuthenticationCommandRequest
    ) -> str | GatewayTransientAction:
        """Execute one authentication command."""


class AuthenticationCommandHandler:
    """Execute ``/auth`` without access to the concrete gateway."""

    def __init__(
        self,
        *,
        identity: GatewayAuthenticationActorResolver,
        secure_broker: GatewaySecureBroker,
        team_id: str,
        link_results: GatewayLinkResultWatches | None = None,
        model_endpoints: ModelEndpointUses = tuple,
    ) -> None:
        self._identity = identity
        self._secure_broker = secure_broker
        self._sign_ins = GatewayBrokerSignIns(secure_broker=secure_broker, team_id=team_id)
        # The endpoints the configuration uses, for the /auth model page.
        self._model_endpoints = model_endpoints
        # Watches the team_client page and posts how it ended (link_results).
        self._link_results = link_results

    async def execute(
        self, request: AuthenticationCommandRequest
    ) -> str | GatewayTransientAction:
        """Execute a broker authentication command for the resolved actor."""

        if self._sign_ins.unavailable_message:
            return self._sign_ins.unavailable_message
        person_id = resolve_actor_canonical_user_id(request.actor, self._identity)
        if not person_id:
            return "Credential authentication is available only to registered users."
        words = request.command.split()
        target = words[1].strip().lower() if len(words) > 1 else "status"
        rest = [word.strip() for word in words[2:]]
        method = rest[0].lower() if rest else ""
        if target == "status" and not rest:
            status = _render_auth_status(
                await self._secure_broker.get_auth_status(provider=None, context=self._sign_ins.person_context(person_id))
            )
            return status + "\n\n" + await model_key_status_text(self._secure_broker.get_model_key_status)
        if target == MODEL_KEY_PROVIDER:
            return await self._model_keys(person_id, rest)
        if target in TEAM_CLIENT_PROVIDERS and method == TEAM_CLIENT_METHOD and len(rest) == 1:
            return await self._team_client(target, person_id, request)
        if target in _CREDENTIAL_STORES:
            return await self._credential_store(target, rest, person_id)
        return AUTH_COMMAND_USAGE

    async def _team_client(
        self, provider: str, person_id: str, request: AuthenticationCommandRequest
    ) -> str | GatewayTransientAction:
        """Open the page that stores the team's sign-in app, and watch it.

        The team's sign-in app is an administrator's setting; the gateway
        vouches for the roles and the broker decides.
        """

        roles = self._identity.team_roles_for_person(person_id)
        restart = team_client_command(provider)
        page = await self._sign_ins.open_page(
            provider=provider,
            method=TEAM_CLIENT_METHOD,
            context=self._sign_ins.person_context(person_id, roles),
            restart_command=restart,
        )
        if not isinstance(page, BrokerSignInPage):
            return page
        target = result_target(request, person_id) if self._link_results is not None else None
        if target is not None and self._link_results is not None:
            self._link_results.watch_sign_in(
                page=page,
                restart_command=restart,
                target=target,
                tool_id=SIGN_IN_TOOL_BY_PROVIDER[provider].tool_id,
                team_app=True,
            )
        return page.action

    async def _model_keys(self, person_id: str, words: list[str]) -> str | GatewayTransientAction:
        """Open the page that tests and stores model keys: the team's (administrators) or the person's own.

        The gateway vouches for the roles; the broker decides again.
        """

        roles = self._identity.team_roles_for_person(person_id)
        method = model_key_page_method(words, roles)
        if method is None:
            return AUTH_COMMAND_USAGE
        return await self._sign_ins.start(
            provider=MODEL_KEY_PROVIDER,
            method=method,
            context=self._sign_ins.person_context(person_id, roles),
            restart_command=OWN_MODEL_COMMAND if method == PERSONAL_KEY_METHOD else MODEL_KEY_COMMAND,
            options=model_key_page_options(self._model_endpoints()),
        )

    async def _credential_store(self, target: str, rest: list[str], person_id: str) -> str | GatewayTransientAction:
        """Bitwarden's sign-in form and server setting; the other stores report status only."""

        context = self._sign_ins.person_context(person_id)
        if target == "bitwarden" and rest[:1] == ["server"]:
            return await self._configure_bitwarden_server(rest[1:], context)
        if rest:
            return f"{provider_display_name(target)} does not support broker auth method '{rest[0]}'."
        if target == "bitwarden":
            return await self._sign_ins.start(
                provider=target, method="", context=context, restart_command="/auth bitwarden"
            )
        payload = await self._secure_broker.get_auth_status(provider=target, context=context)
        return (
            _render_auth_status(payload)
            + "\n"
            + f"{provider_display_name(target)} uses broker-side CLI or environment authentication "
            "and has no sign-in page."
        )

    async def _configure_bitwarden_server(self, words: list[str], context: dict[str, Any]) -> str:
        server_url = words[0] if words else ""
        if not server_url:
            return "Usage: /auth bitwarden server <https://bitwarden.example.com>"
        try:
            payload = await self._secure_broker.configure_bitwarden_server(
                server_url=server_url,
                context=context,
            )
        except RuntimeError as exc:
            return f"Bitwarden server URL update failed: {exc}"
        raw_content = payload.get("content")
        content = (
            {str(key): value for key, value in raw_content.items()}
            if isinstance(raw_content, dict)
            else {}
        )
        configured_url = str(content.get("server_url") or server_url).strip()
        return (
            f"Bitwarden CLI server URL configured: {configured_url}\n"
            "Run /auth bitwarden again and complete broker-side API key login "
            "and unlock for that server."
        )


def _auth_status_line(provider: str, payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "unknown")
    message = str(payload.get("message") or "").strip()
    marker = "authenticated" if payload.get("authenticated") else status
    if message:
        return f"- {provider}: {marker} - {message}"
    return f"- {provider}: {marker}"


def _render_auth_status(payload: dict[str, Any]) -> str:
    providers = payload.get("providers")
    if isinstance(providers, dict):
        lines = ["Secure broker auth status:"]
        for provider in _STATUS_PROVIDERS:
            provider_payload = providers.get(provider)
            if isinstance(provider_payload, dict):
                lines.append(_auth_status_line(provider, provider_payload))
        lines.extend(
            (
                "",
                "Sign in to a tool or link its account with /link <tool_id>, for example "
                "/link cli:github, /link cli:google or /link cli:x.",
            )
        )
        return "\n".join(lines)
    provider = str(payload.get("provider") or "provider")
    return "\n".join(
        ["Secure broker auth status:", _auth_status_line(provider, payload)]
    )
