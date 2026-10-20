"""The ``access`` section: who may use what, and the records that go with it.

What travels: lasting grants, blocks, shares and credential-use permissions
(active, not tied to one task, not expired), the team's data folder
registrations, account links (which stored credential a tool uses for the team
or a person), the team's sharing choices (whether its account for a service,
and its model keys, serve everyone: ``access_control.account_sharing``), each
person's settings (team model keys, their own model, shared folders) and
starter-profile choices.

Version 3 renamed operations that blocks may name. Releases exported
version 3 before the action tools were merged, so version 3 files may still
contain old operation names. The import uses one pure conversion
(``_rename_operations``) during both review and application. Operation names
are catalog values, so the record schema stays at version 3. A block naming an
operation this version does not have at all is imported for attention: it
blocks the whole tool here (``access_control.stale_blocks``) until an
administrator replaces it.

What stays behind: task and one-time grants (they end with their task),
including an agent's restrictions, which are bound to their request,
revoked, denied and pending grants, grants that carry the approval id of a
request that was not granted (an older version could leave one when it
stopped between a decision's grants and its outcome; logged, see
``AccessRepository.grants_without_granted_outcome``), access requests and the
claims of decisions running on them (``AccessRepository.claim_decision``,
transient), decision recovery records (export waits for the gateway to
reconcile them), change plans, notices and the audit log (history), and the
gateway's copy of the people (rebuilt at gateway start). Standard role access is not stored as
grants; it comes from the release (``access_control.standard_grants``).

One account link per tool and owner is active (a unique index in the access
store). A file from a store that held two (two link forms finished at once
before the index existed) has its older link refused with the newer one
named; the newer one is the one that was in use.

Each record travels as its own versioned contract (``GrantV1``,
``DataFolderRecordV1``, ``AccountLinkV1``, ``PersonSettingsV1``) with its id,
times and provenance, validated by this version's model and written by the
access store's own methods, so the audit trail keeps the original grantor.

The import refuses a record that would name something the new instance will
not have: a tool, tool set or agent type this version and the imported
sections do not define, a person who is not imported, a data folder that is
not, or a parent grant that is refused. A data folder whose path is not an
existing folder on the new host (or overlaps protected locations) is refused
with the command to register it again. Profile choices travel so the
configured starter profile does not revoke imported grants at the first start.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, ClassVar

import yaml
from pydantic import BaseModel, Field, JsonValue

from custom_messages.contracts.access_v2 import MODEL_KEYS_SHARING_SUBJECT
from secure_tools.chat_commands import ACCOUNTS_SHARE, accounts_command
from team_transfer.format import TransferModel
from team_transfer.errors import TransferError
from team_transfer.sections import (
    ExportSource,
    ImportContext,
    PlannedRecord,
    RecordOutcome,
    SectionExport,
    SectionFiles,
    SectionRecords,
)

logger = logging.getLogger(__name__)

_USERS_FOLDER = "users/"
_DATA_FOLDER = "data/"
_SHARED_FOLDERS = ("team", "agents")


class AccessKind(StrEnum):
    GRANT = "grant"
    DATA_FOLDER = "data_folder"
    ACCOUNT_LINK = "account_link"
    PERSON_SETTINGS = "person_settings"
    PROFILE_CHOICE = "profile_choice"
    ACCOUNT_SHARING = "account_sharing"


# Operations this version renamed, by their old target id (``tool#operation``).
# Both supported transfer formats may name them; review and application use
# the same conversion. Runtime tool calls never accept these old names.
RENAMED_OPERATIONS: dict[str, str] = {
    # find_tools left fn:access for fn:tool_loading (tools loaded on request).
    "fn:access#find_tools": "fn:tool_loading#find_tools",
    # The memory functions became memory(action=...) and topic_tags(action=...)
    # (custom_tools.providers.memory). Both banks' functions became one action,
    # so a block on one bank's now stops the action for both: never less.
    "fn:memory#list_coremem": "fn:memory#memory.list",
    "fn:memory#list_usermem": "fn:memory#memory.list",
    "fn:memory#create_coremem": "fn:memory#memory.create",
    "fn:memory#create_usermem": "fn:memory#memory.create",
    "fn:memory#replace_coremem": "fn:memory#memory.replace",
    "fn:memory#replace_usermem": "fn:memory#memory.replace",
    "fn:memory#delete_coremem": "fn:memory#memory.delete",
    "fn:memory#delete_usermem": "fn:memory#memory.delete",
    "fn:memory#undo_memory_action": "fn:memory#memory.undo",
    "fn:memory#list_topic_tags": "fn:memory#topic_tags.list",
    "fn:memory#tag_thread": "fn:memory#topic_tags.tag_thread",
    "fn:memory#tag_turn": "fn:memory#topic_tags.tag_turn",
    "fn:memory#untag_thread": "fn:memory#topic_tags.untag",
    "fn:memory#find_topic_threads": "fn:memory#topic_tags.find",
    # The capability functions became four action tools
    # (custom_tools.providers.capabilities). plan_hosted_mcp_server_change
    # became two actions (plan_install, plan_remove), so a block on it has no
    # one new name: it stays as it was and blocks the whole tool, for
    # attention (access_control.stale_blocks).
    "fn:capabilities#list_mcp_connections": "fn:capabilities#mcp_connections.list",
    "fn:capabilities#propose_mcp_connection": "fn:capabilities#mcp_connections.propose",
    "fn:capabilities#list_hosted_mcp_servers": "fn:capabilities#hosted_mcp_servers.list",
    "fn:capabilities#propose_hosted_mcp_server": "fn:capabilities#hosted_mcp_servers.propose",
    "fn:capabilities#manage_hosted_mcp_server.start": "fn:capabilities#hosted_mcp_servers.start",
    "fn:capabilities#manage_hosted_mcp_server.stop": "fn:capabilities#hosted_mcp_servers.stop",
    "fn:capabilities#list_installed_permissions": "fn:capabilities#installed_permissions.list",
    "fn:capabilities#propose_installed_permission": "fn:capabilities#installed_permissions.propose",
    "fn:capabilities#plan_generated_agent_definition": "fn:capabilities#specialist_definitions.plan",
    "fn:capabilities#get_generated_agent_provisioning_status": "fn:capabilities#specialist_definitions.status",
    "fn:capabilities#rollback_generated_agent_definition": "fn:capabilities#specialist_definitions.plan_removal",
    # Thirteen Objective functions became objectives, objective_work_sessions
    # and objective_work_profiles (custom_tools.providers.objectives).
    "fn:objectives#get_objective": "fn:objectives#objectives.get",
    "fn:objectives#list_objectives": "fn:objectives#objectives.list",
    "fn:objectives#update_objective_progress": "fn:objectives#objectives.update_progress",
    "fn:objectives#update_objective_status": "fn:objectives#objectives.update_status",
    "fn:objectives#suggest_objective_work_session": "fn:objectives#objective_work_sessions.suggest",
    "fn:objectives#enable_objective_work_sessions": "fn:objectives#objective_work_sessions.enable",
    "fn:objectives#disable_objective_work_sessions": "fn:objectives#objective_work_sessions.disable",
    "fn:objectives#get_objective_work_status": "fn:objectives#objective_work_sessions.status",
    "fn:objectives#start_objective_work_session": "fn:objectives#objective_work_sessions.start",
    "fn:objectives#list_objective_work_profiles": "fn:objectives#objective_work_profiles.list",
    "fn:objectives#export_objective_work_profile_yaml": "fn:objectives#objective_work_profiles.export",
    "fn:objectives#apply_objective_work_profile_proposal": "fn:objectives#objective_work_profiles.apply",
    "fn:objectives#remove_installed_objective_work_profile": "fn:objectives#objective_work_profiles.remove",
}


class AccessRecordV1(TransferModel):
    kind: AccessKind
    record_id: str = Field(min_length=1, max_length=400)
    document: dict[str, JsonValue]


def _store(storage_settings: Any) -> Any:
    from storage.access_repository import AccessRepository

    return AccessRepository(storage_settings)


def _lasting(grant: Any, now: datetime) -> bool:
    from custom_messages.contracts.access_v2 import GrantLifetime, GrantStatus

    if grant.status is not GrantStatus.ACTIVE or grant.task_id is not None:
        return False
    if grant.lifetime is GrantLifetime.UNTIL_REVOKED:
        return True
    return (
        grant.lifetime is GrantLifetime.UNTIL
        and grant.expires_at_utc is not None
        and grant.expires_at_utc > now
    )


class AccessSection:
    name: ClassVar[str] = "access"
    # 2: the team's sharing choices (AccountSharingV1) travel.
    # 3: blocks name the operations as this version calls them (RENAMED_OPERATIONS).
    schema_version: ClassVar[int] = 3
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = AccessRecordV1

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        store = _store(source.config.manager_storage_settings)
        team = source.team_id
        if store.decision_recoveries(team):
            raise TransferError(
                "An access decision is awaiting confirmation. Start the gateway, let its Inbox reconcile the "
                "decision after the ten-minute claim period, then stop the runtime and export again."
            )
        now = datetime.now(timezone.utc)
        records: list[AccessRecordV1] = []
        for folder in store.data_folders(team):
            records.append(_record(AccessKind.DATA_FOLDER, folder.name, folder))
        undecided = {grant.grant_id: request for grant, request in store.grants_without_granted_outcome(team)}
        for grant in store.list_team(team):
            if grant.grant_id in undecided:
                _skip_without_granted_outcome(grant.grant_id, undecided[grant.grant_id])
                continue
            if _lasting(grant, now):
                records.append(_record(AccessKind.GRANT, grant.grant_id, grant))
        for link in store.list_links(team):
            records.append(_record(AccessKind.ACCOUNT_LINK, link.link_id, link))
        for settings in store.team_person_settings(team):
            records.append(
                _record(AccessKind.PERSON_SETTINGS, settings.person_id, settings)
            )
        for sharing in store.team_account_sharing(team):
            records.append(_record(AccessKind.ACCOUNT_SHARING, sharing.subject, sharing))
        for person_id, profile, source_kind, chosen_at in store.profile_choices(team):
            choice = {
                "team_id": team,
                "person_id": person_id,
                "profile": profile,
                "source": source_kind,
                "chosen_at_utc": chosen_at.isoformat(),
            }
            records.append(
                AccessRecordV1(
                    kind=AccessKind.PROFILE_CHOICE, record_id=person_id, document=choice
                )
            )
        return SectionExport(
            records=[record.model_dump(mode="json") for record in records]
        )

    # -- import ----------------------------------------------------------------

    def plan(
        self, records: SectionRecords, context: ImportContext
    ) -> list[PlannedRecord]:
        known = _Known.build(context)
        chosen = _sharing_in_file(records)
        newest = _newest_links(records)
        planned = []
        for raw in _dependency_order(_rename_operations(records)):
            record = AccessRecordV1.model_validate(raw)
            key = f"{record.kind.value}/{record.record_id}"
            try:
                problem = _older_link(record, newest) or _CHECKS[record.kind](record.document, known)
            except ValueError as exc:  # the model refuses the document
                problem = f"this version refuses it: {exc}"
            if problem:
                planned.append(
                    PlannedRecord(self.name, key, RecordOutcome.REFUSED, problem)
                )
                continue
            if record.kind is AccessKind.DATA_FOLDER:
                known.data_folders.add(record.record_id)
            if record.kind is AccessKind.GRANT:
                known.grants.add(record.record_id)
            attention = _unused_team_link(record, known, chosen, fresh=not context.existing) or _widened_block(
                record, known
            )
            if attention:
                planned.append(PlannedRecord(self.name, key, RecordOutcome.ATTENTION, attention))
                continue
            planned.append(PlannedRecord(self.name, key, RecordOutcome.IMPORT))
        return planned

    def apply(
        self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext
    ) -> None:
        from assistant_gateway.config import GatewayInstanceConfig

        accepted = {
            record.key for record in plan if record.outcome is not RecordOutcome.REFUSED
        }
        config = GatewayInstanceConfig.load_for_process(
            context.layout.instance_config_path
        )
        store = _store(config.manager_storage_settings)
        for raw in _dependency_order(_rename_operations(records)):
            record = AccessRecordV1.model_validate(raw)
            if f"{record.kind.value}/{record.record_id}" in accepted:
                _WRITERS[record.kind](store, record.document)
        logger.warning(
            "Team access imported. event=team_transfer_access_imported records=%s refused=%s",
            len(accepted),
            len(plan) - len(accepted),
        )


def _skip_without_granted_outcome(grant_id: str, request: Any) -> None:
    logger.warning(
        "A grant whose access request was not granted stays behind. event=team_transfer_grant_without_outcome "
        "grant_id=%s request_id=%s request_status=%s",
        grant_id,
        request.request_id,
        request.status.value,
    )


def _record(kind: AccessKind, record_id: str, model: Any) -> AccessRecordV1:
    return AccessRecordV1(
        kind=kind, record_id=record_id, document=model.model_dump(mode="json")
    )


def _dependency_order(records: SectionRecords) -> SectionRecords:
    """Parents precede children even after a replacement reassigns old children.

    Store creation times are not dependency order. Cycles and missing parents
    remain in the list so the planner explicitly refuses them.
    """
    folders = [r for r in records if r.get("kind") == AccessKind.DATA_FOLDER.value]
    grants = {
        r["record_id"]: r for r in records if r.get("kind") == AccessKind.GRANT.value
    }
    children: dict[str, list[str]] = {}
    waiting = set()
    for key, raw in grants.items():
        parent = raw["document"].get("parent_grant_id")
        if parent in grants:
            children.setdefault(parent, []).append(key)
            waiting.add(key)
    ready = deque(key for key in grants if key not in waiting)
    ordered = []
    seen = set()
    while ready:
        key = ready.popleft()
        seen.add(key)
        ordered.append(grants[key])
        ready.extend(children.get(key, ()))
    ordered.extend(raw for key, raw in grants.items() if key not in seen)
    other = [
        r
        for r in records
        if r.get("kind") not in {AccessKind.DATA_FOLDER.value, AccessKind.GRANT.value}
    ]
    return [*folders, *ordered, *other]


@dataclass
class _Known:
    """What the new instance will have, from this version and the sections planned before this one."""

    tools: set[str]
    # This version's own tools, to check sharing choices against.
    release_tools: dict[str, Any]
    # Complete destination definitions, including accepted team tools and MCP servers.
    tool_definitions: dict[str, Any]
    tool_sets: set[str]
    agent_types: set[str]
    people: set[str]
    protected: tuple[Any, ...]
    data_folders: set[str] = field(default_factory=set)
    grants: set[str] = field(default_factory=set)
    schedules: set[str] | None = None

    @classmethod
    def build(cls, context: ImportContext) -> "_Known":
        from access_catalog.catalog import load_catalog
        from access_control.data_folders import protected_locations
        from team_transfer.integrations import accepted_mcp_tool_definitions
        from team_transfer.team_tools import accepted_tool_definitions

        release = load_catalog(context.layout.root if context.existing else None)
        definitions = {
            **release.tools,
            **accepted_tool_definitions(context),
            **accepted_mcp_tool_definitions(context),
        }
        tools = set(release.tools)
        for key in context.accepted("team_tools"):
            tools.update(key.split(","))
        for key in context.accepted("mcp"):
            tools.update(_mcp_tool_ids(context, key))
        for key in context.accepted("procedures"):
            tools.update(_procedure_tool_ids(context, key))
        layout = context.layout
        return cls(
            tools=tools,
            release_tools=dict(release.tools),
            tool_definitions=definitions,
            tool_sets=set(release.tool_sets),
            agent_types=known_agent_types(context),
            people=context.accepted("people"),
            protected=protected_locations(
                layout.root, instance_config=layout.instance_config_path
            ),
            data_folders={
                key.split("/", 1)[1]
                for key in context.baseline_keys.get("access", ())
                if key.startswith("data_folder/")
            },
            grants={
                key.split("/", 1)[1]
                for key in context.baseline_keys.get("access", ())
                if key.startswith("grant/")
            },
            schedules={
                key.split("/", 1)[1]
                for key in context.accepted("schedules")
                if key.startswith("schedule/")
            }
            if "schedules" in context.scratch.get("planned_sections", set())
            else None,
        )

    def target_problem(self, kind: str, target_id: str) -> str:
        from custom_messages.contracts.access_v2 import TargetKind

        if kind == TargetKind.TOOL.value and target_id not in self.tools:
            return f"the tool {target_id} does not exist in the new instance"
        if kind == TargetKind.TOOL_SET.value and target_id not in self.tool_sets:
            return f"the tool set {target_id} does not exist in this version"
        if (
            kind == TargetKind.OPERATION.value
            and target_id.split("#", 1)[0] not in self.tools
        ):
            return f"the operation {target_id} belongs to a tool the new instance does not have"
        if kind in {TargetKind.FOLDER.value, TargetKind.FILE.value}:
            return self.folder_problem(target_id)
        return ""

    def folder_problem(self, folder_id: str) -> str:
        from runtime_settings.instance_layout import person_folder_name

        if folder_id.startswith(_USERS_FOLDER):
            owner_folder = folder_id[len(_USERS_FOLDER) :].split("/", 1)[0]
            if owner_folder not in {
                person_folder_name(person) for person in self.people
            }:
                return f"the folder {folder_id} belongs to a person who is not imported"
        elif folder_id.startswith(_DATA_FOLDER):
            name = folder_id[len(_DATA_FOLDER) :].split("/", 1)[0]
            if name not in self.data_folders:
                return f"the data folder {name} is not imported"
        elif folder_id.split("/", 1)[0] not in _SHARED_FOLDERS:
            return f"this version has no folder {folder_id}"
        return ""

    def subject_problem(self, kind: str, subject_id: str, team_id: str) -> str:
        from access_control.entities import agent_type_of
        from custom_messages.contracts.access_v2 import SubjectKind

        if kind == SubjectKind.USER.value and subject_id not in self.people:
            return f"it is for {subject_id}, who is not imported"
        if kind == SubjectKind.AGENT_TYPE.value:
            # Agent-type subjects are <team>/<type> (access_control.entities.agent_type_id);
            # comparing the whole id with type names refused every one.
            agent_type = agent_type_of(team_id, subject_id)
            if agent_type is None:
                return f"{subject_id} is not an agent type of team {team_id}"
            if agent_type not in self.agent_types:
                return f"the agent type {agent_type} does not exist in the new instance"
        if (
            kind == SubjectKind.SCHEDULE.value
            and self.schedules is not None
            and subject_id not in self.schedules
        ):
            return f"the schedule {subject_id} is not selected or present in the target"
        return ""


def _mcp_tool_ids(context: ImportContext, key: str) -> set[str]:
    from access_catalog.catalog import mcp_tool_id

    kind, _, record_id = key.partition("/")
    if kind != "connection":
        return set()
    for raw in [*context.baseline.get("mcp", ()), *context.records("mcp")]:
        if raw.get("record_id") == record_id:
            descriptor = raw["document"].get("descriptor", {})
            return {mcp_tool_id(str(descriptor.get("connection_name", "")))}
    return set()


def _procedure_tool_ids(context: ImportContext, key: str) -> set[str]:
    for raw in [
        *context.baseline.get("procedures", ()),
        *context.records("procedures"),
    ]:
        if raw.get("definition_id") == key:
            return {f"procedure:{raw['document'].get('handler_id', '')}"}
    return set()


_AGENT_UNIT_KINDS = frozenset({"agent_definitions", "agent_bundle", "specialist"})


def known_agent_types(context: ImportContext) -> set[str]:
    """Agent types the new instance will have: this version's and the imported control units' ones."""

    from custom_agents.agent_catalog import load_agent_catalog

    agent_types = set(load_agent_catalog(include_configured=False).agent_types)
    agent_types.update(_imported_agent_types(context))
    return agent_types


def _imported_agent_types(context: ImportContext) -> set[str]:
    """Agent types defined by the control units the import accepts."""

    accepted = context.accepted("control")
    found: set[str] = set()
    for baseline, records in (
        (True, context.baseline.get("control", ())),
        (False, context.records("control")),
    ):
        for raw in records:
            if raw.get("unit") in accepted and raw.get("kind") in _AGENT_UNIT_KINDS:
                for entry in raw.get("files", ()):
                    found.update(
                        _agent_types_in(
                            context, str(entry.get("path", "")), baseline=baseline
                        )
                    )
    return found


def _agent_types_in(context: ImportContext, path: str, *, baseline=False) -> set[str]:
    if not path.endswith((".yaml", ".yml")):
        return set()
    document = (
        yaml.safe_load(context.file("control", path, baseline=baseline).decode("utf-8"))
        or {}
    )
    types = document.get("agent_types") if isinstance(document, dict) else None
    return {str(name) for name in (types or {})}


# -- checks and writers, one per kind --------------------------------------------------


def _check_grant(document: dict[str, Any], known: _Known) -> str:
    from custom_messages.contracts.access_v2 import GrantV1

    grant = GrantV1.model_validate(document)
    if grant.parent_grant_id and grant.parent_grant_id not in known.grants:
        return f"its parent grant {grant.parent_grant_id} is not imported"
    if grant.for_person_id and grant.for_person_id not in known.people:
        return f"it serves {grant.for_person_id}, who is not imported"
    return known.subject_problem(
        grant.subject.kind.value, grant.subject.id, grant.team_id
    ) or known.target_problem(grant.target.kind.value, grant.target.id)


def _widened_block(record: AccessRecordV1, known: _Known) -> str:
    """A block naming an operation this version's tool does not have: it will block the whole tool."""

    from access_catalog.catalog import missing_operation
    from access_control.stale_blocks import subject_name
    from custom_messages.contracts.access_v2 import GrantKind, GrantV1

    if record.kind is not AccessKind.GRANT:
        return ""
    grant = GrantV1.model_validate(record.document)
    if grant.kind is not GrantKind.BLOCK:
        return ""
    operation = missing_operation(known.tool_definitions, grant.target)
    if operation is None:
        return ""
    tool_id = grant.target.id.split("#", 1)[0]
    return (
        f"this version has no operation {operation} in {tool_id}, so the block will stop "
        f"{subject_name(grant.subject)} from using all of {tool_id}; after the import, replace it with a block on "
        "one of the tool's operations (an Inbox notice lists them)"
    )


def _check_data_folder(document: dict[str, Any], known: _Known) -> str:
    from access_control.data_folders import registration_problem
    from custom_messages.contracts.access_v2 import DataFolderRecordV1

    folder = DataFolderRecordV1.model_validate(document)
    problem = registration_problem(folder.name, folder.path, protected=known.protected)
    if problem:
        return f"{problem} Register it again with `assistant_cli access folders add` once the folder is there."
    return ""


def _check_link(document: dict[str, Any], known: _Known) -> str:
    from custom_messages.contracts.access_v2 import AccountLinkV1, LinkOwnerKind

    link = AccountLinkV1.model_validate(document)
    if link.tool_id not in known.tools:
        return f"the tool {link.tool_id} does not exist in the new instance"
    if link.owner_kind is LinkOwnerKind.PERSON and link.owner_id not in known.people:
        return f"it belongs to {link.owner_id}, who is not imported"
    tool = known.release_tools.get(link.tool_id)
    if tool is not None and link.owner_kind.value not in tool.link_owners:
        # This version would never use it (the GitHub API takes only each person's own account).
        whose = "only each person's own account" if link.owner_kind is LinkOwnerKind.TEAM else "only the team's account"
        return f"this version uses {whose} for {tool.display_name}, so the link would never be used"
    return ""


def _link_owner_key(document: dict[str, Any]) -> tuple[str, str, str, str]:
    def text(name: str) -> str:
        return str(document.get(name) or "")

    return text("team_id"), text("tool_id"), text("owner_kind"), text("owner_id")


def _newest_links(records: SectionRecords) -> dict[tuple[str, str, str, str], str]:
    """The newest account link in the file for each tool and owner: the one in use (``AccessRepository.active_link``)."""

    newest: dict[tuple[str, str, str, str], tuple[datetime, str]] = {}
    for raw in records:
        document = raw.get("document")
        if raw.get("kind") != AccessKind.ACCOUNT_LINK.value or not isinstance(document, dict):
            continue
        try:
            created = datetime.fromisoformat(str(document.get("created_at_utc")))
        except ValueError:
            continue  # the model refuses it in the record's own check
        order = (created, str(raw.get("record_id") or ""))
        key = _link_owner_key(document)
        if key not in newest or order > newest[key]:
            newest[key] = order
    return {key: record_id for key, (_created, record_id) in newest.items()}


def _older_link(record: AccessRecordV1, newest: dict[tuple[str, str, str, str], str]) -> str:
    """Refuse an account link a newer one for the same tool and owner replaces (one active link each)."""

    if record.kind is not AccessKind.ACCOUNT_LINK:
        return ""
    kept = newest.get(_link_owner_key(record.document))
    if kept is None or kept == record.record_id:
        return ""
    return (
        f"the newer account link {kept} for the same tool and owner replaces it (one link per tool and owner "
        "is in use, and the newer one was)"
    )


def _sharing_in_file(records: SectionRecords) -> dict[str, str]:
    """The sharing the file chose per tool (``AccountSharingV1``), by tool id."""

    return {
        str(raw.get("record_id")): str((raw.get("document") or {}).get("sharing"))
        for raw in records
        if raw.get("kind") == AccessKind.ACCOUNT_SHARING.value
    }


def _unused_team_link(record: AccessRecordV1, known: _Known, chosen: dict[str, str], *, fresh: bool) -> str:
    """Why an imported team account link would not be used yet, or empty.

    A team link for a service that is private after the import (the file's
    choice, or this version's starting sharing, such as X's) is kept, as
    ``/accounts`` keeps one, so sharing the service again uses it; the review
    names the command. ``chosen`` holds this batch's sharing records. A later
    batch (``fresh`` False) without one for the tool cannot know what the
    destination chose since, so it says nothing rather than guess.
    """

    from access_control.account_sharing import default_sharing
    from custom_messages.contracts.access_v2 import AccountLinkV1, AccountSharing, LinkOwnerKind

    if record.kind is not AccessKind.ACCOUNT_LINK:
        return ""
    link = AccountLinkV1.model_validate(record.document)
    tool = known.release_tools.get(link.tool_id)
    if link.owner_kind is not LinkOwnerKind.TEAM or tool is None or not tool.sharing_choice:
        return ""
    if link.tool_id not in chosen and not fresh:
        return ""
    sharing = chosen.get(link.tool_id) or default_sharing(tool).value
    if sharing != AccountSharing.PRIVATE.value:
        return ""
    why = "as the team chose" if link.tool_id in chosen else "where this version starts it"
    return (
        f"imported, but not used while {tool.display_name} is private ({why}): agents use only each "
        f"person's own account. {accounts_command(ACCOUNTS_SHARE, link.tool_id)} after the import uses it."
    )


def _check_person_settings(document: dict[str, Any], known: _Known) -> str:
    from custom_messages.contracts.person_settings_v1 import PersonSettingsV1

    settings = PersonSettingsV1.model_validate(document)
    return (
        ""
        if settings.person_id in known.people
        else f"{settings.person_id} is not imported"
    )


def _check_account_sharing(document: dict[str, Any], known: _Known) -> str:
    from access_control.account_sharing import fixed_sharing
    from custom_messages.contracts.access_v2 import AccountSharingV1

    sharing = AccountSharingV1.model_validate(document)
    if sharing.subject == MODEL_KEYS_SHARING_SUBJECT:
        return ""
    if sharing.subject not in known.tools:
        return f"the tool {sharing.subject} does not exist in the new instance"
    tool = known.release_tools.get(sharing.subject)
    if tool is None:
        return ""
    fixed = fixed_sharing(tool)
    if fixed is not None and fixed is not sharing.sharing:
        return f"this version keeps {tool.display_name} always {fixed.value}"
    return ""


def _check_profile_choice(document: dict[str, Any], known: _Known) -> str:
    return (
        ""
        if document.get("person_id") in known.people
        else f"{document.get('person_id')} is not imported"
    )


def _write_grant(store: Any, document: dict[str, Any]) -> None:
    from custom_messages.contracts.access_v2 import GrantV1

    store.create(GrantV1.model_validate(document))


def _write_data_folder(store: Any, document: dict[str, Any]) -> None:
    from custom_messages.contracts.access_v2 import DataFolderRecordV1

    store.register_data_folder(DataFolderRecordV1.model_validate(document))


def _write_link(store: Any, document: dict[str, Any]) -> None:
    from custom_messages.contracts.access_v2 import AccountLinkV1

    store.create_link(AccountLinkV1.model_validate(document))


def _write_person_settings(store: Any, document: dict[str, Any]) -> None:
    from custom_messages.contracts.person_settings_v1 import PersonSettingsV1

    settings = PersonSettingsV1.model_validate(document)
    store.change_person_settings(
        settings.team_id, settings.person_id, lambda _current: settings
    )


def _write_account_sharing(store: Any, document: dict[str, Any]) -> None:
    from custom_messages.contracts.access_v2 import AccountSharingV1

    store.set_account_sharing(AccountSharingV1.model_validate(document))


def _write_profile_choice(store: Any, document: dict[str, Any]) -> None:
    store.record_profile_choice(
        str(document["team_id"]),
        str(document["person_id"]),
        str(document["profile"]),
        source=str(document["source"]),
        at=datetime.fromisoformat(str(document["chosen_at_utc"])),
    )


_CHECKS: dict[AccessKind, Callable[[dict[str, Any], _Known], str]] = {
    AccessKind.GRANT: _check_grant,
    AccessKind.DATA_FOLDER: _check_data_folder,
    AccessKind.ACCOUNT_LINK: _check_link,
    AccessKind.PERSON_SETTINGS: _check_person_settings,
    AccessKind.PROFILE_CHOICE: _check_profile_choice,
    AccessKind.ACCOUNT_SHARING: _check_account_sharing,
}
_WRITERS: dict[AccessKind, Callable[[Any, dict[str, Any]], None]] = {
    AccessKind.GRANT: _write_grant,
    AccessKind.DATA_FOLDER: _write_data_folder,
    AccessKind.ACCOUNT_LINK: _write_link,
    AccessKind.PERSON_SETTINGS: _write_person_settings,
    AccessKind.PROFILE_CHOICE: _write_profile_choice,
    AccessKind.ACCOUNT_SHARING: _write_account_sharing,
}

def _rename_operations(records: SectionRecords) -> SectionRecords:
    """Point known old operation names at their replacements without changing the source records.

    Idempotent: planning and application use it because version 3 exports may
    contain old names. Unknown operations keep the existing whole-tool block
    and attention note.
    """

    converted = []
    for raw in records:
        document = raw.get("document") or {}
        target = document.get("target") if isinstance(document, dict) else None
        renamed = (
            RENAMED_OPERATIONS.get(str(target.get("id") or ""))
            if isinstance(target, dict) and target.get("kind") == "operation" else None
        )
        if raw.get("kind") == AccessKind.GRANT.value and renamed:
            raw = {**raw, "document": {**document, "target": {**target, "id": renamed}}}
        converted.append(raw)
    return converted


__all__ = ["AccessKind", "AccessRecordV1", "AccessSection", "RENAMED_OPERATIONS", "known_agent_types"]
