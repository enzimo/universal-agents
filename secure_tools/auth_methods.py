"""Canonical sign-in names shared by gateway and broker.

Tools a person signs in to (``ToolSignIn``): ``/link <tool>`` starts the
tool's sign-in in the secure broker for the person who typed it, so one
command covers every tool, whether it uses a sign-in or a linked account.
The broker has code for each sign-in (``secure_tool_broker.auth_flows``),
which is why this list lives next to the method names rather than in the
tool catalog; ``tests/test_access_catalog.py`` keeps it and the catalog's
``account`` values in step.
"""

from __future__ import annotations

from dataclasses import dataclass


# The method a plain sign-in starts (``/link <tool>`` and the setup page).
# GitHub starts the browser sign-in (``gh auth login --web``), so nobody has
# to create a personal access token; pasting one stays available with
# ``/link cli:github token`` (owner's decision 2026-09-29).
DEFAULT_AUTH_METHODS: dict[str, str] = {
    "github": "web",
    "google": "web",
    "x": "web",
    "bitwarden": "session",
}

# team_client: an administrator stores the team's sign-in app (Google OAuth
# client, X app), which people sign in through with their own accounts.
TEAM_CLIENT_METHOD = "team_client"
GOOGLE_TEAM_CLIENT_METHOD = TEAM_CLIENT_METHOD
GOOGLE_AUTH_METHODS = frozenset({"web", "json_token", TEAM_CLIENT_METHOD})
X_SIGN_IN_PROVIDER = "x"
# Providers whose team sign-in app an administrator stores with /auth <provider> team_client.
TEAM_CLIENT_PROVIDERS = frozenset({"google", X_SIGN_IN_PROVIDER})


def team_client_command(provider: str) -> str:
    """The administrators' command that stores a provider's team sign-in app."""

    return f"/auth {provider} {TEAM_CLIENT_METHOD}"


@dataclass(frozen=True)
class ToolSignIn:
    """How a person signs in to one catalog tool with their own account.

    ``methods`` are the ways a person may choose (``/link cli:github web``);
    ``default_method`` is the one plain ``/link <tool>`` starts. With
    ``team_link`` the tool also takes the team's account link as a fallback
    for people who have not signed in (``/link <tool> team``); without it the
    tool uses only each person's own sign-in.
    """

    tool_id: str
    provider: str
    display_name: str
    default_method: str
    methods: frozenset[str]
    team_link: bool = False

    @property
    def command(self) -> str:
        return f"/link {self.tool_id}"

    @property
    def team_client_command(self) -> str | None:
        return team_client_command(self.provider) if self.provider in TEAM_CLIENT_PROVIDERS else None


SIGN_IN_TOOLS: dict[str, ToolSignIn] = {
    sign_in.tool_id: sign_in
    for sign_in in (
        ToolSignIn(
            "cli:github", "github", "GitHub", DEFAULT_AUTH_METHODS["github"], frozenset({"token", "web", "web-all"})
        ),
        ToolSignIn("cli:google", "google", "Google", DEFAULT_AUTH_METHODS["google"], frozenset({"web", "json_token"})),
        ToolSignIn(
            "cli:x", X_SIGN_IN_PROVIDER, "X", DEFAULT_AUTH_METHODS[X_SIGN_IN_PROVIDER], frozenset({"web"}), team_link=True
        ),
    )
}
SIGN_IN_TOOL_BY_PROVIDER: dict[str, ToolSignIn] = {item.provider: item for item in SIGN_IN_TOOLS.values()}


def tool_sign_in(tool_id: str) -> ToolSignIn | None:
    """The sign-in a person uses for ``tool_id``, or None for tools without one."""

    return SIGN_IN_TOOLS.get(str(tool_id or "").strip())


def sign_in_command(provider: str) -> str:
    """The command that signs a person in to a provider (``/link cli:github``)."""

    return SIGN_IN_TOOL_BY_PROVIDER[provider].command


def sign_in_instead_text(sign_in: ToolSignIn, *, team: bool) -> str:
    """Why a link was refused for a sign-in tool, and what to do instead (gateway and broker)."""

    if not team:
        return (
            f"{sign_in.display_name} uses each person's own sign-in, not a linked account. "
            f"Sign in with {sign_in.command}."
        )
    text = (
        f"{sign_in.display_name} sign-ins are personal: each person signs in with their own account "
        f"with {sign_in.command}, and agents use it only for work for that person. "
        f"There is no team account for {sign_in.display_name}."
    )
    if sign_in.team_client_command:
        text += (
            f" Administrators set the team's {sign_in.display_name} sign-in app, which people sign in "
            f"through, with {sign_in.team_client_command}."
        )
    return text


def normalize_auth_method(provider: str, method: str) -> str:
    """Return the canonical broker auth method without accepting removed aliases."""

    provider_name = str(provider or "").strip().lower()
    normalized = str(method or "").strip().lower()
    if provider_name == "google" and normalized in {"", "form"}:
        return "web"
    return normalized
