"""The ``control`` section: the team's control files (``InstanceLayout.control_dir``).

What it carries: TeamLead's ``SOUL.md``, the model overrides, the startup
roster, team templates, operator-written agent definitions and agent bundles,
Objective profile files, and any other file an operator keeps there. Parts of
``control/`` that other sections own are left to them: team skills
(``control/skills``) and team-added tools (``control/access``).

Specialists TeamLead generated (``control/agent_bundles/team_lead_generated/<type>``)
travel here too, each with its ``provisioning.json``. Their extra check is the
runtime's own rule (``custom_agents.generated_agent_runtime``): the record is
ready, belongs to this team and names this agent type, ``agent.yaml`` still
has the SHA-256 that was approved, and its default tools are the approved
ones. The approval reference inside the record is kept as it is; nothing at
runtime looks it up in the Inbox.

Files travel as units: one file, or a whole agent bundle folder (a bundle's
prompt files belong with its definition). Each unit is checked with this
version's own loader against a temporary copy of the carried files before
anything is written, so a unit this version refuses is listed with the
loader's reason and the instance keeps onboarding's default for it. A file
this section does not know is carried unchecked and flagged.

Control files are written only by trusted code (``InstanceLayout`` module
docstring); the import is such code, run by the operator on the host.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, ClassVar, Final

import yaml
from pydantic import BaseModel, Field

from runtime_settings.instance_layout import (
    ACCESS_DIRNAME,
    AGENT_BUNDLES_DIRNAME,
    AGENT_DEFINITIONS_DIRNAME,
    GENERATED_AGENT_BUNDLES_DIRNAME,
    LLM_OVERRIDES_FILENAME,
    OBJECTIVE_PROFILES_DIRNAME,
    SKILLS_DIRNAME,
    SOUL_FILENAME,
    TEAM_COMPOSITION_FILENAME,
    TEAM_TEMPLATES_FILENAME,
)
from team_transfer.container import TransferFileError, iter_files
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

# control/access holds team-added tools (team_tools section).
_OWNED_ELSEWHERE: Final = (PurePosixPath(SKILLS_DIRNAME), PurePosixPath(ACCESS_DIRNAME))
_SPECIALIST_RECORD: Final = "provisioning.json"
_SPECIALIST_CATALOG: Final = "agent.yaml"


class ControlKind(StrEnum):
    SOUL = "soul"
    LLM_OVERRIDES = "llm_overrides"
    TEAM_COMPOSITION = "team_composition"
    TEAM_TEMPLATES = "team_templates"
    AGENT_DEFINITIONS = "agent_definitions"
    AGENT_BUNDLE = "agent_bundle"
    SPECIALIST = "specialist"
    OBJECTIVE_PROFILE = "objective_profile"
    OTHER = "other"


class ControlFileV1(TransferModel):
    path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ControlUnitV1(TransferModel):
    """One file, or one agent bundle folder, relative to ``control/``."""

    unit: str = Field(min_length=1, max_length=1024)
    kind: ControlKind
    files: tuple[ControlFileV1, ...] = Field(min_length=1)


def _kind_of(relative: PurePosixPath) -> tuple[str, ControlKind]:
    """The unit a control file belongs to, and what kind of unit it is."""

    first = relative.parts[0]
    single = {
        SOUL_FILENAME: ControlKind.SOUL,
        LLM_OVERRIDES_FILENAME: ControlKind.LLM_OVERRIDES,
        TEAM_COMPOSITION_FILENAME: ControlKind.TEAM_COMPOSITION,
        TEAM_TEMPLATES_FILENAME: ControlKind.TEAM_TEMPLATES,
    }
    if len(relative.parts) == 1 and first in single:
        return first, single[first]
    if (
        relative.parts[:2] == (AGENT_BUNDLES_DIRNAME, GENERATED_AGENT_BUNDLES_DIRNAME)
        and len(relative.parts) > 3
    ):
        return "/".join(relative.parts[:3]), ControlKind.SPECIALIST
    if first == AGENT_BUNDLES_DIRNAME and len(relative.parts) > 2:
        return "/".join(relative.parts[:2]), ControlKind.AGENT_BUNDLE
    if first in {
        AGENT_DEFINITIONS_DIRNAME,
        AGENT_BUNDLES_DIRNAME,
    } and relative.suffix.lower() in {".yaml", ".yml"}:
        return str(relative), ControlKind.AGENT_DEFINITIONS
    if first == OBJECTIVE_PROFILES_DIRNAME and relative.suffix.lower() in {
        ".yaml",
        ".yml",
    }:
        return str(relative), ControlKind.OBJECTIVE_PROFILE
    return str(relative), ControlKind.OTHER


def _owned_elsewhere(relative: PurePosixPath) -> bool:
    return any(
        relative == owned or owned in relative.parents for owned in _OWNED_ELSEWHERE
    )


class ControlSection:
    name: ClassVar[str] = "control"
    schema_version: ClassVar[int] = 2
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = ControlUnitV1

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        control = source.layout.control_dir
        units: dict[str, tuple[ControlKind, list[ControlFileV1]]] = {}
        if control.is_dir():
            for path in iter_files(control):
                relative = PurePosixPath(path.relative_to(control).as_posix())
                if _owned_elsewhere(relative):
                    continue
                data = path.read_bytes()
                if relative == PurePosixPath(LLM_OVERRIDES_FILENAME):
                    data = _model_choices(source, data)
                    if data is None:
                        continue
                files.add(str(relative), data)
                unit, kind = _kind_of(relative)
                entry = ControlFileV1(
                    path=str(relative),
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
                units.setdefault(unit, (kind, []))[1].append(entry)
        return SectionExport(
            records=[
                ControlUnitV1(unit=unit, kind=kind, files=tuple(entries)).model_dump(
                    mode="json"
                )
                for unit, (kind, entries) in sorted(units.items())
            ]
        )

    def plan(
        self, records: SectionRecords, context: ImportContext
    ) -> list[PlannedRecord]:
        units = [ControlUnitV1.model_validate(record) for record in records]
        with tempfile.TemporaryDirectory(prefix="ua-control-check-") as scratch:
            shadow = Path(scratch) / "control"
            for unit in units:
                for entry in unit.files:
                    _write(shadow, entry.path, self._data(context, entry))
            planned = [
                self._check(unit, shadow, context.reader.manifest.team_id)
                for unit in units
            ]
            accepted = {
                record.key
                for record in planned
                if record.outcome is not RecordOutcome.REFUSED
            }
            # Validate references using only the definitions that will actually
            # be written. A refused bundle must not make a roster look valid.
            for unit in units:
                if unit.unit not in accepted:
                    for entry in unit.files:
                        (shadow / entry.path).unlink(missing_ok=True)
            known = _accepted_agent_types(units, accepted, shadow, context)
            for index, unit in enumerate(units):
                if unit.unit in accepted:
                    planned[index] = _check_control_references(
                        unit, planned[index], known, shadow
                    )
            return planned

    def _data(self, context: ImportContext, entry: ControlFileV1) -> bytes:
        data = context.file(self.name, entry.path)
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise TransferFileError(
                f"The control file {entry.path} does not match its record."
            )
        return data

    def _check(self, unit: ControlUnitV1, shadow: Path, team_id: str) -> PlannedRecord:
        if unit.kind is ControlKind.OTHER:
            return PlannedRecord(
                self.name,
                unit.unit,
                RecordOutcome.ATTENTION,
                "carried as it is; this version does not check this file",
            )
        try:
            if unit.kind is ControlKind.SPECIALIST:
                _check_specialist(shadow / unit.unit, team_id)
            else:
                _CHECKS[unit.kind](shadow / unit.unit, shadow)
        except (
            Exception
        ) as exc:  # each loader reports in its own way; the message is the reason
            return PlannedRecord(
                self.name,
                unit.unit,
                RecordOutcome.REFUSED,
                f"this version refuses it: {exc}",
            )
        return PlannedRecord(self.name, unit.unit, RecordOutcome.IMPORT)

    def apply(
        self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext
    ) -> None:
        accepted = {
            record.key for record in plan if record.outcome is not RecordOutcome.REFUSED
        }
        control = context.layout.control_dir
        for record in records:
            unit = ControlUnitV1.model_validate(record)
            if unit.unit not in accepted:
                continue
            for entry in unit.files:
                _write(control, entry.path, self._data(context, entry))
        logger.info(
            "Team control files imported. event=team_transfer_control_imported units=%s refused=%s",
            len(accepted),
            len(plan) - len(accepted),
        )


def _accepted_agent_types(units, accepted, shadow, context):
    from custom_agents.agent_catalog import load_agent_catalog

    paths = [
        shadow / unit.unit
        for unit in units
        if unit.unit in accepted
        and unit.kind
        in {
            ControlKind.AGENT_DEFINITIONS,
            ControlKind.AGENT_BUNDLE,
            ControlKind.SPECIALIST,
        }
    ]
    known = set(load_agent_catalog(paths, include_configured=False).agent_types)
    if context.existing:
        from team_transfer.access import known_agent_types

        known.update(known_agent_types(context))
    return known


def _check_control_references(unit, decision, known, shadow):
    if unit.kind is ControlKind.TEAM_COMPOSITION:
        from custom_agents.team_composition import load_team_composition

        roster = load_team_composition(shadow / unit.unit)
        referenced = {a.named_agent_type for a in roster.startup_agents.agents}
        referenced.update(roster.ephemeral_agents.agent_types)
        missing = referenced - known
        if missing:
            return PlannedRecord(
                "control",
                unit.unit,
                RecordOutcome.REFUSED,
                "agent definitions are missing or refused: "
                + ", ".join(sorted(missing)),
            )
    return decision


def _write(root: Path, relative: str, data: bytes) -> None:
    target = root / PurePosixPath(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _model_choices(source: ExportSource, data: bytes) -> bytes | None:
    """Carry edits to the overrides, not the model defaults bootstrap copied."""
    from assistant_gateway.bootstrap import render_bootstrap_control_file
    from team_transfer.settings import changed_settings, _set_path

    saved = yaml.safe_load(data) or {}
    baseline = (
        yaml.safe_load(
            render_bootstrap_control_file(source.config, LLM_OVERRIDES_FILENAME)
        )
        or {}
    )
    choices: dict[str, Any] = {}
    for path, value in changed_settings(saved, baseline):
        _set_path(choices, path, value)
    return yaml.safe_dump(choices, sort_keys=False).encode("utf-8") if choices else None


# -- checks with this version's loaders ---------------------------------------------


def _check_soul(path: Path, _shadow: Path) -> None:
    from custom_agents.runtime_setting_registry import MAX_SOUL_BYTES

    data = path.read_bytes()
    if len(data) > MAX_SOUL_BYTES:
        raise ValueError(f"SOUL.md is larger than {MAX_SOUL_BYTES} bytes")
    data.decode("utf-8")


def _check_llm_overrides(path: Path, _shadow: Path) -> None:
    from config_loader import validate_llm_override_payload

    result = validate_llm_override_payload(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    if not result["valid"]:
        raise ValueError("; ".join(result["errors"]))


def _check_team_composition(path: Path, _shadow: Path) -> None:
    from custom_agents.team_composition import load_team_composition

    load_team_composition(path)


def _check_team_templates(path: Path, shadow: Path) -> None:
    from custom_agents.team_templates import load_team_templates

    load_team_templates(str(shadow.parent), str(path))


def _check_agent_definitions(path: Path, _shadow: Path) -> None:
    from custom_agents.agent_catalog import load_agent_catalog

    load_agent_catalog([path], include_configured=False)


def _check_specialist(bundle: Path, team_id: str) -> None:
    """The runtime's rule for a generated specialist (``generated_agent_runtime``), applied before import."""

    from custom_agents.agent_catalog import load_agent_catalog
    from custom_agents.generated_agent_runtime import GeneratedAgentProvisioningRecordV1

    record = GeneratedAgentProvisioningRecordV1.model_validate_json(
        (bundle / _SPECIALIST_RECORD).read_text(encoding="utf-8")
    )
    catalog = bundle / _SPECIALIST_CATALOG
    if record.status != "ready":
        raise ValueError("its provisioning record is not ready")
    if record.team_id != team_id:
        raise ValueError(f"its provisioning record belongs to team {record.team_id}")
    if hashlib.sha256(catalog.read_bytes()).hexdigest() != record.catalog_sha256:
        raise ValueError("agent.yaml changed after the specialist was approved")
    definitions = load_agent_catalog([catalog], include_configured=False).agent_types
    definition = definitions.get(record.agent_type)
    if definition is None:
        raise ValueError(f"agent.yaml does not define {record.agent_type}")
    if tuple(definition.default_tools) != tuple(record.default_tools):
        raise ValueError("its default tools differ from the approved ones")


def _check_objective_profile(path: Path, _shadow: Path) -> None:
    from custom_agents.objective_work_config import load_objective_work_execution_config

    load_objective_work_execution_config([str(path)])


_CHECKS: dict[ControlKind, Callable[[Path, Path], Any]] = {
    ControlKind.SOUL: _check_soul,
    ControlKind.LLM_OVERRIDES: _check_llm_overrides,
    ControlKind.TEAM_COMPOSITION: _check_team_composition,
    ControlKind.TEAM_TEMPLATES: _check_team_templates,
    ControlKind.AGENT_DEFINITIONS: _check_agent_definitions,
    ControlKind.AGENT_BUNDLE: _check_agent_definitions,
    ControlKind.OBJECTIVE_PROFILE: _check_objective_profile,
}

__all__ = ["ControlFileV1", "ControlKind", "ControlSection", "ControlUnitV1"]
