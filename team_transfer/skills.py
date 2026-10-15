"""The ``skills`` section: the team's skills.

Two kinds travel:

- operator skills in ``control/skills/<name>`` (added by hand or synced from an
  external repository, with that sync's manifest file);
- reviewed installs in ``InstanceLayout.installed_skills_dir/<name>`` with the
  attestation trusted code wrote when a person approved the install
  (``custom_agents.skill_installation``).

An attestation binds the install to its exact files (``tree_sha256``) and to
its absolute folder (``installed_path``). The import keeps the first and
rewrites the second for the new root, then runs the runtime's own load-time
check (``verify_managed_skill_attestation_paths``) on the written skill, so an
install whose files do not match their approved digest is refused, never
re-approved by the move. Install plans, receipts, quarantine and removed
copies are history and stay behind.

Each file's executable bit is carried, because skills may bundle scripts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Final, Optional

from pydantic import BaseModel, Field, JsonValue

from runtime_settings.instance_layout import SKILLS_DIRNAME
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

ATTESTATIONS_DIRNAME: Final = "attestations"
SKILL_FILENAME: Final = "SKILL.md"
_INSTALLED_PREFIX: Final = "installed"


class SkillKind(StrEnum):
    OPERATOR = "operator"
    INSTALLED = "installed"


class SkillFileV1(TransferModel):
    path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    executable: bool = False


class SkillUnitV1(TransferModel):
    """One skill folder, or one loose file in ``control/skills`` (``name`` is its name there)."""

    name: str = Field(min_length=1, max_length=255)
    kind: SkillKind
    files: tuple[SkillFileV1, ...] = Field(min_length=1)
    # Reviewed installs only: the attestation as trusted code wrote it.
    attestation: Optional[dict[str, JsonValue]] = None

    @property
    def key(self) -> str:
        return f"{self.kind.value}/{self.name}"


def _paths(layout: Any) -> tuple[Path, Path, Path]:
    operator = layout.control_dir / SKILLS_DIRNAME
    installed = layout.installed_skills_dir
    return operator, installed, layout.skill_installation_dir / ATTESTATIONS_DIRNAME


class SkillsSection:
    name: ClassVar[str] = "skills"
    schema_version: ClassVar[int] = 1
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = SkillUnitV1

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        operator, installed, attestations = _paths(source.layout)
        units = [*self._export_root(operator, SkillKind.OPERATOR, files, None)]
        units += self._export_root(installed, SkillKind.INSTALLED, files, attestations)
        return SectionExport(records=[unit.model_dump(mode="json") for unit in units])

    def _export_root(
        self, root: Path, kind: SkillKind, files: SectionFiles, attestations: Optional[Path]
    ) -> list[SkillUnitV1]:
        if not root.is_dir():
            return []
        units = []
        for entry in sorted(root.iterdir()):
            if entry.is_symlink():
                raise TransferFileError(f"{entry} is a link; the export does not follow links.")
            members = list(iter_files(entry)) if entry.is_dir() else [entry]
            if not members:
                continue
            attestation = None
            if attestations is not None:
                attestation_path = attestations / f"{entry.name}.json"
                attestation = json.loads(attestation_path.read_text(encoding="utf-8")) if attestation_path.is_file() else None
            carried = [self._carry(path, entry, kind, files) for path in members]
            units.append(SkillUnitV1(name=entry.name, kind=kind, files=tuple(carried), attestation=attestation))
        return units

    def _carry(self, path: Path, entry: Path, kind: SkillKind, files: SectionFiles) -> SkillFileV1:
        relative = path.relative_to(entry).as_posix() if entry.is_dir() else ""
        data = path.read_bytes()
        files.add(_member(kind, entry.name, relative), data)
        return SkillFileV1(
            path=relative or ".",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            executable=bool(path.stat().st_mode & stat.S_IXUSR),
        )

    # -- import ----------------------------------------------------------------

    def plan(self, records: SectionRecords, context: ImportContext) -> list[PlannedRecord]:
        units = [SkillUnitV1.model_validate(record) for record in records]
        with tempfile.TemporaryDirectory(prefix="ua-skill-check-") as scratch:
            return [self._check(unit, context, Path(scratch)) for unit in units]

    def _check(self, unit: SkillUnitV1, context: ImportContext, scratch: Path) -> PlannedRecord:
        try:
            if unit.kind is SkillKind.INSTALLED:
                self._check_installed(unit, context, scratch)
            elif unit.files[0].path != "." and not any(entry.path == SKILL_FILENAME for entry in unit.files):
                return PlannedRecord(
                    self.name, unit.key, RecordOutcome.ATTENTION, f"carried as it is; it has no {SKILL_FILENAME}"
                )
        except (PermissionError, ValueError) as exc:
            return PlannedRecord(self.name, unit.key, RecordOutcome.REFUSED, str(exc))
        return PlannedRecord(self.name, unit.key, RecordOutcome.IMPORT)

    def _check_installed(self, unit: SkillUnitV1, context: ImportContext, scratch: Path) -> None:
        """The runtime's load-time check, run on a scratch copy laid out like the new instance."""

        from custom_agents.skill_installation import verify_managed_skill_attestation_paths

        if unit.attestation is None:
            raise ValueError("a reviewed install without its attestation cannot be trusted")
        installed = scratch / unit.name / "installed"
        attestations = scratch / unit.name / ATTESTATIONS_DIRNAME
        self._write_unit(unit, context, installed, attestations)
        verify_managed_skill_attestation_paths(
            skill_path=installed / unit.name, installed_root=installed, attestations_root=attestations
        )

    def apply(self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext) -> None:
        from custom_agents.skill_installation import verify_managed_skill_attestation_paths

        accepted = {record.key for record in plan if record.outcome is not RecordOutcome.REFUSED}
        operator, installed, attestations = _paths(context.layout)
        for record in records:
            unit = SkillUnitV1.model_validate(record)
            if unit.key not in accepted:
                continue
            if unit.kind is SkillKind.INSTALLED:
                self._write_unit(unit, context, installed, attestations)
                verify_managed_skill_attestation_paths(
                    skill_path=installed / unit.name, installed_root=installed, attestations_root=attestations
                )
            else:
                self._write_files(unit, context, operator)
        logger.info(
            "Team skills imported. event=team_transfer_skills_imported skills=%s refused=%s",
            len(accepted),
            len(plan) - len(accepted),
        )

    def _write_unit(self, unit: SkillUnitV1, context: ImportContext, installed: Path, attestations: Path) -> None:
        self._write_files(unit, context, installed)
        attestation = dict(unit.attestation or {})
        attestation["installed_path"] = str((installed / unit.name).resolve())
        attestations.mkdir(parents=True, exist_ok=True)
        target = attestations / f"{unit.name}.json"
        if target.exists():
            target.chmod(0o600)
        target.write_text(json.dumps(attestation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # Trusted code writes attestations read-only (atomic_write_bytes immutable=True).
        target.chmod(0o444)

    def _write_files(self, unit: SkillUnitV1, context: ImportContext, root: Path) -> None:
        destination = root / unit.name
        if destination.exists():
            shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
        for entry in unit.files:
            data = context.file(self.name, _member(unit.kind, unit.name, "" if entry.path == "." else entry.path))
            if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise TransferFileError(f"The skill file {unit.key}/{entry.path} does not match its record.")
            target = destination if entry.path == "." else destination / PurePosixPath(entry.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if entry.executable:
                os.chmod(target, target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _member(kind: SkillKind, name: str, relative: str) -> str:
    base = f"{SKILLS_DIRNAME}/{name}" if kind is SkillKind.OPERATOR else f"{_INSTALLED_PREFIX}/{name}"
    return f"{base}/{relative}" if relative else base


__all__ = ["SkillFileV1", "SkillKind", "SkillUnitV1", "SkillsSection"]
