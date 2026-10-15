"""The gateway's side of the ``/accounts`` chat command (``GatewayAccountSharingPort``).

The same rows (``ServiceAccounts``) feed the web chat's Settings > Accounts
tab (``page``, read only), so the tab and the chat list say the same thing.

It also answers ``/setup``: what the person still sets up for their own work
(``assistant_gateway.personal_setup.PersonalSetupSteps``).

Each call acts as the person who typed the command, with the team roles they
hold now (looked up again at every call, never taken from the chat). Reading
is open to every member; changing needs an administrator
(``access_control.people.account_sharing_refusal``). The rules and the record
are ``access_control.account_sharing``.

A change is stored with who made it (and logged in the access event log),
and every administrator gets an Inbox notice, because it changes whose
account agents use for everyone's work. A choice that is already in force
changes nothing and sends nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from access_catalog.catalog import CatalogSource, ToolCatalog
from access_control.account_sharing import (
    ServiceSharing,
    SharingNotChosen,
    TeamAccountSharing,
    fixed_sharing,
    fixed_sharing_text,
    uses_account,
)
from access_control.inbox import StoredAccessInbox
from access_control.people import PERSON_ROLE_ADMIN, account_sharing_refusal
from assistant_gateway.access_inbox import access_inbox_settings
from assistant_gateway.access_setup import MembershipRoles
from assistant_gateway.application.account_overview import (
    AccountsPage,
    AccountsRefused,
    AccountStatus,
    ServiceAccounts,
)
from assistant_gateway.personal_setup import PersonalSetupSteps
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    AccessNoticeV1,
    AccountMode,
    AccountSharing,
    LinkOwnerKind,
    ToolDefinitionV1,
)
from id_generation import new_id
from secure_tools.auth_methods import tool_sign_in
from secure_tools.chat_commands import (
    ACCOUNTS_COMMAND,
    ACCOUNTS_MODEL_SUBJECT,
    ACCOUNTS_PRIVATE,
    ACCOUNTS_SHARE,
    OWN_MODEL_COMMAND,
    link_command,
)
from storage.access_repository import AccessRepository

logger = logging.getLogger(__name__)

_NOT_A_MEMBER = "You are not an active member of this team, so you cannot see or change whose accounts agents use."
MODEL_KEYS_NAME = "Model keys"
_EXPLANATION = (
    "Shared: the team's account serves anyone without their own. "
    "Private: agents use only each person's own account."
)
_NOT_NOTIFIED = " Administrators could not be told in the Inbox; the change still applies."


def model_keys_line(sharing: AccountSharing, *, command: Callable[[str], str] = str) -> str:
    """What the team's model-key sharing means for people added from now on.

    ``command`` formats the command a person sends (``code`` in the
    ``/accounts`` list; plain in change replies).
    """

    if sharing is AccountSharing.SHARED:
        return "shared: people added from now on may use the team's model keys."
    return f"private: people added from now on set up their own model with {command(OWN_MODEL_COMMAND)}."


def tool_effect(tool: ToolDefinitionV1, sharing: AccountSharing, *, team_linked: bool) -> str:
    """What a tool's sharing means for people's work, with the commands to act on it."""

    if sharing is AccountSharing.SHARED:
        effect = "the team's account serves the work of anyone who has not linked their own."
        if not team_linked:
            effect += (
                " No team account is linked yet: an administrator links it with "
                f"{link_command(tool.tool_id, LinkOwnerKind.TEAM)}."
            )
        return effect
    own = link_command(tool.tool_id)
    effect = (
        "agents use only each person's own account; work that serves no person, such as the team's "
        f"background jobs, cannot use it. Each person sets up their own with {own}."
    )
    if team_linked:
        effect += " The team's account stays linked but is not used."
    return effect


def _code(text: str) -> str:
    """A command or tool id as Markdown code, so it stands out in chat."""

    return f"`{text}`"


def _label(text: str) -> str:
    """A label underlined (``__text__``: the web chat, Telegram and Discord show it underlined)."""

    return f"__{text}__:"


def _sharing_words(row: ServiceAccounts) -> str:
    """A service's sharing in a few words, with how a person sets up their own for an always-private one."""

    if row.fixed and row.sharing is AccountSharing.PRIVATE:
        how = "signs in" if row.signs_in else "links their own"
        # The "Your own" part gives the command when it is shown; say it once.
        if row.own_account is not None:
            return f"always private: each person {how}."
        return f"always private: each person {how} with {_code(row.setup_command)}."
    if row.fixed:
        return "always shared: it uses only the team's account."
    return f"{row.sharing.value}."


_TEAM_ACCOUNT_WORDS = {
    AccountStatus.LINKED: "linked.",
    AccountStatus.NOT_LINKED: "not linked.",
    AccountStatus.NOT_USED: "linked, not used while private.",
}


def _own_account_words(row: ServiceAccounts) -> str:
    if row.own_account is AccountStatus.LINKED:
        return "linked."
    if row.own_account is AccountStatus.SIGN_IN:
        return f"sign in with {_code(row.setup_command)}."
    return f"not linked; link it with {_code(row.setup_command)}."


def service_line(row: ServiceAccounts) -> str:
    """One ``/accounts`` list item in chat Markdown: the name in bold, ids and commands as code, labels underlined."""

    if row.subject == ACCOUNTS_MODEL_SUBJECT:
        return f"- **{row.name}**: {model_keys_line(row.sharing, command=_code)}"
    parts = [f"- **{row.name}** ({_code(row.subject)}): {_sharing_words(row)}"]
    if row.team_account is not None:
        parts.append(f"{_label('Team account')} {_TEAM_ACCOUNT_WORDS[row.team_account]}")
    if row.own_account is not None:
        parts.append(f"{_label('Your own')} {_own_account_words(row)}")
    return " ".join(parts)


class GatewayAccountSharing:
    """Implements ``GatewayAccountSharingPort`` and the Settings Accounts tab (module docstring)."""

    def __init__(
        self,
        *,
        store: Callable[[], AccessRepository],
        catalog: Callable[[], ToolCatalog],
        team_id: str,
        send_notice: Callable[[AccessNoticeV1], None],
        setup: PersonalSetupSteps,
        display_name: Callable[[str], str],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._team_id = team_id
        self._send_notice = send_notice
        self._setup = setup
        # The person's name for administrators' notices (the id when the gateway has none).
        self._display_name = display_name
        self._now = now

    @classmethod
    def from_config(cls, config: Any) -> "GatewayAccountSharing":
        """Opened from the gateway's settings; storage is touched only when a command runs."""

        settings = access_inbox_settings(config)
        catalogs = CatalogSource(settings.instance_root, service_urls=settings.service_urls)

        def store() -> AccessRepository:
            return AccessRepository(settings.storage_settings)

        return cls(
            store=store,
            catalog=catalogs.current,
            team_id=settings.team_id,
            send_notice=lambda notice: StoredAccessInbox(store()).send_notice(notice),
            setup=PersonalSetupSteps(store=store, catalog=catalogs.current, team_id=settings.team_id),
            display_name=lambda person_id: MembershipRoles.for_config(config).display_name(person_id),
        )

    def _sharing(self, store: AccessRepository) -> TeamAccountSharing:
        return TeamAccountSharing(store, self._team_id, now=self._now)

    def _team_linked(self, store: AccessRepository, tool_id: str) -> bool:
        return store.active_link(self._team_id, tool_id, LinkOwnerKind.TEAM, self._team_id) is not None

    # -- reading ---------------------------------------------------------------------

    def services(self, *, person_id: str) -> tuple[ServiceAccounts, ...]:
        """The team's model keys, then every service that uses an account, as the person sees them."""

        store = self._store()
        sharing = self._sharing(store)
        tools = self._catalog().tools
        model = sharing.model_keys()
        rows = [
            ServiceAccounts(
                subject=ACCOUNTS_MODEL_SUBJECT,
                name=MODEL_KEYS_NAME,
                sharing=model,
                fixed=False,
                setup_command=OWN_MODEL_COMMAND if model is AccountSharing.PRIVATE else "",
            )
        ]
        rows += [
            self._service(store, tools[row.subject], row, person_id) for row in sharing.overview(tools.values())
        ]
        return tuple(rows)

    def overview(self, *, person_id: str, roles: tuple[str, ...]) -> str:
        if not roles:
            return _NOT_A_MEMBER
        lines = ["Whose account agents use for each service:"]
        lines += [service_line(row) for row in self.services(person_id=person_id)]
        share = _code(f"{ACCOUNTS_COMMAND} {ACCOUNTS_SHARE} <tool_id|{ACCOUNTS_MODEL_SUBJECT}>")
        private = _code(f"{ACCOUNTS_COMMAND} {ACCOUNTS_PRIVATE} <tool_id|{ACCOUNTS_MODEL_SUBJECT}>")
        lines += [_EXPLANATION, f"Administrators change it with {share} or {private}."]
        return "\n".join(lines)

    def page(self, *, person_id: str, roles: tuple[str, ...]) -> AccountsPage:
        """The Settings Accounts tab: the ``/accounts`` rows, and whether this person may change sharing.

        Raises ``AccountsRefused`` for someone who is not an active member, as
        ``overview`` answers them in chat.
        """

        if not roles:
            raise AccountsRefused(_NOT_A_MEMBER)
        return AccountsPage(
            services=self.services(person_id=person_id),
            can_change=account_sharing_refusal(actor_roles=roles) is None,
            share_command=f"{ACCOUNTS_COMMAND} {ACCOUNTS_SHARE}",
            private_command=f"{ACCOUNTS_COMMAND} {ACCOUNTS_PRIVATE}",
        )

    def _service(self, store: AccessRepository, tool: ToolDefinitionV1, row: ServiceSharing, person_id: str) -> ServiceAccounts:
        """One service: its sharing, then the team's and the person's own account where the tool takes them."""

        signs_in = tool_sign_in(tool.tool_id) is not None
        return ServiceAccounts(
            subject=row.subject,
            name=row.display_name,
            sharing=row.sharing,
            fixed=row.fixed,
            team_account=self._team_account(store, tool, row),
            own_account=self._own_account(store, tool, row, person_id, signs_in=signs_in),
            setup_command=link_command(tool.tool_id),
            signs_in=signs_in,
        )

    def _team_account(self, store: AccessRepository, tool: ToolDefinitionV1, row: ServiceSharing) -> AccountStatus | None:
        if tool.account is not AccountMode.LINKED_CREDENTIAL or LinkOwnerKind.TEAM not in tool.link_owners:
            return None
        if not self._team_linked(store, tool.tool_id):
            return AccountStatus.NOT_LINKED
        if row.sharing is AccountSharing.PRIVATE:
            return AccountStatus.NOT_USED
        return AccountStatus.LINKED

    def _own_account(
        self, store: AccessRepository, tool: ToolDefinitionV1, row: ServiceSharing, person_id: str, *, signs_in: bool
    ) -> AccountStatus | None:
        """Whether the person has their own account; ``None`` when the sharing words already say how."""

        if signs_in:
            return None if row.fixed else AccountStatus.SIGN_IN
        if tool.account is not AccountMode.LINKED_CREDENTIAL or LinkOwnerKind.PERSON not in tool.link_owners:
            return None
        own = store.active_link(self._team_id, tool.tool_id, LinkOwnerKind.PERSON, person_id) is not None
        return AccountStatus.LINKED if own else AccountStatus.NOT_LINKED

    def setup(self, *, person_id: str, roles: tuple[str, ...]) -> str:
        if not roles:
            return _NOT_A_MEMBER
        return self._setup.text(person_id)

    # -- changing --------------------------------------------------------------------

    def change(self, *, subject: str, shared: bool, person_id: str, roles: tuple[str, ...]) -> str:
        if not roles:
            return _NOT_A_MEMBER
        refusal = account_sharing_refusal(actor_roles=roles)
        if refusal is not None:
            return refusal
        target = AccountSharing.SHARED if shared else AccountSharing.PRIVATE
        store = self._store()
        sharing = self._sharing(store)
        if subject.lower() == ACCOUNTS_MODEL_SUBJECT:
            return self._change_model_keys(sharing, target, person_id)
        tool = self._catalog().tools.get(subject)
        if tool is None or not uses_account(tool):
            return (
                f"{subject} is not a service that uses an account. "
                f"{ACCOUNTS_COMMAND} lists the ones that do."
            )
        if fixed_sharing(tool) is not None:
            return fixed_sharing_text(tool)
        return self._change_tool(store, sharing, tool, target, person_id)

    def _change_model_keys(self, sharing: TeamAccountSharing, target: AccountSharing, person_id: str) -> str:
        effect = model_keys_line(target)
        if sharing.model_keys() is target:
            return f"{MODEL_KEYS_NAME} are already {effect}"
        sharing.choose_model_keys(target, by=person_id)
        self._log(ACCOUNTS_MODEL_SUBJECT, target, person_id)
        existing = "People already in the team keep their own setting; change one with /user team-keys <usr_...> on|off."
        notified = self._notify(
            subject=ACCOUNTS_MODEL_SUBJECT,
            person_id=person_id,
            title=f"The team's model keys are now {target.value}",
            summary=f"{self._display_name(person_id)} made the team's model keys {effect} {existing}",
        )
        return f"{MODEL_KEYS_NAME} are now {effect} {existing}{notified}"

    def _change_tool(
        self,
        store: AccessRepository,
        sharing: TeamAccountSharing,
        tool: ToolDefinitionV1,
        target: AccountSharing,
        person_id: str,
    ) -> str:
        name = tool.display_name
        effect = tool_effect(tool, target, team_linked=self._team_linked(store, tool.tool_id))
        if sharing.for_tool(tool) is target:
            return f"{name} is already {target.value}: {effect}"
        try:
            sharing.choose_tool(tool, target, by=person_id)
        except SharingNotChosen as exc:
            return str(exc)
        self._log(tool.tool_id, target, person_id)
        notified = self._notify(
            subject=tool.tool_id,
            person_id=person_id,
            title=f"{name} is now {target.value}",
            summary=f"{self._display_name(person_id)} made {name} ({tool.tool_id}) {target.value}: {effect}",
        )
        return f"{name} is now {target.value}: {effect}{self._your_own_work(store, tool, target, person_id)}{notified}"

    def _your_own_work(self, store: AccessRepository, tool: ToolDefinitionV1, target: AccountSharing, person_id: str) -> str:
        """For a service just made private: whether the administrator's own work still has an account.

        The team's account stops serving everyone, the administrator who set it
        up included. Their stored team key is not turned into their own (only
        the broker stores credentials, and moving a key between owners is not
        one of its operations), so the reply says plainly what to send to keep
        using the service themselves.
        """

        if target is not AccountSharing.PRIVATE or not self._team_linked(store, tool.tool_id):
            return ""
        if self._setup.has_own_account(tool, person_id):
            return " Your own work keeps using your own account."
        how = "sign in" if tool_sign_in(tool.tool_id) is not None else "link your own account"
        return (
            " Your own work stops using the team's account too: to keep using it yourself, "
            f"{how} with {link_command(tool.tool_id)}."
        )

    def _log(self, subject: str, target: AccountSharing, person_id: str) -> None:
        logger.info(
            "An administrator changed whether the team's account for a service is shared. "
            "event=account_sharing_changed subject=%s sharing=%s person_id=%s",
            subject,
            target.value,
            person_id,
        )

    def _notify(self, *, subject: str, person_id: str, title: str, summary: str) -> str:
        """Tell every administrator in the Inbox; returns a note for the reply when that failed."""

        try:
            self._send_notice(
                AccessNoticeV1(
                    notice_id=new_id("access_notice"),
                    team_id=self._team_id,
                    kind=AccessNoticeKind.ACCOUNT_SHARING_CHANGED,
                    recipient_roles=(PERSON_ROLE_ADMIN,),
                    title=title[:400],
                    summary=summary[:4000],
                    created_at_utc=self._now(),
                )
            )
        except Exception:  # noqa: BLE001 - the change is stored; the reply says the notice failed
            logger.exception(
                "Could not tell administrators that account sharing changed. "
                "event=account_sharing_notice_failed subject=%s person_id=%s",
                subject,
                person_id,
            )
            return _NOT_NOTIFIED
        return ""


__all__ = [
    "GatewayAccountSharing",
    "MODEL_KEYS_NAME",
    "model_keys_line",
    "service_line",
    "tool_effect",
]
