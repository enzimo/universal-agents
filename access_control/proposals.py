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
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from access_catalog.catalog import (
    MCP_SERVERS_TOOL_SET,
    CatalogError,
    CatalogSource,
    ToolCatalog,
    builtin_tool_ids,
)
from access_control.account_sharing import TeamAccountSharing, team_account_private_text
from access_catalog.instance_tools import (
    ProposedToolFile,
    private_host_names,
    private_hosts,
    read_proposed_tool_file,
    remove_instance_tool,
    remove_proposed_tool_file,
    replace_instance_tool,
    restore_instance_tool,
    restore_removed_instance_tool,
    tool_definition,
    with_proposed_tools,
    write_instance_tool,
)
from access_control.cli_binaries import Downloader, fetch_https, workspaces_root
from access_control.data_folders import DataFolderRegistry, protected_locations, registration_problem
from access_control.decision_effects import EffectJournal, OwnedFile, RemovedFile, ReplacedFile
from access_control.proposal_clis import cli_rows, install_programs
from access_control.proposal_errors import ProposalRefused
from access_control.tool_change_diffs import describe_injections, tool_change_rows
from access_control.tool_removals import (
    active_grants_for_tool,
    active_grants_for_tool_in_store,
    check_tool_removal_conflicts,
    grant_targets_tool,
    load_removable_tool_file,
    reviewed_removal_impact_ids,
    tool_removal_rows,
    with_tool_removal_baselines,
)
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import PersonPrincipal, agent_type_id
from access_control.folders import (
    FOLDER_ROOT_DATA,
    FOLDER_ROOT_USERS,
    DataFolder,
    FolderGrantOnFileError,
    FolderIdError,
    FolderRoots,
    PathSpellingError,
    check_folder_target,
    grantable_folder_id,
    normalize_folder_id,
)
from access_control.grant_entries import GrantPlan
from access_control.mcp_servers import default_proposal_grants
from access_control.people import is_administrator
from access_control.requests import ACCESS_PROPOSAL_CHANGE, LEVEL_WORDS, AccessRequests
from access_control.stale_blocks import active_operation_blocks_for_tool, report_stale_blocks
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessProposalV1,
    AccessRequestStatus,
    AccessRequestV1,
    AccountLinkV1,
    AccountMode,
    CHANGE_ITEMS_MAX_COUNT,
    ChangeApprovalMode,
    ChangeApprovalRecordV1,
    DataFolderRecordV1,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    LinkOwnerKind,
    LinkPurpose,
    OwnerScope,
    ProposedAccountLinkV1,
    ProposedCliToolV1,
    ProposedGrantV1,
    ProposedHttpToolV1,
    ReviewItemV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
    ToolDefinitionV1,
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


def proposal_digest(proposal: AccessProposalV1) -> str:
    """SHA-256 of the proposal; omits newly added fields at defaults so pre-upgrade digests stay stable."""

    payload = proposal.model_dump(mode="json")
    if not payload.get("tool_changes"):
        payload.pop("tool_changes", None)
    if not payload.get("tool_removals"):
        payload.pop("tool_removals", None)
    for tool in payload.get("tools") or ():
        if not tool.get("gate_injections"):
            tool.pop("gate_injections", None)
    for link in payload.get("account_links") or ():
        if link.get("link_purpose") == LinkPurpose.ACCOUNT.value:
            link.pop("link_purpose", None)
    return contract_digest(payload)


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


def _unsupported_tool_change_fields(definition: ToolDefinitionV1) -> tuple[str, ...]:
    """Fields on an existing ``proposed-*.yaml`` file that ``ProposedHttpToolV1`` cannot express."""

    unsupported: list[str] = []
    if definition.owner_scope is not OwnerScope.TEAM:
        unsupported.append("owner_scope")
    if definition.link_owners != ("person", "team"):
        unsupported.append("link_owners")
    if definition.default_sharing is not None:
        unsupported.append("default_sharing")
    if definition.tool_sets:
        unsupported.append("tool_sets")
    if definition.installed_cli is not None:
        unsupported.append("installed_cli")
    return tuple(unsupported)


def load_changeable_tool_file(
    instance_root: Path, catalog: ToolCatalog, tool_id: str
) -> ProposedToolFile:
    """Read and verify the single-tool ``proposed-*.yaml`` file that a ``tool_changes`` item may replace.

    Built-in tools (``ua_definitions``), unknown tools, non-HTTP tools,
    operator hand-written catalog files, and proposal files carrying
    operator-only settings that a tool change cannot express are refused with
    a plain reason and a structured warning log.
    """

    if tool_id in builtin_tool_ids():
        logger.warning(
            "Refused a proposed tool change for a built-in tool. event=proposal_tool_change_refused "
            "reason=builtin_tool tool_id=%s",
            tool_id,
        )
        raise ProposalRefused(f"{tool_id} is a built-in tool and cannot be changed through an access proposal.")
    existing = catalog.tools.get(tool_id)
    if existing is None:
        logger.warning(
            "Refused a proposed tool change for an unknown tool. event=proposal_tool_change_refused "
            "reason=unknown_tool tool_id=%s",
            tool_id,
        )
        raise ProposalRefused(f"{tool_id} is not in the tool catalog; propose it as a new tool instead.")
    if existing.kind is not ToolKind.HTTP:
        logger.warning(
            "Refused a proposed tool change for a non-HTTP tool. event=proposal_tool_change_refused "
            "reason=not_http_tool tool_id=%s kind=%s",
            tool_id,
            existing.kind.value,
        )
        raise ProposalRefused(f"Only web API tools (http:*) can be changed through tool_changes, not {tool_id}.")
    try:
        tool_file = read_proposed_tool_file(instance_root, tool_id)
    except FileNotFoundError as exc:
        logger.warning(
            "Refused a proposed tool change for an operator-managed catalog file. event=proposal_tool_change_refused "
            "reason=operator_managed_file tool_id=%s",
            tool_id,
        )
        raise ProposalRefused(
            f"{tool_id} is defined in an operator-managed catalog file, not a proposal-written file, "
            "so it cannot be changed through an access proposal."
        ) from exc
    except CatalogError as exc:
        logger.warning(
            "Refused a proposed tool change because its catalog file is invalid. event=proposal_tool_change_refused "
            "reason=invalid_proposed_file tool_id=%s error=%s",
            tool_id,
            exc,
        )
        raise ProposalRefused(f"{tool_id} cannot be changed: {exc}") from exc
    unsupported = _unsupported_tool_change_fields(tool_file.definition)
    if unsupported:
        logger.warning(
            "Refused a proposed tool change because the file sets operator-only fields. "
            "event=proposal_tool_change_refused reason=unsupported_file_settings tool_id=%s fields=%s",
            tool_id,
            ",".join(unsupported),
        )
        raise ProposalRefused(
            f"{tool_id} has settings a tool change cannot keep ({', '.join(unsupported)}); "
            "an operator must edit its catalog file."
        )
    return tool_file


def with_tool_change_baselines(
    proposal: AccessProposalV1, *, instance_root: Path, catalog: ToolCatalog
) -> AccessProposalV1:
    """Pin ``previous_definition_digest`` on every ``tool_changes`` item from its current file bytes.

    Always overwrites any value supplied on the input so agents can never pin
    their own digest; runs before compiling and storing the proposal.
    """

    if not proposal.tool_changes:
        return proposal
    new_tool_ids = {tool.tool_id for tool in proposal.tools}
    seen: set[str] = set()
    pinned = []
    for item in proposal.tool_changes:
        if item.tool_id in seen:
            raise ProposalRefused(f"{item.tool_id} is listed more than once in tool_changes.")
        if item.tool_id in new_tool_ids:
            raise ProposalRefused(f"{item.tool_id} cannot be listed in both tools and tool_changes.")
        seen.add(item.tool_id)
        tool_file = load_changeable_tool_file(instance_root, catalog, item.tool_id)
        pinned.append(item.model_copy(update={"previous_definition_digest": tool_file.sha256}))
    return proposal.model_copy(update={"tool_changes": tuple(pinned)})


# A proposal's one-line summary (ChangeProposalV1.summary), counted from its
# review rows: what each section does, in the card's section order.
_SUMMARY_SECTION_WORDS = {
    "tool": ("add {n} new tool", "add {n} new tools"),
    "tool_change": ("change {n} tool", "change {n} tools"),
    "tool_removal": ("remove {n} tool", "remove {n} tools"),
    "access": ("give {n} access", "give {n} accesses"),
    "restriction": ("add {n} restriction", "add {n} restrictions"),
    "folder": ("register {n} folder", "register {n} folders"),
    "account": ("link {n} credential", "link {n} credentials"),
    "revocation": ("remove {n} access", "remove {n} accesses"),
    "delivery": ("deliver {n} file", "deliver {n} files"),
}
_SUMMARY_FLAG_WORDS = {
    "new_host": "a new host",
    "credential_to_new_host": "stored credentials sent to a new host",
    "private_address": "a private address",
    "block_widened": "a restriction that will block a whole tool",
    "block_moved": "a restriction whose blocked route changes",
    "full_access": "full access",
    "can_hand_on": "access that can be handed on",
    "runs_code": "code that runs",
    "new_folder": "a new folder",
    "setup_steps": "setup steps people will be shown",
    "new_program": "a new program",
    "download_host": "a download",
    "team_account": "the team's account",
}


def _summary_bucket(row: ReviewItemV1) -> str:
    if row.section == "tool" and row.item_id.startswith("change-"):
        return "tool_change"
    return row.section


def proposal_summary(rows: tuple[ReviewItemV1, ...]) -> str:
    """The proposal in one line from its review rows, such as "Access proposal: add 1 new tool, give 2 accesses"."""

    # A tool's extra rows (its commands, its setup steps, its change details, its removal impacts) are not more tools.
    items = [
        row
        for row in rows
        if not (row.section in {"tool", "tool_change", "tool_removal"} and row.item_id.count("-") > 1)
    ]
    counts = {section: sum(1 for row in items if _summary_bucket(row) == section) for section in _SUMMARY_SECTION_WORDS}
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


def _target_name(catalog: ToolCatalog, target: TargetRefV1, *, for_block: bool = False) -> str:
    if target.kind is TargetKind.FOLDER:
        return f"the path {target.id}" if for_block else f"the {target.id}/ folder"
    if target.kind is TargetKind.TOOL_SET:
        tool_set = catalog.tool_sets.get(target.id)
        return tool_set.display_name if tool_set else target.id
    tool_id, _, operation = target.id.partition("#")
    tool = catalog.tools.get(tool_id)
    name = tool.display_name if tool else tool_id
    return f"{name} ({operation})" if operation else name


def _tool_words(tool: ProposedHttpToolV1) -> str:
    routes = "; ".join(
        f"{route.name} ({route.method} {route.path}) needs {LEVEL_WORDS[route.level]}" for route in tool.routes
    )
    base = (
        f"New tool {tool.display_name} ({tool.tool_id}) for {', '.join(tool.hosts)}: reading (GET) needs read, "
        "changes (POST, PUT, PATCH) need read-and-change, deleting (DELETE) needs full"
    )
    words = f"{base}; {routes}" if routes else base
    if getattr(tool, "gate_injections", ()):
        words += f". Uses a team gate credential ({describe_injections(tool.gate_injections)})"
    if tool.setup is not None and tool.setup.prerequisites:
        words += f". Set up first: {', '.join(tool.setup.prerequisites)}"
    return words


def _setup_step_rows(index: int | str, tool: ProposedHttpToolV1 | ProposedCliToolV1) -> list[ReviewItemV1]:
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
    # Instance root for checking proposal-written tool files in tool_changes and tool_removals.
    instance_root: Path | None = None
    # Active links (account and gate) for one tool_id, used for re-link, new-host, and removal impact warnings.
    links_for_tool: Callable[[str], list[AccountLinkV1]] = lambda _tool_id: []
    # Active operation blocks on one tool_id, used to warn when route removals widen them.
    blocks_for_tool: Callable[[str], list[GrantV1]] = lambda _tool_id: []
    # Active grants and blocks targeting one tool_id, used for tool_removals impact rows.
    grants_for_tool: Callable[[str], list[GrantV1]] = lambda _tool_id: []
    # Optional live FolderRoots override (otherwise derived from instance_root + data_folders).
    folder_roots: Callable[[], FolderRoots] | None = None
    # False at submission (skip probing inside newly proposed data folders before admin approval);
    # True in ProposalApplier at apply time (enforce disk checks on proposed data folders too).
    include_proposed_data_folders: bool = False

    def _effective_folder_roots(self, proposal: AccessProposalV1) -> FolderRoots | None:
        if self.folder_roots is not None:
            base = self.folder_roots()
        elif self.instance_root is not None:
            from runtime_settings.instance_layout import InstanceLayout

            layout = InstanceLayout.at(self.instance_root)
            guarded = self.protected or protected_locations(layout.root)
            usable = self.data_folders.usable(guarded) if self.data_folders is not None else ()
            base = FolderRoots.for_layout(layout, usable)
        else:
            return None
        if not self.include_proposed_data_folders or not proposal.data_folders:
            return base
        existing_names = {folder.name for folder in base.data_folders}
        extra = tuple(
            DataFolder(folder.name, Path(os.path.realpath(Path(folder.path).expanduser())), folder.writable)
            for folder in proposal.data_folders
            if folder.name not in existing_names
        )
        return FolderRoots(base.workspaces_dir, (*base.data_folders, *extra))

    def _check_folder_disk_target(
        self,
        target: TargetRefV1,
        roots: FolderRoots | None,
        proposed_folder_names: set[str],
        *,
        for_block: bool,
    ) -> None:
        if target.kind not in {TargetKind.FOLDER, TargetKind.FILE}:
            return
        parts = normalize_folder_id(target.id).split("/")
        if not self.include_proposed_data_folders and parts[0] == FOLDER_ROOT_DATA and parts[1] in proposed_folder_names:
            # C04: do not probe host directories only proposed in this request before admin approval.
            return
        if roots is None:
            logger.warning(
                "Skipping proposal folder target disk check because no folder_roots or instance_root is set. "
                "event=folder_target_check_skipped target_kind=%s target=%s for_block=%s",
                target.kind.value,
                target.id,
                for_block,
            )
            return
        try:
            check_folder_target(roots, target.id, for_block=for_block)
        except (PathSpellingError, FolderGrantOnFileError, FolderIdError) as exc:
            raise ProposalRefused(str(exc)) from exc

    def compile(
        self, proposal: AccessProposalV1, *, person: PersonPrincipal | None, task_id: str | None
    ) -> CompiledProposal:
        servers = self._servers(proposal)
        removed_files = self._tool_removal_files(proposal)
        changed_files = self._tool_change_files(proposal)
        effective_roots = self._effective_folder_roots(proposal)
        proposed_folder_names = {folder.name for folder in proposal.data_folders}
        try:
            candidate = with_proposed_tools(
                self.catalog,
                proposal.tools,
                proposal.clis,
                proposal.tool_changes,
                proposal.tool_removals,
            )
            for record in servers:
                candidate.add_mcp_tool(record)
            candidate.validate()
        except (CatalogError, ValueError) as exc:
            if proposal.tool_removals and not (proposal.tools or proposal.clis or proposal.tool_changes or servers):
                raise ProposalRefused(f"A proposed tool cannot be removed: {exc}") from exc
            if proposal.tool_removals:
                raise ProposalRefused(f"The proposed tool changes cannot be applied: {exc}") from exc
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
        rows += self._tool_change_rows(proposal, changed_files)
        rows += self._tool_removal_rows(proposal, removed_files)
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
            self._check_folder_disk_target(
                grant.target, effective_roots, proposed_folder_names, for_block=False
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
        rows += self._block_rows(candidate, proposal, effective_roots, proposed_folder_names)
        rows += self._folder_rows(proposal)
        rows += self._account_rows(candidate, proposal)
        rows += self._revocation_rows(proposal)
        if len(rows) > CHANGE_ITEMS_MAX_COUNT:
            logger.warning(
                "Refused an access proposal with too many review rows to show on one card. "
                "event=proposal_review_rows_limit_exceeded row_count=%s limit=%s",
                len(rows),
                CHANGE_ITEMS_MAX_COUNT,
            )
            raise ProposalRefused(
                f"This proposal needs {len(rows)} review rows, which exceeds the {CHANGE_ITEMS_MAX_COUNT}-row "
                "review card limit; split it into smaller proposals so every change can be shown."
            )
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

    def _tool_change_files(self, proposal: AccessProposalV1) -> list[ProposedToolFile]:
        """Read and verify each ``tool_changes`` target file once against its pinned baseline."""

        if not proposal.tool_changes:
            return []
        if self.instance_root is None:
            raise ProposalRefused("Tool changes cannot be validated without an instance root.")
        new_tool_ids = {tool.tool_id for tool in proposal.tools}
        seen: set[str] = set()
        files: list[ProposedToolFile] = []
        for item in proposal.tool_changes:
            if item.tool_id in seen:
                raise ProposalRefused(f"{item.tool_id} is listed more than once in tool_changes.")
            if item.tool_id in new_tool_ids:
                raise ProposalRefused(f"{item.tool_id} cannot be listed in both tools and tool_changes.")
            seen.add(item.tool_id)
            tool_file = load_changeable_tool_file(self.instance_root, self.catalog, item.tool_id)
            if item.previous_definition_digest is None:
                logger.warning(
                    "Refused a proposed tool change with no pinned baseline digest. "
                    "event=proposal_tool_change_refused reason=missing_baseline tool_id=%s",
                    item.tool_id,
                )
                raise ProposalRefused(f"The proposed change to {item.tool_id} is missing its baseline digest.")
            if item.previous_definition_digest != tool_file.sha256:
                logger.warning(
                    "Refused a proposed tool change whose file changed after preparation. "
                    "event=proposal_tool_change_refused reason=baseline_changed tool_id=%s",
                    item.tool_id,
                )
                raise ProposalRefused(
                    f"The catalog file for {item.tool_id} changed after the proposal was prepared; propose it again."
                )
            files.append(tool_file)
        return files

    def _tool_change_rows(
        self, proposal: AccessProposalV1, changed_files: list[ProposedToolFile]
    ) -> list[ReviewItemV1]:
        rows: list[ReviewItemV1] = []
        for index, (item, tool_file) in enumerate(zip(proposal.tool_changes, changed_files)):
            proposed_def = tool_definition(item)
            try:
                diff, item_rows = tool_change_rows(
                    index,
                    current=tool_file.definition,
                    proposed=proposed_def,
                    active_links=self.links_for_tool(item.tool_id),
                    active_blocks=self.blocks_for_tool(item.tool_id),
                )
            except ValueError as exc:
                logger.warning(
                    "Refused a proposed tool change whose diff could not fit without truncation. "
                    "event=proposal_tool_change_refused reason=review_row_too_long tool_id=%s error=%s",
                    item.tool_id,
                    exc,
                )
                raise ProposalRefused(
                    f"The proposed change to {item.tool_id} cannot be shown on the review card without hiding part "
                    f"of the change: {exc}"
                ) from exc
            if not diff.has_changes:
                logger.warning(
                    "Refused a proposed tool change with no effective differences. "
                    "event=proposal_tool_change_refused reason=no_changes tool_id=%s",
                    item.tool_id,
                )
                raise ProposalRefused(
                    f"The proposed change to {item.tool_id} makes no changes to its current definition."
                )
            rows += item_rows
        return rows

    def _tool_removal_files(self, proposal: AccessProposalV1) -> list[ProposedToolFile]:
        """Read and verify each ``tool_removals`` target file once against its pinned baseline."""

        if not proposal.tool_removals:
            return []
        check_tool_removal_conflicts(proposal)
        if self.instance_root is None:
            raise ProposalRefused("Tool removals cannot be validated without an instance root.")
        files: list[ProposedToolFile] = []
        for item in proposal.tool_removals:
            tool_file = load_removable_tool_file(self.instance_root, self.catalog, item.tool_id)
            if item.expected_sha256 is None:
                logger.warning(
                    "Refused a proposed tool removal with no pinned baseline digest. "
                    "event=proposal_tool_removal_refused reason=missing_baseline tool_id=%s",
                    item.tool_id,
                )
                raise ProposalRefused(f"The proposed removal of {item.tool_id} is missing its baseline digest.")
            if item.expected_sha256 != tool_file.sha256:
                logger.warning(
                    "Refused a proposed tool removal whose file changed after preparation. "
                    "event=proposal_tool_removal_refused reason=baseline_changed tool_id=%s",
                    item.tool_id,
                )
                raise ProposalRefused(
                    f"The catalog file for {item.tool_id} changed after the proposal was prepared; propose it again."
                )
            files.append(tool_file)
        return files

    def _tool_removal_rows(
        self, proposal: AccessProposalV1, removed_files: list[ProposedToolFile]
    ) -> list[ReviewItemV1]:
        rows: list[ReviewItemV1] = []
        for index, (item, tool_file) in enumerate(zip(proposal.tool_removals, removed_files)):
            active_grants, active_blocks = active_grants_for_tool(self.grants_for_tool(item.tool_id), item.tool_id)
            try:
                item_rows = tool_removal_rows(
                    index,
                    tool_file.definition,
                    active_grants=active_grants,
                    active_blocks=active_blocks,
                    active_links=self.links_for_tool(item.tool_id),
                )
            except ValueError as exc:
                logger.warning(
                    "Refused a proposed tool removal whose impact could not fit without truncation. "
                    "event=proposal_tool_removal_refused reason=review_row_too_long tool_id=%s error=%s",
                    item.tool_id,
                    exc,
                )
                raise ProposalRefused(
                    f"The proposed removal of {item.tool_id} cannot be shown on the review card without hiding "
                    f"part of its impact: {exc}"
                ) from exc
            rows += item_rows
        return rows

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

    def _block_rows(
        self,
        catalog: ToolCatalog,
        proposal: AccessProposalV1,
        effective_roots: FolderRoots | None = None,
        proposed_folder_names: set[str] | None = None,
    ) -> list[ReviewItemV1]:
        rows = []
        proposed_names = proposed_folder_names or set()
        for index, block in enumerate(proposal.blocks):
            self._check_target(catalog, block.target, what="A restriction")
            self._check_folder_disk_target(
                block.target, effective_roots, proposed_names, for_block=True
            )
            rows.append(
                ReviewItemV1(
                    item_id=f"block-{index}",
                    section="restriction",
                    text=(
                        f"{block.agent_type} can no longer use {_target_name(catalog, block.target, for_block=True)}. "
                        f"Why: {block.reason}"
                    ),
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
        rows: list[ReviewItemV1] = []
        for index, link in enumerate(proposal.account_links):
            if link.purpose.strip().lower() in {"gate", "account"}:
                logger.warning(
                    "Refused a proposed account link whose purpose text looks like link_purpose. "
                    "event=proposal_account_link_purpose_ambiguous tool_id=%s purpose=%s",
                    link.tool_id,
                    link.purpose.strip().lower(),
                )
                raise ProposalRefused(
                    'purpose is your reason in plain words; set link_purpose to "gate" for the team gate credential '
                    'or "account" for an account credential.'
                )
            rows.append(
                self._gate_link_row(index, catalog, link)
                if link.link_purpose is LinkPurpose.GATE
                else self._account_link_row(index, catalog, link)
            )
        return rows

    @staticmethod
    def _gate_link_row(index: int, catalog: ToolCatalog, link: ProposedAccountLinkV1) -> ReviewItemV1:
        tool = catalog.tools.get(link.tool_id)
        if tool is None or tool.kind is not ToolKind.HTTP or not tool.gate_injections:
            raise ProposalRefused(f"{link.tool_id} does not use a team gate credential.")
        return ReviewItemV1(
            item_id=f"account-{index}",
            section="account",
            text=(
                f"After approval you choose the team's gate credential {tool.display_name} sends on "
                f"every request to its hosts: {link.purpose}"
            ),
        )

    def _account_link_row(self, index: int, catalog: ToolCatalog, link: ProposedAccountLinkV1) -> ReviewItemV1:
        tool = catalog.tools.get(link.tool_id)
        if tool is None or tool.account is not AccountMode.LINKED_CREDENTIAL:
            raise ProposalRefused(f"{link.tool_id} does not use a linked credential.")
        team = link.owner == LinkOwnerKind.TEAM.value
        if link.owner not in tool.link_owners:
            whose = "the team's account" if not team else "each person's own account"
            raise ProposalRefused(f"{tool.display_name} uses only {whose}.")
        if team and not self.account_sharing.team_account_used(tool):
            raise ProposalRefused(team_account_private_text(tool))
        owner = "the team's account" if team else "the account of the person this task works for"
        return ReviewItemV1(
            item_id=f"account-{index}",
            section="account",
            text=f"After approval you choose the credential {tool.display_name} uses ({owner}): {link.purpose}",
        )

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


def _service_with_catalog(service: AccessService, catalog: ToolCatalog) -> AccessService:
    """Build an unshared ``AccessService`` against ``catalog``, sharing ``service.store`` and ``service.cedar``."""

    return AccessService(
        store=service.store,
        catalog=catalog,
        cedar=service.cedar,
        team_id=service.team_id,
        clock=service.clock,
        agent_role_of=service.agent_role_of,
        lead_agent_type=service.lead_agent_type,
        folder_roots=service.folder_roots,
    )


def _check_removal_grants_authority(
    check_service: AccessService,
    reviewer: PersonPrincipal,
    grants: Iterable[GrantV1],
) -> None:
    """Verify ``reviewer`` may revoke every live grant or block removed alongside a tool."""

    for grant in grants:
        if grant.kind is GrantKind.BLOCK:
            if not AccessRequests.may_unblock(reviewer, grant):
                raise HandDownRefused(f"You cannot remove block {grant.grant_id} on {grant.target.id}.")
        elif not check_service.may_revoke(reviewer, grant):
            raise HandDownRefused(f"You cannot remove grant {grant.grant_id} on {grant.target.id}.")


def _check_removal_grants_reviewed(
    request: AccessRequestV1,
    grants: Iterable[GrantV1],
    reviewed_grant_ids: frozenset[str],
) -> None:
    """Refuse if active/pending grants or blocks appeared after the proposal was compiled for review."""

    unreviewed = [grant.grant_id for grant in grants if grant.grant_id not in reviewed_grant_ids]
    if unreviewed:
        logger.warning(
            "Refused a tool removal because new grants or blocks appeared after review. "
            "event=proposal_tool_removal_impact_changed request_id=%s unreviewed_grants=%s",
            request.request_id,
            ",".join(unreviewed),
        )
        raise HandDownRefused(
            "New access grants or restrictions were added for a removed tool after this proposal was prepared; "
            "TeamLead must propose the removal again so the full impact can be reviewed."
        )


def _check_removal_links_reviewed(
    request: AccessRequestV1,
    links: Iterable[AccountLinkV1],
    reviewed_link_ids: frozenset[str],
) -> None:
    """Refuse if active account or gate links appeared after the proposal was compiled for review."""

    unreviewed = [link.link_id for link in links if link.link_id not in reviewed_link_ids]
    if unreviewed:
        logger.warning(
            "Refused a tool removal because new credential links appeared after review. "
            "event=proposal_tool_removal_impact_changed request_id=%s unreviewed_links=%s",
            request.request_id,
            ",".join(unreviewed),
        )
        raise HandDownRefused(
            "New credential links were added for a removed tool after this proposal was prepared; "
            "TeamLead must propose the removal again so the full impact can be reviewed."
        )


def _reviewed_removal_sets(request: AccessRequestV1) -> tuple[frozenset[str], frozenset[str]]:
    if request.change is None:
        return frozenset(), frozenset()
    return reviewed_removal_impact_ids(request.change.items)


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
    account_links: dict[str, list[ProposedAccountLinkV1]] = field(default_factory=dict)
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
            instance_root=self.instance_root,
            links_for_tool=lambda tool_id: [
                link for link in service.store.list_links(service.team_id, purpose=None) if link.tool_id == tool_id
            ],
            blocks_for_tool=lambda tool_id: active_operation_blocks_for_tool(
                service.store, service.team_id, tool_id, now=service.clock()
            ),
            grants_for_tool=lambda tool_id: active_grants_for_tool_in_store(
                service.store, service.team_id, tool_id, now=service.clock()
            ),
            include_proposed_data_folders=True,
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
            raise HandDownRefused(
                "Only an owner or an administrator approves new tools, tool changes, tool removals, MCP servers and data folders."
            )
        if proposal.mcp_servers and self.mcp_store is None:
            raise HandDownRefused("This gateway cannot install MCP servers: it has no MCP registry.")
        person = self.person(request.person_id) if request.person_id else None
        try:
            compiled = self.compiler(requests).compile(proposal, person=person, task_id=request.work_id)
        except ProposalRefused as exc:
            raise HandDownRefused(f"The proposal no longer applies: {exc}") from exc
        applied = self._apply_items(requests, request, reviewer, finished, compiled, claim)
        if proposal.tool_changes:
            self._report_stale_blocks(requests)
        if proposal.account_links:
            self.account_links[request.request_id] = list(proposal.account_links)
        logger.warning(
            "An access proposal was applied. event=proposal_applied request_id=%s by=%s tools=%s tool_changes=%s "
            "tool_removals=%s mcp_servers=%s grants=%s folders=%s revoked=%s",
            request.request_id,
            reviewer.person_id,
            ",".join([*(tool.tool_id for tool in proposal.tools), *(cli.tool_id for cli in proposal.clis)]),
            ",".join(changed.tool_id for changed in proposal.tool_changes),
            ",".join(removed.tool_id for removed in proposal.tool_removals),
            ",".join(item.connection_id for item in proposal.mcp_servers),
            ",".join(applied.grant_ids),
            ",".join(folder.name for folder in proposal.data_folders),
            ",".join(proposal.revoke_grant_ids),
        )
        return applied

    def _report_stale_blocks(self, requests: AccessRequests) -> None:
        """Notify administrators immediately when a tool change widens an operation block to the whole tool."""

        service = requests.service
        try:
            report_stale_blocks(
                self.catalogs.current(),
                service.store,
                service.team_id,
                now=service.clock(),
            )
        except Exception:  # noqa: BLE001 - the tool change is committed and the block already fails closed
            logger.exception(
                "Could not report widened blocks after applying a tool change. "
                "event=stale_block_check_failed team_id=%s",
                service.team_id,
            )

    def _precheck_tool_removals(
        self,
        requests: AccessRequests,
        request: AccessRequestV1,
        reviewer: PersonPrincipal,
        compiled: CompiledProposal,
    ) -> ToolCatalog | None:
        """Capture the pre-removal catalog and run a fast impact + authority pre-check before touching disk."""

        if not compiled.proposal.tool_removals:
            return None
        pre_removal_catalog = self.catalogs.current()
        pre_removal_service = _service_with_catalog(requests.service, pre_removal_catalog)
        now = requests.service.clock()
        reviewed_grant_ids, reviewed_link_ids = _reviewed_removal_sets(request)
        all_links = requests.service.store.list_links(requests.service.team_id, purpose=None)
        for item in compiled.proposal.tool_removals:
            live = active_grants_for_tool_in_store(
                requests.service.store, requests.service.team_id, item.tool_id, now=now
            )
            _check_removal_grants_reviewed(request, live, reviewed_grant_ids)
            _check_removal_grants_authority(pre_removal_service, reviewer, live)
            tool_links = [link for link in all_links if link.tool_id == item.tool_id]
            _check_removal_links_reviewed(request, tool_links, reviewed_link_ids)
        return pre_removal_catalog

    def _finish_committed_recovery(self, requests: AccessRequests, recovery: DecisionRecovery, claim: DecisionClaim) -> None:
        if self._discard_removed_staging(recovery, raise_on_error=False):
            requests.clear_recovery(claim)

    def _apply_items(
        self,
        requests: AccessRequests,
        request: AccessRequestV1,
        reviewer: PersonPrincipal,
        finished: AccessRequestV1,
        compiled: CompiledProposal,
        claim: DecisionClaim,
    ) -> AccessRequestV1:
        """Write tool files, replace changed tools, and install MCP servers, then store the rest together; undo all on any failure.

        Each download runs between two renewals of the decision's claim
        (``AccessRequests.renew_claim``): the download gets the claim's whole
        time limit, and a decision that lost the request meanwhile stops
        before it writes anything. Any failure (also a busy or lost database)
        undoes the files, replacements, programs and servers this approval
        wrote, unless the outcome was stored after all; refusals are reported
        as such and other errors are raised again as they were.
        """

        pre_removal_catalog = self._precheck_tool_removals(requests, request, reviewer, compiled)
        recovery = requests.recovery_record(
            claim,
            finished,
            {"tool_files": [], "replaced_tool_files": [], "removed_tool_files": [], "programs": [], "servers": []},
        )
        requests.service.store.save_decision_recovery(recovery)
        journal = EffectJournal(requests.service.store, recovery)

        def prepare_program(root: Path, parts: tuple[str, ...], descriptor: int, body: bytes) -> None:
            journal.prepare("programs", root, parts, descriptor, body)

        def prepare_tool(path: Path, descriptor: int, body: bytes, temporary: Path) -> None:
            journal.prepare(
                "tool_files", path.parent, (path.name,), descriptor, body, staging_parts=(temporary.name,)
            )

        def prepare_replacement(
            path: Path, descriptor: int, body: bytes, temporary: Path, previous_bytes: bytes
        ) -> None:
            journal.prepare_replacement(
                "replaced_tool_files",
                path.parent,
                (path.name,),
                descriptor,
                body,
                previous_bytes,
                staging_parts=(temporary.name,),
            )

        def prepare_removal(
            path: Path, device: int, inode: int, previous_bytes: bytes, staging_path: Path
        ) -> None:
            journal.prepare_removal(
                "removed_tool_files",
                (path.name,),
                device,
                inode,
                previous_bytes,
                staging_parts=(staging_path.name,),
            )

        def publish_effect(commit: Callable[[], None]) -> None:
            with requests.service.store.decision_effect_transaction(claim):
                commit()

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
            for removed in compiled.proposal.tool_removals:
                assert removed.expected_sha256 is not None
                remove_proposed_tool_file(
                    self.instance_root,
                    removed.tool_id,
                    expected_sha256=removed.expected_sha256,
                    before_remove=prepare_removal,
                    publish=publish_effect,
                    keep_staging=True,
                )
            new_tools: tuple[ProposedHttpToolV1 | ProposedCliToolV1, ...] = (
                *compiled.proposal.tools,
                *compiled.proposal.clis,
            )
            for tool in new_tools:
                write_instance_tool(self.instance_root, tool, before_write=prepare_tool)
            for changed in compiled.proposal.tool_changes:
                assert changed.previous_definition_digest is not None
                replace_instance_tool(
                    self.instance_root,
                    changed,
                    expected_previous_sha256=changed.previous_definition_digest,
                    before_replace=prepare_replacement,
                    publish=publish_effect,
                )
            self._install_servers(
                compiled.proposal, request, reviewer, prepare_server, claim, now=requests.service.clock()
            )
            requests.service.catalog = self.catalogs.current()
            applied = self._store(
                requests,
                request,
                reviewer,
                finished,
                compiled,
                claim,
                pre_removal_catalog=pre_removal_catalog,
            )
            self._finish_committed_recovery(requests, recovery, claim)
            return applied
        except BaseException as exc:
            stored = requests.outcome_stored_despite(exc, request.request_id, reviewer, finished.decided_at_utc)
            if stored is not None:
                self._finish_committed_recovery(requests, recovery, claim)
                return stored
            # Require the recovery row to still exist so a stalled claim whose
            # recovery row was already reconciled by maintenance never replays
            # undo, while allowing cleanup while this decision's own claim is live.
            stored = requests.reconcile_decision(
                recovery,
                lambda latest, current, elsewhere: self.recover(requests, latest, current, elsewhere),
                require_record=True,
                skip_live_claim=False,
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

    def _discard_removed_staging(self, recovery: DecisionRecovery, *, raise_on_error: bool) -> bool:
        """Unlink this decision's removal staging files after commit or when GRANTED."""

        records = recovery.effects.get("removed_tool_files", ())
        if not records:
            return True
        from runtime_settings.instance_layout import InstanceLayout

        directory = InstanceLayout.at(self.instance_root).instance_tools_dir
        removed = [RemovedFile.model_validate(record) for record in records]
        failures: list[Exception] = []
        for item in removed:
            try:
                item.discard_staging(directory)
            except Exception as exc:  # noqa: BLE001 - retry on next recovery pass
                failures.append(exc)
                logger.exception(
                    "Could not unlink a committed removal's staging file; Inbox recovery will retry. "
                    "event=proposal_removal_staging_cleanup_failed request_id=%s path=%s",
                    recovery.claim.request_id,
                    "/".join(item.staging_parts),
                )
        if failures:
            if raise_on_error:
                raise failures[0]
            return False
        return True

    def _keep_removed_for_other_decision(
        self, recovery: DecisionRecovery, current: AccessRequestV1, claimed_elsewhere: bool
    ) -> None:
        if current.status is AccessRequestStatus.GRANTED:
            self._discard_removed_staging(recovery, raise_on_error=True)
        logger.warning(
            "Another decision owns or approved this proposal; removed tool files are not restored. "
            "event=proposal_removals_kept request_id=%s status=%s claimed_elsewhere=%s",
            recovery.claim.request_id,
            current.status.value,
            claimed_elsewhere,
        )

    def recover(
        self, requests: AccessRequests, recovery: DecisionRecovery,
        current: AccessRequestV1 | None, claimed_elsewhere: bool,
    ) -> AccessRequestV1 | None:
        """Reconcile under the store lock; an exception's type never establishes ownership."""

        if recovery.matches(current):
            self._discard_removed_staging(recovery, raise_on_error=True)
            return current
        effects = recovery.effects
        from runtime_settings.instance_layout import InstanceLayout

        directory = InstanceLayout.at(self.instance_root).instance_tools_dir
        written = [OwnedFile.model_validate(record) for record in effects["tool_files"]]
        replaced = [ReplacedFile.model_validate(record) for record in effects["replaced_tool_files"]]
        removed = [RemovedFile.model_validate(record) for record in effects.get("removed_tool_files", ())]
        programs = [OwnedFile.model_validate(record) for record in effects["programs"]]
        installed = [MCPConnectionRecordV1.model_validate(record) for record in effects["servers"]]
        if current is not None and (
            current.status is AccessRequestStatus.GRANTED
            or (current.status is AccessRequestStatus.WAITING and claimed_elsewhere)
        ):
            # A newer decision may have reused these programs or approved the
            # removal. Keep them even when renewal failed with a database error.
            programs = []
            if removed:
                self._keep_removed_for_other_decision(recovery, current, claimed_elsewhere)
                removed = []
            if current.status is AccessRequestStatus.GRANTED:
                written, replaced, installed = [], [], []
            logger.warning(
                "Another decision may use this proposal's programs; they are kept. "
                "event=proposal_programs_kept request_id=%s", recovery.claim.request_id,
            )
        self._undo_items(
            requests, directory, recovery.claim.request_id, written, replaced, removed, programs, installed
        )
        return None

    def _undo_items(
        self,
        requests: AccessRequests,
        directory: Path,
        request_id: str,
        written: list[OwnedFile],
        replaced: list[ReplacedFile],
        removed: list[RemovedFile],
        programs: list[OwnedFile],
        installed: list[MCPConnectionRecordV1],
    ) -> None:
        """Take back the files, replacements, removals, programs and servers a failed approval applied (each step logged on failure)."""

        steps: list[tuple[str, Callable[[], None]]] = [
            *((
                f"tool file {'/'.join(owned.parts)}",
                lambda owned=owned: owned.remove(directory, lambda: remove_instance_tool(directory.joinpath(*owned.parts))),
            ) for owned in written),
            *((
                f"replaced tool file {'/'.join(item.parts)}",
                lambda item=item: item.restore(
                    directory,
                    lambda previous, target=directory.joinpath(*item.parts): restore_instance_tool(target, previous),
                ),
            ) for item in reversed(replaced)),
            *((
                f"removed tool file {'/'.join(item.parts)}",
                lambda item=item: item.restore(
                    directory,
                    lambda item=item: restore_removed_instance_tool(
                        directory.joinpath(*item.parts),
                        directory.joinpath(*item.staging_parts),
                        expected_device=item.device,
                        expected_inode=item.inode,
                    ),
                ),
            ) for item in reversed(removed)),
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
        *,
        pre_removal_catalog: ToolCatalog | None = None,
    ) -> AccessRequestV1:
        service = requests.service
        now = service.clock()
        proposal = compiled.proposal
        removed_tool_ids = tuple(item.tool_id for item in proposal.tool_removals)
        pre_removal_service = (
            _service_with_catalog(service, pre_removal_catalog) if pre_removal_catalog is not None else None
        )
        grants = self._all_grants(request, reviewer, finished, compiled, now)
        for grant in grants:
            refusal = self._refusal(requests, reviewer, grant)
            if refusal:
                raise HandDownRefused(refusal)
        folders = self._folders(reviewer, now, proposal)
        self._check_stored_folder_targets(proposal, grants)
        # Removing access removes its whole entry (access_control.grant_entries),
        # as Remove on the Access page does.
        revoke_ids: list[str] = []
        for grant_id in proposal.revoke_grant_ids:
            removed = service.store.get(grant_id)
            if removed is None:
                raise HandDownRefused(f"You cannot remove {grant_id}.")
            revoke_service = (
                pre_removal_service
                if pre_removal_service is not None
                and any(grant_targets_tool(removed, tool_id) for tool_id in removed_tool_ids)
                else service
            )
            for held in revoke_service.entry_grants(removed):
                if not revoke_service.may_revoke(reviewer, held):
                    raise HandDownRefused(f"You cannot remove {held.grant_id}.")
                revoke_ids.append(held.grant_id)
        ending = tuple(dict.fromkeys(revoke_ids))

        reviewed_grant_ids, reviewed_link_ids = _reviewed_removal_sets(request)

        def check_removal_grants(matched: list[GrantV1]) -> None:
            _check_removal_grants_reviewed(request, matched, reviewed_grant_ids)
            _check_removal_grants_authority(pre_removal_service or service, reviewer, matched)

        def check_removal_links(matched: list[AccountLinkV1]) -> None:
            _check_removal_links_reviewed(request, matched, reviewed_link_ids)

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
            revoke_tool_ids=removed_tool_ids,
            check_tool_removal_grants=check_removal_grants if removed_tool_ids else None,
            check_tool_removal_links=check_removal_links if removed_tool_ids else None,
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
        now: datetime,
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
    def _lifetime(
        item: ProposedGrantV1, keep: bool, finished: AccessRequestV1, now: datetime
    ) -> tuple[GrantLifetime, datetime | None]:
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

    def _base(
        self, request: AccessRequestV1, reviewer: PersonPrincipal, now: datetime, **fields: Any
    ) -> GrantV1:
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

    def _grant(
        self,
        request: AccessRequestV1,
        reviewer: PersonPrincipal,
        now: datetime,
        item: ProposedGrantV1,
        lifetime: GrantLifetime,
        expires: datetime | None = None,
    ) -> GrantV1:
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

    def _person_grant(
        self,
        request: AccessRequestV1,
        reviewer: PersonPrincipal,
        now: datetime,
        target: TargetRefV1,
        level: AccessLevel,
    ) -> GrantV1:
        return self._base(
            request,
            reviewer,
            now,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=str(request.person_id)),
            target=target,
            level=level,
            lifetime=GrantLifetime.UNTIL_REVOKED,
        )

    def _block(
        self,
        request: AccessRequestV1,
        reviewer: PersonPrincipal,
        now: datetime,
        agent_type: str,
        target: TargetRefV1,
        reason: str,
    ) -> GrantV1:
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

    def _check_stored_folder_targets(self, proposal: AccessProposalV1, grants: list[GrantV1]) -> None:
        """Re-check folder/file grants and blocks against effective FolderRoots right before apply_together."""

        folder_grants = [grant for grant in grants if grant.target.kind in {TargetKind.FOLDER, TargetKind.FILE}]
        if not folder_grants:
            return
        from runtime_settings.instance_layout import InstanceLayout

        layout = InstanceLayout.at(self.instance_root)
        guarded = self.protected or protected_locations(layout.root)
        usable = self.data_folders().usable(guarded)
        existing_names = {folder.name for folder in usable}
        extra = tuple(
            DataFolder(folder.name, Path(os.path.realpath(Path(folder.path).expanduser())), folder.writable)
            for folder in proposal.data_folders
            if folder.name not in existing_names
        )
        roots = FolderRoots.for_layout(layout, (*usable, *extra))
        for grant in folder_grants:
            try:
                check_folder_target(roots, grant.target.id, for_block=grant.kind is GrantKind.BLOCK)
            except (PathSpellingError, FolderGrantOnFileError, FolderIdError) as exc:
                raise HandDownRefused(str(exc)) from exc

    def _folders(
        self, reviewer: PersonPrincipal, now: datetime, proposal: AccessProposalV1
    ) -> list[DataFolderRecordV1]:
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
    "check_tool_removal_conflicts",
    "load_changeable_tool_file",
    "load_removable_tool_file",
    "proposal_digest",
    "proposal_summary",
    "with_mcp_defaults",
    "with_tool_change_baselines",
    "with_tool_removal_baselines",
]
