"""Move pinned memories from one instance to another (``assistant_cli memory export|import``).

Pinned memories are the short facts agents see on every turn: the team's
(``coremem``, one set per team) and each person's (``usermem``, one set per
person id). An owner moving to a brand-new instance exports them from the old
instance's store into a versioned JSON file and imports that file into the new
instance's store.

Design notes, so a later reader can map the structure to its reasons:

- Only pinned memories move. The learned memory graph (``memory_claims`` and
  its tables) is derived state with embeddings tied to one embedding profile;
  it is out of scope and the new instance learns it again.
- The export reads an instance that usually runs an earlier version, whose
  configuration this version refuses and whose files may use the old layout.
  So it never uses the refusing loaders: ``custom_agents.pinned_memory_source``
  reads only the instance name, team id and storage settings from the old
  file, finds the database the way that version did (or takes ``--sqlite`` /
  ``--postgres-dsn-file``), and reads it through a read-only connection with no
  schema setup. It selects the same facts as ``list_unexpired_facts``: expired
  facts are not pruned (a write) and facts that start later are kept, so it is
  safe against a running instance. People the old instance removed from the
  team are left out: their memories are held only until deletion.
- The import writes to the new instance, which runs this version, so it uses
  the normal loaders and repositories (``open_memory_instance``).
- Person ids differ between instances, so a personal fact is imported only
  for a known target: an explicit ``--map-person OLD=NEW``, or the same id
  when the new instance's people records (the gateway's copy of the team's
  people, ``access_control.people_directory``) list that person as not
  removed. Anything else is refused and listed, never guessed.
- One planner (``plan_import``) decides every fact's outcome from a read-only
  snapshot of the new store, and both ``--dry-run`` and the real import use
  it, so a dry run shows exactly what the import would do. Writes then go
  through the repository's normal ``create_coremem``/``create_usermem``, so
  imported facts get the same ``create`` history event as new facts, the
  same source-key idempotency and the same capacity check at write time.
- Re-running is safe: a fact whose text (or source key) is already in the
  same bank for the same owner is skipped as already there.
- The importer is recorded as the author (``created_by`` = the memory
  import); the original author and creation time stay in the fact's metadata
  under ``imported_from``. This reserved record stays in storage; agent views
  and context hydration exclude it while retaining ordinary skill metadata.
  A fact already imported once keeps its first
  ``imported_from`` (the true origin).
- The file carries no secrets and no conversation, task, session or chat ids
  from the old instance (they point at records the new instance does not
  have). It does carry people's memories, so it is written readable only by
  its owner and never over an existing file.

Operator commands run on the host are not agent-callable operations, so they
have no tool-catalog level (as with ``access drop-old-tables``).
"""

from __future__ import annotations

import argparse
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Iterable, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from custom_agents.memory_plugins.config import normalize_memory_config
from custom_agents.pinned_memory_metadata import IMPORTED_FROM_KEY
from custom_agents.pinned_memory_source import (
    ExportSource,
    SourceOptions,
    read_source_memories,
    resolve_export_source,
)
from custom_agents.pinned_memory_runtime import (
    PINNED_MEMORY_BANKS,
    bank_capacity,
    normalize_bank_metadata,
    normalize_bank_tags,
)
from custom_messages.contracts.access_v2 import PersonStanding
from storage.backends import STORAGE_BACKEND_SQLITE
from storage.config import StorageSettings
from storage.pinned_memory_repository import (
    COREMEM_BANK,
    USERMEM_BANK,
    PinnedMemoryCapacityError,
    PinnedMemoryFactRow,
    PinnedMemoryRepository,
    create_pinned_memory_repository,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

EXPORT_FORMAT: Final = "universal-agents-pinned-memory"
EXPORT_SCHEMA_VERSION: Final = 1
IMPORT_ACTOR = {"kind": "system", "id": "memory_import", "display_name": "Memory import"}
_PREVIEW_CHARS = 60


# -- the file format ------------------------------------------------------------


class ExportedFactV1(BaseModel):
    """One pinned fact as the old instance held it."""

    model_config = ConfigDict(extra="forbid")

    bank: str
    person_id: Optional[str] = Field(default=None, min_length=1, max_length=240)
    memory_id: int = Field(ge=1, description="The fact's number in the old instance, for reference only")
    fact_text: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    valid_from_utc: str
    invalid_after_utc: Optional[str] = None
    source_key: Optional[str] = Field(default=None, max_length=240)
    created_by_kind: str
    created_by_id: Optional[str] = None
    created_by_display_name: Optional[str] = None
    created_at_utc: str
    updated_at_utc: str

    @field_validator("bank")
    @classmethod
    def _known_bank(cls, value: str) -> str:
        if value not in PINNED_MEMORY_BANKS:
            raise ValueError(f"bank must be {COREMEM_BANK} or {USERMEM_BANK}, not {value!r}")
        return value

    @model_validator(mode="after")
    def _person_only_for_usermem(self) -> "ExportedFactV1":
        if self.bank == USERMEM_BANK and not self.person_id:
            raise ValueError("a usermem fact needs person_id")
        if self.bank == COREMEM_BANK and self.person_id:
            raise ValueError("a coremem fact has no person_id")
        return self


class PinnedMemoryExportV1(BaseModel):
    """A memory export file (``assistant_cli memory export``)."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["universal-agents-pinned-memory"] = EXPORT_FORMAT
    schema_version: Literal[1] = EXPORT_SCHEMA_VERSION
    exported_at_utc: str
    # Null when the export could not tell: no --instance-name and no
    # configuration, or no team memory stored.
    source_instance: Optional[str] = None
    source_team_id: Optional[str] = None
    facts: list[ExportedFactV1] = Field(default_factory=list)


# -- opening an instance's store --------------------------------------------------


@dataclass(frozen=True)
class MemoryInstance:
    """What the export and import need from one instance's configuration."""

    instance_name: str
    team_id: str
    storage_settings: StorageSettings
    memory_config: dict[str, Any]


def open_memory_instance(config_path: str) -> MemoryInstance:
    """Read the new instance's ``instance_config.yaml`` or ``config.yaml`` the way the lead agent does.

    Only the import uses this: the new instance runs this version, so its file
    must pass the normal loaders. The memory settings (capacity, tag and
    metadata limits) and the storage come from the lead agent type's resolved
    settings, because the lead (manager) owns the pinned memory store.
    """

    from assistant_gateway.config import GatewayInstanceConfig
    from assistant_gateway.runtime_config import (
        build_runtime_config_from_instance_config,
        is_instance_config_document,
    )
    from config_loader import load_config, resolve_agent_type_settings
    from default_values import AGENT_DEFAULTS
    from storage.config import resolve_storage_settings

    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"There is no configuration file at {path}.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, dict) and is_instance_config_document(raw):
        instance_config = GatewayInstanceConfig.load_for_process(path)
        config = build_runtime_config_from_instance_config(instance_config)
        lead_type = instance_config.manager.named_agent_type
        instance_name = instance_config.instance_name
    else:
        config = load_config(str(path), search_default_paths=False)
        lead_type = AGENT_DEFAULTS.named_agent_type.value
        instance_name = path.parent.name
    runtime = resolve_agent_type_settings(config, lead_type)
    storage = resolve_storage_settings(
        {
            "backend": runtime["storage_backend"],
            "sqlite": {"path": runtime["storage_sqlite_path"]},
            "postgres": {"dsn": runtime["storage_postgres_dsn"]},
        },
        base_dir=runtime["instance_root"],
    )
    return MemoryInstance(
        instance_name=instance_name,
        team_id=str(config.get("agent.default_team_id") or AGENT_DEFAULTS.default_team_id.value),
        storage_settings=storage,
        memory_config=normalize_memory_config(runtime.get("memory_config")),
    )


def _people_standing(instance: MemoryInstance, team_id: str) -> Optional[dict[str, PersonStanding]]:
    """The gateway's copy of the team's people, or None if the gateway has not recorded them yet."""

    from access_control.people_directory import PeopleDirectory
    from storage.access_repository import AccessRepository

    directory = PeopleDirectory(AccessRepository(instance.storage_settings), team_id)
    if not directory.synced():
        return None
    return {
        person_id: person.standing
        for person_id, person in directory.people().items()
        if person.standing is not None
    }


# -- export -------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportSelection:
    """Which memories to export. Nothing chosen means the team's and everyone's."""

    team: bool = False
    people: tuple[str, ...] = ()
    all_people: bool = False

    def resolved(self) -> "ExportSelection":
        if self.team or self.people or self.all_people:
            return self
        return ExportSelection(team=True, all_people=True)

    def wants_person(self, person_id: str) -> bool:
        return self.all_people or person_id in self.people


@dataclass
class ExportResult:
    document: PinnedMemoryExportV1
    removed_people: list[str] = field(default_factory=list)
    people_without_facts: list[str] = field(default_factory=list)
    team_note: str = ""  # how the team id was chosen


def _exported_fact(row: PinnedMemoryFactRow) -> ExportedFactV1:
    return ExportedFactV1(
        bank=row.bank,
        person_id=row.canonical_user_id if row.bank == USERMEM_BANK else None,
        memory_id=row.memory_id,
        fact_text=row.fact_text,
        tags=row.tags,
        metadata=row.metadata,
        valid_from_utc=row.valid_from_utc,
        invalid_after_utc=row.invalid_after_utc,
        source_key=row.source_key or None,
        created_by_kind=row.created_by_kind,
        created_by_id=row.created_by_id,
        created_by_display_name=row.created_by_display_name,
        created_at_utc=row.created_at_utc,
        updated_at_utc=row.updated_at_utc,
    )


def build_export(
    rows: Iterable[PinnedMemoryFactRow],
    *,
    instance_name: Optional[str],
    team_id: Optional[str],
    selection: ExportSelection,
    removed_people: Iterable[str] = (),
    now_utc: Optional[str] = None,
) -> ExportResult:
    """Choose the facts to export from a read-only list of the old store's facts."""

    selection = selection.resolved()
    removed = set(removed_people)
    facts: list[ExportedFactV1] = []
    left_out: set[str] = set()
    people_with_facts: set[str] = set()
    for row in rows:
        if row.bank == COREMEM_BANK and selection.team and team_id and row.team_id == team_id:
            facts.append(_exported_fact(row))
        elif row.bank == USERMEM_BANK and selection.wants_person(row.canonical_user_id):
            if row.canonical_user_id in removed:
                left_out.add(row.canonical_user_id)
                continue
            people_with_facts.add(row.canonical_user_id)
            facts.append(_exported_fact(row))
    document = PinnedMemoryExportV1(
        exported_at_utc=now_utc or utc_now_iso(),
        source_instance=instance_name,
        source_team_id=team_id,
        facts=facts,
    )
    missing = [person for person in selection.people if person not in people_with_facts | left_out]
    return ExportResult(document, sorted(left_out), missing)


def _write_new_private_file(path: Path, text: str) -> None:
    """Write a file that must not exist yet, readable only by its owner."""

    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SystemExit(f"{path} already exists. Choose a new file name; the export never overwrites a file.") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def export_memories(
    source: ExportSource, out_path: str, selection: ExportSelection, *, team_id: Optional[str] = None
) -> ExportResult:
    """``memory export``: read the old instance's pinned memories and write the export file."""

    read = read_source_memories(source, team_id=team_id)
    result = build_export(
        read.facts,
        instance_name=source.instance_name,
        team_id=read.team_id,
        selection=selection,
        removed_people=read.removed_people,
    )
    result.team_note = read.team_note
    _write_new_private_file(Path(out_path), result.document.model_dump_json(indent=2) + "\n")
    logger.info(
        "Pinned memory export written. event=pinned_memory_export_finished instance=%s team_id=%s backend=%s "
        "facts=%s removed_people_left_out=%s",
        source.instance_name or "",
        read.team_id or "",
        source.backend,
        len(result.document.facts),
        len(result.removed_people),
    )
    return result


def _instance_label(name: Optional[str]) -> str:
    return f"instance {name!r}" if name else "an instance with no recorded name"


def export_summary(result: ExportResult, out_path: str) -> list[str]:
    facts = result.document.facts
    team_count = sum(1 for fact in facts if fact.bank == COREMEM_BANK)
    per_person = Counter(fact.person_id for fact in facts if fact.bank == USERMEM_BANK)
    lines = [
        f"Team id: {result.document.source_team_id or 'none'} ({result.team_note}).",
        f"Wrote {len(facts)} pinned memories from {_instance_label(result.document.source_instance)} to {out_path}.",
        f"  Team memory (coremem): {team_count}",
        f"  Personal memory (usermem): {sum(per_person.values())} for {len(per_person)} people",
    ]
    lines += [f"    {person}: {count}" for person, count in sorted(per_person.items())]
    if result.removed_people:
        lines.append(
            "Left out people removed from the team (their memories are held only until deletion): "
            + ", ".join(result.removed_people)
        )
    if result.people_without_facts:
        lines.append("No memories found for: " + ", ".join(result.people_without_facts))
    lines.append("The file holds people's memories: keep it private and delete it after the import.")
    return lines


# -- import ------------------------------------------------------------------------


class ImportOutcome(StrEnum):
    IMPORTED = "imported"
    ALREADY_THERE = "already there"
    NO_ROOM = "no room"
    NO_PERSON = "refused: no person to receive it"
    INVALID = "refused: breaks the new instance's limits"
    EXPIRED = "expired"


# Outcomes that mean a fact the owner wanted did not arrive.
NOT_IMPORTED = (ImportOutcome.NO_ROOM, ImportOutcome.NO_PERSON, ImportOutcome.INVALID)


@dataclass(frozen=True)
class PersonTargets:
    """Who each old person id becomes in the new instance.

    ``explicit`` holds ``--map-person OLD=NEW``. ``known`` is the new
    instance's people records (None when its gateway has not recorded them
    yet, in which case only explicit mappings are used and not checked).
    """

    explicit: dict[str, str]
    known: Optional[dict[str, PersonStanding]]

    def resolve(self, old_person: str) -> tuple[Optional[str], str]:
        target = self.explicit.get(old_person, old_person)
        if self.known is None:
            if old_person in self.explicit:
                return target, ""
            return None, (
                "no --map-person was given, and the new instance has no people records yet "
                "(start its gateway once, or map the person)"
            )
        standing = self.known.get(target)
        if standing is None:
            return None, f"{target} is not a person in the new instance's team"
        if standing is PersonStanding.REMOVED:
            return None, f"{target} was removed from the new instance's team"
        return target, ""


@dataclass(frozen=True)
class PlannedFact:
    fact: ExportedFactV1
    owner: str  # the new team id (coremem) or new person id (usermem); "" when refused
    outcome: ImportOutcome
    reason: str = ""
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Scope:
    """What one bank already holds for one owner, updated as the plan fills it."""

    capacity: int
    used: int = 0
    texts: set[str] = field(default_factory=set)
    source_keys: set[str] = field(default_factory=set)

    def add(self, text: str, source_key: Optional[str]) -> None:
        self.used += 1
        self.texts.add(text)
        if source_key:
            self.source_keys.add(source_key)


def _scope_key(bank: str, owner: str) -> tuple[str, str]:
    return bank, owner


def _row_owner(row: PinnedMemoryFactRow) -> str:
    return row.team_id if row.bank == COREMEM_BANK else row.canonical_user_id


def _existing_scopes(
    rows: Iterable[PinnedMemoryFactRow], memory_config: dict[str, Any]
) -> dict[tuple[str, str], _Scope]:
    scopes: dict[tuple[str, str], _Scope] = {}
    for row in rows:
        scope = scopes.setdefault(_scope_key(row.bank, _row_owner(row)), _Scope(bank_capacity(memory_config, row.bank)))
        scope.add(row.fact_text.strip(), row.source_key)
    return scopes


def _provenance(fact: ExportedFactV1, source_instance: Optional[str]) -> dict[str, Any]:
    """The small ``imported_from`` record kept in the fact's metadata."""

    record = {
        "instance": source_instance,
        "created_at_utc": fact.created_at_utc,
        "created_by_kind": fact.created_by_kind,
        "created_by_id": fact.created_by_id,
        "created_by_display_name": fact.created_by_display_name,
    }
    return {key: value for key, value in record.items() if value}


def _utc_datetime(value: str) -> datetime:
    """An ISO timestamp as an aware datetime; one without a time zone is taken as UTC, as the store writes UTC."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _is_expired(fact: ExportedFactV1, now: datetime) -> bool:
    if not fact.invalid_after_utc:
        return False
    return _utc_datetime(fact.invalid_after_utc) <= now


@dataclass(frozen=True)
class _PlanContext:
    document: PinnedMemoryExportV1
    team_id: str
    targets: PersonTargets
    memory_config: dict[str, Any]
    now: datetime


def _checked_payload(fact: ExportedFactV1, context: _PlanContext) -> tuple[list[str], dict[str, Any]]:
    """Tags and metadata checked against the new instance's limits, plus the import record."""

    tags = normalize_bank_tags(context.memory_config, fact.bank, fact.tags)
    metadata = normalize_bank_metadata(context.memory_config, fact.bank, fact.metadata)
    if IMPORTED_FROM_KEY not in metadata:
        metadata[IMPORTED_FROM_KEY] = _provenance(fact, context.document.source_instance)
    return tags, metadata


def _plan_one(fact: ExportedFactV1, context: _PlanContext, scopes: dict[tuple[str, str], _Scope]) -> PlannedFact:
    owner: Optional[str]
    if fact.bank == COREMEM_BANK:
        owner, reason = context.team_id, ""
    else:
        owner, reason = context.targets.resolve(str(fact.person_id))
    if owner is None:
        return PlannedFact(fact, "", ImportOutcome.NO_PERSON, reason)
    if _is_expired(fact, context.now):
        return PlannedFact(fact, owner, ImportOutcome.EXPIRED, "its end time has passed")
    scope = scopes.setdefault(_scope_key(fact.bank, owner), _Scope(bank_capacity(context.memory_config, fact.bank)))
    text = fact.fact_text.strip()
    if not text:
        # The writer trims facts too. Refuse this before allocating capacity
        # or starting a team import, so a bad edit cannot leave partial state.
        return PlannedFact(fact, owner, ImportOutcome.INVALID, "fact_text must be non-empty")
    if text in scope.texts or (fact.source_key and fact.source_key in scope.source_keys):
        return PlannedFact(fact, owner, ImportOutcome.ALREADY_THERE)
    try:
        tags, metadata = _checked_payload(fact, context)
    except ValueError as exc:
        return PlannedFact(fact, owner, ImportOutcome.INVALID, str(exc))
    if scope.used >= scope.capacity:
        return PlannedFact(fact, owner, ImportOutcome.NO_ROOM, f"the {fact.bank} bank is full ({scope.capacity} facts)")
    scope.add(text, fact.source_key)
    return PlannedFact(fact, owner, ImportOutcome.IMPORTED, "", tuple(tags), metadata)


def plan_import(
    document: PinnedMemoryExportV1,
    *,
    existing_rows: Iterable[PinnedMemoryFactRow],
    team_id: str,
    targets: PersonTargets,
    memory_config: dict[str, Any],
    now_utc: Optional[str] = None,
) -> list[PlannedFact]:
    """Decide each fact's outcome from a snapshot of the new store; changes nothing."""

    context = _PlanContext(
        document=document,
        team_id=team_id,
        targets=targets,
        memory_config=memory_config,
        now=_utc_datetime(now_utc or utc_now_iso()),
    )
    scopes = _existing_scopes(existing_rows, memory_config)
    return [_plan_one(fact, context, scopes) for fact in document.facts]


def _create(repository: PinnedMemoryRepository, planned: PlannedFact, capacity: int) -> None:
    fact = planned.fact
    common: dict[str, Any] = {
        "fact_text": fact.fact_text,
        "capacity": capacity,
        "valid_from_utc": fact.valid_from_utc,
        "invalid_after_utc": fact.invalid_after_utc,
        "tags": list(planned.tags),
        "metadata": planned.metadata,
        "source_key": fact.source_key,
        "source": {},
        "actor": dict(IMPORT_ACTOR),
    }
    if fact.bank == COREMEM_BANK:
        repository.create_coremem(team_id=planned.owner, **common)
    else:
        repository.create_usermem(canonical_user_id=planned.owner, **common)


def apply_import(
    repository: PinnedMemoryRepository, plan: list[PlannedFact], *, memory_config: dict[str, Any]
) -> list[PlannedFact]:
    """Write the planned facts; the store's own capacity check still has the last word."""

    applied: list[PlannedFact] = []
    for planned in plan:
        if planned.outcome is not ImportOutcome.IMPORTED:
            applied.append(planned)
            continue
        try:
            _create(repository, planned, bank_capacity(memory_config, planned.fact.bank))
        except PinnedMemoryCapacityError:
            # Only when the running instance added facts after the plan was made.
            logger.warning(
                "Pinned memory import found the bank full while writing. event=pinned_memory_import_no_room "
                "bank=%s owner=%s old_memory_id=%s",
                planned.fact.bank,
                planned.owner,
                planned.fact.memory_id,
            )
            applied.append(PlannedFact(planned.fact, planned.owner, ImportOutcome.NO_ROOM, "the bank filled up during the import"))
            continue
        applied.append(planned)
    return applied


def parse_person_map(pairs: Iterable[str]) -> dict[str, str]:
    """``OLD=NEW`` pairs from ``--map-person``."""

    mapping: dict[str, str] = {}
    for pair in pairs:
        old, sep, new = str(pair).partition("=")
        old, new = old.strip(), new.strip()
        if not sep or not old or not new:
            raise SystemExit(f"--map-person needs OLD=NEW, such as usr_old=usr_new; got {pair!r}.")
        if old in mapping and mapping[old] != new:
            raise SystemExit(f"--map-person gives {old} two different new ids.")
        mapping[old] = new
    return mapping


def read_export_file(path: str) -> PinnedMemoryExportV1:
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise SystemExit(f"There is no export file at {file_path}.")
    try:
        return PinnedMemoryExportV1.model_validate_json(file_path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise SystemExit(
            f"{file_path} is not a memory export this version can read "
            f"(format {EXPORT_FORMAT}, schema_version {EXPORT_SCHEMA_VERSION}):\n{exc}"
        ) from exc


@dataclass
class ImportRun:
    document: PinnedMemoryExportV1
    team_id: str
    outcomes: list[PlannedFact]
    dry_run: bool
    unused_mappings: list[str]
    people_records: bool


def import_memories(
    config_path: str,
    in_path: str,
    *,
    person_map: dict[str, str],
    team_id: Optional[str] = None,
    dry_run: bool = False,
) -> ImportRun:
    """``memory import``: plan against the new store and, unless a dry run, write the facts."""

    document = read_export_file(in_path)
    instance = open_memory_instance(config_path)
    target_team = str(team_id or instance.team_id).strip()
    if not target_team:
        raise SystemExit("The new instance has no team id; pass --team-id.")
    settings = instance.storage_settings
    if dry_run and settings.backend == STORAGE_BACKEND_SQLITE and not Path(str(settings.sqlite_path)).is_file():
        # A dry run must not create the new instance's database: it is empty and knows nobody.
        repository, existing, known = None, [], None
    else:
        repository = create_pinned_memory_repository(settings, logger=logger)
        existing, known = repository.list_unexpired_facts(), _people_standing(instance, target_team)
    plan = plan_import(
        document,
        existing_rows=existing,
        team_id=target_team,
        targets=PersonTargets(explicit=person_map, known=known),
        memory_config=instance.memory_config,
    )
    if dry_run or repository is None:
        outcomes = plan
    else:
        outcomes = apply_import(repository, plan, memory_config=instance.memory_config)
    exported_people = {fact.person_id for fact in document.facts if fact.person_id}
    counts = Counter(item.outcome.name for item in outcomes)
    logger.info(
        "Pinned memory import finished. event=pinned_memory_import_finished source_instance=%s team_id=%s "
        "dry_run=%s outcomes=%s",
        document.source_instance,
        target_team,
        dry_run,
        dict(sorted(counts.items())),
    )
    return ImportRun(
        document=document,
        team_id=target_team,
        outcomes=outcomes,
        dry_run=dry_run,
        unused_mappings=sorted(old for old in person_map if old not in exported_people),
        people_records=known is not None,
    )


def _owner_label(bank: str, owner: str, old_person: Optional[str]) -> str:
    if bank == COREMEM_BANK:
        return f"Team memory (coremem, team {owner})"
    if old_person and old_person != owner:
        return f"Person {owner} (was {old_person}) (usermem)"
    return f"Person {owner} (usermem)"


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _PREVIEW_CHARS else flat[: _PREVIEW_CHARS - 3] + "..."


def _count_lines(outcomes: list[PlannedFact]) -> list[str]:
    groups: dict[str, Counter[str]] = {}
    for item in outcomes:
        if not item.owner:
            continue
        label = _owner_label(item.fact.bank, item.owner, item.fact.person_id)
        groups.setdefault(label, Counter())[item.outcome.value] += 1
    return [
        f"  {label}: " + ", ".join(f"{count} {outcome}" for outcome, count in sorted(counter.items()))
        for label, counter in sorted(groups.items())
    ]


def _problem_lines(outcomes: list[PlannedFact]) -> list[str]:
    problems = [item for item in outcomes if item.outcome in NOT_IMPORTED]
    if not problems:
        return []
    lines = [f"Not imported ({len(problems)}):"]
    for item in problems:
        who = f"person {item.fact.person_id}" if item.fact.bank == USERMEM_BANK else "team"
        lines.append(
            f"  {item.fact.bank} #{item.fact.memory_id} ({who}) {item.outcome.value}: {item.reason}\n"
            f"    {_preview(item.fact.fact_text)}"
        )
    return lines


def import_summary(run: ImportRun) -> list[str]:
    verb = "Would import" if run.dry_run else "Imported"
    imported = sum(1 for item in run.outcomes if item.outcome is ImportOutcome.IMPORTED)
    lines = [
        f"{verb} {imported} of {len(run.outcomes)} pinned memories from "
        f"{_instance_label(run.document.source_instance)} (exported {run.document.exported_at_utc}) into team {run.team_id}.",
        *_count_lines(run.outcomes),
        *_problem_lines(run.outcomes),
    ]
    if run.unused_mappings:
        lines.append("These --map-person ids have no memories in the file: " + ", ".join(run.unused_mappings))
    if not run.people_records:
        lines.append(
            "The new instance's gateway has not recorded its people yet, so mapped person ids were not checked."
        )
    if run.dry_run:
        lines.append("Dry run: nothing was changed.")
    return lines


# -- command line -------------------------------------------------------------------


def add_memory_parsers(subparsers: Any) -> None:
    """The ``memory export`` and ``memory import`` subcommands of ``assistant_cli``."""

    memory = subparsers.add_parser(
        "memory",
        help=(
            "Export or import pinned memories (the team's and people's facts) to move them to a new "
            "instance; the learned memory graph is not included"
        ),
    )
    commands = memory.add_subparsers(dest="memory_command", required=True)
    export = commands.add_parser(
        "export",
        help="Write the old instance's pinned memories to a new JSON file (reads only; safe while it runs)",
        description=(
            "Read the old instance's pinned memories without changing anything and write them to a new JSON file. "
            "Name the old database with --sqlite or --postgres-dsn-file, or let --config find it. The old instance "
            "may run an earlier version: --config is only used to find its database (where that version kept it, "
            "including the old workspace/dbs/ folder) and its instance name; settings this version removed are "
            "ignored. If the old files are mounted somewhere else, for example read-only at /old in a container, "
            "paths under the recorded instance_root are read under the folder that holds --config. The team id "
            "comes from the stored team memory."
        ),
        epilog=(
            "With none of --team, --person or --all-people, the team's and every person's memory is exported. "
            "An instance name or team id the export cannot tell is recorded as null in the file."
        ),
    )
    export.add_argument(
        "--config",
        default=None,
        help="The old instance's instance_config.yaml or config.yaml, used only to find its database and name",
    )
    export.add_argument("--env", default=None, help="Optional .env file to load before connecting")
    database = export.add_mutually_exclusive_group()
    database.add_argument(
        "--sqlite", default=None, metavar="PATH", help="The old instance's manager database (task_history.db) to read"
    )
    database.add_argument(
        "--postgres-dsn-file",
        default=None,
        metavar="PATH",
        help="File holding the old instance's PostgreSQL connection string (for example a Docker secret)",
    )
    export.add_argument(
        "--team-id",
        default=None,
        help="The team whose team memory to export; needed only when the database holds several teams' team memory",
    )
    export.add_argument(
        "--instance-name",
        default=None,
        help="Name to record for the old instance (default: instance_name from --config, else none)",
    )
    export.add_argument("--out", required=True, help="New JSON file to write (never overwritten)")
    export.add_argument("--team", action="store_true", help="Include the team's memory (coremem)")
    people = export.add_mutually_exclusive_group()
    people.add_argument(
        "--person", action="append", default=[], metavar="PERSON_ID", help="Include this person's memory (repeatable)"
    )
    people.add_argument("--all-people", action="store_true", help="Include every person's memory")
    load = commands.add_parser(
        "import", help="Add the pinned memories from an export file to the new instance, skipping ones already there"
    )
    load.add_argument("--config", required=True, help="The new instance's instance_config.yaml or config.yaml")
    load.add_argument("--env", default=None, help="Optional .env file to load before connecting")
    load.add_argument("--in", dest="input_path", required=True, help="The export file")
    load.add_argument(
        "--map-person",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Import OLD person's memory for NEW person (repeatable); unmapped people keep their id "
        "only if the new instance knows them",
    )
    load.add_argument("--team-id", default=None, help="Team to receive the team memory (default: the new instance's team)")
    load.add_argument("--dry-run", action="store_true", help="Show what would be imported without changing anything")


def run_memory_command(args: argparse.Namespace) -> None:
    if args.memory_command == "export":
        selection = ExportSelection(team=bool(args.team), people=tuple(args.person), all_people=bool(args.all_people))
        source = resolve_export_source(
            SourceOptions(
                config_path=args.config,
                sqlite_path=args.sqlite,
                postgres_dsn_file=args.postgres_dsn_file,
                instance_name=args.instance_name,
            )
        )
        result = export_memories(source, args.out, selection, team_id=args.team_id)
        print("\n".join([f"Read the {source.describe()}.", *export_summary(result, args.out)]))
        return
    run = import_memories(
        args.config,
        args.input_path,
        person_map=parse_person_map(args.map_person),
        team_id=args.team_id,
        dry_run=bool(args.dry_run),
    )
    print("\n".join(import_summary(run)))
    if any(item.outcome in NOT_IMPORTED for item in run.outcomes):
        raise SystemExit(1)


__all__ = [
    "EXPORT_FORMAT",
    "EXPORT_SCHEMA_VERSION",
    "IMPORT_ACTOR",
    "IMPORTED_FROM_KEY",
    "ExportSelection",
    "ExportedFactV1",
    "ImportOutcome",
    "PersonTargets",
    "PinnedMemoryExportV1",
    "add_memory_parsers",
    "apply_import",
    "build_export",
    "export_memories",
    "import_memories",
    "plan_import",
    "run_memory_command",
]
