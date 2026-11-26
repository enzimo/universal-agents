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
import secrets
import shutil
import stat
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Final, Iterator, Optional

from pydantic import BaseModel, Field, JsonValue, field_validator, model_validator

from runtime_settings.instance_layout import SKILLS_DIRNAME
from team_transfer.container import TransferFileError, check_member_name, iter_files
from team_transfer.format import TransferModel
from team_transfer.sections import (
    TEAM_ID_RENAMES,
    ExportSource,
    ImportContext,
    PlannedRecord,
    RecordOutcome,
    SectionExport,
    SectionFiles,
    SectionRecords,
    holds_no_team_id,
)

logger = logging.getLogger(__name__)

ATTESTATIONS_DIRNAME: Final = "attestations"
SKILL_FILENAME: Final = "SKILL.md"
_INSTALLED_PREFIX: Final = "installed"
# Leading-dot sibling names follow the existing .incoming-* / .ua-*-backup-* conventions
# so a killed import never looks like a skill folder to the loader or the next export.
_STAGING_PREFIX: Final = ".incoming-transfer-"
_BACKUP_PREFIX: Final = ".ua-transfer-backup-"
_TRANSFER_TEMP_PREFIXES: Final = (_STAGING_PREFIX, _BACKUP_PREFIX)


def _validate_skill_name(value: str) -> str:
    if "/" in value:
        raise ValueError(f"Skill name must be a single folder name: {value!r}.")
    try:
        check_member_name(value)
    except TransferFileError as exc:
        raise ValueError(f"Skill name must be a single safe folder name: {value!r}.") from exc
    return value


def _validate_skill_file_path(value: str) -> str:
    if value == ".":
        return value
    try:
        check_member_name(value)
    except TransferFileError as exc:
        raise ValueError(
            f"Skill file path must be a normalized relative POSIX path: {value!r}."
        ) from exc
    return value


def _validate_unique_skill_paths(key: str, files: tuple["SkillFileV1", ...]) -> None:
    if any(entry.path == "." for entry in files):
        if len(files) != 1:
            raise ValueError("A loose skill file must have '.' as its only file path.")
        return
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()
    for entry in files:
        path = entry.path
        if path in seen_files:
            raise ValueError(f"Skill {key} contains duplicate file path: {path!r}.")
        if path in seen_dirs:
            raise ValueError(f"Skill {key} uses {path!r} as both a file and a folder.")
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in seen_files:
                raise ValueError(f"Skill {key} places {path!r} beneath a file path.")
            seen_dirs.add(parent)
        seen_files.add(path)


class SkillKind(StrEnum):
    OPERATOR = "operator"
    INSTALLED = "installed"


class SkillFileV1(TransferModel):
    path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    executable: bool = False

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _validate_skill_file_path(value)


class SkillUnitV1(TransferModel):
    """One skill folder, or one loose file in ``control/skills`` (``name`` is its name there)."""

    name: str = Field(min_length=1, max_length=255)
    kind: SkillKind
    files: tuple[SkillFileV1, ...] = Field(min_length=1)
    # Reviewed installs only: the attestation as trusted code wrote it.
    attestation: Optional[dict[str, JsonValue]] = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _validate_skill_name(value)

    @model_validator(mode="after")
    def _check_files(self) -> "SkillUnitV1":
        _validate_unique_skill_paths(self.key, self.files)
        return self

    @property
    def key(self) -> str:
        return f"{self.kind.value}/{self.name}"


def _paths(layout: Any) -> tuple[Path, Path, Path]:
    operator = layout.control_dir / SKILLS_DIRNAME
    installed = layout.installed_skills_dir
    return operator, installed, layout.skill_installation_dir / ATTESTATIONS_DIRNAME


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _swap_path(staging: Path, destination: Path) -> Path | None:
    """Move ``staging`` into ``destination``, keeping a sibling backup when replacing."""

    backup: Path | None = None
    if destination.exists() or destination.is_symlink():
        backup = destination.parent / f"{_BACKUP_PREFIX}{secrets.token_hex(8)}"
        os.rename(destination, backup)
    try:
        os.rename(staging, destination)
    except BaseException:
        if backup is not None and (backup.exists() or backup.is_symlink()):
            try:
                os.rename(backup, destination)
            except OSError:
                logger.warning(
                    "Could not restore backed-up skill after failed swap. "
                    "event=team_transfer_skill_rollback_failed backup=%s destination=%s",
                    backup,
                    destination,
                    exc_info=True,
                )
        raise
    return backup


def _rollback_swap(destination: Path, backup: Path | None) -> None:
    """Undo a completed swap by removing ``destination`` and restoring ``backup``."""

    try:
        _remove_path(destination)
        if backup is not None and (backup.exists() or backup.is_symlink()):
            os.rename(backup, destination)
    except OSError:
        logger.warning(
            "Could not restore backed-up skill during rollback. "
            "event=team_transfer_skill_rollback_failed backup=%s destination=%s",
            backup,
            destination,
            exc_info=True,
        )


def _rollback_attestation(attestation_path: Path, previous_bytes: bytes | None) -> None:
    """Restore the prior attestation bytes, or remove a newly written one when none existed."""

    from custom_agents.governance.authorized_operations import atomic_write_bytes

    try:
        if previous_bytes is not None:
            atomic_write_bytes(attestation_path, previous_bytes, immutable=True)
        else:
            attestation_path.unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "Could not restore skill attestation during rollback. "
            "event=team_transfer_skill_attestation_rollback_failed attestation=%s",
            attestation_path,
            exc_info=True,
        )


def _should_skip_export_entry(entry: Path, kind: SkillKind) -> bool:
    if kind is SkillKind.INSTALLED and (not entry.is_dir() or entry.name.startswith(".")):
        logger.warning(
            "Skipping non-skill entry in installed skills folder during export. "
            "event=team_transfer_installed_skill_entry_skipped entry=%r",
            entry.name,
        )
        return True
    if kind is SkillKind.OPERATOR and entry.name.startswith(_TRANSFER_TEMP_PREFIXES):
        logger.warning(
            "Skipping leftover transfer staging entry in operator skills folder during export. "
            "event=team_transfer_operator_skill_entry_skipped entry=%r",
            entry.name,
        )
        return True
    return False


def _require_installed_unit_shape(unit: SkillUnitV1) -> dict[str, JsonValue]:
    from custom_agents.skill_repository import is_valid_skill_name

    if not is_valid_skill_name(unit.name):
        raise ValueError(
            f"Installed skill name {unit.name!r} is not a valid managed skill name."
        )
    if unit.files[0].path == ".":
        raise ValueError("A reviewed skill install must be a folder, not a loose file.")
    if unit.attestation is None:
        raise ValueError("a reviewed install without its attestation cannot be trusted")
    return unit.attestation


def _checked_destination(root: Path, unit: SkillUnitV1) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    destination = root / unit.name
    if destination.parent.resolve() != root.resolve():
        raise TransferFileError(f"Skill name escapes its folder: {unit.key}.")
    return destination


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
            if _should_skip_export_entry(entry, kind):
                continue
            members = list(iter_files(entry)) if entry.is_dir() else [entry]
            if not members:
                continue
            attestation = None
            if attestations is not None:
                attestation_path = attestations / f"{entry.name}.json"
                attestation = (
                    json.loads(attestation_path.read_text(encoding="utf-8"))
                    if attestation_path.is_file()
                    else None
                )
            carried = [self._carry(path, entry, kind, files) for path in members]
            units.append(
                SkillUnitV1(
                    name=entry.name, kind=kind, files=tuple(carried), attestation=attestation
                )
            )
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

    def _read_verified_file(
        self, unit: SkillUnitV1, entry: SkillFileV1, context: ImportContext
    ) -> bytes:
        member = _member(unit.kind, unit.name, "" if entry.path == "." else entry.path)
        data = context.file(self.name, member)
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise TransferFileError(
                f"The skill file {unit.key}/{entry.path} does not match its record."
            )
        return data

    def _iter_verified_files(
        self, unit: SkillUnitV1, context: ImportContext
    ) -> Iterator[tuple[SkillFileV1, bytes]]:
        for entry in unit.files:
            yield entry, self._read_verified_file(unit, entry, context)

    def plan(self, records: SectionRecords, context: ImportContext) -> list[PlannedRecord]:
        units = [SkillUnitV1.model_validate(record) for record in records]
        return [self._check(unit, context) for unit in units]

    def _check(self, unit: SkillUnitV1, context: ImportContext) -> PlannedRecord:
        from custom_agents.skill_repository import SkillRepositoryError

        try:
            if unit.kind is SkillKind.INSTALLED:
                self._check_installed(unit, context)
            else:
                self._check_operator(unit, context)
                if unit.files[0].path != "." and not any(
                    entry.path == SKILL_FILENAME for entry in unit.files
                ):
                    return PlannedRecord(
                        self.name,
                        unit.key,
                        RecordOutcome.ATTENTION,
                        f"carried as it is; it has no {SKILL_FILENAME}",
                    )
        except (PermissionError, ValueError, SkillRepositoryError) as exc:
            return PlannedRecord(self.name, unit.key, RecordOutcome.REFUSED, str(exc))
        return PlannedRecord(self.name, unit.key, RecordOutcome.IMPORT)

    def _check_operator(self, unit: SkillUnitV1, context: ImportContext) -> None:
        for _entry, _data in self._iter_verified_files(unit, context):
            pass

    def _check_installed(self, unit: SkillUnitV1, context: ImportContext) -> None:
        """Run the runtime's load-time check in memory without touching disk."""

        from custom_agents.skill_installation import verify_managed_skill_attestation_digest
        from custom_agents.skill_repository import build_skill_tree_manifest_from_files

        attestation = _require_installed_unit_shape(unit)
        manifest = build_skill_tree_manifest_from_files(
            (entry.path, data) for entry, data in self._iter_verified_files(unit, context)
        )
        verify_managed_skill_attestation_digest(
            skill_name=unit.name,
            attestation=attestation,
            tree_sha256=manifest.tree_sha256,
        )

    def apply(
        self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext
    ) -> None:
        accepted = {
            record.key for record in plan if record.outcome is not RecordOutcome.REFUSED
        }
        operator, installed, attestations = _paths(context.layout)
        for record in records:
            unit = SkillUnitV1.model_validate(record)
            if unit.key not in accepted:
                continue
            if unit.kind is SkillKind.INSTALLED:
                self._write_unit(unit, context, installed, attestations)
            else:
                self._write_files(unit, context, operator)
        logger.info(
            "Team skills imported. event=team_transfer_skills_imported skills=%s refused=%s",
            len(accepted),
            len(plan) - len(accepted),
        )

    def _stage_skill_dir(
        self, unit: SkillUnitV1, context: ImportContext, root: Path
    ) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        staging = root / f"{_STAGING_PREFIX}{secrets.token_hex(8)}"
        os.mkdir(staging)
        staging_resolved = staging.resolve()
        try:
            for entry, data in self._iter_verified_files(unit, context):
                target = staging / PurePosixPath(entry.path)
                if staging_resolved not in target.resolve().parents:
                    raise TransferFileError(
                        f"Skill file path escapes its folder: {unit.key}/{entry.path}."
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "xb") as handle:
                    handle.write(data)
                if entry.executable:
                    os.chmod(
                        target,
                        target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
                    )
        except BaseException:
            _remove_path(staging)
            raise
        return staging

    def _write_loose_file(
        self, unit: SkillUnitV1, context: ImportContext, root: Path
    ) -> None:
        entry = unit.files[0]
        data = self._read_verified_file(unit, entry, context)
        destination = _checked_destination(root, unit)
        staging = root / f"{_STAGING_PREFIX}{secrets.token_hex(8)}"
        backup: Path | None = None
        try:
            with open(staging, "xb") as handle:
                handle.write(data)
            if entry.executable:
                os.chmod(
                    staging,
                    staging.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
                )
            if destination.is_dir() and not destination.is_symlink():
                backup = _swap_path(staging, destination)
            else:
                os.replace(staging, destination)
        finally:
            _remove_path(staging)
        if backup is not None:
            _remove_path(backup)

    def _write_unit(
        self,
        unit: SkillUnitV1,
        context: ImportContext,
        installed: Path,
        attestations: Path,
    ) -> None:
        from custom_agents.governance.authorized_operations import atomic_write_bytes
        from custom_agents.skill_installation import (
            verify_managed_skill_attestation_digest,
            verify_managed_skill_attestation_paths,
        )
        from custom_agents.skill_repository import build_skill_tree_manifest

        raw_attestation = _require_installed_unit_shape(unit)
        destination = _checked_destination(installed, unit)
        staging = self._stage_skill_dir(unit, context, installed)
        attestation_path = attestations / f"{unit.name}.json"
        previous_attestation: bytes | None = None
        attestation_written = False
        try:
            staged_manifest = build_skill_tree_manifest(staging)
            verify_managed_skill_attestation_digest(
                skill_name=unit.name,
                attestation=raw_attestation,
                tree_sha256=staged_manifest.tree_sha256,
            )
            attestation = dict(raw_attestation)
            attestation["installed_path"] = str(destination.resolve())
            attestation_bytes = (
                json.dumps(attestation, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")

            if attestation_path.is_file() and not attestation_path.is_symlink():
                previous_attestation = attestation_path.read_bytes()

            backup = _swap_path(staging, destination)
            try:
                atomic_write_bytes(attestation_path, attestation_bytes, immutable=True)
                attestation_written = True
                verify_managed_skill_attestation_paths(
                    skill_path=destination,
                    installed_root=installed,
                    attestations_root=attestations,
                )
            except BaseException:
                _rollback_swap(destination, backup)
                if attestation_written:
                    _rollback_attestation(attestation_path, previous_attestation)
                raise
            if backup is not None:
                _remove_path(backup)
        finally:
            _remove_path(staging)

    def _write_files(self, unit: SkillUnitV1, context: ImportContext, root: Path) -> None:
        if unit.files[0].path == ".":
            self._write_loose_file(unit, context, root)
            return
        destination = _checked_destination(root, unit)
        staging = self._stage_skill_dir(unit, context, root)
        backup: Path | None = None
        try:
            backup = _swap_path(staging, destination)
        finally:
            _remove_path(staging)
        if backup is not None:
            _remove_path(backup)


def _member(kind: SkillKind, name: str, relative: str) -> str:
    base = f"{SKILLS_DIRNAME}/{name}" if kind is SkillKind.OPERATOR else f"{_INSTALLED_PREFIX}/{name}"
    return f"{base}/{relative}" if relative else base


# Skill folders and their install records name no team.
TEAM_ID_RENAMES[SkillsSection.name] = holds_no_team_id

__all__ = ["SkillFileV1", "SkillKind", "SkillUnitV1", "SkillsSection"]
