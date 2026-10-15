"""Later batches use the same signed backup and never overwrite imported records."""

from __future__ import annotations

import json
import os
import fcntl
from contextlib import contextmanager
from typing import Any

from team_transfer.errors import TransferError
from team_transfer.sections import ExportSource, ImportContext
from team_transfer.selection import keyed_records


def previous_records(context: ImportContext) -> dict[str, set[str]]:
    path = context.layout.team_import_receipt_path
    if not path.is_file():
        raise TransferError(
            "Later batches require a completed team import from this backup."
        )
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("source_mac") != context.reader.manifest.mac:
        raise TransferError(
            "This instance's earlier import used a different backup. Use that same transfer file."
        )
    return {section: set(keys) for section, keys in receipt["records"].items()}


class _SnapshotFiles:
    def __init__(self, section: str, context: ImportContext):
        self.section = section
        self.context = context

    def add(self, relative: str, data: bytes) -> str:
        from team_transfer.format import section_files_prefix

        name = section_files_prefix(self.section) + relative
        self.context.baseline_files[name] = data
        return name


@contextmanager
def import_lock(layout):
    """Keep the batch receipt and its writes under one operator lock."""
    path = layout.team_import_lock_path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TransferError(
                "Another import is in progress for this instance. Finish it before starting this batch."
            ) from exc
        yield
    finally:
        os.close(fd)


def snapshot_target(context: ImportContext, sections) -> None:
    """Read present records with this release's exporters, without an archive or secrets."""
    from team_transfer.settings import planned_config, storage_environment
    from storage.read_only import read_only_repositories

    config = context.scratch.get("settings.original_config") or planned_config(context)
    source = ExportSource(
        config, context.layout, config.gateway.team_id, config.instance_name
    )
    with storage_environment(context), read_only_repositories():
        for section in sections:
            if section.encrypted or section.name == "settings":
                continue
            records = section.export(
                source, _SnapshotFiles(section.name, context)
            ).records
            context.baseline[section.name] = records
            context.baseline_keys[section.name] = set(
                keyed_records(section.name, records)
            )


@contextmanager
def stopped_instance(layout):
    """The supervisor's lock excludes a running runtime and competing imports."""
    from runtime_hosting.process_run import ProcessRunBusy, acquire_process_run

    try:
        lock = acquire_process_run(layout.process_run_lock_path)
    except ProcessRunBusy as exc:
        raise TransferError(
            "Stop this instance before importing settings, schedules, or memories."
        ) from exc
    if not lock.proven:
        if lock.fd is not None:
            os.close(lock.fd)
        raise TransferError(
            "This filesystem cannot prove the instance is stopped; import requires a working file lock."
        )
    try:
        yield
    finally:
        if lock.fd is not None:
            os.close(lock.fd)


def receipt(context: ImportContext, imported: dict[str, list[str]]) -> dict[str, Any]:
    previous = previous_records(context) if context.existing else {}
    if context.existing:
        saved = json.loads(
            context.layout.team_import_receipt_path.read_text(encoding="utf-8")
        )
        initial_control = saved["initial_control"]
    else:
        from team_transfer.control import ControlSection
        from team_transfer.settings import planned_config

        config = (
            planned_config(context) if "settings.config" in context.scratch else None
        )
        source = ExportSource(
            config,
            context.layout,
            context.reader.manifest.team_id,
            context.reader.manifest.instance_name,
        )
        records = (
            ControlSection().export(source, _SnapshotFiles("control", context)).records
            if config
            else []
        )
        initial_control = keyed_records("control", records)
    return {
        "source_mac": context.reader.manifest.mac,
        "initial_control": initial_control,
        "records": {
            section: sorted(
                previous.get(section, set()) | set(imported.get(section, ()))
            )
            for section in set(previous) | set(imported)
        },
    }


def control_was_edited(context, key: str) -> bool:
    """Only untouched bootstrap files may be replaced by a later selection."""
    if "control.current" not in context.scratch:
        context.scratch["control.current"] = keyed_records(
            "control", context.baseline.get("control", [])
        )
        saved = json.loads(
            context.layout.team_import_receipt_path.read_text(encoding="utf-8")
        )
        context.scratch["control.initial"] = saved["initial_control"]
    current = context.scratch["control.current"]
    if key not in current:
        return False
    return current[key] != context.scratch["control.initial"].get(key)
