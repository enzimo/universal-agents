"""The runtime's own schedule kinds, in one table the gateway and the manager share.

A system-managed schedule (``ScheduleManagementClass.SYSTEM``) names its
purpose with a ``SystemScheduleKind``. Code in several places treats the kinds
differently: which agent type runs them, whether they belong to one person,
whether a run works for no person, how they are delivered and whether they are
internal maintenance. Before this table each place kept its own list of names
or kinds, so adding a kind meant finding and changing every list. Now each
place asks this table. A new kind needs a row and, when its runs need preparing,
a hook in ``custom_agents``. A kind that works for no person also needs a fire
path that signs system work, as described below.

Why here: the gateway's application layer may not import manager code and
``custom_messages`` may not import runtime hosts
(``tests/test_runtime_architecture_boundaries.py``), so the facts are plain
data in the message package. Runtime behavior stays in ``custom_agents`` and is
named here only by a hook name: ``preparer`` names a function registered in
``custom_agents.scheduled_runtime.SYSTEM_SCHEDULE_PREPARERS``.

Columns (``SystemScheduleKindSpec``):

- ``name_prefix``: every schedule of the kind is named with it. Fired runs are
  recognized by name, as before kinds existed. ``None`` when the name alone
  does not identify the kind: a heartbeat launcher is a ``heartbeat:`` name
  together with a heartbeat source (``custom_agents.heartbeat_store.
  is_heartbeat_launcher``), and people may use that name for their own
  schedules.
- ``scope``: ``PERSON`` schedules are one person's own work; when that person
  leaves the team they are deleted with the person's other schedules.
  ``TEAM`` schedules are the team's; they move to another administrator
  (``custom_agents.person_departure_runtime``) and travel in a team transfer
  without their person (``team_transfer.schedules``).
- ``agent_type``: the agent type that does the work, or ``None`` when the
  agent that created or claimed the schedule does it.
- ``local_worker``: ``agent_type`` claims the schedule itself, on a worker the
  local scheduler starts on demand under the default startup policy
  (``custom_agents.local_schedule_policy``). Only such system schedules accept a
  scheduled-worker configuration, and only for ``agent_type``.
- ``system_work``: permits and identity checks allow runs for no person
  (``custom_agents.schedule_permits``, ``access_control.task_identity``).
  The kind's fire path must call ``custom_agents.schedule_permits.sign_system_run``;
  the ordinary fire path signs runs for the schedule's owner.
- ``delivery_mode``: the gateway context delivery mode the schedule's runs carry.
- ``internal``: internal maintenance, never user-facing. A recurring one gets
  no dedicated conversation (``custom_agents.schedule_conversation_context``),
  every schedule named in its name family (the prefix up to the first colon)
  is treated the same way, and a request carrying its delivery mode gets no
  cross-conversation recall hint (``custom_agents.request_prompt_context``).
- ``preparer``: the hook that prepares a fired run on ``agent_type`` before the
  run is admitted for the schedule's owner (memory review creates its review
  jobs). It runs only on that agent type.
- ``legacy_name_match``: a schedule of the kind saved before kinds existed (its
  kind unclassified) is recognized by its name. Only the memory-review
  schedules predate kinds; no new kind needs it.
- ``coordinator_event``: the event type and class the runtime coordinator
  records for a fire (``custom_agents.task_runtime_coordinator``); ``None`` for
  an ordinary scheduled message. ``advisory`` fires are never deferred behind
  active work.

To add a kind: add the ``SystemScheduleKind`` member, add its row below, name
its schedules with ``name_prefix``, and register its ``preparer`` when it has
one. A per-person kind the gateway installs also needs a
``GatewayPersonSystemScheduleDefinition`` in the gateway's settings
(``assistant_gateway.gateway``), which gives its id prefix, cadence and
instruction. For ``system_work=True``, provide a fire path that calls
``sign_system_run`` and test an actual fired occurrence through admission.
The table alone does not select that signing path. Internal-improvement
maintenance already has its own path in ``custom_agents.internal_improvement_runtime``.
``tests/test_system_schedule_kinds.py`` checks the table and its classification
rules; ``tests/test_schedule_permits.py`` checks permit issuance and identity
verification for a new no-person kind, not its automatic firing.

Lookups read ``SYSTEM_SCHEDULE_KINDS`` at call time, so the table is the only
copy of these facts.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from custom_messages.contracts.runtime_v1 import (
    INTERNAL_IMPROVEMENT_AGENT_TYPE,
    KNOWLEDGE_CURATOR_AGENT_TYPE,
    MEM_REVIEW_AGENT_TYPE,
)
from custom_messages.enums import ScheduleManagementClass, SystemScheduleKind
from id_generation import operational_id

# -- names and delivery modes the gateway and the manager both use ------------------

MEMORY_REVIEW_SCHEDULE_PREFIX = "memory_review:"
COREMEM_AUDIT_SCHEDULE_NAME_PREFIX = f"{MEMORY_REVIEW_SCHEDULE_PREFIX}coremem:"
USERMEM_AUDIT_SCHEDULE_NAME_PREFIX = f"{MEMORY_REVIEW_SCHEDULE_PREFIX}usermem:"
STALE_CONVERSATION_SCHEDULE_NAME_PREFIX = f"{MEMORY_REVIEW_SCHEDULE_PREFIX}stale_conversations:"
INTERNAL_IMPROVEMENT_SCHEDULE_NAME_PREFIX = "internal_improvement:maintenance:"
AFTER_ACTION_REPORT_REVIEW_SCHEDULE_NAME_PREFIX = "after_action_report_batch_review:"
DELEGATION_ASSURANCE_SCHEDULE_NAME_PREFIX = "delegation_assurance:"
ISOLATED_RECOVERY_CHECKPOINT_SCHEDULE_NAME_PREFIX = "isolated_recovery_checkpoint:"
PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX = "provider_overload_retry:"
OBJECTIVE_WORK_SESSION_SCHEDULE_NAME_PREFIX = "objective_work_session:"
KNOWLEDGE_CURATION_PERSON_SCHEDULE_NAME_PREFIX = "knowledge_curation:person:"
KNOWLEDGE_CURATION_TEAM_SCHEDULE_NAME_PREFIX = "knowledge_curation:team:"

HEARTBEAT_DELIVERY_MODE = "heartbeat"
MEMORY_REVIEW_DELIVERY_MODE = "memory_review"
INTERNAL_IMPROVEMENT_DELIVERY_MODE = "internal_improvement"
KNOWLEDGE_CURATION_DELIVERY_MODE = "knowledge_curation"

# The hook name of memory review's run preparation
# (custom_agents.scheduled_runtime.prepare_memory_review_schedule).
MEMORY_REVIEW_PREPARER = "memory_review"
# Knowledge curation's run preparation, which skips a run with nothing to review
# (custom_agents.scheduled_runtime.prepare_knowledge_curation_schedule).
KNOWLEDGE_CURATION_PREPARER = "knowledge_curation"

_MEMORY_REVIEW_EVENT = ("memory_review_schedule", "advisory")


class SystemScheduleScope(StrEnum):
    """Whose a system schedule is (module docstring, ``scope``)."""

    PERSON = "person"
    TEAM = "team"


@dataclass(frozen=True)
class SystemScheduleKindSpec:
    """One row of the table: what the runtime does with schedules of one kind (module docstring)."""

    kind: SystemScheduleKind
    name_prefix: str | None
    scope: SystemScheduleScope
    agent_type: str | None = None
    local_worker: bool = False
    system_work: bool = False
    delivery_mode: str | None = None
    internal: bool = False
    preparer: str | None = None
    legacy_name_match: bool = False
    coordinator_event: tuple[str, str] | None = None

    @property
    def name_family(self) -> str | None:
        """The name prefix up to its first colon (``memory_review``); used in reason codes and log events."""

        return self.name_prefix.split(":", 1)[0] if self.name_prefix else None


def _memory_review(kind: SystemScheduleKind, name_prefix: str, scope: SystemScheduleScope) -> SystemScheduleKindSpec:
    """A memory-review row; the three differ only in kind, name and scope."""

    return SystemScheduleKindSpec(
        kind=kind,
        name_prefix=name_prefix,
        scope=scope,
        agent_type=MEM_REVIEW_AGENT_TYPE,
        local_worker=True,
        delivery_mode=MEMORY_REVIEW_DELIVERY_MODE,
        internal=True,
        preparer=MEMORY_REVIEW_PREPARER,
        legacy_name_match=True,
        coordinator_event=_MEMORY_REVIEW_EVENT,
    )


_PERSON = SystemScheduleScope.PERSON
_TEAM = SystemScheduleScope.TEAM

# Every SystemScheduleKind except UNCLASSIFIED has exactly one row.
SYSTEM_SCHEDULE_KINDS: tuple[SystemScheduleKindSpec, ...] = (
    # The gateway's heartbeat launcher; the manager starts the source's items.
    SystemScheduleKindSpec(
        kind=SystemScheduleKind.HEARTBEAT_LAUNCHER,
        name_prefix=None,
        scope=_TEAM,
        delivery_mode=HEARTBEAT_DELIVERY_MODE,
        internal=True,
    ),
    # Memory review (custom_agents.memory_review_runtime): the core-facts audit
    # is the team's; the personal-facts audit and stale-conversation review are
    # one pair per person, signed for that person.
    _memory_review(SystemScheduleKind.MEMORY_REVIEW_COREMEM, COREMEM_AUDIT_SCHEDULE_NAME_PREFIX, _TEAM),
    _memory_review(SystemScheduleKind.MEMORY_REVIEW_USERMEM, USERMEM_AUDIT_SCHEDULE_NAME_PREFIX, _PERSON),
    _memory_review(
        SystemScheduleKind.MEMORY_REVIEW_STALE_CONVERSATIONS, STALE_CONVERSATION_SCHEDULE_NAME_PREFIX, _PERSON
    ),
    # Internal-improvement maintenance (custom_agents.internal_improvement_runtime):
    # the team lead claims it and starts InternalImprovementAgent; its runs work
    # for no person.
    SystemScheduleKindSpec(
        kind=SystemScheduleKind.INTERNAL_IMPROVEMENT,
        name_prefix=INTERNAL_IMPROVEMENT_SCHEDULE_NAME_PREFIX,
        scope=_TEAM,
        agent_type=INTERNAL_IMPROVEMENT_AGENT_TYPE,
        system_work=True,
        delivery_mode=INTERNAL_IMPROVEMENT_DELIVERY_MODE,
        internal=True,
        coordinator_event=("internal_improvement_schedule", "advisory"),
    ),
    # One-time review of reports about one person's work. It must never move
    # to another administrator when that person leaves.
    SystemScheduleKindSpec(
        kind=SystemScheduleKind.AFTER_ACTION_REPORT_REVIEW,
        name_prefix=AFTER_ACTION_REPORT_REVIEW_SCHEDULE_NAME_PREFIX,
        scope=_PERSON,
        internal=True,
    ),
    # Obsolete pickup checks, dropped when they fire
    # (scheduled_runtime.OBSOLETE_DELEGATION_ASSURANCE_PREFIX). Not internal:
    # their names were never reserved, and none run any more.
    SystemScheduleKindSpec(
        kind=SystemScheduleKind.DELEGATION_ASSURANCE,
        name_prefix=DELEGATION_ASSURANCE_SCHEDULE_NAME_PREFIX,
        scope=_TEAM,
    ),
    # One-time follow-ups of a person's task.
    SystemScheduleKindSpec(
        kind=SystemScheduleKind.ISOLATED_RECOVERY_CHECKPOINT,
        name_prefix=ISOLATED_RECOVERY_CHECKPOINT_SCHEDULE_NAME_PREFIX,
        scope=_PERSON,
        internal=True,
    ),
    SystemScheduleKindSpec(
        kind=SystemScheduleKind.PROVIDER_OVERLOAD_RETRY,
        name_prefix=PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX,
        scope=_PERSON,
        internal=True,
        coordinator_event=("provider_retry", "deferrable"),
    ),
    # A person's objective work sessions; user-facing, so each gets its own conversation.
    SystemScheduleKindSpec(
        kind=SystemScheduleKind.OBJECTIVE_WORK_SESSION,
        name_prefix=OBJECTIVE_WORK_SESSION_SCHEDULE_NAME_PREFIX,
        scope=_PERSON,
    ),
    # Knowledge curation (knowledge_base, KnowledgeCuratorAgent, started on
    # demand): one run per person, signed for them, reviews the team changes
    # proposed from their work; the team run works for no person and tidies
    # the team pages. Both skip a run with nothing to do.
    SystemScheduleKindSpec(
        kind=SystemScheduleKind.KNOWLEDGE_CURATION_PERSON,
        name_prefix=KNOWLEDGE_CURATION_PERSON_SCHEDULE_NAME_PREFIX,
        scope=_PERSON,
        agent_type=KNOWLEDGE_CURATOR_AGENT_TYPE,
        local_worker=True,
        delivery_mode=KNOWLEDGE_CURATION_DELIVERY_MODE,
        internal=True,
        preparer=KNOWLEDGE_CURATION_PREPARER,
        coordinator_event=("knowledge_curation_schedule", "advisory"),
    ),
    SystemScheduleKindSpec(
        kind=SystemScheduleKind.KNOWLEDGE_CURATION_TEAM,
        name_prefix=KNOWLEDGE_CURATION_TEAM_SCHEDULE_NAME_PREFIX,
        scope=_TEAM,
        agent_type=KNOWLEDGE_CURATOR_AGENT_TYPE,
        local_worker=True,
        system_work=True,
        delivery_mode=KNOWLEDGE_CURATION_DELIVERY_MODE,
        internal=True,
        preparer=KNOWLEDGE_CURATION_PREPARER,
        coordinator_event=("knowledge_curation_schedule", "advisory"),
    ),
)


# -- lookups --------------------------------------------------------------------------


def system_schedule_specs() -> tuple[SystemScheduleKindSpec, ...]:
    """Every row, read at call time."""

    return SYSTEM_SCHEDULE_KINDS


def _kind_value(kind: Any) -> str:
    return str(getattr(kind, "value", kind) or "")


def system_schedule_spec(kind: Any) -> SystemScheduleKindSpec | None:
    """The row of a kind (enum member or its value); ``None`` for unclassified or unknown kinds."""

    value = _kind_value(kind)
    if not value or value == SystemScheduleKind.UNCLASSIFIED.value:
        return None
    return next((spec for spec in system_schedule_specs() if spec.kind.value == value), None)


def system_schedule_spec_for_name(schedule_name: str | None) -> SystemScheduleKindSpec | None:
    """The row whose ``name_prefix`` starts this name; ``None`` when none does.

    No prefix starts another (``tests/test_system_schedule_kinds.py``), so at
    most one row matches.
    """

    name = str(schedule_name or "")
    return next(
        (spec for spec in system_schedule_specs() if spec.name_prefix and name.startswith(spec.name_prefix)),
        None,
    )


def classified_system_schedule_spec(schedule: Any) -> SystemScheduleKindSpec | None:
    """The row of a system-managed schedule: by its kind, or by name for one saved before kinds existed.

    ``None`` for a person's own schedule and for a system schedule of no known
    kind. A name stands in for an unclassified kind only for rows with
    ``legacy_name_match``.
    """

    if getattr(schedule, "management_class", None) != ScheduleManagementClass.SYSTEM:
        return None
    kind = getattr(schedule, "system_schedule_kind", None)
    spec = system_schedule_spec(kind)
    if spec is not None or _kind_value(kind) not in ("", SystemScheduleKind.UNCLASSIFIED.value):
        return spec
    by_name = system_schedule_spec_for_name(getattr(schedule, "schedule_name", None))
    return by_name if by_name is not None and by_name.legacy_name_match else None


def is_person_system_schedule(schedule: Any) -> bool:
    """Whether a system-managed schedule is one person's own work (``scope`` is ``PERSON``).

    Such a schedule is deleted with its owner when they leave the team; every
    other system schedule is the team's.
    """

    spec = classified_system_schedule_spec(schedule)
    return spec is not None and spec.scope is SystemScheduleScope.PERSON


def is_system_work_kind(kind: Any) -> bool:
    """Whether runs of this kind work for no person (``system_work``)."""

    spec = system_schedule_spec(kind)
    return spec is not None and spec.system_work


def internal_schedule_name_prefixes() -> tuple[str, ...]:
    """The name families of internal kinds (``memory_review:``); any schedule named in one is not user-facing."""

    return tuple(
        sorted({f"{spec.name_family}:" for spec in system_schedule_specs() if spec.internal and spec.name_family})
    )


def internal_delivery_modes() -> frozenset[str]:
    """The delivery modes of internal kinds' runs (``memory_review``)."""

    return frozenset(spec.delivery_mode for spec in system_schedule_specs() if spec.internal and spec.delivery_mode)


# -- per-person schedule ids and names ----------------------------------------------

# Person ids made of these characters go into schedule ids unchanged; others
# (a configured first-user id may contain spaces) are made safe and given a
# short digest so two people never share a schedule id.
_SCHEDULE_ID_SAFE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SCHEDULE_ID_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_.:-]+")


def _schedule_id_person_part(person_id: str) -> str:
    if _SCHEDULE_ID_SAFE.match(person_id):
        return person_id
    digest = hashlib.sha256(person_id.encode("utf-8")).hexdigest()[:8]
    return f"{_SCHEDULE_ID_UNSAFE_CHARS.sub('_', person_id).strip('_')}_{digest}"


def person_system_schedule_id(id_prefix: str, instance_name: str, person_id: str) -> str:
    """The stable id of one person's system schedule, e.g. ``mem_rev_user_<instance>_<person>``.

    Different from the old team-wide memory-review id (``mem_rev_user_<instance>``),
    so an install never matches the old schedule.
    """

    person = str(person_id or "").strip()
    if not person:
        raise ValueError("person_id is required for a per-person system schedule")
    return operational_id(id_prefix, f"{instance_name}_{_schedule_id_person_part(person)}")


def person_system_schedule_name(name_prefix: str, instance_name: str, person_id: str) -> str:
    """The name of one person's system schedule, e.g. ``memory_review:usermem:<instance>:<person>``.

    It starts with the kind's ``name_prefix``, so the manager still tells the
    kind (and what a run must prepare) from the name.
    """

    return f"{name_prefix}{instance_name}:{str(person_id or '').strip()}"


__all__ = [
    "AFTER_ACTION_REPORT_REVIEW_SCHEDULE_NAME_PREFIX",
    "COREMEM_AUDIT_SCHEDULE_NAME_PREFIX",
    "DELEGATION_ASSURANCE_SCHEDULE_NAME_PREFIX",
    "HEARTBEAT_DELIVERY_MODE",
    "INTERNAL_IMPROVEMENT_DELIVERY_MODE",
    "INTERNAL_IMPROVEMENT_SCHEDULE_NAME_PREFIX",
    "ISOLATED_RECOVERY_CHECKPOINT_SCHEDULE_NAME_PREFIX",
    "MEMORY_REVIEW_DELIVERY_MODE",
    "KNOWLEDGE_CURATION_DELIVERY_MODE",
    "KNOWLEDGE_CURATION_PERSON_SCHEDULE_NAME_PREFIX",
    "KNOWLEDGE_CURATION_PREPARER",
    "KNOWLEDGE_CURATION_TEAM_SCHEDULE_NAME_PREFIX",
    "MEMORY_REVIEW_PREPARER",
    "MEMORY_REVIEW_SCHEDULE_PREFIX",
    "OBJECTIVE_WORK_SESSION_SCHEDULE_NAME_PREFIX",
    "PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX",
    "STALE_CONVERSATION_SCHEDULE_NAME_PREFIX",
    "SYSTEM_SCHEDULE_KINDS",
    "SystemScheduleKindSpec",
    "SystemScheduleScope",
    "USERMEM_AUDIT_SCHEDULE_NAME_PREFIX",
    "classified_system_schedule_spec",
    "internal_delivery_modes",
    "internal_schedule_name_prefixes",
    "is_person_system_schedule",
    "is_system_work_kind",
    "person_system_schedule_id",
    "person_system_schedule_name",
    "system_schedule_spec",
    "system_schedule_spec_for_name",
    "system_schedule_specs",
]
