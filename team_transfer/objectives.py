"""The ``objectives`` section: the team lead's open objectives and their work setup.

What travels: every objective of the team that is still open (proposed,
active, paused or blocked), the installed Objective work profiles people kept
(active ones), and the accepted Objective work policies (active, paused or
disabled: each is a choice someone made). Each travels as its own versioned
contract (``ObjectiveRecord``, ``ObjectiveWorkInstalledProfile``,
``ObjectiveWorkPolicy``) and is validated by this version's model.

What stays behind: closed objectives, Objective work sessions, checkpoints and
profile proposals. They are history of work done, not configuration (owner's
decision: configuration only). An objective keeps the ids of the plans, tasks
and conversations it names; the new instance has none of them and treats them
as absent (a missing plan is no plan).

Why these choices:

- Objectives are the team lead's lasting records: their owner is the team
  lead's durable scope (``custom_agents.durable_owner_scope``), derived from
  the team id and the lead's agent type. The import writes them under the new
  instance's scope, which is the same unless the carried settings changed the
  lead's agent type. Records that belong to an agent incarnation instead (an
  owner that is not a durable scope) are refused: no running agent of the new
  instance would ever read them.
- An objective, profile or policy that works for a person who is not imported
  is refused, as is a policy whose objective or installed profile is refused.
- An objective that was being deleted is refused: its deletion would not
  finish in the new instance.

The Objective work schedules these policies run travel in the ``schedules``
section, which comes after this one and checks that their objective arrived.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, ClassVar, Optional

from pydantic import BaseModel, Field, JsonValue

from team_transfer.format import TransferModel
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

# The repositories cap these listings; reaching the cap means some records
# might be missing, so the export stops instead of leaving them out unlisted.
_LIST_LIMIT = 500
# Open objectives this version carries (the others are closed or being deleted).
_LASTING_OBJECTIVE_STATUSES = frozenset({"proposed", "active", "paused", "blocked"})


class ObjectiveKind(StrEnum):
    OBJECTIVE = "objective"
    WORK_PROFILE = "work_profile"
    WORK_POLICY = "work_policy"


class ObjectiveTransferRecordV1(TransferModel):
    kind: ObjectiveKind
    record_id: str = Field(min_length=1, max_length=400)
    document: dict[str, JsonValue]


def _key(kind: ObjectiveKind, record_id: str) -> str:
    return f"{kind.value}/{record_id}"


def owner_scope(config: Any) -> str:
    """The team lead's durable scope in an instance (where its objectives live)."""

    from custom_agents.durable_owner_scope import manager_durable_scope_id

    scope = manager_durable_scope_id(team_id=config.gateway.team_id, named_agent_type=config.gateway.named_agent_type)
    if not scope:
        from team_transfer.errors import TransferError

        raise TransferError("The configuration names no team lead team and agent type, so objectives have no owner.")
    return scope


def _objectives(storage: Any) -> Any:
    from storage.objective_repository import create_objective_repository

    return create_objective_repository(storage)


def _work(storage: Any) -> Any:
    from storage.objective_work_repository import create_objective_work_repository

    return create_objective_work_repository(storage)


def _capped(records: list[Any], what: str) -> list[Any]:
    if len(records) >= _LIST_LIMIT:
        from team_transfer.errors import TransferError

        raise TransferError(
            f"The team has {_LIST_LIMIT} or more {what}, more than the export can read at once. Close or remove "
            "the ones no longer needed and export again."
        )
    return records


class ObjectivesSection:
    name: ClassVar[str] = "objectives"
    schema_version: ClassVar[int] = 1
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = ObjectiveTransferRecordV1

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        storage = source.config.manager_storage_settings
        scope = owner_scope(source.config)
        work = _work(storage)
        records = [
            _record(ObjectiveKind.OBJECTIVE, objective.objective_id, objective)
            for objective in _objectives(storage).list_team_objectives(team_id=source.team_id)
        ]
        for person in _team_person_ids(source):
            profiles = work.list_installed_profiles(
                owner_agent_id=scope, owner_canonical_user_id=person, include_removed=False, limit=_LIST_LIMIT
            )
            records.extend(
                _record(ObjectiveKind.WORK_PROFILE, profile.installed_profile_id, profile)
                for profile in _capped(profiles, f"installed work profiles for {person}")
            )
        policies = _capped(work.list_policies(owner_agent_id=scope, limit=_LIST_LIMIT), "Objective work policies")
        records.extend(_record(ObjectiveKind.WORK_POLICY, policy.objective_id, policy) for policy in policies)
        return SectionExport(records=[record.model_dump(mode="json") for record in records])

    # -- import ----------------------------------------------------------------

    def plan(self, records: SectionRecords, context: ImportContext) -> list[PlannedRecord]:
        known = _Known(people=context.accepted("people"), team_id=context.reader.manifest.team_id)
        for key in context.baseline_keys.get(self.name, ()):
            kind, record_id = key.split("/", 1)
            known.accept(ObjectiveKind(kind), record_id)
        planned = []
        # Objectives first, then profiles, then the policies that name both.
        for raw in sorted(records, key=lambda raw: _KIND_ORDER.index(ObjectiveKind(raw["kind"]))):
            record = ObjectiveTransferRecordV1.model_validate(raw)
            key = _key(record.kind, record.record_id)
            try:
                problem = _CHECKS[record.kind](record.document, known)
            except ValueError as exc:  # this version's model refuses the document
                problem = f"this version refuses it: {exc}"
            if problem:
                planned.append(PlannedRecord(self.name, key, RecordOutcome.REFUSED, problem))
                continue
            known.accept(record.kind, record.record_id)
            planned.append(PlannedRecord(self.name, key, RecordOutcome.IMPORT))
        return planned

    def apply(self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext) -> None:
        from team_transfer.settings import planned_config

        accepted = {record.key for record in plan if record.outcome is not RecordOutcome.REFUSED}
        config = planned_config(context)
        storage = config.manager_storage_settings
        writer = _Writer(objectives=_objectives(storage), work=_work(storage), scope=owner_scope(config))
        for raw in records:
            record = ObjectiveTransferRecordV1.model_validate(raw)
            if _key(record.kind, record.record_id) in accepted:
                writer.write(record.kind, record.document)
        logger.info(
            "Team objectives imported. event=team_transfer_objectives_imported records=%s refused=%s scope=%s",
            len(accepted),
            len(plan) - len(accepted),
            writer.scope,
        )


def _team_person_ids(source: ExportSource) -> list[str]:
    """Everyone with a membership in the team, removed people too, so their records are listed as refused."""

    from storage.gateway_state_repository import create_gateway_state_repository

    repository = create_gateway_state_repository(source.config.state_storage_settings)
    return sorted({member.canonical_user_id for member in repository.list_team_people(source.team_id)})


def _record(kind: ObjectiveKind, record_id: str, model: Any) -> ObjectiveTransferRecordV1:
    return ObjectiveTransferRecordV1(kind=kind, record_id=record_id, document=model.model_dump(mode="json"))


_KIND_ORDER = (ObjectiveKind.OBJECTIVE, ObjectiveKind.WORK_PROFILE, ObjectiveKind.WORK_POLICY)


@dataclass
class _Known:
    """People the import accepts and the objective records accepted so far."""

    people: set[str]
    team_id: str
    accepted: dict[ObjectiveKind, set[str]] = field(default_factory=dict)

    def accept(self, kind: ObjectiveKind, record_id: str) -> None:
        self.accepted.setdefault(kind, set()).add(record_id)

    def has(self, kind: ObjectiveKind, record_id: str) -> bool:
        return record_id in self.accepted.get(kind, set())

    def person_problem(self, person_id: Optional[str]) -> str:
        if person_id and person_id not in self.people:
            return f"it works for {person_id}, who is not imported"
        return ""


def _scope_problem(owner_agent_id: Optional[str]) -> str:
    from custom_agents.durable_owner_scope import MANAGER_SCOPE_PREFIX

    if not str(owner_agent_id or "").startswith(f"{MANAGER_SCOPE_PREFIX}:"):
        return f"it belongs to the agent {owner_agent_id}, not to the team lead's lasting records"
    return ""


def _check_objective(document: dict[str, Any], known: _Known) -> str:
    from custom_messages.records.objectives import ObjectiveRecord

    objective = ObjectiveRecord.model_validate(document)
    if objective.status.value not in _LASTING_OBJECTIVE_STATUSES:
        return f"it was {objective.status.value} when the team was exported"
    if objective.team_id and objective.team_id != known.team_id:
        return f"it belongs to the team {objective.team_id}"
    return _scope_problem(objective.owner_agent_id) or known.person_problem(objective.owner_canonical_user_id)


def _check_profile(document: dict[str, Any], known: _Known) -> str:
    from custom_messages.records.objectives import ObjectiveWorkInstalledProfile

    profile = ObjectiveWorkInstalledProfile.model_validate(document)
    return _scope_problem(profile.owner_agent_id) or known.person_problem(profile.owner_canonical_user_id)


def _check_policy(document: dict[str, Any], known: _Known) -> str:
    from custom_messages.records.objectives import ObjectiveWorkPolicy

    policy = ObjectiveWorkPolicy.model_validate(document)
    if not known.has(ObjectiveKind.OBJECTIVE, policy.objective_id):
        return f"its objective {policy.objective_id} is not imported"
    if policy.installed_profile_id and not known.has(ObjectiveKind.WORK_PROFILE, policy.installed_profile_id):
        return f"its installed work profile {policy.installed_profile_id} is not imported"
    return _scope_problem(policy.owner_agent_id) or known.person_problem(policy.owner_canonical_user_id)


_CHECKS: dict[ObjectiveKind, Callable[[dict[str, Any], _Known], str]] = {
    ObjectiveKind.OBJECTIVE: _check_objective,
    ObjectiveKind.WORK_PROFILE: _check_profile,
    ObjectiveKind.WORK_POLICY: _check_policy,
}


@dataclass
class _Writer:
    """Writes accepted records under the new instance's team lead scope (module docstring)."""

    objectives: Any
    work: Any
    scope: str

    def write(self, kind: ObjectiveKind, document: dict[str, Any]) -> None:
        from custom_messages.records.objectives import (
            ObjectiveRecord,
            ObjectiveWorkInstalledProfile,
            ObjectiveWorkPolicy,
        )

        if kind is ObjectiveKind.OBJECTIVE:
            # A record already present was skipped while planning, so this only adds new ones.
            self.objectives.insert_objective(self._owned(ObjectiveRecord.model_validate(document)))
        elif kind is ObjectiveKind.WORK_PROFILE:
            self.work.save_installed_profile(self._owned(ObjectiveWorkInstalledProfile.model_validate(document)))
        else:
            self.work.save_policy(self._owned(ObjectiveWorkPolicy.model_validate(document)))

    def _owned(self, record: Any) -> Any:
        if record.owner_agent_id != self.scope:
            logger.warning(
                "An imported objective record moves to the new team lead's scope. "
                "event=team_transfer_objective_scope_changed record=%s old_scope=%s new_scope=%s",
                type(record).__name__,
                record.owner_agent_id,
                self.scope,
            )
        return record.model_copy(update={"owner_agent_id": self.scope})


__all__ = ["ObjectiveKind", "ObjectiveTransferRecordV1", "ObjectivesSection", "owner_scope"]
