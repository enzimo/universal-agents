"""The export process an owner's ``/upgrade`` starts (``assistant_cli team export-requested``).

The supervisor starts this process for one export request
(``team_transfer.export_requests``), with its full environment, because the
export carries every operator secret and needs the broker's operator token.
It runs the same engine as ``team export``:

1. check that the requesting person is still an active owner;
2. collect the passphrase the owner typed on the broker's export page, once
   (operator-only route; the broker keeps no copy afterwards);
3. write the archive into ``exports/`` (0600, never overwritten), then give
   it and the folder to whoever owns the data folder;
4. report the result to the page, which the gateway posts in the chat.

Any failure after the request was taken is reported the same way, so the page
and the chat always say how the export ended. A stop signal (the instance
shutting down) is one of them: ``stop_on_signal`` turns SIGTERM into
``ExportStopped``, so the half-written archive is removed (``export_team``
discards it) and the page says the instance stopped. The passphrase lives only
in this process's memory and is never logged, printed or written.

File owner (owner's decision 2026-10-14): the container runs as root, so on a
Linux host a new file would belong to root and only root could copy it. The
export and ``exports/`` are given to the data folder's owner instead; the file
stays 0600 and encrypted with the passphrase.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

from team_transfer.broker_credentials import BrokerEndpoint, BrokerUnavailable, broker_endpoint, call_broker
from team_transfer.container import TransferFileError
from team_transfer.crypto import TransferKeyError
from team_transfer.errors import TransferError
from team_transfer.export_requests import (
    TEAM_EXPORT_PASSPHRASE_ROUTE,
    TEAM_EXPORT_RESULT_ROUTE,
    TeamExportPassphraseRequestV1,
    TeamExportPassphraseV1,
    TeamExportResultV1,
    export_file_name,
    export_sections,
    short_error,
)
from team_transfer.service import export_source, export_team, require_active_owner

logger = logging.getLogger(__name__)

_FAILURES = (TransferError, TransferFileError, TransferKeyError, ValueError, OSError)


class ExportStopped(TransferError):
    """The instance is shutting down; the export stopped before it finished."""


def stop_on_signal() -> None:
    """Turn the supervisor's SIGTERM into ``ExportStopped`` in this process (module docstring).

    Python accepts signal handlers only in the main thread, where the export
    command always runs; elsewhere (an embedding test) nothing changes.
    """

    def _stop(_signum: int, _frame: object) -> None:
        raise ExportStopped("The instance stopped during the export.")

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _stop)


def run_requested_export(
    config_path: str | Path,
    *,
    export_id: str,
    owner_person_id: str,
    now: datetime | None = None,
) -> TeamExportResultV1:
    """Run one requested export and report how it ended to its page (module docstring)."""

    source = export_source(config_path)
    endpoint = broker_endpoint(source.config, source.layout)
    try:
        require_active_owner(source.config, source.team_id, owner_person_id)
        collected = call_broker(
            endpoint,
            TEAM_EXPORT_PASSPHRASE_ROUTE,
            TeamExportPassphraseRequestV1(
                export_id=export_id, team_id=source.team_id, owner_person_id=owner_person_id
            ).model_dump(),
        )
        passphrase = TeamExportPassphraseV1.model_validate(collected).passphrase
        exports_dir = _exports_folder(source.layout.exports_dir)
        out = exports_dir / export_file_name(source.instance_name, now or datetime.now(timezone.utc))
        report = export_team(source, out, passphrase, owner_person_id=owner_person_id)
        del passphrase
        _give_to_data_folder_owner((exports_dir, report.path), source.layout.root)
        result = TeamExportResultV1(
            export_id=export_id,
            team_id=source.team_id,
            owner_person_id=owner_person_id,
            ok=True,
            file_name=report.path.name,
            size_bytes=report.path.stat().st_size,
            sections=export_sections(report.manifest),
        )
    except BrokerUnavailable as exc:
        logger.warning("The broker did not answer during a requested export. event=team_export_broker_unavailable")
        result = _failed(export_id, source.team_id, owner_person_id, f"The secure tool broker did not answer. {exc}")
    except _FAILURES as exc:
        result = _failed(export_id, source.team_id, owner_person_id, str(exc))
    _report(endpoint, result)
    logger.info(
        "Requested team export ended. event=team_export_process_finished export_id=%s ok=%s file_name=%s",
        export_id,
        result.ok,
        result.file_name or "-",
    )
    return result


def _failed(export_id: str, team_id: str, owner_person_id: str, why: str) -> TeamExportResultV1:
    logger.warning(
        "Requested team export failed. event=team_export_failed export_id=%s owner_person_id=%s error=%s",
        export_id,
        owner_person_id,
        short_error(why)[:300],
    )
    return TeamExportResultV1(
        export_id=export_id, team_id=team_id, owner_person_id=owner_person_id, ok=False, error=short_error(why)
    )


def _report(endpoint: BrokerEndpoint, result: TeamExportResultV1) -> None:
    """Tell the page; a page that is gone (expired, broker restarted) leaves the result in the log only."""

    try:
        call_broker(endpoint, TEAM_EXPORT_RESULT_ROUTE, result.model_dump(mode="json"))
    except TransferError as exc:
        logger.warning(
            "The export's page could not be told how it ended. event=team_export_result_unreported "
            "export_id=%s ok=%s error=%s",
            result.export_id,
            result.ok,
            short_error(str(exc))[:300],
        )


def _exports_folder(path: Path) -> Path:
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _give_to_data_folder_owner(paths: tuple[Path, ...], root: Path) -> None:
    """Give the export to the data folder's owner when running as root (module docstring)."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    folder = root.stat()
    if (folder.st_uid, folder.st_gid) == (0, 0):
        return
    for path in paths:
        os.chown(path, folder.st_uid, folder.st_gid, follow_symlinks=False)
    logger.info(
        "Gave the export to the data folder's owner. event=team_export_owner_set uid=%s gid=%s",
        folder.st_uid,
        folder.st_gid,
    )


__all__ = ["ExportStopped", "run_requested_export", "stop_on_signal"]
