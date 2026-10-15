"""Which agent-definition changes pause a scheduled worker, and what changed.

A running agent is built from two kinds of content:

- what its definition file declares: role, its own prompt text, its default
  tools (tool catalog ids or tool set ids) and the skills flag; and
- what the running code expands that into: the functions of its role's
  standard tool sets and of its default tools, the shared base prompt and
  backstory text.

A schedule's worker policy is saved against the target definition. Only a change
to a team-written declaration pauses the schedule (``AGENT_DEFINITION_CHANGED``);
the owner reviews the differences and TeamLead re-saves the policy. A definition
shipped with the code changes only with a deploy, which is the operator's
approval, so it is never compared, and code-only changes never pause a schedule.
The full launch fingerprint (``launch_definition_digest``) still guards a single
start against an edit made while that start is in progress.

This split follows the access model's rule for tool sets: a set covers tools
added to it later. Hashing the declared ids rather than the functions behind
them means a release that adds a tool to a set does not invalidate schedules or
their approvals. None of these fingerprints depends on grants: the tools a
definition expands to come from its role and default tools, and what an agent
may actually use is decided per call by the access service.
"""

from __future__ import annotations

import logging
from typing import Any

from custom_messages.contracts.schedule_worker_v1 import (
    DEFINITION_SOURCE_RELEASE,
    DEFINITION_SOURCE_TEAM,
    AgentDefinitionSource,
    ReleaseDefinitionChangeV1,
    ScheduleWorkerBindingV1,
    WorkerDefinitionChangesV1,
    WorkerDefinitionRecordV1,
    utc_now,
    worker_digest,
)
from custom_tools.provider_contract import tool_name

_logger = logging.getLogger(__name__)

DEFINITION_SOURCE_UNKNOWN = "agent_definition_source_unknown"
DEFINITION_DECLARATION_MISSING = "agent_definition_declaration_missing"
_DECLARED_FIELDS = (
    "role",
    "own_prompt",
    "include_shared_prompt",
    "backstory",
    "default_tools",
    "skills_enabled",
)
_RELEASE_DECLARED_FIELDS = ("role", "default_tools", "skills_enabled")


def launch_definition_digest(definition: dict[str, Any]) -> str:
    """Fingerprint of everything the worker will run with, as expanded by the code.

    ``tools`` are the definition's own functions (its role's standard tools and
    its declared default tools), never the ones grants add while it runs.
    """
    return worker_digest(
        {
            "type": definition.get("named_agent_type"),
            "goal": definition.get("goal"),
            "role": definition.get("role"),
            "backstory": definition.get("backstory"),
            "tools": sorted(tool_name(item) for item in definition.get("tools", ())),
            "default_tools": sorted(definition.get("default_tools", ())),
            "skills_enabled": definition.get("skills_enabled"),
        }
    )


def definition_source_of(definition: dict[str, Any]) -> AgentDefinitionSource:
    source = definition.get("agent_definition_source")
    if source == DEFINITION_SOURCE_RELEASE:
        return DEFINITION_SOURCE_RELEASE
    if source == DEFINITION_SOURCE_TEAM:
        return DEFINITION_SOURCE_TEAM
    # Definitions come from the catalog loader, which always labels them.
    raise ValueError(DEFINITION_SOURCE_UNKNOWN)


def declared_definition(definition: dict[str, Any]) -> dict[str, Any]:
    declared = definition.get("declared_definition")
    if not isinstance(declared, dict) or any(
        field not in declared for field in _DECLARED_FIELDS
    ):
        raise ValueError(DEFINITION_DECLARATION_MISSING)
    return {field: declared[field] for field in _DECLARED_FIELDS}


def authored_definition_digest(definition: dict[str, Any]) -> str:
    """Fingerprint of the declaration only, saved with new worker policies."""
    agent_type = definition.get("named_agent_type")
    source = definition_source_of(definition)
    if source == DEFINITION_SOURCE_RELEASE:
        return worker_digest({"source": source, "type": agent_type})
    return worker_digest(
        {"source": source, "type": agent_type, **declared_definition(definition)}
    )


def definition_is_current(
    binding: ScheduleWorkerBindingV1, definition: dict[str, Any]
) -> bool:
    """Whether a policy may still start and admit workers of its target definition.

    A shipped definition is always current for its own type; the stored value
    remains part of the schedule's execution identity and is not compared.
    """
    if definition.get("named_agent_type") != binding.target_agent_type:
        return False
    if definition_source_of(definition) == DEFINITION_SOURCE_RELEASE:
        return True
    return binding.definition_digest == authored_definition_digest(definition)


def schedule_definition_digest(
    binding: ScheduleWorkerBindingV1 | None, definition: dict[str, Any]
) -> str:
    """The definition value that belongs in a schedule's execution identity.

    A current binding keeps its saved value, so re-saving an unchanged schedule
    keeps its schedule-scoped approvals. A new or changed target uses the
    current authored fingerprint.
    """
    if binding is not None and definition_is_current(binding, definition):
        return binding.definition_digest
    return authored_definition_digest(definition)


def _prompt_digest(definition: dict[str, Any]) -> str:
    return worker_digest(
        {"goal": definition.get("goal"), "backstory": definition.get("backstory")}
    )


def build_definition_record(
    definition: dict[str, Any],
    *,
    schedule_id: str,
    team_id: str,
    policy_revision: int,
    last_release_change: ReleaseDefinitionChangeV1 | None = None,
) -> WorkerDefinitionRecordV1:
    source = definition_source_of(definition)
    declared = declared_definition(definition)
    return WorkerDefinitionRecordV1(
        schedule_id=schedule_id,
        team_id=team_id,
        policy_revision=policy_revision,
        agent_type=str(definition.get("named_agent_type") or ""),
        source=source,
        source_path=str(definition.get("agent_definition_source_path") or ""),
        authored_digest=authored_definition_digest(definition),
        launch_digest=launch_definition_digest(definition),
        role=str(declared["role"]),
        # Team text is kept so a reviewer can compare it after an edit. Shipped
        # prompt text lives in the installed code and is not copied here.
        own_prompt=str(declared["own_prompt"])
        if source == DEFINITION_SOURCE_TEAM
        else None,
        own_prompt_digest=worker_digest(declared["own_prompt"]),
        prompt_digest=_prompt_digest(definition),
        include_shared_prompt=bool(declared["include_shared_prompt"]),
        backstory=str(declared["backstory"]),
        default_tools=tuple(declared["default_tools"]),
        skills_enabled=bool(declared["skills_enabled"]),
        tool_names=tuple(
            sorted({tool_name(item) for item in definition.get("tools", ())})
        ),
        last_release_change=last_release_change,
    )


def _added_removed(
    before: tuple[str, ...], after: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(sorted(set(after) - set(before))),
        tuple(sorted(set(before) - set(after))),
    )


def definition_changes(
    binding: ScheduleWorkerBindingV1,
    record: WorkerDefinitionRecordV1 | None,
    definition: dict[str, Any],
) -> WorkerDefinitionChangesV1:
    """Compare the saved record with the current definition for a reviewer."""
    current = build_definition_record(
        definition,
        schedule_id=binding.schedule_id,
        team_id=binding.team_id,
        policy_revision=binding.revision,
    )
    requires_review = not definition_is_current(binding, definition)
    if record is None:
        return WorkerDefinitionChangesV1(
            requires_review=requires_review,
            saved_record_available=False,
            source_after=current.source,
            role_after=current.role,
            own_prompt_after=current.own_prompt,
        )
    tools_added, tools_removed = _added_removed(record.tool_names, current.tool_names)
    defaults_added, defaults_removed = _added_removed(
        record.default_tools, current.default_tools
    )
    own_prompt_changed = record.own_prompt_digest != current.own_prompt_digest
    return WorkerDefinitionChangesV1(
        requires_review=requires_review,
        saved_record_available=True,
        source_before=record.source,
        source_after=current.source,
        role_before=record.role,
        role_after=current.role,
        own_prompt_changed=own_prompt_changed,
        own_prompt_before=record.own_prompt if own_prompt_changed else None,
        own_prompt_after=current.own_prompt if own_prompt_changed else None,
        prompt_changed=record.prompt_digest != current.prompt_digest,
        include_shared_prompt_changed=record.include_shared_prompt
        != current.include_shared_prompt,
        backstory_changed=record.backstory != current.backstory,
        skills_enabled_changed=record.skills_enabled != current.skills_enabled,
        default_tools_added=defaults_added,
        default_tools_removed=defaults_removed,
        tools_added=tools_added,
        tools_removed=tools_removed,
    )


def _release_change(
    stored: WorkerDefinitionRecordV1, current: WorkerDefinitionRecordV1
) -> ReleaseDefinitionChangeV1:
    tools_added, tools_removed = _added_removed(stored.tool_names, current.tool_names)
    return ReleaseDefinitionChangeV1(
        changed_at_utc=utc_now(),
        tools_added=tools_added,
        tools_removed=tools_removed,
        prompt_changed=stored.prompt_digest != current.prompt_digest,
        declaration_changed=any(
            getattr(stored, field) != getattr(current, field)
            for field in _RELEASE_DECLARED_FIELDS
        ),
    )


def refresh_definition_record(
    agent: Any, binding: ScheduleWorkerBindingV1
) -> WorkerDefinitionRecordV1 | None:
    """Record what a deploy changed for a schedule whose definition is current.

    Runs on the lead's reconciliation pass. A paused (changed) team definition
    keeps its saved record so the reviewer sees what was approved. The binding
    and its revision are never touched here.
    """
    records = agent._schedule_store.worker_records
    definition = agent._local_team_runtime.resolve_definition(binding.target_agent_type)
    stored: WorkerDefinitionRecordV1 | None = records.get_definition_record(
        binding.schedule_id, binding.team_id
    )
    if definition is None or not definition_is_current(binding, definition):
        return stored
    current = build_definition_record(
        definition,
        schedule_id=binding.schedule_id,
        team_id=binding.team_id,
        policy_revision=binding.revision,
    )
    if stored is None:
        if records.save_definition_record(current):
            _logger.info(
                "Recorded scheduled worker definition. "
                "event=schedule_worker_definition_recorded schedule_id=%s agent_type=%s source=%s",
                binding.schedule_id,
                binding.target_agent_type,
                current.source,
            )
        return current
    if stored.launch_digest == current.launch_digest:
        return stored
    change = _release_change(stored, current)
    refreshed = current.model_copy(update={"last_release_change": change})
    if records.save_definition_record(refreshed):
        _logger.info(
            "Scheduled worker definition changed with the running code; the schedule "
            "keeps running. event=schedule_worker_definition_refreshed schedule_id=%s "
            "agent_type=%s source=%s tools_added=%s tools_removed=%s prompt_changed=%s "
            "declaration_changed=%s",
            binding.schedule_id,
            binding.target_agent_type,
            current.source,
            list(change.tools_added),
            list(change.tools_removed),
            change.prompt_changed,
            change.declaration_changed,
        )
        _notice_release_change(agent, binding, change)
    return refreshed


def _notice_release_change(agent: Any, binding: ScheduleWorkerBindingV1, change: ReleaseDefinitionChangeV1) -> None:
    """Tell the schedule's owner what a release changed in its worker (the schedule keeps running)."""

    from custom_agents.access_runtime import send_access_notice
    from custom_messages.contracts.access_v2 import AccessNoticeKind

    parts = []
    if change.tools_added:
        parts.append("tools added: " + ", ".join(change.tools_added))
    if change.tools_removed:
        parts.append("tools removed: " + ", ".join(change.tools_removed))
    if change.prompt_changed:
        parts.append("its instructions changed")
    if change.declaration_changed:
        parts.append("its declared settings changed")
    send_access_notice(
        agent,
        kind=AccessNoticeKind.RELEASE_CHANGED,
        title=f"An update changed {binding.target_agent_type}, which runs one of your schedules",
        summary=(
            f"The installed update changed {binding.target_agent_type} ("
            + ("; ".join(parts) or "details in the schedule's history")
            + f"). Schedule {binding.schedule_id} keeps running with its existing access."
        ),
        person_id=binding.owner_canonical_user_id,
    )
