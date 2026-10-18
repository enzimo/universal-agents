"""Access change proposals: several changes reviewed once and applied together (Phase 6).

When the standard grants and hand-downs are not enough (a service the catalog
does not know, access nobody may hand on), TeamLead proposes the whole change
with ``propose_access_change``. The proposal (``AccessProposalV1``) holds only
typed items: web API tools, MCP servers to install, grants for agent types,
restrictions, data folders, account-link requests and revocations. Section 7
of the access plan is the design; the rules here are:

- **Checked before review.** ``ProposalCompiler`` validates every item against
  the catalog as it would be with the proposed tools, and describes the
  effect in trusted code's words, one review row per item (the card never
  shows the agent's own description of the effect). Items that broaden access
  are flagged: a new host, a private network address, full access, hand-on
  rights, code execution, a new folder.
- **Whose work.** Grants serve the work of the person the proposal's task
  works for (decision D13). When that person lacks the access, the proposal
  gives it to them too, on its own row.
- **One review.** The proposal is one ``CHANGE`` request of kind
  ``access_proposal`` on a platform tool at full, so only an administrator
  decides it; each grant row that asks for lasting access has a "Keep for
  later tasks" box, and an unticked row lasts for the task.
- **Applied together, at the decision.** ``ProposalApplier`` (registered in
  the gateway, where the reviewer's verified roles are known) re-checks the
  stored plan's digest and every item against the reviewer's authority,
  writes each new tool's catalog file (``access_catalog.instance_tools``),
  and stores grants, blocks, data folders, revocations and the request's
  outcome in one transaction. If anything fails, the tool files are removed
  again and nothing is applied. A tool with no grants gives nobody anything,
  so writing its file first is safe.
- **Credentials last.** Which stored credential a new tool uses is chosen by
  a person in the broker's form after approval; the gateway opens it for
  the approver.
- **MCP servers (plan 7d).** An MCP server item names a pending connection in
  the MCP registry by id and record digest. Installing it makes it the team
  tool ``mcp:<name>``; the same review decides who may use it. When the
  proposal gives no grant rows for a server, ``with_mcp_defaults`` adds the
  rows of ``access_control.mcp_servers`` (and a team account link for a
  server that declares credential slots) before the proposal is stored, so
  the card shows exactly what is approved. Applying activates the record
  (recorded as the reviewer's approval with the request id) before the store
  transaction and puts the pending record back if the transaction fails; an
  active tool with no grants gives nobody anything, like a new tool file.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from access_catalog.catalog import MCP_SERVERS_TOOL_SET, CatalogError, CatalogSource, ToolCatalog
from access_control.account_sharing import TeamAccountSharing, team_account_private_text
from access_catalog.instance_tools import (
    private_host_names,
    private_hosts,
    remove_instance_tool,
    with_proposed_tools,
    write_instance_tool,
)
from access_control.cli_binaries import Downloader, fetch_https, workspaces_root
from access_control.data_folders import DataFolderRegistry, registration_problem
from access_control.decision_effects import EffectJournal, OwnedFile
from access_control.proposal_clis import cli_rows, install_programs
from access_control.decisions import HandDownRefused
from access_control.entities import PersonPrincipal, agent_type_id
from access_control.folders import FOLDER_ROOT_USERS, FolderIdError, grantable_folder_id
from access_control.grant_entries import GrantPlan
from access_control.mcp_servers import default_proposal_grants
from access_control.people import is_administrator
from access_control.requests import ACCESS_PROPOSAL_CHANGE, LEVEL_WORDS, AccessRequests
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessProposalV1,
    AccessRequestStatus,
    AccessRequestV1,
    AccountMode,
    ChangeApprovalMode,
    ChangeApprovalRecordV1,
    DataFolderRecordV1,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    LinkOwnerKind,
    ProposedAccountLinkV1,
    ProposedGrantV1,
    ReviewItemV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
    ToolKind,
    contract_digest,
    summary_line,
)
from id_generation import new_id
from mcp_control.registry import (
    MCPConnectionLifecycle,
    MCPConnectionRecordV1,
    MCPConnectionStore,
    MCPConnectionTransport,
)
from storage.access_repository import AccessStoreConflict, DecisionClaim, DecisionRecovery

logger = logging.getLogger(__name__)

ACCESS_PROPOSAL_KIND = ACCESS_PROPOSAL_CHANGE
GRANTABLE_TARGETS = frozenset({TargetKind.TOOL, TargetKind.OPERATION, TargetKind.TOOL_SET, TargetKind.FOLDER})
MCPRecordLookup = Callable[[str], MCPConnectionRecordV1 | None]


class ProposalRefused(ValueError):
    """The proposal cannot be reviewed or applied as it is; the message says why."""


def proposal_digest(proposal: AccessProposalV1) -> str:
    return contract_digest(proposal.model_dump(mode="json"))


def _no_mcp_records(_connection_id: str) -> MCPConnectionRecordV1 | None:
    return None


def with_mcp_defaults(proposal: AccessProposalV1, record_for: MCPRecordLookup, *, lead_agent_type: str) -> AccessProposalV1:
    """The proposal with default rows for each MCP server it installs (module docstring).

    Runs before the proposal is compiled and stored, so the defaults are part
    of what the reviewer sees and of the digest they approve. A server the
    lookup does not know is left alone; compiling refuses it with the reason.
    """

    grants = list(proposal.grants)
    links = list(proposal.account_links)
    for item in proposal.mcp_servers:
        record = record_for(item.connection_id)
        if record is None:
            continue
        tool_id = record.descriptor.capability_id
        if not any(grant.target.id.partition("#")[0] == tool_id for grant in grants):
            grants += default_proposal_grants(tool_id, lead_agent_type=lead_agent_type)
        if record.descriptor.credential_injections and not any(link.tool_id == tool_id for link in links):
            links.append(
                ProposedAccountLinkV1(
                    tool_id=tool_id,
                    owner="team",
                    purpose=f"The credential the MCP server {record.descriptor.connection_name} needs.",
                )
            )
    if len(grants) == len(proposal.grants) and len(links) == len(proposal.account_links):
        return proposal
    return AccessProposalV1.model_validate(
        {**proposal.model_dump(mode="python"), "grants": tuple(grants), "account_links": tuple(links)}
    )


def _mcp_server_words(record: MCPConnectionRecordV1) -> str:
    descriptor = record.descriptor
    if descriptor.transport is MCPConnectionTransport.STDIO:
        where = f"a local command the broker runs: {' '.join((descriptor.command or '', *descriptor.args))}"
    else:
        where = descriptor.endpoint or f"the address in {descriptor.server_url_env}"
    if descriptor.credential_injections:
        fields = ", ".join(sorted({f"{slot.field_name} (as {slot.target_name})" for slot in descriptor.credential_injections}))
        credentials = f"The broker adds the linked account's {fields} to its requests; no agent sees the credential."
    elif descriptor.requires_legacy_credentials:
        credentials = "It uses credentials set in the operator's configuration."
    else:
        credentials = "It uses no stored credential."
    return (
        f"Install the MCP server {descriptor.connection_name} ({descriptor.transport.value}, {where}) as the team "
        f"tool {descriptor.capability_id}. Agents use its functions only with access to it (the access rows). "
        f"{credentials}"
    )[:1000]


def _mcp_server_flags(record: MCPConnectionRecordV1) -> tuple[str, ...]:
    descriptor = record.descriptor
    flags = ["new_host"]
    if descriptor.transport is MCPConnectionTransport.STDIO:
        flags.append("runs_code")
    elif descriptor.endpoint and private_host_names([urlsplit(descriptor.endpoint).hostname or ""]):
        flags.append("private_address")
    return tuple(flags)


# A proposal's one-line summary (ChangeProposalV1.summary), counted from its
# review rows: what each section does, in the card's section order.
_SUMMARY_SECTION_WORDS = {
    "tool": ("add {n} new tool", "add {n} new tools"),
    "access": ("give {n} access", "give {n} accesses"),
    "restriction": ("add {n} restriction", "add {n} restrictions"),
    "folder": ("register {n} folder", "register {n} folders"),
    "account": ("link {n} credential", "link {n} credentials"),
    "revocation": ("remove {n} access", "remove {n} accesses"),
    "delivery": ("deliver {n} file", "deliver {n} files"),
}
_SUMMARY_FLAG_WORDS = {
    "new_host": "a new host",
    "private_address": "a private address",
    "full_access": "full access",
    "can_hand_on": "access that can be handed on",
    "runs_code": "code that runs",
    "new_folder": "a new folder",
    "setup_steps": "setup steps people will be shown",
    "new_program": "a new program",
    "download_host": "a download",
    "team_account": "the team's account",
}


def proposal_summary(rows: tuple[ReviewItemV1, ...]) -> str:
    """The proposal in one line from its review rows, such as "Access proposal: add 1 new tool, give 2 accesses"."""

    # A tool's extra rows (its commands, its setup steps) are not more tools.
    items = [row for row in rows if not (row.section == "tool" and row.item_id.count("-") > 1)]
    counts = {section: sum(1 for row in items if row.section == section) for section in _SUMMARY_SECTION_WORDS}
    parts = [
        _SUMMARY_SECTION_WORDS[section][count != 1].format(n=count) for section, count in counts.items() if count
    ]
    flags = sorted({flag for row in rows for flag in row.flags}, key=list(_SUMMARY_FLAG_WORDS).index)
    warning = f" (includes {', '.join(_SUMMARY_FLAG_WORDS[flag] for flag in flags)})" if flags else ""
    return summary_line(f"Access proposal: {', '.join(parts) or 'no changes'}{warning}. Takes effect when approved.")


@dataclass(frozen=True)
class CompiledProposal:
    proposal: AccessProposalV1
    digest: str
    rows: tuple[ReviewItemV1, ...]
    # Targets (with level) the task's person must be given too (dual check).
    person_needs: tuple[tuple[TargetRefV1, AccessLevel], ...] = ()


def _target_name(catalog: ToolCatalog, target: TargetRefV1) -> str:
    if target.kind is TargetKind.FOLDER:
        return f"the {target.id}/ folder"
    if target.kind is TargetKind.TOOL_SET:
        tool_set = catalog.tool_sets.get(target.id)
        return tool_set.display_name if tool_set else target.id
    tool_id, _, operation = target.id.partition("#")
    tool = catalog.tools.get(tool_id)
    name = tool.display_name if tool else tool_id
    return f"{name} ({operation})" if operation else name


def _tool_words(tool) -> str:
    routes = "; ".join(
        f"{route.name} ({route.method} {route.path}) needs {LEVEL_WORDS[route.level]}" for route in tool.routes
    )
    base = (
        f"New tool {tool.display_name} ({tool.tool_id}) for {', '.join(tool.hosts)}: reading (GET) needs read, "
        "changes (POST, PUT, PATCH) need read-and-change, deleting (DELETE) needs full"
    )
    words = f"{base}; {routes}" if routes else base
    if tool.setup is not None and tool.setup.prerequisites:
        words += f". Set up first: {', '.join(tool.setup.prerequisites)}"
    return words


def _setup_step_rows(index: int | str, tool) -> list[ReviewItemV1]:
    """One row per proposed setup step, in the exact words people will be shown."""

    prefix = index if isinstance(index, str) else f"tool-{index}"
    rows = []
    for number, step in enumerate(tool.setup.external_steps if tool.setup else (), start=1):
        who = "an administrator" if step.who == "administrator" else "the person"
        link = f" at {step.url}" if step.url else ""
        check = f" How to tell it worked: {step.verify}" if step.verify else ""
        rows.append(
            ReviewItemV1(
                item_id=f"{prefix}-step-{number}",
                section="tool",
                text=f"Setup step {number} for {tool.display_name}, done by {who}{link}: \"{step.title}\". "
                f"{step.instructions}{check}",
                flags=("setup_steps",),
            )
        )
    return rows


@dataclass
class ProposalCompiler:
    """Validates a proposal and writes its review rows."""

    catalog: ToolCatalog
    team_id: str
    holds: Callable[[PersonPrincipal, TargetRefV1, AccessLevel], bool]
    # The team's sharing choices: a team account link for a private service
    # would never be used, so it is refused before anyone reviews it.
    account_sharing: TeamAccountSharing
    data_folders: DataFolderRegistry | None = None
    protected: tuple[Path, ...] = ()
    grant_exists: Callable[[str], GrantV1 | None] = lambda _grant_id: None
    # Pending MCP connections by id (the MCP registry); none by default.
    mcp_records: MCPRecordLookup = _no_mcp_records
    # The one agent type that keeps lasting access ("access follows the work").
    lead_agent_type: str = "TeamArchitect"

    def compile(
        self, proposal: AccessProposalV1, *, person: PersonPrincipal | None, task_id: str | None
    ) -> CompiledProposal:
        servers = self._servers(proposal)
        try:
            candidate = with_proposed_tools(self.catalog, proposal.tools, proposal.clis)
            for record in servers:
                candidate.add_mcp_tool(record)
            candidate.validate()
        except (CatalogError, ValueError) as exc:
            raise ProposalRefused(f"A proposed tool cannot be added: {exc}") from exc
        new_mcp = {record.descriptor.capability_id for record in servers}
        new_tools = {tool.tool_id for tool in proposal.tools} | {cli.tool_id for cli in proposal.clis} | new_mcp
        rows: list[ReviewItemV1] = []
        for index, tool in enumerate(proposal.tools):
            flags = ["new_host"] + (["private_address"] if private_hosts(tool) else [])
            rows.append(ReviewItemV1(item_id=f"tool-{index}", section="tool", text=_tool_words(tool), flags=tuple(flags)))
            rows += _setup_step_rows(index, tool)
        for index, cli in enumerate(proposal.clis):
            rows += cli_rows(index, cli, _setup_step_rows)
        rows += [
            ReviewItemV1(item_id=f"server-{index}", section="tool", text=_mcp_server_words(record), flags=_mcp_server_flags(record))
            for index, record in enumerate(servers)
        ]
        needs: dict[tuple[str, str], tuple[TargetRefV1, AccessLevel]] = {}
        for index, grant in enumerate(proposal.grants):
            self._check_target(candidate, grant.target, what="Access")
            if grant.target.kind is TargetKind.OPERATION:
                raise ProposalRefused(
                    f"Access is given for a whole tool at a level, not for one operation: propose "
                    f"{grant.target.id.partition('#')[0]} at the level the operation needs."
                )
            if grant.agent_type != self.lead_agent_type and grant.lifetime != "task":
                # Checked here so a person never reviews what cannot be applied
                # (AccessService.worker_lasting_refusal refuses it again).
                raise ProposalRefused(
                    f"Workers keep no lasting access, so {grant.agent_type} cannot keep {grant.target.id}. "
                    f"Give {self.lead_agent_type} the access with delegation_depth 1: its workers then get it for "
                    "each task. Or give the worker access for this task only (lifetime 'task')."
                )
            rows.append(self._grant_row(candidate, grant, index, task_id, new_tools))
            if person is not None and not self._person_holds(person, grant, new_tools, new_mcp):
                key = (grant.target.id, grant.level.value)
                if key not in needs or needs[key][1].rank < grant.level.rank:
                    needs[key] = (grant.target, grant.level)
        for index, (target, level) in enumerate(needs.values()):
            assert person is not None
            rows.append(
                ReviewItemV1(
                    item_id=f"person-{index}",
                    section="access",
                    text=(
                        f"The person this task works for ({person.person_id}) gets {LEVEL_WORDS[level]} access to "
                        f"{_target_name(candidate, target)} too, because agents never exceed their person."
                    ),
                    flags=("full_access",) if level is AccessLevel.FULL else (),
                )
            )
        rows += self._block_rows(candidate, proposal)
        rows += self._folder_rows(proposal)
        rows += self._account_rows(candidate, proposal)
        rows += self._revocation_rows(proposal)
        return CompiledProposal(proposal, proposal_digest(proposal), tuple(rows), tuple(needs.values()))

    # -- items ----------------------------------------------------------------------

    @staticmethod
    def _check_target(catalog: ToolCatalog, target: TargetRefV1, *, what: str) -> None:
        if target.kind not in GRANTABLE_TARGETS:
            raise ProposalRefused(f"{what} can be on a tool, one operation, a tool set or a folder, not {target.kind.value}.")
        if target.kind is TargetKind.FOLDER:
            try:
                folder = grantable_folder_id(target.id)
            except FolderIdError as exc:
                raise ProposalRefused(str(exc)) from exc
            if folder.split("/", 1)[0] == FOLDER_ROOT_USERS:
                raise ProposalRefused("A person's own folder is shared by its owner, never proposed.")
            return
        if target.kind is TargetKind.TOOL_SET:
            if target.id not in catalog.tool_sets:
                raise ProposalRefused(f"{target.id} is not a tool set.")
            return
        tool_id, _, operation = target.id.partition("#")
        tool = catalog.tools.get(tool_id)
        if tool is None:
            raise ProposalRefused(f"{tool_id} is not in the tool catalog or in this proposal.")
        if target.kind is TargetKind.OPERATION and tool.operation(operation) is None:
            raise ProposalRefused(f"{tool_id} has no operation {operation!r}.")

    def _servers(self, proposal: AccessProposalV1) -> list[MCPConnectionRecordV1]:
        """The pending MCP connections the proposal installs, each checked against its record."""

        servers: list[MCPConnectionRecordV1] = []
        for item in proposal.mcp_servers:
            record = self.mcp_records(item.connection_id)
            if record is None:
                raise ProposalRefused(f"There is no MCP connection {item.connection_id}.")
            name = record.descriptor.connection_name
            if record.lifecycle is not MCPConnectionLifecycle.PENDING:
                raise ProposalRefused(
                    f"The MCP server {name} is {record.lifecycle.value}; only a pending one can be installed."
                )
            if record.record_digest != item.record_digest:
                raise ProposalRefused(f"The MCP server {name} changed after it was proposed; propose it again.")
            if record.descriptor.team_id != self.team_id:
                raise ProposalRefused(f"The MCP server {name} belongs to another team.")
            if any(existing.connection_id == record.connection_id for existing in servers):
                raise ProposalRefused(f"The MCP server {name} is named twice.")
            servers.append(record)
        return servers

    def _person_holds(
        self, person: PersonPrincipal, grant: ProposedGrantV1, new_tools: set[str], new_mcp: set[str]
    ) -> bool:
        tool_id = grant.target.id.partition("#")[0]
        if tool_id in new_mcp:
            # An installed server joins the mcp_servers set, so whoever holds
            # the set (members, as a standard set) will hold the new tool.
            return self.holds(person, TargetRefV1(kind=TargetKind.TOOL_SET, id=MCP_SERVERS_TOOL_SET), grant.level)
        if tool_id in new_tools:
            # Nobody holds a tool that does not exist yet, except administrators,
            # who hold every team tool (std:admin).
            return is_administrator(person.roles)
        return self.holds(person, grant.target, grant.level)

    @staticmethod
    def _grant_row(
        catalog: ToolCatalog, grant: ProposedGrantV1, index: int, task_id: str | None, new_tools: set[str]
    ) -> ReviewItemV1:
        flags: list[str] = []
        if grant.level is AccessLevel.FULL:
            flags.append("full_access")
        if grant.delegation_depth:
            flags.append("can_hand_on")
        tool = catalog.tools.get(grant.target.id.partition("#")[0])
        if tool is not None and tool.kind is ToolKind.CODE:
            flags.append("runs_code")
        if grant.target.id.partition("#")[0] in new_tools:
            # Access to a proposed tool: a new program for a CLI, a new host otherwise.
            flags.append("new_program" if tool is not None and tool.installed_cli is not None else "new_host")
        text = f"{grant.agent_type}: {LEVEL_WORDS[grant.level]} access to {_target_name(catalog, grant.target)}"
        if grant.delegation_depth:
            text += ", and it may hand it on to workers"
        if grant.lifetime == "task":
            text += " (this task only)"
        if grant.reason:
            text += f". Why: {grant.reason}"
        keepable = grant.lifetime == "until_revoked" and bool(task_id)
        return ReviewItemV1(item_id=f"grant-{index}", section="access", text=text, flags=tuple(flags), keepable=keepable)

    def _block_rows(self, catalog: ToolCatalog, proposal: AccessProposalV1) -> list[ReviewItemV1]:
        rows = []
        for index, block in enumerate(proposal.blocks):
            self._check_target(catalog, block.target, what="A restriction")
            rows.append(
                ReviewItemV1(
                    item_id=f"block-{index}",
                    section="restriction",
                    text=f"{block.agent_type} can no longer use {_target_name(catalog, block.target)}. Why: {block.reason}",
                )
            )
        return rows

    def _folder_rows(self, proposal: AccessProposalV1) -> list[ReviewItemV1]:
        rows = []
        existing = self.data_folders.records() if self.data_folders is not None else []
        for index, folder in enumerate(proposal.data_folders):
            problem = registration_problem(folder.name, folder.path, protected=self.protected, existing=existing)
            if problem:
                raise ProposalRefused(f"The data folder {folder.name} cannot be registered: {problem}")
            access = "read and change" if folder.writable else "read only"
            rows.append(
                ReviewItemV1(
                    item_id=f"folder-{index}",
                    section="folder",
                    text=f"Register {folder.path} as data/{folder.name} ({access}); agents still need folder grants.",
                    flags=("new_folder",),
                )
            )
        return rows

    def _account_rows(self, catalog: ToolCatalog, proposal: AccessProposalV1) -> list[ReviewItemV1]:
        rows = []
        for index, link in enumerate(proposal.account_links):
            tool = catalog.tools.get(link.tool_id)
            if tool is None or tool.account is not AccountMode.LINKED_CREDENTIAL:
                raise ProposalRefused(f"{link.tool_id} does not use a linked credential.")
            team = link.owner == LinkOwnerKind.TEAM
            if link.owner not in tool.link_owners:
                whose = "the team's account" if not team else "each person's own account"
                raise ProposalRefused(f"{tool.display_name} uses only {whose}.")
            if team and not self.account_sharing.team_account_used(tool):
                raise ProposalRefused(team_account_private_text(tool))
            owner = "the team's account" if team else "the account of the person this task works for"
            rows.append(
                ReviewItemV1(
                    item_id=f"account-{index}",
                    section="account",
                    text=f"After approval you choose the credential {tool.display_name} uses ({owner}): {link.purpose}",
                )
            )
        return rows

    def _revocation_rows(self, proposal: AccessProposalV1) -> list[ReviewItemV1]:
        rows = []
        for index, grant_id in enumerate(proposal.revoke_grant_ids):
            grant = self.grant_exists(grant_id)
            if grant is None or grant.team_id != self.team_id or grant.status is not GrantStatus.ACTIVE:
                raise ProposalRefused(f"There is no active grant {grant_id} to remove.")
            subject = grant.subject.id.split("/", 1)[-1]
            rows.append(
                ReviewItemV1(
                    item_id=f"revoke-{index}",
                    section="revocation",
                    # Removal takes the grant's whole entry (access_control.grant_entries).
                    text=(
                        f"Remove all of {subject}'s access to {_target_name(self.catalog, grant.target)} "
                        f"(including {LEVEL_WORDS[grant.level]} access {grant_id}) and what it handed on from it."
                    ),
                )
            )
        return rows


# Failures that mean the approval no longer applies; they are reported as a
# refusal. Anything else (a busy or lost database) is raised again as it was.
_APPLY_REFUSALS = (HandDownRefused, CatalogError, AccessStoreConflict, PermissionError, ValueError)


@dataclass
class ProposalApplier:
    """Applies an approved proposal at the decision (``access_control.requests.ChangeApplier``)."""

    requests: Callable[[], AccessRequests]
    instance_root: Path
    catalogs: CatalogSource
    data_folders: Callable[[], DataFolderRegistry]
    protected: tuple[Path, ...]
    person: Callable[[str], PersonPrincipal | None]
    # The MCP registry, for proposals that install MCP servers (none: refused).
    mcp_store: Callable[[], MCPConnectionStore] | None = None
    # Account links to open for the approver after the decision, by request id.
    account_links: dict[str, list] = field(default_factory=dict)
    # How a proposed CLI's program is downloaded (https only; tests stand in).
    download: Downloader = fetch_https

    def compiler(self, requests: AccessRequests) -> ProposalCompiler:
        service = requests.service
        store = self.mcp_store() if self.mcp_store is not None else None
        return ProposalCompiler(
            catalog=service.catalog,
            team_id=service.team_id,
            holds=lambda person, target, level: service.holds(person, target, level, task_id=None),
            account_sharing=TeamAccountSharing(service.store, service.team_id),
            data_folders=self.data_folders(),
            protected=self.protected,
            grant_exists=service.store.get,
            mcp_records=store.get if store is not None else _no_mcp_records,
            lead_agent_type=requests.lead_agent_type,
        )

    def stored_proposal(self, requests: AccessRequests, request: AccessRequestV1) -> AccessProposalV1:
        assert request.change is not None
        stored = requests.service.store.get_plan(request.change.change_id)
        if stored is None or stored[1] != ACCESS_PROPOSAL_KIND or stored[2] != request.change.plan_digest:
            raise HandDownRefused("The proposal's plan is missing or changed; TeamLead must propose it again.")
        proposal = AccessProposalV1.model_validate(stored[3])
        if proposal_digest(proposal) != request.change.plan_digest:
            raise HandDownRefused("The proposal does not match what was reviewed; TeamLead must propose it again.")
        return proposal

    def apply(
        self, request: AccessRequestV1, reviewer: PersonPrincipal, finished: AccessRequestV1, claim: DecisionClaim
    ) -> AccessRequestV1:
        """Apply an approved proposal under the decision's claim (``AccessRequests.decide``).

        The claim is held from before the first file is written until the
        outcome is stored, so a second approval of the same proposal is
        refused before it touches anything: it can never take back tool
        files, programs or an MCP server the first approval installed.
        """

        requests = self.requests()
        proposal = self.stored_proposal(requests, request)
        if proposal.installs and not is_administrator(reviewer.roles):
            raise HandDownRefused("Only an owner or an administrator approves new tools, MCP servers and data folders.")
        if proposal.mcp_servers and self.mcp_store is None:
            raise HandDownRefused("This gateway cannot install MCP servers: it has no MCP registry.")
        person = self.person(request.person_id) if request.person_id else None
        try:
            compiled = self.compiler(requests).compile(proposal, person=person, task_id=request.work_id)
        except ProposalRefused as exc:
            raise HandDownRefused(f"The proposal no longer applies: {exc}") from exc
        applied = self._apply_items(requests, request, reviewer, finished, compiled, claim)
        if proposal.account_links:
            self.account_links[request.request_id] = list(proposal.account_links)
        logger.warning(
            "An access proposal was applied. event=proposal_applied request_id=%s by=%s tools=%s mcp_servers=%s "
            "grants=%s folders=%s revoked=%s",
            request.request_id,
            reviewer.person_id,
            ",".join(tool.tool_id for tool in (*proposal.tools, *proposal.clis)),
            ",".join(item.connection_id for item in proposal.mcp_servers),
            ",".join(applied.grant_ids),
            ",".join(folder.name for folder in proposal.data_folders),
            ",".join(proposal.revoke_grant_ids),
        )
        return applied

    def _apply_items(
        self,
        requests: AccessRequests,
        request: AccessRequestV1,
        reviewer: PersonPrincipal,
        finished: AccessRequestV1,
        compiled: CompiledProposal,
        claim: DecisionClaim,
    ) -> AccessRequestV1:
        """Write tool files and install MCP servers, then store the rest together; undo all on any failure.

        Each download runs between two renewals of the decision's claim
        (``AccessRequests.renew_claim``): the download gets the claim's whole
        time limit, and a decision that lost the request meanwhile stops
        before it writes anything. Any failure (also a busy or lost database)
        undoes the files, programs and servers this approval wrote, unless the
        outcome was stored after all; refusals are reported as such and other
        errors are raised again as they were.
        """

        recovery = requests.recovery_record(claim, finished, {"tool_files": [], "programs": [], "servers": []})
        requests.service.store.save_decision_recovery(recovery)
        journal = EffectJournal(requests.service.store, recovery)

        def prepare_program(root: Path, parts: tuple[str, ...], descriptor: int, body: bytes) -> None:
            journal.prepare("programs", root, parts, descriptor, body)

        def prepare_tool(path: Path, descriptor: int, body: bytes, temporary: Path) -> None:
            journal.prepare(
                "tool_files", path.parent, (path.name,), descriptor, body, staging_parts=(temporary.name,)
            )

        def prepare_server(record: MCPConnectionRecordV1) -> None:
            recovery.effects["servers"].append(record.model_dump(mode="json"))
            requests.service.store.save_decision_recovery(recovery)

        def download(url: str) -> bytes:
            requests.renew_claim(claim)
            data = self.download(url)
            requests.renew_claim(claim)
            return data

        try:
            install_programs(self.instance_root, compiled.proposal.clis, download, before_write=prepare_program)
            for tool in (*compiled.proposal.tools, *compiled.proposal.clis):
                write_instance_tool(self.instance_root, tool, before_write=prepare_tool)
            self._install_servers(
                compiled.proposal, request, reviewer, prepare_server, claim, now=requests.service.clock()
            )
            requests.service.catalog = self.catalogs.current()
            applied = self._store(requests, request, reviewer, finished, compiled, claim)
            requests.clear_recovery(claim)
            return applied
        except BaseException as exc:
            stored = requests.outcome_stored_despite(exc, request.request_id, reviewer, finished.decided_at_utc)
            if stored is not None:
                requests.clear_recovery(claim)
                return stored
            stored = requests.reconcile_decision(
                recovery, lambda latest, current, elsewhere: self.recover(requests, latest, current, elsewhere)
            )
            if stored is not None:
                return stored
            logger.warning(
                "An approved proposal could not be applied; nothing changed. event=proposal_apply_failed "
                "request_id=%s by=%s error_type=%s error=%s",
                request.request_id,
                reviewer.person_id,
                type(exc).__name__,
                exc,
            )
            if isinstance(exc, _APPLY_REFUSALS):
                raise HandDownRefused(f"The proposal could not be applied, so nothing changed: {exc}") from exc
            raise

    def recover(
        self, requests: AccessRequests, recovery: DecisionRecovery,
        current: AccessRequestV1 | None, claimed_elsewhere: bool,
    ) -> AccessRequestV1 | None:
        """Reconcile under the store lock; an exception's type never establishes ownership."""

        if recovery.matches(current):
            return current
        effects = recovery.effects
        from runtime_settings.instance_layout import InstanceLayout

        directory = InstanceLayout.at(self.instance_root).instance_tools_dir
        written = [OwnedFile.model_validate(record) for record in effects["tool_files"]]
        programs = [OwnedFile.model_validate(record) for record in effects["programs"]]
        installed = [MCPConnectionRecordV1.model_validate(record) for record in effects["servers"]]
        if current is not None and (
            current.status is AccessRequestStatus.GRANTED
            or (current.status is AccessRequestStatus.WAITING and claimed_elsewhere)
        ):
            # A newer decision may have reused these programs. Keep them even
            # when renewal failed with a database error rather than ClaimLost.
            programs = []
            if current.status is AccessRequestStatus.GRANTED:
                written, installed = [], []
            logger.warning(
                "Another decision may use this proposal's programs; they are kept. "
                "event=proposal_programs_kept request_id=%s", recovery.claim.request_id,
            )
        self._undo_items(requests, directory, recovery.claim.request_id, written, programs, installed)
        return None

    def _undo_items(
        self,
        requests: AccessRequests,
        directory: Path,
        request_id: str,
        written: list[OwnedFile],
        programs: list[OwnedFile],
        installed: list[MCPConnectionRecordV1],
    ) -> None:
        """Take back the files, programs and servers a failed approval wrote (each step logged on failure)."""

        steps: list[tuple[str, Callable[[], None]]] = [
            *((
                f"tool file {'/'.join(owned.parts)}",
                lambda owned=owned: owned.remove(directory, lambda: remove_instance_tool(directory.joinpath(*owned.parts))),
            ) for owned in written),
            *((f"program {'/'.join(owned.parts)}", lambda owned=owned: owned.remove(workspaces_root(self.instance_root))) for owned in programs),
            ("MCP servers", lambda: self._restore_servers(installed, request_id=request_id)),
            ("catalog", lambda: setattr(requests.service, "catalog", self.catalogs.current())),
        ]
        failures: list[Exception] = []
        for what, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - finish other steps, retain the journal for retry
                failures.append(exc)
                logger.exception(
                    "Could not take back part of a failed proposal. event=proposal_undo_failed part=%s", what
                )
        if failures:
            raise failures[0]

    def _install_servers(
        self,
        proposal: AccessProposalV1,
        request: AccessRequestV1,
        reviewer: PersonPrincipal,
        before_install: Callable[[MCPConnectionRecordV1], None],
        claim: DecisionClaim,
        *,
        now: datetime,
    ) -> None:
        """Activate each proposed server exactly as reviewed; ``installed`` collects the pending originals."""

        if not proposal.mcp_servers or self.mcp_store is None:
            return
        store = self.mcp_store()
        approval = ChangeApprovalRecordV1(
            mode=ChangeApprovalMode.PERSON_APPROVAL,
            approved_by=reviewer.person_id,
            approved_at_utc=now,
            access_request_id=request.request_id,
        )
        for item in proposal.mcp_servers:
            record = store.require(item.connection_id)
            if record.lifecycle is not MCPConnectionLifecycle.PENDING or record.record_digest != item.record_digest:
                raise HandDownRefused(
                    f"The MCP server {record.descriptor.connection_name} changed after the review; "
                    "TeamLead must propose it again."
                )
            before_install(record)
            with self.requests().service.store.decision_effect_transaction(claim):
                store.update_lifecycle(record.connection_id, lifecycle=MCPConnectionLifecycle.ACTIVE, approval=approval)
            logger.warning(
                "An MCP server was installed by an approved proposal. event=mcp_connection_approved "
                "connection_id=%s request_id=%s by=%s",
                record.connection_id,
                request.request_id,
                reviewer.person_id,
            )

    def _restore_servers(self, installed: list[MCPConnectionRecordV1], *, request_id: str) -> None:
        """Put back the pending records of servers installed by a proposal that then failed."""

        if not installed or self.mcp_store is None:
            return
        store = self.mcp_store()
        for record in installed:
            current = store.require(record.connection_id)
            if current.lifecycle is MCPConnectionLifecycle.PENDING and current.record_digest == record.record_digest:
                continue
            ignored = {"record_digest", "updated_at_utc", "lifecycle", "approval"}
            if (
                current.lifecycle is not MCPConnectionLifecycle.ACTIVE
                or current.approval is None or current.approval.access_request_id != request_id
                or current.model_dump(exclude=ignored) != record.model_dump(exclude=ignored)
            ):
                logger.warning(
                    "An MCP record changed since this decision; its replacement is kept. "
                    "event=access_decision_artifact_changed connection_id=%s", record.connection_id,
                )
                continue
            store.save(record)
            logger.warning(
                "An MCP server installed by a failed proposal is pending again. event=mcp_connection_install_undone "
                "connection_id=%s",
                record.connection_id,
            )

    # -- the one transaction -----------------------------------------------------------

    def _store(
        self,
        requests: AccessRequests,
        request: AccessRequestV1,
        reviewer: PersonPrincipal,
        finished: AccessRequestV1,
        compiled: CompiledProposal,
        claim: DecisionClaim,
    ) -> AccessRequestV1:
        service = requests.service
        now = service.clock()
        proposal = compiled.proposal
        grants = self._all_grants(request, reviewer, finished, compiled, now)
        for grant in grants:
            refusal = self._refusal(requests, reviewer, grant)
            if refusal:
                raise HandDownRefused(refusal)
        folders = self._folders(reviewer, now, proposal)
        # Removing access removes its whole entry (access_control.grant_entries),
        # as Remove on the Access page does.
        revoke_ids: list[str] = []
        for grant_id in proposal.revoke_grant_ids:
            removed = service.store.get(grant_id)
            if removed is None:
                raise HandDownRefused(f"You cannot remove {grant_id}.")
            for held in service.entry_grants(removed):
                if not service.may_revoke(reviewer, held):
                    raise HandDownRefused(f"You cannot remove {held.grant_id}.")
                revoke_ids.append(held.grant_id)
        ending = tuple(dict.fromkeys(revoke_ids))

        def outcome(plan: GrantPlan | None) -> AccessRequestV1:
            assert plan is not None
            given = [grant.grant_id for grant in plan.new] + [cover.grant_id for cover in plan.covered.values()]
            return finished.model_copy(update={"grant_ids": tuple(dict.fromkeys(given)), "applied_at_utc": now})

        # Planned inside the store's write transaction (one entry per tool),
        # and stored with the outcome under the decision's claim.
        _plan, applied = service.store.apply_together(
            service.team_id,
            plan=lambda: service.plan_grants(grants, ending=ending),
            data_folders=folders,
            revoke_ids=ending,
            finish=outcome,
            claim_id=claim.claim_id,
            actor=SubjectRefV1(kind=SubjectKind.USER, id=reviewer.person_id),
            at=now,
        )
        assert applied is not None
        return applied

    def _all_grants(
        self,
        request: AccessRequestV1,
        reviewer: PersonPrincipal,
        finished: AccessRequestV1,
        compiled: CompiledProposal,
        now,
    ) -> list[GrantV1]:
        """Agent grants (kept rows as proposed, the rest for the task), the person's own, and blocks."""

        kept = set(finished.kept_item_ids)
        grants: list[GrantV1] = []
        for index, item in enumerate(compiled.proposal.grants):
            lifetime, expires = self._lifetime(item, f"grant-{index}" in kept or not request.work_id, finished, now)
            grants.append(self._grant(request, reviewer, now, item, lifetime, expires))
        grants += [self._person_grant(request, reviewer, now, target, level) for target, level in compiled.person_needs]
        grants += [
            self._block(request, reviewer, now, block.agent_type, block.target, block.reason)
            for block in compiled.proposal.blocks
        ]
        return grants

    @staticmethod
    def _lifetime(item: ProposedGrantV1, keep: bool, finished: AccessRequestV1, now):
        if item.lifetime != "until_revoked" or not keep:
            return GrantLifetime.TASK, None
        if finished.keep_hours:
            return GrantLifetime.UNTIL, now + timedelta(hours=finished.keep_hours)
        return GrantLifetime.UNTIL_REVOKED, None

    @staticmethod
    def _refusal(requests: AccessRequests, reviewer: PersonPrincipal, grant: GrantV1) -> str | None:
        if grant.kind is GrantKind.BLOCK:
            if requests.service.may_give(reviewer, grant.target, AccessLevel.READ, task_id=None):
                return None
            return f"You cannot restrict {grant.target.id}: it is beyond your own authority."
        return requests.service.grant_refusal(reviewer, grant)

    def _base(self, request: AccessRequestV1, reviewer: PersonPrincipal, now, **fields) -> GrantV1:
        title = request.change.title if request.change else "access proposal"
        task_bound = fields.get("lifetime") is GrantLifetime.TASK
        return GrantV1(
            grant_id=new_id("grant"),
            team_id=request.team_id,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=reviewer.person_id),
            approval_id=request.request_id,
            reason=f"Proposal: {title}"[:2000],
            created_at_utc=now,
            # Access for one task covers the whole request (every agent on it).
            task_id=request.work_id if task_bound else None,
            **fields,
        )

    def _grant(self, request, reviewer, now, item: ProposedGrantV1, lifetime: GrantLifetime, expires=None) -> GrantV1:
        return self._base(
            request,
            reviewer,
            now,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(request.team_id, item.agent_type)),
            target=item.target,
            level=item.level,
            lifetime=lifetime,
            expires_at_utc=expires,
            delegation_depth=item.delegation_depth,
            for_person_id=request.person_id,
        )

    def _person_grant(self, request, reviewer, now, target: TargetRefV1, level: AccessLevel) -> GrantV1:
        return self._base(
            request,
            reviewer,
            now,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=str(request.person_id)),
            target=target,
            level=level,
            lifetime=GrantLifetime.UNTIL_REVOKED,
        )

    def _block(self, request, reviewer, now, agent_type: str, target: TargetRefV1, reason: str) -> GrantV1:
        return self._base(
            request,
            reviewer,
            now,
            kind=GrantKind.BLOCK,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(request.team_id, agent_type)),
            target=target,
            level=AccessLevel.FULL,
            lifetime=GrantLifetime.UNTIL_REVOKED,
        ).model_copy(update={"reason": reason[:2000]})

    def _folders(self, reviewer: PersonPrincipal, now, proposal: AccessProposalV1) -> list[DataFolderRecordV1]:
        registry = self.data_folders()
        records = []
        for folder in proposal.data_folders:
            registry.check(reviewer, folder.name, folder.path, protected=self.protected)
            records.append(
                registry.new_record(
                    reviewer, name=folder.name, path=folder.path, writable=folder.writable, reason=proposal.reason, now=now
                )
            )
        return records


__all__ = [
    "ACCESS_PROPOSAL_KIND",
    "CompiledProposal",
    "MCPRecordLookup",
    "ProposalApplier",
    "ProposalCompiler",
    "ProposalRefused",
    "proposal_digest",
    "proposal_summary",
    "with_mcp_defaults",
]
