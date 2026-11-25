"""``propose_access_change`` and ``get_proposal_status``: TeamLead's access proposals (Phase 6).

When a service is missing from the tool catalog, a proposal-written web API or
installed CLI tool needs changing or removing, or access is needed that nobody
may hand on, TeamLead proposes the whole change at once: new web API or
command-line tools, changes to proposal-written web API tools
(``tool_changes``), removals of proposal-written web API or command-line tools
(``tool_removals``), MCP servers to install, access for agent types,
restrictions, data folders to register, account or gate links to set up and
grants to remove. Trusted code checks it and writes the review card
(``access_control.proposals``); an administrator approves it once in the Inbox
or in chat, and the gateway applies everything together. The task pauses while
it waits, like ``request_access``, and this tool then returns what was applied.
When the proposal asks for account links, the approver chooses the credentials
in forms right after approving, and the task stays paused until those forms end
(``assistant_gateway.access_inbox``); the result then says which links exist.
With authorization off there are no grants: the person agrees in conversation
(``confirmed=true``) and the new tools, tool changes, tool removals, MCP servers
and data folders are applied directly; an installed MCP server's tools load on
the next model call of each agent it is meant for (``custom_agents.tool_sync``).

``submit_and_wait`` and ``apply_by_agreement`` are shared with
``mcp_connections(action="propose")`` (``custom_tools.providers.capabilities``), which
records a new MCP connection and proposes installing it in the same call.
"""

from __future__ import annotations

from access_control.cli_binaries import fetch_https
from access_control.proposal_clis import install_programs
from access_control.decision_effects import OwnedFile, RemovedFile, ReplacedFile

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from strands import tool
from strands.interrupt import InterruptException
from strands.types.tools import ToolContext

from access_catalog.catalog import CatalogError, ToolCatalog, load_catalog
from access_catalog.instance_tools import (
    remove_instance_tool,
    remove_proposed_tool_file,
    replace_instance_tool,
    restore_instance_tool,
    restore_removed_instance_tool,
    tool_definition,
    with_proposed_tools,
    write_instance_tool,
)
from access_control.account_sharing import TeamAccountSharing
from access_control.data_folders import DataFolderRefused, DataFolderRegistry, registration_problem
from access_control.decisions import HandDownRefused
from access_control.entities import PersonPrincipal
from access_control.identity import IdentityRejected
from access_control.proposals import (
    ACCESS_PROPOSAL_KIND,
    ProposalCompiler,
    ProposalRefused,
    load_changeable_tool_file,
    proposal_summary,
    with_mcp_defaults,
    with_tool_change_baselines,
    with_tool_removal_baselines,
)
from access_control.stale_blocks import active_operation_blocks_for_tool
from access_control.tool_change_diffs import tool_change_rows
from access_control.tool_removals import (
    active_grants_for_tool_in_store,
    load_removable_tool_file,
    tool_removal_rows,
)
from custom_messages.contracts.access_v2 import (
    AccessProposalV1,
    AccessRequestStatus,
    AccessRequestV1,
    ChangeApprovalMode,
    ChangeApprovalRecordV1,
    ChangeProposalV1,
    CredentialInjectionV1,
    DataFolderRecordV1,
    LinkOwnerKind,
    LinkPurpose,
    SubjectKind,
    SubjectRefV1,
    injections_match,
)
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.task_v1 import ACCESS_REQUEST_INTERRUPT_KIND
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers.access import ACCESS_REQUEST_INTERRUPT_NAME
from custom_tools.runtime_context import active_task_context, get_agent_ref
from id_generation import new_id
from storage.access_repository import AccessRepository, AccessStoreConflict

logger = logging.getLogger(__name__)

MAX_EFFECT_LINES = 40
# Who approved a change made with authorization off (access_control.changes).
AGREEMENT_APPROVER = "agreement"
# The errors a proposal tool reports to the model as a refusal.
PROPOSAL_REFUSALS = (DataFolderRefused, HandDownRefused, IdentityRejected, ProposalRefused, RuntimeError, ValueError)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _proposal(
    title: str,
    reason: str,
    tools: list[dict[str, Any]] | None,
    mcp_servers: list[dict[str, Any]] | None,
    grants: list[dict[str, Any]] | None,
    blocks: list[dict[str, Any]] | None,
    account_links: list[dict[str, Any]] | None,
    data_folders: list[dict[str, Any]] | None,
    revoke_grant_ids: list[str] | None,
    clis: list[dict[str, Any]] | None = None,
    tool_changes: list[dict[str, Any]] | None = None,
    tool_removals: list[dict[str, Any]] | None = None,
) -> AccessProposalV1:
    return AccessProposalV1.model_validate(
        {
            "title": title,
            "reason": reason,
            "tools": tuple(tools or ()),
            "tool_changes": tuple(tool_changes or ()),
            "tool_removals": tuple(tool_removals or ()),
            "clis": tuple(clis or ()),
            "mcp_servers": tuple(mcp_servers or ()),
            "grants": tuple(grants or ()),
            "blocks": tuple(blocks or ()),
            "account_links": tuple(account_links or ()),
            "data_folders": tuple(data_folders or ()),
            "revoke_grant_ids": tuple(revoke_grant_ids or ()),
        }
    )


def _expected_link_injections(
    proposal: AccessProposalV1,
    tool_id: str,
    purpose: LinkPurpose,
    catalog: ToolCatalog | None,
) -> tuple[CredentialInjectionV1, ...] | None:
    """Expected injections for ``tool_id`` and ``purpose`` after this proposal applied."""

    for item in (*proposal.tool_changes, *proposal.tools):
        if item.tool_id == tool_id:
            return item.gate_injections if purpose is LinkPurpose.GATE else item.credential_injections
    for cli in proposal.clis:
        if cli.tool_id == tool_id:
            return () if purpose is LinkPurpose.GATE else cli.credential_injections
    tool_def = catalog.tools.get(tool_id) if catalog is not None else None
    if tool_def is None:
        return None
    return tool_def.gate_injections if purpose is LinkPurpose.GATE else tool_def.credential_injections


def _link_states(
    store: AccessRepository | None,
    request: AccessRequestV1,
    catalog: ToolCatalog | None = None,
) -> list[dict[str, Any]]:
    """For each account or gate link the proposal asked for: whether a matching link exists now.

    The approver's forms ended before the task was told of the decision, so
    this says what was linked; ``person`` links belong to the approver. An
    older active link whose injections no longer match the updated tool
    definition is reported as not linked.
    """

    if store is None or request.change is None:
        return []
    stored = store.get_plan(request.change.change_id)
    if stored is None:
        return []
    proposal = AccessProposalV1.model_validate(stored[3])
    approver = request.decided_by.id if request.decided_by is not None else ""
    states = []
    for item in proposal.account_links:
        owner_kind = LinkOwnerKind(item.owner)
        owner_id = request.team_id if owner_kind is LinkOwnerKind.TEAM else approver
        active = (
            store.active_link(
                request.team_id, item.tool_id, owner_kind, owner_id, purpose=item.link_purpose
            )
            if owner_id
            else None
        )
        expected = _expected_link_injections(proposal, item.tool_id, item.link_purpose, catalog)
        linked = active is not None and (expected is None or injections_match(active.injections, expected))
        entry: dict[str, Any] = {"tool_id": item.tool_id, "owner": item.owner, "linked": linked}
        if item.link_purpose is LinkPurpose.GATE:
            entry["link_purpose"] = item.link_purpose.value
        states.append(entry)
    return states


def _result(
    request: AccessRequestV1,
    store: AccessRepository | None = None,
    catalog: ToolCatalog | None = None,
) -> dict[str, Any]:
    if request.status is AccessRequestStatus.GRANTED and request.applied_at_utc is not None:
        applied: dict[str, Any] = {
            "status": "applied",
            "proposal_id": request.change.change_id if request.change else None,
            "grant_ids": list(request.grant_ids),
            "kept_for_later_tasks": list(request.kept_item_ids),
            "note": request.decision_note or None,
            "message": (
                "Applied. Tool catalog additions, changes, and removals are active now; an installed MCP server's tools appear "
                "on the next model call of each agent that may use it, for the work of the person it serves."
            ),
        }
        links = _link_states(store, request, catalog=catalog)
        if links:
            applied["account_links"] = links
            applied["message"] += (
                " account_links says which credentials were linked in the approver's forms. For an account that is "
                "not linked, call set_up_tools for it (it opens the person's account link form), or tell them an "
                "administrator can link the team's account with /link <tool_id> team. For a team gate credential "
                "that is not linked, an administrator can link it with /link <tool_id> gate."
            )
        return applied
    if request.status is AccessRequestStatus.DENIED:
        message = "The proposal was denied. Do not propose it again unchanged; tell the person what will not happen."
    elif request.status is AccessRequestStatus.WITHDRAWN:
        message = "The proposal was withdrawn because its task ended."
    elif request.status is AccessRequestStatus.GRANTED:
        message = f"Approved but not applied: {request.apply_error or 'the gateway could not apply it'}."
    else:
        message = "Still waiting for an administrator to decide. Do not propose it again."
    return {"status": request.status.value, "request_id": request.request_id, "message": message}


def _mcp_store(agent: Any) -> Any:
    from custom_agents.providers.mcp_connections import resolve_agent_mcp_connection_paths
    from mcp_control.registry import MCPConnectionStore

    return MCPConnectionStore(resolve_agent_mcp_connection_paths(agent))


def _optional_access_store(agent: Any) -> AccessRepository | None:
    """Return the access store when configured on ``agent``, or ``None`` if unavailable in auth-off mode."""

    task_manager = getattr(agent, "task_manager", None)
    storage = getattr(task_manager, "storage_settings", None) if task_manager is not None else None
    if storage is None:
        return None
    return AccessRepository(storage)


def _agreement_rows(
    agent: Any, proposal: AccessProposalV1, change_rows: list[str], removal_rows: list[str]
) -> list[str]:
    rows = [f"Add {tool.display_name} ({tool.tool_id}) for {', '.join(tool.hosts)}" for tool in proposal.tools]
    rows += [
        f"Add {cli.display_name} ({cli.tool_id}) ({cli.installed_cli.binary_path})"
        for cli in proposal.clis
    ]
    rows += change_rows
    rows += removal_rows
    if proposal.mcp_servers:
        store = _mcp_store(agent)
        for item in proposal.mcp_servers:
            record = store.get(item.connection_id)
            name = record.descriptor.connection_name if record else item.connection_id
            where = (record.descriptor.endpoint or record.descriptor.command or "") if record else ""
            rows.append(f"Install the MCP server {name}" + (f" ({where})" if where else ""))
    rows += [f"Register {folder.path} as data/{folder.name}" for folder in proposal.data_folders]
    return rows


def _validate_agreement_catalog(catalog: Any, proposal: AccessProposalV1) -> None:
    if not (proposal.tools or proposal.clis or proposal.tool_changes or proposal.tool_removals):
        return
    try:
        with_proposed_tools(
            catalog,
            proposal.tools,
            proposal.clis,
            proposal.tool_changes,
            proposal.tool_removals,
        )
    except (CatalogError, ValueError) as exc:
        if proposal.tool_removals and not (proposal.tools or proposal.clis or proposal.tool_changes):
            raise ProposalRefused(f"A proposed tool cannot be removed: {exc}") from exc
        if proposal.tool_removals:
            raise ProposalRefused(f"The proposed tool changes cannot be applied: {exc}") from exc
        raise ProposalRefused(f"A proposed tool cannot be added: {exc}") from exc


def _agreement_tool_change_rows(
    instance_root: Path,
    catalog: Any,
    proposal: AccessProposalV1,
    *,
    all_links: list[Any],
    store: AccessRepository | None,
    team_id: str,
    now: datetime,
) -> list[str]:
    change_rows: list[str] = []
    for index, item in enumerate(proposal.tool_changes):
        tool_file = load_changeable_tool_file(instance_root, catalog, item.tool_id)
        if item.previous_definition_digest != tool_file.sha256:
            raise ProposalRefused(
                f"The catalog file for {item.tool_id} changed after the proposal was prepared; propose it again."
            )
        blocks = active_operation_blocks_for_tool(store, team_id, item.tool_id, now=now) if store and team_id else []
        diff, rows = tool_change_rows(
            index,
            current=tool_file.definition,
            proposed=tool_definition(item),
            active_links=[link for link in all_links if link.tool_id == item.tool_id],
            active_blocks=blocks,
        )
        if not diff.has_changes:
            logger.warning(
                "Refused a proposed tool change with no effective differences. "
                "event=proposal_tool_change_refused reason=no_changes tool_id=%s",
                item.tool_id,
            )
            raise ProposalRefused(f"The proposed change to {item.tool_id} makes no changes to its current definition.")
        change_rows += [row.text for row in rows]
    return change_rows


def _agreement_tool_removal_rows(
    instance_root: Path,
    catalog: Any,
    proposal: AccessProposalV1,
    *,
    all_links: list[Any],
) -> list[str]:
    removal_rows: list[str] = []
    for index, removal in enumerate(proposal.tool_removals):
        tool_file = load_removable_tool_file(instance_root, catalog, removal.tool_id)
        if removal.expected_sha256 != tool_file.sha256:
            raise ProposalRefused(
                f"The catalog file for {removal.tool_id} changed after the proposal was prepared; propose it again."
            )
        try:
            rows = tool_removal_rows(
                index,
                tool_file.definition,
                active_grants=(),
                active_blocks=(),
                active_links=[link for link in all_links if link.tool_id == removal.tool_id],
            )
        except ValueError as exc:
            logger.warning(
                "Refused a proposed tool removal whose impact could not fit without truncation. "
                "event=proposal_tool_removal_refused reason=review_row_too_long tool_id=%s error=%s",
                removal.tool_id,
                exc,
            )
            raise ProposalRefused(
                f"The proposed removal of {removal.tool_id} cannot be shown without hiding part of its impact: {exc}"
            ) from exc
        removal_rows += [row.text for row in rows]
    return removal_rows


def _prepare_agreement_proposal(
    agent: Any, instance_root: Path, proposal: AccessProposalV1
) -> tuple[AccessProposalV1, list[str], list[str], AccessRepository | None]:
    """Validate proposed tool additions, replacements, and removals before agreement or apply."""

    catalog = load_catalog(instance_root)
    proposal = with_tool_removal_baselines(proposal, instance_root=instance_root, catalog=catalog)
    proposal = with_tool_change_baselines(proposal, instance_root=instance_root, catalog=catalog)
    _validate_agreement_catalog(catalog, proposal)
    needs_store = bool(proposal.tool_changes or proposal.tool_removals or proposal.data_folders)
    store = _optional_access_store(agent) if needs_store else None
    team_id = str(getattr(agent, "team_id", "") or "")
    all_links = store.list_links(team_id, purpose=None) if store is not None and team_id else []
    now = datetime.now(timezone.utc)
    change_rows = _agreement_tool_change_rows(
        instance_root, catalog, proposal, all_links=all_links, store=store, team_id=team_id, now=now
    )
    removal_rows = _agreement_tool_removal_rows(instance_root, catalog, proposal, all_links=all_links)
    return proposal, change_rows, removal_rows, store


def _prepare_agreement_mcp_servers(store: Any, proposal: AccessProposalV1) -> list[Any]:
    """Validate every proposed MCP server record before activating any of them."""

    from mcp_control.registry import MCPConnectionLifecycle

    records: list[Any] = []
    seen: set[str] = set()
    for item in proposal.mcp_servers:
        if item.connection_id in seen:
            raise ProposalRefused(
                f"The MCP server {item.connection_id} is listed more than once in the same proposal."
            )
        seen.add(item.connection_id)
        record = store.require(item.connection_id)
        if record.lifecycle is not MCPConnectionLifecycle.PENDING or record.record_digest != item.record_digest:
            raise ProposalRefused(
                f"The MCP server {record.descriptor.connection_name} is not the pending server you proposed; "
                "propose it again."
            )
        records.append(record)
    return records


def _install_by_agreement(
    agent: Any,
    proposal: AccessProposalV1,
    *,
    approval: ChangeApprovalRecordV1,
    mcp_originals: list[Any],
) -> list[Any]:
    """Activate the proposed MCP servers with the person's agreement, recording each original before activating."""

    from mcp_control.registry import MCPConnectionLifecycle

    if not proposal.mcp_servers:
        return []
    store = _mcp_store(agent)
    pending_records = _prepare_agreement_mcp_servers(store, proposal)
    installed: list[Any] = []
    for record in pending_records:
        mcp_originals.append(record)
        installed.append(
            store.update_lifecycle(record.connection_id, lifecycle=MCPConnectionLifecycle.ACTIVE, approval=approval)
        )
    return installed


def _restore_agreement_server(agent: Any, record: Any, approval: ChangeApprovalRecordV1) -> None:
    """Restore one pending MCP server record activated before an agreement apply failed."""

    from mcp_control.registry import MCPConnectionLifecycle

    store = _mcp_store(agent)
    current = store.require(record.connection_id)
    if current.lifecycle is MCPConnectionLifecycle.PENDING and current.record_digest == record.record_digest:
        return
    ignored = {"record_digest", "updated_at_utc", "lifecycle", "approval"}
    if (
        current.lifecycle is not MCPConnectionLifecycle.ACTIVE
        or current.approval != approval
        or current.model_dump(exclude=ignored) != record.model_dump(exclude=ignored)
    ):
        logger.warning(
            "An MCP record changed during agreement rollback; its replacement is kept. "
            "event=proposal_agreement_mcp_changed connection_id=%s",
            record.connection_id,
        )
        return
    store.save(record)


def _prepare_agreement_folders(
    proposal: AccessProposalV1,
    store: AccessRepository | None,
    team_id: str,
    protected_paths: tuple[Path, ...],
    *,
    now: datetime,
) -> list[DataFolderRecordV1]:
    """Pre-validate all proposed data folders (including intra-proposal overlaps) before writing anything."""

    if not proposal.data_folders:
        return []
    if store is None:
        raise ProposalRefused("Data folders cannot be registered without an access store.")
    registry = DataFolderRegistry(store, team_id)
    owner = PersonPrincipal(AGREEMENT_APPROVER, team_id, ("owner",))
    existing = list(registry.records())
    seen_names = {record.name for record in existing}
    records: list[DataFolderRecordV1] = []
    for folder in proposal.data_folders:
        name = str(folder.name).strip()
        if name in seen_names:
            raise DataFolderRefused(f"A data folder named {name!r} is already registered.")
        problem = registration_problem(name, folder.path, protected=protected_paths, existing=existing)
        if problem:
            raise DataFolderRefused(problem)
        record = registry.new_record(
            owner,
            name=name,
            path=folder.path,
            writable=folder.writable,
            reason=proposal.reason,
            now=now,
        )
        records.append(record)
        existing.append(record)
        seen_names.add(name)
    return records


def _apply_agreement_db_changes(
    store: AccessRepository | None,
    team_id: str,
    folder_records: list[DataFolderRecordV1],
    proposal: AccessProposalV1,
    *,
    now: datetime,
) -> None:
    """Register data folders and clean up removed-tool DB rows in one atomic transaction."""

    if store is None or not team_id:
        return
    removed_ids = tuple(item.tool_id for item in proposal.tool_removals)
    if not folder_records and not removed_ids:
        return
    try:
        store.apply_together(
            team_id,
            data_folders=folder_records,
            revoke_tool_ids=removed_ids,
            actor=SubjectRefV1(kind=SubjectKind.USER, id=AGREEMENT_APPROVER),
            at=now,
        )
    except AccessStoreConflict as exc:
        raise DataFolderRefused(str(exc)) from exc


def _discard_agreement_staging(tools_dir: Path, removed_tools: list[RemovedFile]) -> None:
    for removed_file in removed_tools:
        try:
            removed_file.discard_staging(tools_dir)
        except Exception as exc:  # noqa: BLE001 - DB already committed; log and continue
            logger.warning(
                "Could not unlink a removal staging file after agreement apply. "
                "event=proposal_agreement_staging_cleanup_failed path=%s error=%s",
                "/".join(removed_file.staging_parts),
                exc,
            )


def _rollback_agreement_changes(
    agent: Any,
    tools_dir: Path,
    *,
    approval: ChangeApprovalRecordV1,
    written_tools: list[Path],
    replaced_tools: list[ReplacedFile],
    removed_tools: list[RemovedFile],
    programs: list[tuple[Path, OwnedFile]],
    mcp_originals: list[Any],
) -> None:
    """Undo every authorization-off file/MCP change, isolating each step so one error never aborts the rest."""

    steps: list[tuple[str, Callable[[], None]]] = [
        *((f"tool file {path.name}", lambda path=path: remove_instance_tool(path)) for path in reversed(written_tools)),
        *(
            (
                f"replaced tool file {'/'.join(item.parts)}",
                lambda item=item: item.restore(
                    tools_dir,
                    lambda previous, target=tools_dir.joinpath(*item.parts): restore_instance_tool(target, previous),
                ),
            )
            for item in reversed(replaced_tools)
        ),
        *(
            (
                f"removed tool file {'/'.join(item.parts)}",
                lambda item=item: item.restore(
                    tools_dir,
                    lambda item=item: restore_removed_instance_tool(
                        tools_dir.joinpath(*item.parts),
                        tools_dir.joinpath(*item.staging_parts),
                        expected_device=item.device,
                        expected_inode=item.inode,
                    ),
                ),
            )
            for item in reversed(removed_tools)
        ),
        *(
            (f"program {'/'.join(owned.parts)}", lambda root=root, owned=owned: owned.remove(root))
            for root, owned in reversed(programs)
        ),
        *(
            (
                f"MCP server {record.connection_id}",
                lambda saved=record: _restore_agreement_server(agent, saved, approval),
            )
            for record in reversed(mcp_originals)
        ),
    ]
    for what, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - isolate every rollback step
            logger.warning(
                "A rollback step failed while undoing an authorization-off proposal. "
                "event=proposal_agreement_rollback_step_failed part=%s error_type=%s error=%s",
                what,
                type(exc).__name__,
                exc,
            )


def _when_tools_appear(agent: Any, installed: list[Any]) -> dict[str, str]:
    """For each installed MCP server, when its tools appear (loaded before the next model call)."""

    agent_type = str(getattr(agent, "named_agent_type", "") or "")
    return {
        record.descriptor.capability_id: (
            "Installed. Its tools appear in your tool list from your next step, once the server answers."
            if record.permits_agent_type(agent_type)
            else f"Installed for {', '.join(record.target_agent_types)}; those agents see its tools from their next step."
        )
        for record in installed
    }


def _apply_agreement_files(
    layout_root: Path,
    proposal: AccessProposalV1,
    *,
    programs: list[tuple[Path, OwnedFile]],
    written_tools: list[Path],
    replaced_tools: list[ReplacedFile],
    removed_tools: list[RemovedFile],
) -> None:
    def prepare_program(root: Path, parts: tuple[str, ...], descriptor: int, data: bytes) -> None:
        programs.append((root, OwnedFile.prepared(parts, descriptor, data)))

    def capture_previous(
        path: Path, descriptor: int, body: bytes, temporary: Path, previous_bytes: bytes
    ) -> None:
        replaced_tools.append(
            ReplacedFile.prepared(
                (path.name,), descriptor, body, previous_bytes, staging_parts=(temporary.name,)
            )
        )

    def capture_removed(
        path: Path, device: int, inode: int, previous_bytes: bytes, staging_path: Path
    ) -> None:
        removed_tools.append(
            RemovedFile.prepared(
                (path.name,), device, inode, previous_bytes, staging_parts=(staging_path.name,)
            )
        )

    install_programs(layout_root, proposal.clis, fetch_https, before_write=prepare_program)
    for removed in proposal.tool_removals:
        assert removed.expected_sha256 is not None
        remove_proposed_tool_file(
            layout_root,
            removed.tool_id,
            expected_sha256=removed.expected_sha256,
            before_remove=capture_removed,
            keep_staging=True,
        )
    for proposed_http in proposal.tools:
        written_tools.append(write_instance_tool(layout_root, proposed_http))
    for proposed_cli in proposal.clis:
        written_tools.append(write_instance_tool(layout_root, proposed_cli))
    for changed in proposal.tool_changes:
        assert changed.previous_definition_digest is not None
        replace_instance_tool(
            layout_root,
            changed,
            expected_previous_sha256=changed.previous_definition_digest,
            before_replace=capture_previous,
        )


def apply_by_agreement(
    agent: Any,
    proposal: AccessProposalV1,
    confirmed: bool,
) -> dict[str, Any]:
    """Single-person mode (D8): add, change, or remove tools, MCP servers and data folders after the person's agreement.

    Tool changes and removals apply on ``confirmed=true`` like new tools: the
    ``confirmed=false`` preview lists trusted code's diff or removal impact rows
    for the person, and the file write/stage-remove still refuses if the file
    changed between reading its baseline and applying.
    """

    from custom_tools.workspace_scope import _protected, runtime_layout

    layout = runtime_layout(agent)
    proposal, change_rows, removal_rows, store = _prepare_agreement_proposal(agent, layout.root, proposal)
    rows = _agreement_rows(agent, proposal, change_rows, removal_rows)
    if not confirmed:
        return {
            "status": "agreement_needed",
            "changes": rows,
            "message": "Authorization is off. Explain these changes to the person, get their agreement in the "
            "conversation, and call again with confirmed=true. Grants and restrictions are not needed.",
        }
    now = datetime.now(timezone.utc)
    approval = ChangeApprovalRecordV1(
        mode=ChangeApprovalMode.AGREEMENT, approved_by=AGREEMENT_APPROVER, approved_at_utc=now
    )
    team_id = str(getattr(agent, "team_id", "") or "")
    folder_records = _prepare_agreement_folders(proposal, store, team_id, tuple(_protected(agent)), now=now)
    programs: list[tuple[Path, OwnedFile]] = []
    written_tools: list[Path] = []
    replaced_tools: list[ReplacedFile] = []
    removed_tools: list[RemovedFile] = []
    mcp_originals: list[Any] = []
    installed: list[Any] = []
    try:
        installed = _install_by_agreement(agent, proposal, approval=approval, mcp_originals=mcp_originals)
        _apply_agreement_files(
            layout.root,
            proposal,
            programs=programs,
            written_tools=written_tools,
            replaced_tools=replaced_tools,
            removed_tools=removed_tools,
        )
        _apply_agreement_db_changes(store, team_id, folder_records, proposal, now=now)
    except BaseException:
        _rollback_agreement_changes(
            agent,
            layout.instance_tools_dir,
            approval=approval,
            written_tools=written_tools,
            replaced_tools=replaced_tools,
            removed_tools=removed_tools,
            programs=programs,
            mcp_originals=mcp_originals,
        )
        logger.warning(
            "Applying an access proposal by agreement failed; changes were rolled back. "
            "event=proposal_agreement_apply_failed tools=%s tool_changes=%s tool_removals=%s mcp_servers=%s",
            ",".join([*(t.tool_id for t in proposal.tools), *(c.tool_id for c in proposal.clis)]),
            ",".join(changed.tool_id for changed in proposal.tool_changes),
            ",".join(item.tool_id for item in proposal.tool_removals),
            ",".join(record.connection_id for record in mcp_originals),
        )
        raise
    _discard_agreement_staging(layout.instance_tools_dir, removed_tools)
    logger.warning(
        "An access proposal was applied on the person's agreement (authorization off). "
        "event=proposal_applied_by_agreement tools=%s tool_changes=%s tool_removals=%s mcp_servers=%s folders=%s",
        ",".join([*(t.tool_id for t in proposal.tools), *(c.tool_id for c in proposal.clis)]),
        ",".join(changed.tool_id for changed in proposal.tool_changes),
        ",".join(item.tool_id for item in proposal.tool_removals),
        ",".join(record.connection_id for record in installed),
        ",".join(folder.name for folder in proposal.data_folders),
    )
    applied: dict[str, Any] = {"status": "applied", "changes": rows, "message": "Applied with the person's agreement."}
    if installed:
        applied["mcp_servers"] = _when_tools_appear(agent, installed)
    return applied


def _existing_request(agent: Any, tool_context: ToolContext) -> AccessRequestV1 | None:
    """The request this tool call already made (a resumed call finds it by its tool-use id)."""

    from custom_agents.access_runtime import agent_principal, runtime_access, runtime_requests, task_person

    tool_use_id = str(tool_context.tool_use.get("toolUseId") or "") or None
    if not tool_use_id:
        return None
    requests = runtime_requests(agent)
    _person, task_id = task_person(agent, runtime_access(agent))
    return requests.service.store.request_for_tool_use(
        team_id=requests.service.team_id,
        agent_id=agent_principal(agent).agent_id,
        task_id=task_id,
        tool_use_id=tool_use_id,
    )


def _submit(agent: Any, proposal: AccessProposalV1, tool_context: ToolContext) -> AccessRequestV1:
    from custom_agents.access_runtime import runtime_access, runtime_requests, task_access
    from custom_tools.workspace_scope import _protected, runtime_layout

    access = runtime_access(agent)
    requests = runtime_requests(agent)
    principal, person, task_id = task_access(agent, access)
    service = requests.service
    layout = runtime_layout(agent)
    catalog = access.catalog
    mcp_records = _mcp_store(agent).get if proposal.mcp_servers else (lambda _connection_id: None)
    # Default rows for MCP servers and pinned baselines for tool removals and
    # tool changes are added before the proposal is compiled and stored, so the
    # card and the approved digest cover them exactly (access_control.proposals).
    proposal = with_mcp_defaults(proposal, mcp_records, lead_agent_type=TEAM_ARCHITECT_AGENT_TYPE)
    proposal = with_tool_removal_baselines(proposal, instance_root=layout.root, catalog=catalog)
    proposal = with_tool_change_baselines(proposal, instance_root=layout.root, catalog=catalog)
    compiled = ProposalCompiler(
        catalog=catalog,
        team_id=service.team_id,
        holds=lambda someone, target, level: service.holds(someone, target, level, task_id=None),
        account_sharing=TeamAccountSharing(service.store, service.team_id),
        data_folders=DataFolderRegistry(service.store, service.team_id),
        protected=_protected(agent),
        grant_exists=service.store.get,
        mcp_records=mcp_records,
        lead_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
        instance_root=layout.root,
        links_for_tool=lambda tool_id: [
            link for link in service.store.list_links(service.team_id, purpose=None) if link.tool_id == tool_id
        ],
        blocks_for_tool=lambda tool_id: active_operation_blocks_for_tool(
            service.store, service.team_id, tool_id, now=service.clock()
        ),
        grants_for_tool=lambda tool_id: active_grants_for_tool_in_store(
            service.store, service.team_id, tool_id, now=service.clock()
        ),
    ).compile(proposal, person=person, task_id=principal.work_id)
    change_id = new_id("proposal")
    service.store.save_plan(
        service.team_id,
        change_id,
        ACCESS_PROPOSAL_KIND,
        compiled.digest,
        compiled.proposal.model_dump(mode="json"),
        at=service.clock(),
    )
    effect = tuple(row.text[:1000] for row in compiled.rows[:MAX_EFFECT_LINES])
    change = ChangeProposalV1(
        kind=ACCESS_PROPOSAL_KIND,
        change_id=change_id,
        plan_digest=compiled.digest,
        title=f"access proposal: {proposal.title}",
        summary=proposal_summary(compiled.rows),
        effect=effect,
        items=compiled.rows,
    )
    _task, meta = active_task_context(agent)
    outcome = requests.submit_change(
        requester=principal,
        person=person,
        proposal=change,
        reason=proposal.reason,
        task_id=task_id,
        conversation_id=getattr(meta, "conversation_id", None),
        tool_use_id=str(tool_context.tool_use.get("toolUseId") or "") or None,
    )
    if outcome.request is None:
        raise HandDownRefused("Proposals are always reviewed by an administrator; nothing was applied.")
    return outcome.request


def submit_and_wait(agent: Any, build: Callable[[], AccessProposalV1], tool_context: ToolContext) -> dict[str, Any]:
    """Submit the proposal ``build`` returns (once per tool call) and pause until an administrator decides.

    A resumed call finds its request by its tool-use id and does not build
    the proposal again (``build`` may record things, such as a new MCP
    connection).
    """

    from custom_agents.access_runtime import runtime_requests

    request = _existing_request(agent, tool_context) or _submit(agent, build(), tool_context)
    if request.status is AccessRequestStatus.WAITING:
        tool_context.interrupt(
            ACCESS_REQUEST_INTERRUPT_NAME,
            reason={
                "kind": ACCESS_REQUEST_INTERRUPT_KIND,
                "access_request_id": request.request_id,
                "summary": f"Waiting for an administrator to review the proposal: {request.change.title if request.change else ''}.",
            },
        )
        request = runtime_requests(agent).service.store.get_request(request.request_id) or request
    service = runtime_requests(agent).service
    return _result(request, service.store, service.catalog)


def _propose(
    proposal: AccessProposalV1,
    tool_context: ToolContext,
    confirmed: bool,
) -> dict[str, Any]:
    from custom_agents.access_runtime import authorization_enabled

    agent = get_agent_ref()
    if agent is None:
        raise RuntimeError("No agent is running this tool.")
    if not authorization_enabled(getattr(agent, "authorization_config", None)):
        return apply_by_agreement(agent, proposal, confirmed)
    return submit_and_wait(agent, lambda: proposal, tool_context)


@tool(context=True)
async def propose_access_change(
    title: str,
    reason: str,
    tool_context: ToolContext,
    tools: list[dict[str, Any]] | None = None,
    tool_changes: list[dict[str, Any]] | None = None,
    tool_removals: list[dict[str, Any]] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    clis: list[dict[str, Any]] | None = None,
    grants: list[dict[str, Any]] | None = None,
    blocks: list[dict[str, Any]] | None = None,
    account_links: list[dict[str, Any]] | None = None,
    data_folders: list[dict[str, Any]] | None = None,
    revoke_grant_ids: list[str] | None = None,
    confirmed: bool = False,
) -> str:
    """Propose several access changes for one review by an administrator; your task waits.

    Use it when a service is not in the tool catalog (find_tools finds nothing,
    or a call is refused as an unlisted host), when an existing proposal-written
    web API tool needs new hosts, routes, credential injections, or a team gate
    credential, when a proposal-written web API or installed CLI tool is no
    longer needed, or when you need access nobody can hand on. Items:
    - tools: new web API tools, each {"tool_id": "http:<name>", "display_name",
      "description", "hosts": [exact host names], "routes": [{"name",
      "method", "path", "level"}], "uses_credential": true,
      "credential_injections": [{"placement": "header", "name": "X-API-Key",
      "field": "api_key", "template": "{value}"}], "gate_injections":
      [{"placement": "header"|"query", "name": "X-Gate-Secret", "field":
      "gate_secret", "template": "{value}"}], "setup": {"external_steps":
      [{"step_id", "title", "instructions", "url" (https), "verify", "who":
      "person"|"administrator"}], "prerequisites": [tool ids]}}. Without a
      route, GET needs read, POST/PUT/PATCH write, DELETE full. gate_injections
      is optional for a team infrastructure gate (such as a Cloudflare rule)
      sent on every request to the tool's hosts alongside any account
      credential; it works with or without uses_credential. setup is optional:
      steps done outside Universal Agents before linking credentials (such as
      creating a token), in plain words, no secrets; set_up_tools shows them
      and waits until they are marked done.
    - tool_changes: full replacement definitions for existing proposal-written
      web API tools (control/access/tools/proposed-*.yaml), with the same
      fields as items in `tools`. Built-in tools and operator hand-written
      files cannot be changed here. Trusted code compares the current file
      against your replacement, lists every difference on the review card, and
      names any existing account or gate links that will have to be linked
      again with /link.
    - tool_removals: proposal-written web API (`http:*`) or installed CLI
      (`cli:*`) tools to remove from the catalog, each {"tool_id": "http:<name>"
      | "cli:<name>"}. Built-in tools, operator hand-written catalog files, and
      MCP servers (`mcp:*`, managed through `mcp_connections`) cannot be
      removed here. Trusted code lists the tool and every active grant, active
      block, and active account or gate link that approval will revoke. For
      `cli:*` tools, workspace files under `team/tools/<name>/` are not
      deleted.
    - clis: command-line tools, each {"tool_id": "cli:<name>", "display_name",
      "description", "installed_cli": {"binary_path": "team/tools/<name>/<file>",
      "sha256", "version", "source_url" (https, optional), "archive_member"
      (optional), "commands": {"repo.list": "read", "issue.create": "write",
      "repo.delete": "full"}, "blocked_commands": {"path": "why"},
      "blocked_flags": [...], "allowed_hosts": [...]}, "credential_injections":
      [{"placement": "env", "name": "TEA_TOKEN", "field": "token"} or
      {"placement": "file", "name": ".config/tea/token", "field": "token",
      "template": "{value}"}], "link_owners": ["person"] | ["person", "team"] |
      ["team"], "setup": {...}}. Put the program in team/tools/<name>/ first
      (or give its https download) and use its SHA-256; unlisted commands need
      full. Classify high-risk commands (deleting, trading, paying) as full.
      The broker runs a checked copy in the sandbox; agents never see the
      credential.
    - mcp_servers: pending MCP connections to install, each {"connection_id",
      "record_digest"} from mcp_connections(action="list") (its action
      "propose" does this for a new server in one step). Without grant rows for a server,
      its proposed agent types and you get full access to all its tools
      (mcp:<name>), and you may hand it on to workers.
    - grants: {"agent_type", "target": {"kind": "tool"|"operation"|"tool_set"|
      "folder", "id"}, "level": "read"|"write"|"full", "lifetime":
      "until_revoked"|"task", "delegation_depth": 0|1, "reason"}. They serve
      the work of the person your task works for.
    - blocks: {"agent_type", "target", "reason"} to restrict a worker.
    - account_links: {"tool_id", "owner": "team"|"person", "purpose" (your
      reason in plain words), "link_purpose": "account"|"gate"}: after approval
      the approver chooses the credential in a form, and your task waits until
      the forms end; the result says which links exist. For link_purpose "gate",
      owner must be "team".
    - data_folders: {"name", "path" (absolute host path), "writable"}.
    - revoke_grant_ids: grants to remove.
    Say plainly why in `reason`; the card shows trusted code's own summary of
    each item. With authorization off, get the person's agreement in the
    conversation and call with confirmed=true (confirmed=false returns the
    summary to explain, including the diff rows for `tool_changes` and impact
    rows for `tool_removals`).
    """

    from custom_agents.access_runtime import AuthorizationOff

    try:
        proposal = _proposal(
            title,
            reason,
            tools,
            mcp_servers,
            grants,
            blocks,
            account_links,
            data_folders,
            revoke_grant_ids,
            clis,
            tool_changes,
            tool_removals,
        )
        outcome = await asyncio.to_thread(_propose, proposal, tool_context, confirmed)
    except InterruptException:
        raise
    except ValidationError as exc:
        return _json({"status": "refused", "reason": f"The proposal is not valid: {exc.errors(include_url=False)}"})
    except (AuthorizationOff, *PROPOSAL_REFUSALS) as exc:
        return _json({"status": "refused", "reason": str(exc)})
    return _json(outcome)


@tool
def get_proposal_status(proposal_id: str) -> str:
    """The state of a proposal you made (the proposal_id propose_access_change returned)."""

    from custom_agents.access_runtime import AuthorizationOff, runtime_requests

    agent = get_agent_ref()
    try:
        requests = runtime_requests(agent)
    except AuthorizationOff as exc:
        return _json({"status": "not_needed", "reason": str(exc)})
    for request in requests.service.store.list_requests(requests.service.team_id):
        if request.change is not None and request.change.change_id == str(proposal_id).strip():
            return _json(_result(request, requests.service.store, requests.service.catalog))
    return _json({"status": "unknown", "reason": f"There is no proposal {proposal_id}."})


PROVIDER = ToolProvider("access_proposals", (propose_access_change, get_proposal_status))

__all__ = [
    "PROPOSAL_REFUSALS",
    "PROVIDER",
    "apply_by_agreement",
    "get_proposal_status",
    "propose_access_change",
    "submit_and_wait",
]
