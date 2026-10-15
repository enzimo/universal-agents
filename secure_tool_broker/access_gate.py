"""The broker's access check for every agent broker action and MCP operation.

For each request it:

1. checks that the calling process may act as the named agent type
   (``access_control.caller_tokens``: a separately started agent process may
   only act as its own type; the manager process runs the team lead and the
   workers inside it);
2. checks who the task works for, from its signed task context or its
   scheduled run identity (``access_control.task_identity``, the same check
   the agents' access hook uses), and that the person and team it names are
   the ones the request claims to act for; identity is enforced in both
   authorization modes (plan section 11);
3. resolves the operation and level from the current tool catalog;
4. refuses requests to hosts no web API tool (``http:*``) covers, in both modes;
5. when authorization is enabled, asks the access service for the agent side
   and the person side (``access_control.decisions``);
6. for a tool that uses a linked credential, finds the account link: the
   person's own link first, then the team's (``AccountLinkV1``) while the team
   shares the service (``access_control.account_sharing``; a private
   service never uses the team's account). Links choose the credential; they
   never grant access. A tool a person signs in to that also takes the team's
   account (X, ``secure_tools.auth_methods``) gets only the team's link here,
   and only while shared: the executor uses the person's own sign-in first.

The person and their roles come only from the signed context, never from
request fields. People acting directly through the gateway (sign-in, credential
forms, approved sign-ups) do not pass through this check: the gateway is the
authority that vouches for people, and those actions concern the person's own
accounts, not tools used by agents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from access_catalog.catalog import UNLISTED_WEB_TOOL, CatalogSource, ToolCatalog, mcp_tool_id
from access_control.account_sharing import TeamAccountSharing, fixed_sharing, fixed_sharing_text
from access_control.caller_tokens import BrokerCaller
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver
from access_control.identity import ContextVerifier, IdentityRejected
from access_control.task_identity import ScheduleRunLookup, job_schedule_id, resolve_task_identity
from custom_messages.contracts.access_v2 import (
    REASON_ACCOUNT_LINK_MISSING,
    AccountLinkV1,
    AccountMode,
    AccountSharing,
    LinkOwnerKind,
    OperationResolutionV1,
    ToolDefinitionV1,
)
from secure_tool_broker.account_links import ACCESS_STORE_MISSING_NEXT_STEP, ACCESS_STORE_MISSING_TEXT
from secure_tool_broker.tool_setup_tracking import note_link_missing
from secure_tools.auth_methods import tool_sign_in
from secure_tools.chat_commands import link_command
from storage.access_repository import AccessRepository
from storage.config import StorageSettings

logger = logging.getLogger(__name__)

REASON_IDENTITY_REJECTED = "access_identity_rejected"
REASON_UNLISTED_HOST = "access_unlisted_host"
REASON_ACCESS_DENIED = "access_denied"


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason_code: str = ""
    message: str = ""
    next_step: str = ""
    resolution: OperationResolutionV1 | None = None
    person_id: str | None = None
    account_link: AccountLinkV1 | None = None

    def as_error(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.message, "reason_code": self.reason_code}
        if self.next_step:
            payload["next_step"] = self.next_step
        if self.resolution is not None:
            payload.update(
                tool_id=self.resolution.tool_id,
                operation=self.resolution.operation,
                level=self.resolution.level.value,
            )
        return payload


def _text(context: Mapping[str, Any], key: str) -> str | None:
    return str(context.get(key) or "").strip() or None


@dataclass
class BrokerAccessGate:
    catalogs: CatalogSource
    verifier: ContextVerifier
    team_id: str
    access: AccessService | None  # None when authorization is disabled
    links: AccessRepository | None = None  # account links; used in both modes
    # Live checks of scheduled runs that carry a schedule permit; without
    # them such runs are refused (task_identity).
    schedule_runs: ScheduleRunLookup | None = None

    @property
    def catalog(self) -> ToolCatalog:
        catalog = self.catalogs.current()
        if self.access is not None and self.access.catalog is not catalog:
            self.access.catalog = catalog
        return catalog

    def _refuse(self, reason_code: str, message: str, context: Mapping[str, Any], **extra: Any) -> GateResult:
        result = GateResult(False, reason_code, message, **extra)
        resolution = result.resolution
        logger.warning(
            "Broker access refused. event=broker_access_refused reason_code=%s tool_id=%s operation=%s "
            "agent_id=%s named_agent_type=%s task_id=%s",
            reason_code,
            resolution.tool_id if resolution else None,
            resolution.operation if resolution else None,
            context.get("agent_id"),
            context.get("named_agent_type"),
            context.get("task_id"),
        )
        return result

    def _identity_problem(self, caller: BrokerCaller, context: Mapping[str, Any]) -> str | None:
        if not caller.runs_agents:
            return "This process does not run agents, so it cannot use tools for one."
        agent_type = _text(context, "named_agent_type")
        if not agent_type or not _text(context, "agent_id"):
            return "The request does not name the agent that makes it."
        if caller.agent_type is not None and agent_type != caller.agent_type:
            return f"The {caller.agent_type} process cannot act as {agent_type}."
        claimed_team = _text(context, "team_id")
        if claimed_team is not None and claimed_team != self.team_id:
            return "The request names another team."
        return None

    def _person(self, context: Mapping[str, Any]) -> tuple[PersonPrincipal | None, str | None, str]:
        """The verified person (None for verified work without one), the scheduled job the task
        is a run of (None outside job runs, ``task_identity.job_schedule_id``), and the request
        it works on (the signed context id, which access for one task is bound to); raises when
        nothing verifies them."""

        verified = resolve_task_identity(
            verifier=self.verifier,
            team_id=self.team_id,
            task_context=context.get("task_context"),
            schedule_run=context.get("schedule_run"),
            schedule_runs=self.schedule_runs,
        )
        if _text(context, "canonical_user_id") != verified.person_id:
            raise IdentityRejected("The request names a different person than its signed task context.")
        schedule_id = (
            job_schedule_id(verified, self.schedule_runs, lead_agent_type=self.access.lead_agent_type)
            if self.access is not None
            else None
        )
        if verified.person_id is None:
            return None, schedule_id, verified.context_id
        return (
            PersonPrincipal(verified.person_id, verified.team_id, verified.person_roles),
            schedule_id,
            verified.context_id,
        )

    def verified_person_id(self, caller: BrokerCaller, context: Mapping[str, Any]) -> str | None:
        """The person a model call's task works for, verified like a tool call's; None for work for nobody.

        Used by the model key lookup (``secure_tool_broker.model_keys``) to
        give a person's own key only to their own tasks. Raises
        ``IdentityRejected`` when the caller or the signed context does not check out.
        """

        problem = self._identity_problem(caller, context)
        if problem is not None:
            raise IdentityRejected(problem)
        person, _schedule_id, _context_id = self._person(context)
        return person.person_id if person is not None else None

    def check_broker_action(
        self, caller: BrokerCaller, provider: str, action: str, params: Mapping[str, Any], context: Mapping[str, Any]
    ) -> GateResult:
        return self.check(caller, self.catalog.resolve_broker_action(provider, action, params), context)

    def check_mcp(
        self, caller: BrokerCaller, connection_name: str, operation: str, context: Mapping[str, Any]
    ) -> GateResult:
        return self.check(caller, self.catalog.resolve_mcp(mcp_tool_id(connection_name), operation), context)

    def check(
        self, caller: BrokerCaller, resolution: OperationResolutionV1, context: Mapping[str, Any]
    ) -> GateResult:
        """Check one resolved operation; resolve it from ``self.catalog`` so the
        access service decides against the same catalog."""

        problem = self._identity_problem(caller, context)
        if problem is not None:
            return self._refuse(REASON_IDENTITY_REJECTED, problem, context, resolution=resolution)
        try:
            person, schedule_id, work_id = self._person(context)
        except (IdentityRejected, ValueError) as exc:
            return self._refuse(REASON_IDENTITY_REJECTED, str(exc), context, resolution=resolution)
        if resolution.tool_id == UNLISTED_WEB_TOOL:
            return self._refuse(
                REASON_UNLISTED_HOST,
                f"No web API tool covers this request ({resolution.operation}).",
                context,
                next_step=(
                    "Ask TeamLead to propose a web API tool for this service, "
                    "or use the web research tools for public pages."
                ),
                resolution=resolution,
            )
        person_id = person.person_id if person else None
        if self.access is not None:
            agent_type = str(_text(context, "named_agent_type"))
            agent = AgentPrincipal(
                str(_text(context, "agent_id")),
                agent_type,
                self.access.agent_role_of(agent_type),
                self.team_id,
                schedule_id=schedule_id,
                work_id=work_id,
            )
            decision = self.access.decide(
                agent=agent,
                person=person,
                resolution=resolution,
                # The signed request id, never the task id the caller names.
                task_id=work_id,
                hand_on_for_task=True,
            )
            if not decision.allowed:
                return GateResult(False, REASON_ACCESS_DENIED, decision.reason, decision.next_step, resolution)
        return self._with_account(resolution, person_id, context)

    def account_sharing(self) -> TeamAccountSharing | None:
        """The team's sharing choices, from the access store the links are in (both authorization modes).

        None when the broker has no access store (``ACCESS_STORE_MISSING_TEXT``).
        """

        if self.links is None:
            return None
        return TeamAccountSharing(self.links, self.team_id)

    def account_link_for(self, tool_id: str, person_id: str | None) -> AccountLinkV1 | None:
        """The person's own link for a tool, or else the team's: only the owners the tool and the team's sharing allow."""

        if self.links is None:
            return None
        tool = self.catalog.tools.get(tool_id)
        if tool is None:
            logger.warning(
                "No account link is used for a tool the catalog does not have. "
                "event=broker_account_link_unknown_tool tool_id=%s",
                tool_id,
            )
            return None
        owners = TeamAccountSharing(self.links, self.team_id).link_owners(tool)
        if person_id and LinkOwnerKind.PERSON in owners:
            own = self.links.active_link(self.team_id, tool_id, LinkOwnerKind.PERSON, person_id)
            if own is not None:
                return own
        if LinkOwnerKind.TEAM not in owners:
            return None
        return self.links.active_link(self.team_id, tool_id, LinkOwnerKind.TEAM, self.team_id)

    def _with_account(
        self, resolution: OperationResolutionV1, person_id: str | None, context: Mapping[str, Any]
    ) -> GateResult:
        tool = self.catalog.tools.get(resolution.tool_id)
        if tool is None or tool.account is not AccountMode.LINKED_CREDENTIAL:
            return GateResult(True, resolution=resolution, person_id=person_id)
        if tool_sign_in(tool.tool_id) is not None:
            # A sign-in tool with a team fallback (X): the executor uses the
            # person's own sign-in first, and this team link (only while the
            # team shares the service) without one; it tells the person how to
            # sign in when neither exists.
            team_link = self.account_link_for(tool.tool_id, None)
            return GateResult(True, resolution=resolution, person_id=person_id, account_link=team_link)
        if self.links is None:
            logger.error(
                "A tool call needs an account but the broker has no access store. "
                "event=broker_access_store_missing tool_id=%s person_id=%s",
                tool.tool_id,
                person_id,
            )
            return self._refuse(
                REASON_ACCOUNT_LINK_MISSING,
                ACCESS_STORE_MISSING_TEXT,
                context,
                next_step=ACCESS_STORE_MISSING_NEXT_STEP,
                resolution=resolution,
            )
        link = self.account_link_for(tool.tool_id, person_id)
        if link is None:
            note_link_missing(self.links, self.team_id, person_id, tool.tool_id)
            team_used = TeamAccountSharing(self.links, self.team_id).team_account_used(tool)
            return self._refuse(
                REASON_ACCOUNT_LINK_MISSING,
                f"No account is linked for {tool.display_name}, so the broker has no credential to use.",
                context,
                next_step=account_link_next_step(tool, team_used=team_used),
                resolution=resolution,
            )
        return GateResult(True, resolution=resolution, person_id=person_id, account_link=link)


def account_link_next_step(tool: ToolDefinitionV1, *, team_used: bool) -> str:
    """What a person does when a tool has no linked account, with the exact commands.

    ``team_used``: whether the team's account would serve them (the team
    shares the service); for a private service only their own can. A tool
    that is always private says why (it takes only each person's own
    account), never that the team chose it; a tool that takes only the
    team's account names only the team's link.
    """

    tool_id = tool.tool_id
    setup = "TeamLead can also open the account link form for the person with a tool setup request."
    if not team_used and fixed_sharing(tool) is AccountSharing.PRIVATE:
        return f"{fixed_sharing_text(tool)} Each person links their own with {link_command(tool_id)}. {setup}"
    if LinkOwnerKind.PERSON not in tool.link_owners:
        # The team's own server (Crawl4AI): a person has no account of their own to link.
        return (
            f"{fixed_sharing_text(tool)} A team administrator links it with "
            f"{link_command(tool_id, LinkOwnerKind.TEAM)}. {setup}"
        )
    if not team_used:
        return (
            f"The team keeps this service private, so each person links their own account with "
            f"{link_command(tool_id)}. {setup}"
        )
    return (
        f"A team administrator links the team's account with {link_command(tool_id, LinkOwnerKind.TEAM)}, or the person "
        f"links their own with {link_command(tool_id)}. {setup}"
    )


def build_broker_access_gate(
    *,
    instance_root: str | Path | None,
    team_id: str,
    authorization_enabled: bool,
    storage_settings: StorageSettings | None = None,
    cedar_binary: str | None = None,
    verify_keys: str | None = None,
    lead_agent_type: str = "TeamArchitect",
    service_urls: Mapping[str, str] | None = None,
) -> BrokerAccessGate:
    """The broker's gate: identity and catalog always, grants when enabled.

    ``service_urls`` gives the configured address of each service the
    instance runs itself (``access_catalog.catalog.CRAWL4AI_URL_SETTING``),
    so the Crawl4AI web tool matches the server the agents' tools call.
    """

    verifier = ContextVerifier.from_env() if verify_keys is None else ContextVerifier.from_value(verify_keys)
    if not verifier.keys:
        logger.error(
            "No task-context verification keys are configured, so the broker refuses every agent tool use. "
            "Regenerate internal credentials. event=task_context_verify_keys_missing"
        )
    catalogs = CatalogSource(instance_root, service_urls=service_urls)
    store = AccessRepository(storage_settings) if storage_settings is not None else None
    access: AccessService | None = None
    if authorization_enabled:
        if store is None:
            raise ValueError("Enabled authorization needs the manager storage for grants.")
        access = AccessService(
            store=store,
            catalog=catalogs.current(),
            cedar=CedarAccessClient(binary=cedar_binary),
            team_id=team_id,
            agent_role_of=agent_role_resolver(lead_agent_type),
            lead_agent_type=lead_agent_type,
        )
    return BrokerAccessGate(
        catalogs=catalogs,
        verifier=verifier,
        team_id=team_id,
        access=access,
        links=store,
        schedule_runs=ScheduleRunLookup.from_storage(storage_settings) if storage_settings is not None else None,
    )


__all__ = [
    "REASON_ACCOUNT_LINK_MISSING",
    "account_link_next_step",
    "BrokerAccessGate",
    "build_broker_access_gate",
    "GateResult",
    "REASON_ACCESS_DENIED",
    "REASON_IDENTITY_REJECTED",
    "REASON_UNLISTED_HOST",
]
