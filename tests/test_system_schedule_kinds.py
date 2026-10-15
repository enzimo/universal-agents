"""The system schedule table (``custom_messages.system_schedules``).

Covers:

- every ``SystemScheduleKind`` but ``UNCLASSIFIED`` has exactly one row, name
  prefixes never start one another, and every preparer the table names is
  registered;
- the table gives exactly the lists each place kept before it (so the change
  to one table changed no behavior): internal names and delivery modes,
  system work, per-person kinds, kinds a local worker runs, coordinator
  events, and memory timers saved before kinds existed;
- every place that creates a system schedule of a fixed kind names it with
  that kind's prefix;
- a new per-person kind (a stand-in row, ``tests/system_schedule_kind_support.py``)
  is internal maintenance (no dedicated conversation, no recall hint), is
  one person's, gets its coordinator event and is prepared only on its own
  agent type; a new kind for no person is system work and the team's.

The stand-in kind is also covered where the real stores are: its on-demand
worker and worker configuration (``test_local_schedule_runtime.py``), its
deletion with a departing person (``test_person_departure.py``), the
gateway's install for each active person
(``test_person_memory_review_schedules.py``) and the permit for no person of
the team kind (``test_schedule_permits.py``).
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_control.task_identity import ScheduleFacts
from custom_agents.local_schedule_policy import local_system_timer_spec, supports_local_worker
from custom_agents.request_prompt_context import _allow_cross_conversation_recall_hint
from custom_agents.schedule_conversation_context import is_user_facing_recurring_schedule
from custom_agents.scheduled_runtime import (
    SYSTEM_SCHEDULE_PREPARERS,
    _prepare_system_timer,
    _schedule_event_kind,
)
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_messages import system_schedules
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    ScheduleManagementClass,
    SystemScheduleKind,
)
from custom_messages.system_schedules import (
    SystemScheduleScope,
    classified_system_schedule_spec,
    internal_delivery_modes,
    internal_schedule_name_prefixes,
    is_person_system_schedule,
    is_system_work_kind,
    system_schedule_spec,
    system_schedule_spec_for_name,
    system_schedule_specs,
)
from tests.system_schedule_kind_support import (
    CURATOR_AGENT_TYPE,
    CURATOR_DELIVERY_MODE,
    NO_PERSON_KIND,
    PERSON_KIND,
    STAND_IN_KIND,
    use_kind,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM = ScheduleManagementClass.SYSTEM
MEMORY_KINDS = {
    SystemScheduleKind.MEMORY_REVIEW_COREMEM,
    SystemScheduleKind.MEMORY_REVIEW_USERMEM,
    SystemScheduleKind.MEMORY_REVIEW_STALE_CONVERSATIONS,
}


def _schedule(
    name: str,
    kind: SystemScheduleKind | None = None,
    *,
    management: ScheduleManagementClass = SYSTEM,
    claim: str | None = None,
    delivery_mode: str | None = None,
    parent_task_id: str | None = None,
) -> ScheduledMessage:
    return ScheduledMessage(
        schedule_name=name,
        management_class=management,
        system_schedule_kind=kind,
        claim_named_agent_type=claim,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="tick", parent_task_id=parent_task_id),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="mia", delivery_mode=delivery_mode)),
        interval_ms=60_000,
    )


# -- the table ------------------------------------------------------------------------


def test_every_kind_but_unclassified_has_exactly_one_row() -> None:
    kinds = [spec.kind for spec in system_schedule_specs()]

    assert len(kinds) == len(set(kinds))
    assert set(kinds) == set(SystemScheduleKind) - {SystemScheduleKind.UNCLASSIFIED}
    assert system_schedule_spec(SystemScheduleKind.UNCLASSIFIED) is None
    assert system_schedule_spec(None) is None


def test_name_prefixes_are_unique_and_never_start_one_another() -> None:
    prefixes = [spec.name_prefix for spec in system_schedule_specs() if spec.name_prefix]

    for prefix in prefixes:
        assert prefix.endswith(":")
        assert [other for other in prefixes if other.startswith(prefix)] == [prefix]


def test_every_named_preparer_is_registered_and_local_worker_rows_are_complete() -> None:
    for spec in system_schedule_specs():
        if spec.preparer is not None:
            assert spec.preparer in SYSTEM_SCHEDULE_PREPARERS, spec.kind
            assert spec.local_worker, f"{spec.kind}: preparers run only on the kind's own local worker"
        if spec.local_worker:
            assert spec.agent_type and spec.delivery_mode and spec.name_prefix, spec.kind


def test_the_table_gives_the_lists_each_place_kept_before() -> None:
    """The values each place hard-coded before the table, so moving them to it changed nothing."""

    # schedule_conversation_context.INTERNAL_SCHEDULE_NAME_PREFIXES and INTERNAL_DELIVERY_MODES.
    assert internal_schedule_name_prefixes() == (
        "after_action_report_batch_review:",
        "internal_improvement:",
        "isolated_recovery_checkpoint:",
        # Knowledge curation, added with KnowledgeCuratorAgent.
        "knowledge_curation:",
        "memory_review:",
        "provider_overload_retry:",
    )
    assert internal_delivery_modes() == {"heartbeat", "internal_improvement", "knowledge_curation", "memory_review"}
    # task_identity.SYSTEM_WORK_SCHEDULE_KINDS.
    assert {kind for kind in SystemScheduleKind if is_system_work_kind(kind)} == {
        SystemScheduleKind.INTERNAL_IMPROVEMENT,
        SystemScheduleKind.KNOWLEDGE_CURATION_TEAM,
    }
    # After-action reviews follow the person whose work produced the reports.
    assert {spec.kind for spec in system_schedule_specs() if spec.scope is SystemScheduleScope.PERSON} == {
        SystemScheduleKind.MEMORY_REVIEW_USERMEM,
        SystemScheduleKind.MEMORY_REVIEW_STALE_CONVERSATIONS,
        SystemScheduleKind.OBJECTIVE_WORK_SESSION,
        SystemScheduleKind.ISOLATED_RECOVERY_CHECKPOINT,
        SystemScheduleKind.PROVIDER_OVERLOAD_RETRY,
        SystemScheduleKind.AFTER_ACTION_REPORT_REVIEW,
        SystemScheduleKind.KNOWLEDGE_CURATION_PERSON,
    }
    # local_schedule_policy.is_memory_review_timer and schedule_worker_service's MemReviewAgent check.
    local = {spec.kind: spec.agent_type for spec in system_schedule_specs() if spec.local_worker}
    assert local == {
        **dict.fromkeys(MEMORY_KINDS, "MemReviewAgent"),
        SystemScheduleKind.KNOWLEDGE_CURATION_PERSON: "KnowledgeCuratorAgent",
        SystemScheduleKind.KNOWLEDGE_CURATION_TEAM: "KnowledgeCuratorAgent",
    }
    # scheduled_runtime._schedule_event_kind.
    assert _schedule_event_kind("provider_overload_retry:task:marker") == ("provider_retry", "deferrable")
    assert _schedule_event_kind("memory_review:usermem:test:mia") == ("memory_review_schedule", "advisory")
    assert _schedule_event_kind("internal_improvement:maintenance:team") == ("internal_improvement_schedule", "advisory")
    assert _schedule_event_kind("Daily report") == ("scheduled_message", "deferrable")
    # The gateway's delivery modes.
    assert system_schedule_spec(SystemScheduleKind.HEARTBEAT_LAUNCHER).delivery_mode == "heartbeat"
    assert {system_schedule_spec(kind).delivery_mode for kind in MEMORY_KINDS} == {"memory_review"}
    assert system_schedule_spec(SystemScheduleKind.INTERNAL_IMPROVEMENT).delivery_mode == "internal_improvement"


def test_memory_timers_saved_before_kinds_are_still_recognized_by_name_and_no_other_kind_is() -> None:
    usermem = "memory_review:usermem:test:mia"
    for kind in (None, SystemScheduleKind.UNCLASSIFIED):
        assert classified_system_schedule_spec(_schedule(usermem, kind)).kind is SystemScheduleKind.MEMORY_REVIEW_USERMEM
        assert is_person_system_schedule(_schedule(usermem, kind))
        assert local_system_timer_spec(_schedule(usermem, kind)).agent_type == "MemReviewAgent"
        # Only memory review predates kinds: other names stand for no kind.
        assert classified_system_schedule_spec(_schedule("provider_overload_retry:t:m", kind)) is None
    assert classified_system_schedule_spec(_schedule(usermem, management=ScheduleManagementClass.USER)) is None

    # A memory timer is recognized by name; its stored kind must be one of the same agent type.
    mismatched = _schedule(usermem, SystemScheduleKind.MEMORY_REVIEW_COREMEM)
    assert local_system_timer_spec(mismatched).kind is SystemScheduleKind.MEMORY_REVIEW_USERMEM
    assert local_system_timer_spec(_schedule(usermem, SystemScheduleKind.INTERNAL_IMPROVEMENT)) is None
    assert local_system_timer_spec(_schedule("Daily report", SystemScheduleKind.MEMORY_REVIEW_COREMEM)) is None
    assert local_system_timer_spec(_schedule(usermem, parent_task_id="task_parent")) is None
    # Only its own agent type runs it on a local worker; an old one of TeamArchitect needs the upgrade.
    assert supports_local_worker(_schedule(usermem, SystemScheduleKind.MEMORY_REVIEW_USERMEM, claim="MemReviewAgent"))
    assert not supports_local_worker(_schedule(usermem, SystemScheduleKind.MEMORY_REVIEW_USERMEM, claim="TeamArchitect"))


def _static_name_prefix(node: ast.expr) -> str | None:
    """The fixed start of a schedule name expression, with table constants resolved; None when dynamic."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    prefix = ""
    for part in node.values:
        if isinstance(part, ast.Constant):
            prefix += str(part.value)
        elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name) and not prefix:
            constant = getattr(system_schedules, part.value.id, None)
            if not isinstance(constant, str):
                break
            prefix += constant
        else:
            break
    return prefix or None


def _fixed_kind(node: ast.expr) -> SystemScheduleKind | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "SystemScheduleKind":
        return SystemScheduleKind[node.attr]
    return None


def _schedules_created_with_a_fixed_kind() -> list[tuple[str, SystemScheduleKind, ast.expr]]:
    found = []
    for path in REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in {".venv", "tests", "target", "node_modules", "deleteme"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            kind = _fixed_kind(keywords["system_schedule_kind"]) if "system_schedule_kind" in keywords else None
            if kind is not None and "schedule_name" in keywords:
                found.append((relative.as_posix(), kind, keywords["schedule_name"]))
    return found


def test_every_schedule_created_with_a_fixed_kind_is_named_with_its_prefix() -> None:
    created = _schedules_created_with_a_fixed_kind()
    dynamic = set()
    for path, kind, name in created:
        spec = system_schedule_spec(kind)
        assert spec is not None, (path, kind)
        if spec.name_prefix is None:
            continue  # recognized by more than its name (the heartbeat launcher)
        prefix = _static_name_prefix(name)
        if prefix is None:
            dynamic.add((path, kind))
            continue
        assert prefix.startswith(spec.name_prefix), (path, kind, prefix)

    assert {kind for _path, kind, _name in created} >= {
        SystemScheduleKind.AFTER_ACTION_REPORT_REVIEW,
        SystemScheduleKind.ISOLATED_RECOVERY_CHECKPOINT,
        SystemScheduleKind.MEMORY_REVIEW_COREMEM,
        SystemScheduleKind.OBJECTIVE_WORK_SESSION,
        SystemScheduleKind.PROVIDER_OVERLOAD_RETRY,
    }
    # The maintenance schedule's name is built by a function (the gateway's
    # refresh gets it from its settings, which use the same function); it
    # uses the table's prefix too.
    assert dynamic == {
        ("custom_agents/internal_improvement_runtime.py", SystemScheduleKind.INTERNAL_IMPROVEMENT),
        ("assistant_gateway/application/maintenance_schedules.py", SystemScheduleKind.INTERNAL_IMPROVEMENT),
    }
    from custom_agents.internal_improvement_runtime import internal_improvement_schedule_name

    assert system_schedule_spec_for_name(internal_improvement_schedule_name("team")).kind is SystemScheduleKind.INTERNAL_IMPROVEMENT


# -- a new kind ---------------------------------------------------------------------------


def test_a_new_per_person_kind_is_internal_maintenance_like_memory_review(monkeypatch: pytest.MonkeyPatch) -> None:
    named = _schedule("knowledge_review:person:test:mia", STAND_IN_KIND, claim=CURATOR_AGENT_TYPE)
    by_delivery = _schedule("Daily notes", management=ScheduleManagementClass.USER, delivery_mode=CURATOR_DELIVERY_MODE)
    request = MessageMeta(
        requester_type=RequesterType.USER_REQUESTER, gateway_context=GatewayContext(delivery_mode=CURATOR_DELIVERY_MODE)
    )
    # Without the row, both schedules are user-facing and the request may get the hint.
    assert is_user_facing_recurring_schedule(named) and is_user_facing_recurring_schedule(by_delivery)
    assert _allow_cross_conversation_recall_hint(request)

    use_kind(monkeypatch, PERSON_KIND)

    assert not is_user_facing_recurring_schedule(named)
    assert not is_user_facing_recurring_schedule(by_delivery)
    assert not _allow_cross_conversation_recall_hint(request)
    chat = request.model_copy(update={"gateway_context": GatewayContext(delivery_mode="conversation")})
    assert _allow_cross_conversation_recall_hint(chat)


def test_a_new_per_person_kind_is_one_persons_and_prepared_only_on_its_own_agent_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared: list[tuple[str, str]] = []

    def prepare(agent: Any, schedule_id: str, scheduled_message: ScheduledMessage) -> ScheduledMessage:
        prepared.append((agent.named_agent_type, schedule_id))
        return scheduled_message.model_copy(update={"payload": Task(task_type=MessageMetaType.REQUEST, problem_statement="prepared")})

    use_kind(monkeypatch, PERSON_KIND, preparer=prepare)
    timer = _schedule("knowledge_review:person:test:mia", STAND_IN_KIND, claim=CURATOR_AGENT_TYPE)

    def agent(agent_type: str) -> SimpleNamespace:
        return SimpleNamespace(
            named_agent_type=agent_type,
            task_manager=SimpleNamespace(operations_runtime=TaskOperationRuntime()),
            _logger=logging.getLogger("test"),
        )

    assert is_person_system_schedule(timer)
    assert supports_local_worker(timer)
    assert _schedule_event_kind(timer.schedule_name) == ("knowledge_review_schedule", "advisory")
    result = asyncio.run(_prepare_system_timer(agent(CURATOR_AGENT_TYPE), "sched_kc", timer))
    assert result.payload.problem_statement == "prepared" and prepared == [(CURATOR_AGENT_TYPE, "sched_kc")]
    with pytest.raises(ValueError, match="knowledge_review_schedule_upgrade_required"):
        asyncio.run(_prepare_system_timer(agent("TeamArchitect"), "sched_kc", timer))
    # A person's own schedule is never prepared.
    own = _schedule("knowledge_review:person:test:mia", management=ScheduleManagementClass.USER)
    assert asyncio.run(_prepare_system_timer(agent(CURATOR_AGENT_TYPE), "sched_own", own)) is own
    assert len(prepared) == 1


def test_a_new_kind_for_no_person_is_system_work_and_the_teams(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = ScheduleFacts(
        schedule_id="sched_kc_team",
        enabled=True,
        owner_id="olive",
        system_managed=True,
        system_kind=STAND_IN_KIND.value,
        runner_type=CURATOR_AGENT_TYPE,
    )
    assert not facts.system_work

    use_kind(monkeypatch, NO_PERSON_KIND)

    assert facts.system_work
    assert not ScheduleFacts("sched_own", True, "olive", False, None).system_work
    assert not is_person_system_schedule(_schedule("knowledge_review:team:test", STAND_IN_KIND))
    from team_transfer.schedules import _team_kinds

    assert STAND_IN_KIND.value in _team_kinds()
