"""The ``team_tools`` section: web API tools and CLIs the team added to its tool catalog.

An administrator-approved access proposal writes each added tool as one file
in the instance's catalog folder (``access_catalog.instance_tools``). A CLI's
entry pins its program (``installed_cli.binary_path`` under ``workspaces/``,
``installed_cli.sha256``), so the program travels with its entry: the team
would otherwise hold access to a tool whose program is gone.

Each file is checked by loading it into a copy of this version's own catalog
(``access_catalog.catalog.load_catalog``), which refuses tool ids that clash
with release tools and fields this version no longer accepts; each program
must still have its approved SHA-256. The approval itself is not repeated:
it was the administrator's decision on the old instance, and the import is
run by the operator.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import stat
import tempfile
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Final, Optional

import yaml
from pydantic import BaseModel, Field

from team_transfer.container import TransferFileError
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

_PROGRAMS_PREFIX: Final = "programs/"
_CATALOG_PREFIX: Final = "catalog/"


class ToolProgramV1(TransferModel):
    """An installed CLI's program, at its path under ``workspaces/``."""

    binary_path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class TeamToolFileV1(TransferModel):
    """One catalog file the team added, and the programs its CLIs need."""

    file_name: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._-]+\.ya?ml$")
    tool_ids: tuple[str, ...] = ()
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    programs: tuple[ToolProgramV1, ...] = ()


def _tools_dir(layout: Any) -> Path:
    return Path(layout.instance_tools_dir)


def _entries(data: bytes) -> list[dict[str, Any]]:
    document = yaml.safe_load(data.decode("utf-8")) or {}
    tools = document.get("tools") if isinstance(document, dict) else None
    return [entry for entry in tools or [] if isinstance(entry, dict)]


class TeamToolsSection:
    name: ClassVar[str] = "team_tools"
    schema_version: ClassVar[int] = 1
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = TeamToolFileV1

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        directory = _tools_dir(source.layout)
        records = []
        for path in sorted(directory.glob("*.yaml")) if directory.is_dir() else []:
            if path.is_symlink() or not path.is_file():
                raise TransferFileError(f"{path} is not a regular file; the export does not follow links.")
            data = path.read_bytes()
            files.add(_CATALOG_PREFIX + path.name, data)
            entries = _entries(data)
            programs = [self._carry_program(entry, source, files) for entry in entries if "installed_cli" in entry]
            records.append(
                TeamToolFileV1(
                    file_name=path.name,
                    tool_ids=tuple(str(entry.get("tool_id", "")) for entry in entries),
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    programs=tuple(program for program in programs if program is not None),
                ).model_dump(mode="json")
            )
        return SectionExport(records=records)

    def _carry_program(self, entry: dict[str, Any], source: ExportSource, files: SectionFiles) -> Optional[ToolProgramV1]:
        binary_path = str((entry.get("installed_cli") or {}).get("binary_path") or "")
        program = source.layout.workspaces_dir / PurePosixPath(binary_path)
        if not binary_path or program.is_symlink() or not program.is_file():
            # The import refuses the entry; the operator sees it there.
            logger.warning(
                "An installed CLI's program is missing at export. event=team_transfer_cli_program_missing "
                "tool_id=%s binary_path=%s",
                entry.get("tool_id"),
                binary_path,
            )
            return None
        data = program.read_bytes()
        files.add(_PROGRAMS_PREFIX + binary_path, data)
        return ToolProgramV1(binary_path=binary_path, size=len(data), sha256=hashlib.sha256(data).hexdigest())

    # -- import ----------------------------------------------------------------

    def plan(self, records: SectionRecords, context: ImportContext) -> list[PlannedRecord]:
        return [self._check(TeamToolFileV1.model_validate(record), context) for record in records]

    def _check(self, record: TeamToolFileV1, context: ImportContext) -> PlannedRecord:
        key = ",".join(record.tool_ids) or record.file_name
        data = self._data(context, _CATALOG_PREFIX + record.file_name, record.size, record.sha256)
        try:
            _check_catalog_file(record.file_name, data)
            programs = {program.binary_path: program for program in record.programs}
            for entry in _entries(data):
                cli = entry.get("installed_cli")
                if cli is None:
                    continue
                program = programs.get(str(cli.get("binary_path") or ""))
                if program is None:
                    raise ValueError(f"the program of {entry.get('tool_id')} was missing on the old instance")
                if program.sha256 != cli.get("sha256"):
                    raise ValueError(f"the program of {entry.get('tool_id')} is not the approved one")
        except Exception as exc:  # the catalog reports in its own way; the message is the reason
            return PlannedRecord(self.name, key, RecordOutcome.REFUSED, f"this version refuses it: {exc}")
        return PlannedRecord(self.name, key, RecordOutcome.IMPORT)

    def _data(self, context: ImportContext, member: str, size: int, sha256: str) -> bytes:
        data = context.file(self.name, member)
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise TransferFileError(f"The team tool file {member} does not match its record.")
        return data

    def apply(self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext) -> None:
        accepted = {record.key for record in plan if record.outcome is not RecordOutcome.REFUSED}
        directory = _tools_dir(context.layout)
        for raw in records:
            record = TeamToolFileV1.model_validate(raw)
            if (",".join(record.tool_ids) or record.file_name) not in accepted:
                continue
            for program in record.programs:
                data = self._data(context, _PROGRAMS_PREFIX + program.binary_path, program.size, program.sha256)
                _write_new(context.layout.workspaces_dir / PurePosixPath(program.binary_path), data, executable=True)
            data = self._data(context, _CATALOG_PREFIX + record.file_name, record.size, record.sha256)
            _write_new(directory / record.file_name, data, executable=False)
        logger.warning(
            "Team-added tools were imported into the catalog. event=team_transfer_tools_imported files=%s refused=%s",
            len(accepted),
            len(plan) - len(accepted),
        )


@lru_cache(maxsize=1)
def _release_catalog() -> Any:
    from access_catalog.catalog import load_catalog

    return load_catalog(None)


def _check_catalog_file(file_name: str, data: bytes) -> Any:
    """Load the file into a copy of this version's catalog, as every process does at start."""

    candidate = copy.deepcopy(_release_catalog())
    with tempfile.TemporaryDirectory(prefix="ua-tool-check-") as scratch:
        path = Path(scratch) / file_name
        path.write_bytes(data)
        candidate.load_file(path, instance=True)
    candidate.validate()
    return candidate


def accepted_tool_definitions(context: ImportContext) -> dict[str, Any]:
    """The accepted catalog files, parsed by the same loader used to validate them.

    Planning must know their operations before writing the destination, so
    access review can warn when an imported block will cover a whole tool.
    Baseline files belong to earlier batches of this same signed transfer.
    """

    accepted = context.accepted("team_tools")
    definitions = {}
    for baseline, records in ((True, context.baseline.get("team_tools", ())), (False, context.records("team_tools"))):
        for raw in records:
            record = TeamToolFileV1.model_validate(raw)
            if (",".join(record.tool_ids) or record.file_name) not in accepted:
                continue
            data = context.file("team_tools", _CATALOG_PREFIX + record.file_name, baseline=baseline)
            catalog = _check_catalog_file(record.file_name, data)
            definitions.update({key: tool for key, tool in catalog.tools.items() if key not in _release_catalog().tools})
    return definitions


def _write_new(path: Path, data: bytes, *, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    if executable:
        os.chmod(path, path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


__all__ = ["TeamToolFileV1", "TeamToolsSection", "ToolProgramV1", "accepted_tool_definitions"]
