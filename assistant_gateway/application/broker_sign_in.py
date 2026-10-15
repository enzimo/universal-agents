"""Open one of the secure broker's sign-in pages for a person the gateway identified.

Two chat commands open these pages, and both go through ``GatewayBrokerSignIns``:

- ``/link <tool>`` for a tool a person signs in to (``cli:github``,
  ``cli:google``, ``cli:x``; ``secure_tools.auth_methods.SIGN_IN_TOOLS``);
- ``/auth`` for what is not a tool's account: the team's sign-in apps
  (``/auth google team_client``, ``/auth x team_client``) and the password
  managers the broker reads saved credentials from (``/auth bitwarden``).

The gateway vouches for the person (and, for a team sign-in app, their current
team roles); the broker decides and renders the page. Credential values go
straight from the page to the broker and never pass through the gateway.

``open_page`` also returns the page's broker-internal status address, which
``/link`` watches to tell the person how the sign-in ended
(``assistant_gateway.application.link_results``); ``/auth`` shows the page
only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlparse

from assistant_gateway.application.ports import (
    GatewaySecureBroker,
    GatewaySecureBrokerRefusal,
)
from assistant_gateway.application.transient_presentation import GatewayTransientAction
from custom_messages.contracts.model_keys_v1 import (
    MODEL_KEY_AUTH_PROVIDER,
    PERSONAL_MODEL_KEY_METHOD,
    TEAM_MODEL_KEY_METHOD,
)
from secure_tools.auth_methods import TEAM_CLIENT_METHOD

LOGGER = logging.getLogger(__name__)

BROKER_SIGN_IN_ACTION_KIND: Final = "broker_authentication"
PROVIDER_DISPLAY_NAMES = {
    "github": "GitHub",
    "google": "Google",
    "x": "X",
    "bitwarden": "Bitwarden",
    "onepassword": "1Password",
    "keepass": "KeePass/KeePassXC",
    "vault": "HashiCorp Vault",
    MODEL_KEY_AUTH_PROVIDER: "Model provider keys",
}
# What the person does on the page, by provider and method ("" is the default).
_PAGE_PURPOSE = {
    ("github", "web"): "complete the GitHub browser sign-in and MFA",
    ("github", "web-all"): "complete the high-privilege GitHub browser sign-in and MFA",
    ("github", "token"): "paste a GitHub token",
    ("google", "web"): "sign in with your Google account",
    ("google", "json_token"): "paste a gog token export JSON",
    ("google", TEAM_CLIENT_METHOD): "store the team's Google sign-in app (OAuth client ID JSON)",
    ("x", "web"): "sign in with your X account",
    ("x", TEAM_CLIENT_METHOD): "store the team's X sign-in app",
    (MODEL_KEY_AUTH_PROVIDER, TEAM_MODEL_KEY_METHOD): "test and save the team's model provider keys",
    (MODEL_KEY_AUTH_PROVIDER, PERSONAL_MODEL_KEY_METHOD): "test and save your own model provider keys",
    ("bitwarden", ""): "complete broker-side API key login and unlock",
    ("bitwarden", "session"): "complete broker-side API key login and unlock",
}


def provider_display_name(provider: str) -> str:
    return PROVIDER_DISPLAY_NAMES.get(provider, provider.title())


def page_purpose(provider: str, method: str) -> str:
    return _PAGE_PURPOSE.get((provider, method), _PAGE_PURPOSE.get((provider, ""), "complete broker sign-in"))


@dataclass(frozen=True)
class BrokerSignInPage:
    """One open sign-in page: the card for the chat and where its status is read.

    ``status_url`` is the broker-internal address (never shown to anyone);
    ``expires_at_utc`` is the page's expiry from the broker.
    """

    action: GatewayTransientAction
    status_url: str
    expires_at_utc: str
    display_name: str


def _login_url_format(login_url: str) -> str:
    parts = [part for part in urlparse(login_url).path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "auth":
        return "signed_v1" if parts[2].startswith("v1.") else "legacy_unsigned"
    return "unknown"


class GatewayBrokerSignIns:
    """Start broker sign-in pages and describe them to the person."""

    def __init__(self, *, secure_broker: GatewaySecureBroker, team_id: str) -> None:
        self._broker = secure_broker
        self._team_id = team_id

    @property
    def unavailable_message(self) -> str:
        """Why the broker cannot open sign-in pages for this gateway, or empty."""

        return str(self._broker.auth_unavailable_message or "")

    def person_context(self, person_id: str, roles: tuple[str, ...] | None = None) -> dict[str, Any]:
        """The trusted context the broker needs; roles only for administrator-only pages."""

        context: dict[str, Any] = {"canonical_user_id": person_id, "team_id": self._team_id, "principal_type": "user"}
        if roles is not None:
            context["person_roles"] = list(roles)
        return context

    async def start(
        self,
        *,
        provider: str,
        method: str,
        context: dict[str, Any],
        restart_command: str,
        options: dict[str, Any] | None = None,
    ) -> str | GatewayTransientAction:
        """Open one sign-in page; the reply carries its link or says plainly why not."""

        page = await self.open_page(
            provider=provider, method=method, context=context, restart_command=restart_command, options=options
        )
        return page.action if isinstance(page, BrokerSignInPage) else page

    async def open_page(
        self,
        *,
        provider: str,
        method: str,
        context: dict[str, Any],
        restart_command: str,
        options: dict[str, Any] | None = None,
    ) -> str | BrokerSignInPage:
        """Open one sign-in page; a string says plainly why it could not open."""

        display_name = provider_display_name(provider)
        LOGGER.info(
            "Requesting broker sign-in page. event=gateway_broker_auth_session_request provider=%s method=%s",
            provider,
            method,
        )
        try:
            payload = await self._broker.create_auth_session(
                provider=provider, method=method or None, context=context, options=options
            )
        except GatewaySecureBrokerRefusal as exc:
            LOGGER.warning(
                "Broker refused the sign-in page. event=gateway_broker_auth_session_refused "
                "provider=%s method=%s status=%s reason_code=%s",
                provider,
                method,
                exc.status,
                exc.reason_code or "-",
            )
            return f"Could not start {display_name} setup. {exc.message}"
        broker_login_url = str(payload.get("login_url") or "")
        if not broker_login_url:
            return str(payload.get("message") or f"The {display_name} sign-in page could not be opened.")
        created_method = str(payload.get("method") or method or "").strip().lower()
        login_url = self._broker.user_form_url(broker_login_url)
        url_format = _login_url_format(broker_login_url)
        LOGGER.info(
            "Broker sign-in page created. event=gateway_broker_auth_session_created provider=%s method=%s "
            "login_url_format=%s login_url_host=%s",
            provider,
            created_method,
            url_format,
            urlparse(login_url).netloc,
        )
        if url_format == "legacy_unsigned":
            LOGGER.error(
                "Broker returned an old in-memory sign-in address. event=gateway_broker_auth_legacy_url_rejected "
                "provider=%s method=%s login_url_host=%s",
                provider,
                created_method,
                urlparse(broker_login_url).netloc,
            )
            return (
                "The secure broker returned an older in-memory sign-in address that will not survive broker "
                "or route changes. Restart the secure tool broker and gateway from the same rebuilt image, "
                f"then run {restart_command} again."
            )
        expires_at = str(payload.get("expires_at") or "")
        return BrokerSignInPage(
            action=GatewayTransientAction(
                kind=BROKER_SIGN_IN_ACTION_KIND,
                label="Open",
                description=(
                    f"{display_name} sign-in started.\n"
                    f"Open the secure page to {page_purpose(provider, created_method)}.\n"
                    "What you enter goes directly to the secure broker and is not sent to the agent."
                ),
                url=login_url,
                expires_at_utc=expires_at or None,
            ),
            status_url=f"{broker_login_url.rstrip('/')}/status",
            expires_at_utc=expires_at,
            display_name=display_name,
        )


__all__ = [
    "BROKER_SIGN_IN_ACTION_KIND",
    "BrokerSignInPage",
    "GatewayBrokerSignIns",
    "page_purpose",
    "provider_display_name",
]
