"""The broker's account link form: a person links a stored credential to a tool.

An account link (``AccountLinkV1``) says which stored credential the broker
uses for one tool, for the team or for one person, and where each credential
field goes. It replaces credential bindings, OpenShell bindings and MCP
credential slots. Links never grant access: agents and people still need
access to the tool.

The gateway opens a session for a person it has identified
(``AccountLinkRequestV1``); the person opens the one-time form, sees exactly
what will happen (whose account, which agents use it, which hosts or
connection, which header or variable receives which field), and either picks
one of their stored credentials (or one of the team's, for administrators) or
types a new one, and confirms. A new credential is stored through the broker's
own credential store path (``collect_credential``) in the link owner's
collection and linked in the same submission, so one form finishes the job
(access plan section 9.4). The broker stores the link in the access store;
credential values never leave the broker.
"""

from __future__ import annotations

import html
import logging
import re
import threading
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from access_catalog.catalog import ToolCatalog
from access_control.account_sharing import TeamAccountSharing, team_account_private_text
from access_control.entities import AGENT_ROLE_SYSTEM, AGENT_ROLE_TEAM_LEAD, AGENT_ROLE_WORKER
from access_control.people import is_administrator
from access_control.standard_grants import standard_grants
from custom_messages.contracts.access_v2 import (
    CREDENTIAL_KEY_PATTERN,
    AccountLinkRequestV1,
    AccountLinkV1,
    AccountMode,
    CredentialInjectionV1,
    GrantKind,
    GrantStatus,
    GrantV1,
    InjectionPlacement,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    ToolDefinitionV1,
    ToolKind,
)
from id_generation import new_id
from secure_tool_broker.auth_pages import render_credential_input, render_session_countdown
from secure_tool_broker.credential_catalog import collect_credential, load_catalog
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.redaction import redact_text
from secure_tools.auth_methods import sign_in_instead_text, tool_sign_in
from storage.access_repository import AccessRepository

if TYPE_CHECKING:
    from access_control.decisions import AccessService

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_CREATED = "account_link_created"
STATUS_DECLINED = "account_link_declined"
STATUS_UNAVAILABLE = "account_link_unavailable"
# An unexpected failure while linking (the error itself is logged).
ACCOUNT_LINK_FAILED = "account_link_failed"
# The tool uses each person's own sign-in (/link <tool> starts it), not a link.
REASON_USES_SIGN_IN = "account_link_uses_sign_in"
# Hub CLIs read a fixed credential field instead of declared injections.
FIXED_CLI_FIELDS = {"cli:huggingface": "api_token", "cli:x": "oauth_json"}

# The "Add a new credential" part of the form sends this marker and one
# ``field__<name>`` value per credential field; the stored-credential part
# sends ``credential_key`` instead.
CREDENTIAL_SOURCE_FIELD = "credential_source"
NEW_CREDENTIAL = "new"
NEW_FIELD_PREFIX = "field__"
REASON_FIELDS_MISSING = "account_link_credential_fields_missing"
REASON_KEY_UNAVAILABLE = "account_link_credential_key_unavailable"
REASON_NOT_STORED = "account_link_credential_not_stored"
# Numbered keys tried for a new credential (<tool>-team, <tool>-team-2, ...).
_MAX_KEY_ATTEMPTS = 100
# Choosing a free key and storing under it happen together, so two forms
# submitted at the same time never pick the same key and replace each other.
_NEW_CREDENTIAL_LOCK = threading.Lock()
_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
# Agent roles in plain words for the "Used by" line; people's roles are left out.
_AGENT_ROLE_WORDS = {
    AGENT_ROLE_TEAM_LEAD: "the team lead",
    AGENT_ROLE_WORKER: "every worker agent",
    AGENT_ROLE_SYSTEM: "the system agents",
}


class AccountLinkRefused(ValueError):
    """The link cannot be created; the message says why in plain language."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass
class AccountLinkSession:
    request: AccountLinkRequestV1
    created_at: datetime
    expires_at: datetime
    status: str = STATUS_PENDING
    result: dict[str, object] | None = None
    used: bool = False
    submission_in_progress: bool = False


def owner_scope(request: AccountLinkRequestV1) -> CredentialOwnerScope:
    if request.owner_kind is LinkOwnerKind.TEAM:
        return CredentialOwnerScope.team(request.team_id)
    return CredentialOwnerScope.user(request.person_id)


REASON_TEAM_ACCOUNT_PRIVATE = "account_link_team_private"
REASON_ACCESS_STORE_MISSING = "account_link_store_missing"
# The broker runs with the manager's access store; only a gate built without
# one (tests, a broken start) has none, and it then finds and saves no accounts.
ACCESS_STORE_MISSING_TEXT = "The broker was started without the manager's access store, so it cannot find or save accounts."
ACCESS_STORE_MISSING_NEXT_STEP = "An operator restarts the broker with the team's storage settings (state.storage)."


def linkable_tool(
    catalog: ToolCatalog, request: AccountLinkRequestV1, sharing: TeamAccountSharing | None
) -> ToolDefinitionV1:
    """The tool to link, after checking the person may link for this owner.

    The team's account is linked only for a service the team shares
    (``access_control.account_sharing``): for a private one it would never
    be used. ``sharing`` is None when the broker has no access store, which
    also means it has nowhere to save the link.
    """

    if sharing is None:
        raise AccountLinkRefused(ACCESS_STORE_MISSING_TEXT, reason_code=REASON_ACCESS_STORE_MISSING)
    tool = catalog.tools.get(request.tool_id)
    if tool is None:
        raise AccountLinkRefused(f"{request.tool_id} is not in the tool catalog.", reason_code="account_link_unknown_tool")
    sign_in = tool_sign_in(tool.tool_id)
    if sign_in is not None and (request.owner_kind is LinkOwnerKind.PERSON or not sign_in.team_link):
        # A person's own account for a sign-in tool is their sign-in, never a link.
        raise AccountLinkRefused(
            sign_in_instead_text(sign_in, team=request.owner_kind is LinkOwnerKind.TEAM),
            reason_code=REASON_USES_SIGN_IN,
        )
    if tool.account is not AccountMode.LINKED_CREDENTIAL:
        raise AccountLinkRefused(
            f"{tool.display_name} does not use an account, so there is nothing to link.",
            reason_code="account_link_not_needed",
        )
    if request.owner_kind.value not in tool.link_owners:
        whose = "the team's account" if tool.link_owners == ("team",) else "each person's own account"
        raise AccountLinkRefused(
            f"{tool.display_name} uses only {whose}.", reason_code="account_link_owner_not_used"
        )
    if request.owner_kind is LinkOwnerKind.TEAM and not sharing.team_account_used(tool):
        raise AccountLinkRefused(team_account_private_text(tool), reason_code=REASON_TEAM_ACCOUNT_PRIVATE)
    if request.owner_kind is LinkOwnerKind.TEAM and not is_administrator(request.person_roles):
        raise AccountLinkRefused(
            "Only a team administrator or owner can link the team's account.",
            reason_code="account_link_team_admin_required",
        )
    return tool


def required_fields(tool: ToolDefinitionV1) -> set[str]:
    fields = {injection.field for injection in tool.credential_injections}
    if tool.tool_id in FIXED_CLI_FIELDS:
        fields.add(FIXED_CLI_FIELDS[tool.tool_id])
    return fields


def credential_choices(scope: CredentialOwnerScope, tool: ToolDefinitionV1) -> list[dict[str, str]]:
    """Stored credentials of the owner that have every field the tool uses."""

    needed = required_fields(tool)
    catalog = load_catalog(scope=scope)
    return [
        {"credential_key": key, "display_name": credential.display_name, "purpose": credential.purpose}
        for key, credential in sorted(catalog.credentials.items())
        if needed <= set(credential.fields)
    ]


# -- "Used by": which agents use the linked account -------------------------------------


def _agent_holder(subject: SubjectRefV1) -> str | None:
    """An agent-side grant subject in plain words; None for people and people's roles."""

    name = subject.id.split("/", 1)[-1]
    if subject.kind is SubjectKind.AGENT_TYPE:
        return name
    if subject.kind is SubjectKind.AGENT:
        return f"agent {subject.id}"
    if subject.kind is SubjectKind.ROLE:
        return _AGENT_ROLE_WORDS.get(name)
    if subject.kind is SubjectKind.TEAM:
        return "every agent in the team"
    return None


def _in_force(grant: GrantV1, now: datetime) -> bool:
    """An active access grant that has not expired or run out of uses (presentation only)."""

    return (
        grant.kind is GrantKind.ACCESS
        and grant.status is GrantStatus.ACTIVE
        and (grant.expires_at_utc is None or now < grant.expires_at_utc)
        and (grant.uses_remaining is None or grant.uses_remaining > 0)
    )


def _serves_owner(grant: GrantV1, request: AccountLinkRequestV1) -> bool:
    """The team's account serves everyone's work; a person's only work for that person (D13)."""

    return request.owner_kind is LinkOwnerKind.TEAM or grant.for_person_id in (None, request.person_id)


def _reaches_tool(grant: GrantV1, tool: ToolDefinitionV1) -> bool:
    target = grant.target
    if target.kind in (TargetKind.TOOL, TargetKind.OPERATION):
        return target.id.partition("#")[0] == tool.tool_id
    return target.kind is TargetKind.TOOL_SET and target.id in tool.tool_sets


def agents_using(access: AccessService, tool: ToolDefinitionV1, request: AccountLinkRequestV1) -> list[str]:
    """Agent types (or single agents, or agent roles) holding access to the tool for the owner's work.

    Presentation only, like the Access page (``assistant_gateway.access_map``):
    it lists access grants in force whose subject is on the agent side and
    whose target reaches the tool (the tool, one of its operations, or a tool
    set it belongs to), including standard grants and grants for one task, so
    the form opened right after a proposal's approval already names the
    agents that were given the tool. Blocks are not subtracted, so the line
    may name more agents than can use the tool right now, never fewer. The
    access service still decides every call.
    """

    now = access.clock()
    holders = {
        holder
        for grant in [*standard_grants(access.team_id), *access.store.list_team(access.team_id)]
        if (holder := _agent_holder(grant.subject)) is not None
        and _in_force(grant, now)
        and _serves_owner(grant, request)
        and _reaches_tool(grant, tool)
    }
    return sorted(holders, key=str.lower)


def _plain_list(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def used_by_text(access: AccessService | None, tool: ToolDefinitionV1, request: AccountLinkRequestV1) -> str:
    """The form's "Used by" line: who uses the linked account, for whose work.

    ``access`` is None when authorization is off; then every agent may call
    the tool, and the line says so instead of listing grants.
    """

    if access is None:
        return f"Any agent that uses {tool.display_name}, because access checks are off for this team."
    holders = agents_using(access, tool, request)
    if not holders:
        return (
            f"No agent holds access to {tool.display_name} yet. "
            "Agents use this account only after they are given access."
        )
    if request.owner_kind is LinkOwnerKind.TEAM:
        sign_in = tool_sign_in(tool.tool_id)
        if sign_in is not None:
            return (
                f"{_plain_list(holders)}, when working for anyone in the team who has not signed in "
                f"with {sign_in.command}."
            )
        return f"{_plain_list(holders)}, when working for anyone in the team who has not linked their own account."
    return f"{_plain_list(holders)}, when working for you."


def _describe_injection(injection: CredentialInjectionV1) -> str:
    shown = html.escape(injection.template.replace("{value}", f"‹{injection.field}›"))
    if injection.placement is InjectionPlacement.HEADER:
        return f"Header <code>{html.escape(injection.name)}</code>: <code>{shown}</code>"
    if injection.placement is InjectionPlacement.QUERY:
        return f"Query value <code>{html.escape(injection.name)}</code>: <code>{shown}</code>"
    if injection.placement is InjectionPlacement.FILE:
        return (
            f"File <code>~/{html.escape(injection.name)}</code> in the program's private home folder, "
            "made for each run and deleted afterwards"
        )
    return f"Environment variable <code>{html.escape(injection.name)}</code>: <code>{shown}</code>"


def _where(tool: ToolDefinitionV1) -> str:
    if tool.kind is ToolKind.HTTP:
        return "Sent only to " + ", ".join(f"<code>{html.escape(host)}</code>" for host in tool.hosts)
    if tool.kind is ToolKind.MCP:
        return f"Used only for the MCP connection <code>{html.escape(tool.display_name)}</code>"
    return f"Used only by the broker for {html.escape(tool.display_name)}"


def _collection_words(request: AccountLinkRequestV1) -> str:
    return "the team's credentials" if request.owner_kind is LinkOwnerKind.TEAM else "your own credentials"


def _confirmation(text: str) -> str:
    return (
        '<label class="broker-confirmation"><input type="checkbox" name="confirm" value="yes" required>'
        f"<span>{html.escape(text)}</span></label>"
    )


def _stored_credential_form(action: str, tool: ToolDefinitionV1, choices: list[dict[str, str]]) -> str:
    """The part of the page that links a credential the owner already stored (only when one fits)."""

    if not choices:
        return ""
    options = "".join(
        f'<option value="{html.escape(item["credential_key"], quote=True)}">'
        f"{html.escape(item['display_name'])} — {html.escape(item['purpose'])}</option>"
        for item in choices
    )
    return (
        '<section class="content-panel"><h2>Use a stored credential</h2>'
        f'<form method="post" action="{action}">'
        '<label for="credential_key">Stored credential</label>'
        f'<select id="credential_key" name="credential_key" required>{options}</select>'
        f"{_confirmation(f'Link this credential to {tool.display_name} as described above.')}"
        '<p><button type="submit">Link account</button></p></form></section>'
    )


def _new_credential_form(action: str, tool: ToolDefinitionV1, request: AccountLinkRequestV1) -> str:
    """The part of the page that stores a new credential and links it in the same submission.

    A separate form, so its required inputs do not block linking a stored
    credential. Every field is a masked input with the shared reveal
    button (ui/DESIGN.md, Controls); values are never put back into a page.
    """

    names = sorted(required_fields(tool))
    if not names:
        return ""
    inputs = "".join(
        f'<div><label for="{NEW_FIELD_PREFIX}{html.escape(name)}">{html.escape(name.replace("_", " ").capitalize())}'
        f' <span class="muted">(‹{html.escape(name)}›)</span></label>'
        + render_credential_input(
            input_id=f"{NEW_FIELD_PREFIX}{name}", input_type="password", autocomplete="new-password", required=True
        )
        + "</div>"
        for name in names
    )
    return (
        '<section class="content-panel"><h2>Add a new credential</h2>'
        f"<p>Type the values {html.escape(tool.display_name)} needs. The broker saves them in "
        f"{_collection_words(request)} and links them in the same step. Nothing already stored is replaced.</p>"
        f'<form method="post" action="{action}">'
        f'<input type="hidden" name="{CREDENTIAL_SOURCE_FIELD}" value="{NEW_CREDENTIAL}">'
        f"{inputs}"
        f"{_confirmation(f'Save this credential and link it to {tool.display_name} as described above.')}"
        '<p><button type="submit">Save and link account</button></p></form></section>'
    )


def render_form(
    session_id: str,
    session: AccountLinkSession,
    tool: ToolDefinitionV1,
    choices: list[dict[str, str]],
    *,
    used_by: str,
) -> str:
    """The labeled form: what the link does, then the stored choices and the new-credential form."""

    request = session.request
    own = "signed in with their own account" if tool_sign_in(tool.tool_id) is not None else "linked their own"
    whose = (
        "<strong>The team's account.</strong> Agents use it for anyone in the team who has access to "
        f"{html.escape(tool.display_name)}, unless that person {own}."
        if request.owner_kind is LinkOwnerKind.TEAM
        else "<strong>Your own account.</strong> Agents use it only when they work for you."
    )
    how = "".join(f"<li>{_describe_injection(item)}</li>" for item in tool.credential_injections)
    if tool.tool_id in FIXED_CLI_FIELDS:
        how += f"<li>The <code>{FIXED_CLI_FIELDS[tool.tool_id]}</code> field, used by the broker only</li>"
    action = f"/account-link/{html.escape(session_id, quote=True)}"
    return (
        '<section class="content-panel"><p class="eyebrow">Account link</p>'
        f"<h1>Link an account to {html.escape(tool.display_name)}?</h1>"
        f"<p>{whose}</p>"
        f"<p><strong>Used by:</strong> {html.escape(used_by)}</p>"
        f"<p><strong>Where it goes:</strong> {_where(tool)}.</p>"
        f"<p><strong>How it is sent:</strong></p><ul>{how}</ul>"
        f"<p><strong>Reason:</strong> {html.escape(request.reason or 'Not given')}</p>"
        "<p>Linking an account gives nobody access. People and agents still need access to "
        f"{html.escape(tool.display_name)} to use it, and the credential value never leaves the broker.</p>"
        f"{render_session_countdown(session.expires_at, label='This form expires in')}</section>"
        f"{_stored_credential_form(action, tool, choices)}"
        f"{_new_credential_form(action, tool, request)}"
    )


def create_link(
    store: AccessRepository,
    request: AccountLinkRequestV1,
    tool: ToolDefinitionV1,
    credential_key: str,
    *,
    now: datetime,
) -> AccountLinkV1:
    choices = {item["credential_key"] for item in credential_choices(owner_scope(request), tool)}
    if credential_key not in choices:
        raise AccountLinkRefused(
            "That credential is not one of the stored credentials offered for this tool.",
            reason_code="account_link_credential_unavailable",
        )
    link = store.create_link(
        AccountLinkV1(
            link_id=new_id("account_link"),
            team_id=request.team_id,
            tool_id=tool.tool_id,
            owner_kind=request.owner_kind,
            owner_id=request.team_id if request.owner_kind is LinkOwnerKind.TEAM else request.person_id,
            credential_key=credential_key,
            injections=tool.credential_injections,
            created_by=SubjectRefV1(kind=SubjectKind.USER, id=request.person_id),
            created_at_utc=now,
        )
    )
    logger.info(
        "Account link created. event=account_link_created link_id=%s tool_id=%s owner_kind=%s person_id=%s",
        link.link_id,
        link.tool_id,
        link.owner_kind.value,
        request.person_id,
    )
    return link


# -- adding a new credential in the form -------------------------------------------


@dataclass(frozen=True)
class LinkChoice:
    """What the person sent: a stored credential's key, or the values of a new credential.

    ``new_values`` holds secret values; it is kept out of ``repr`` and is never
    logged or put into a page.
    """

    credential_key: str = ""
    new_values: dict[str, str] | None = field(default=None, repr=False)


def read_choice(form: Mapping[str, Any]) -> LinkChoice:
    """The person's choice from the submitted form fields."""

    if str(form.get(CREDENTIAL_SOURCE_FIELD) or "") != NEW_CREDENTIAL:
        return LinkChoice(credential_key=str(form.get("credential_key") or "").strip())
    return LinkChoice(
        new_values={
            str(key).removeprefix(NEW_FIELD_PREFIX): value
            for key, value in form.items()
            if str(key).startswith(NEW_FIELD_PREFIX) and isinstance(value, str)
        }
    )


def new_credential_key(tool: ToolDefinitionV1, request: AccountLinkRequestV1, taken: Collection[str]) -> str:
    """A free key for a credential added in the form, derived from the tool and owner.

    ``http:portainer`` gives ``http-portainer-team`` for the team's account and
    ``http-portainer-personal`` for a person's (each owner has its own
    collection). A key already taken gets a number (``-2``, ``-3``, ...), so
    adding a credential never replaces a stored one. Keys follow the account
    link key rule (``CREDENTIAL_KEY_PATTERN``).
    """

    owner = "team" if request.owner_kind is LinkOwnerKind.TEAM else "personal"
    base = f"{_KEY_UNSAFE.sub('-', tool.tool_id).strip('-._')[:100]}-{owner}"
    candidates = (base if number == 1 else f"{base}-{number}" for number in range(1, _MAX_KEY_ATTEMPTS + 1))
    for key in candidates:
        if CREDENTIAL_KEY_PATTERN.fullmatch(key) and key not in taken:
            return key
    raise AccountLinkRefused(
        f"No free name is left for another {tool.display_name} credential. Remove unused ones, then try again.",
        reason_code=REASON_KEY_UNAVAILABLE,
    )


def _without_values(text: str, values: Mapping[str, str]) -> str:
    """Store errors, redacted, and with anything the person typed taken out."""

    text = redact_text(text)
    for value in values.values():
        if value.strip():
            text = text.replace(value.strip(), "[redacted]")
    return text


def add_credential(request: AccountLinkRequestV1, tool: ToolDefinitionV1, values: Mapping[str, str]) -> str:
    """Store the credential the person typed, in the link owner's collection; return its key.

    Uses the broker's own store path (``collect_credential``: the configured
    write source, Sealbox by default, then the catalog entry) with every field
    secret, so the values reach only the credential store. Only the fields
    the tool needs are stored.
    """

    names = sorted(required_fields(tool))
    missing = [name for name in names if not str(values.get(name) or "").strip()]
    if not names or missing:
        raise AccountLinkRefused(
            f"Type a value for {', '.join(missing)}." if missing else f"{tool.display_name} names no fields to fill in.",
            reason_code=REASON_FIELDS_MISSING,
        )
    scope = owner_scope(request)
    typed = {name: values[name] for name in names}
    owner_words = "team account" if request.owner_kind is LinkOwnerKind.TEAM else "personal account"
    with _NEW_CREDENTIAL_LOCK:
        key = new_credential_key(tool, request, taken=load_catalog(scope=scope).credentials)
        result = collect_credential(
            {
                "credential_key": key,
                "display_name": f"{tool.display_name} ({owner_words})",
                "purpose": f"Used by {tool.display_name}; added in the account link form.",
                "fields": [
                    {"name": name, "kind": "secret", "visibility": "secret", "value_source": "user_input"}
                    for name in names
                ],
            },
            typed,
            scope=scope,
        )
    if result.status != "success":
        detail = _without_values(result.error or "The credential store did not say why.", typed)
        logger.warning(
            "New credential for an account link was not stored. event=account_link_credential_not_stored "
            "tool_id=%s owner_kind=%s person_id=%s error_code=%s error=%s",
            tool.tool_id,
            request.owner_kind.value,
            request.person_id,
            (result.content or {}).get("error_code"),
            detail,
        )
        raise AccountLinkRefused(
            f"The broker could not save the new credential, so nothing was linked. {detail}",
            reason_code=REASON_NOT_STORED,
        )
    logger.info(
        "Stored a new credential for an account link. event=account_link_credential_added credential_key=%s "
        "tool_id=%s owner_kind=%s person_id=%s",
        key,
        tool.tool_id,
        request.owner_kind.value,
        request.person_id,
    )
    return key


@dataclass(frozen=True)
class LinkOutcome:
    """A created link; ``added_key`` names the credential stored in the same submission, if any."""

    link: AccountLinkV1
    tool: ToolDefinitionV1
    added_key: str | None = None

    def sentence(self, request: AccountLinkRequestV1) -> str:
        """What happened, for the person's result page (the agent never gets the key)."""

        if self.added_key:
            return (
                f"The broker saved the new credential as {self.added_key} in {_collection_words(request)} "
                f"and linked it to {self.tool.display_name}."
            )
        return f"The chosen credential is now linked to {self.tool.display_name}."


def link_account(
    store: AccessRepository,
    catalog: ToolCatalog,
    request: AccountLinkRequestV1,
    choice: LinkChoice,
    *,
    now: datetime,
) -> LinkOutcome:
    """Link the chosen credential, storing it first when the person typed a new one.

    ``linkable_tool`` runs first, so nothing is stored for someone who may not
    link for this owner.
    """

    tool = linkable_tool(catalog, request, TeamAccountSharing(store, request.team_id))
    if choice.new_values is None:
        return LinkOutcome(create_link(store, request, tool, choice.credential_key, now=now), tool)
    added = add_credential(request, tool, choice.new_values)
    return LinkOutcome(create_link(store, request, tool, added, now=now), tool, added)



def finish_account_link(
    session: AccountLinkSession, store: Any, catalog: Any, *, confirmed: bool, choice: LinkChoice
) -> str:
    """Record the person's answer on the session; return one plain sentence for the result page.

    The session result goes back to the waiting agent through the gateway,
    so it names the link but never the credential key or any value.
    """

    link_request = session.request
    if not confirmed:
        session.status = STATUS_DECLINED
        session.result = {"status": session.status, "message": "The person did not confirm the account link."}
        return "Nothing was linked."
    try:
        outcome = link_account(store, catalog, link_request, choice, now=datetime.now(timezone.utc))
    except AccountLinkRefused as exc:
        session.status = STATUS_UNAVAILABLE
        session.result = {"status": session.status, "error": str(exc), "reason_code": exc.reason_code}
        return str(exc)
    except Exception:
        logger.exception(
            "Account link could not be finished. event=account_link_failed tool_id=%s owner_kind=%s person_id=%s",
            link_request.tool_id,
            link_request.owner_kind.value,
            link_request.person_id,
        )
        session.status = STATUS_UNAVAILABLE
        session.result = {
            "status": session.status,
            "error": "The broker could not finish the account link.",
            "reason_code": ACCOUNT_LINK_FAILED,
        }
        return "The broker could not finish the account link. Nothing was linked."
    session.status = STATUS_CREATED
    session.result = {
        "status": session.status,
        "link_id": outcome.link.link_id,
        "tool_id": outcome.link.tool_id,
        "owner_kind": outcome.link.owner_kind.value,
        "message": (
            "A new credential was saved and linked; no credential value was returned."
            if outcome.added_key
            else "Account linked; no credential value was returned."
        ),
    }
    return outcome.sentence(link_request)

__all__ = [
    "ACCOUNT_LINK_FAILED",
    "finish_account_link",
    "AccountLinkRefused",
    "AccountLinkSession",
    "CREDENTIAL_SOURCE_FIELD",
    "LinkChoice",
    "LinkOutcome",
    "NEW_CREDENTIAL",
    "NEW_FIELD_PREFIX",
    "REASON_FIELDS_MISSING",
    "REASON_KEY_UNAVAILABLE",
    "REASON_NOT_STORED",
    "REASON_USES_SIGN_IN",
    "STATUS_CREATED",
    "STATUS_DECLINED",
    "STATUS_PENDING",
    "STATUS_UNAVAILABLE",
    "add_credential",
    "agents_using",
    "create_link",
    "credential_choices",
    "link_account",
    "linkable_tool",
    "new_credential_key",
    "owner_scope",
    "read_choice",
    "render_form",
    "required_fields",
    "used_by_text",
]
