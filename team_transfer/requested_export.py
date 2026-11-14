"""The export process an owner's ``/upgrade`` starts (``assistant_cli team export-requested``).

The supervisor starts this process for one export request
(``team_transfer.export_requests``), with its full environment, because the
export carries every operator secret and needs the broker's operator token.
It runs the same engine as ``team export``:

1. check that the requesting person is still an active owner;
2. collect the passphrase the owner typed on the broker's export page, once
   (operator-only route; the broker keeps no copy afterwards), with a token
   for this one collection;
3. write the archive into ``exports/`` (0600, never overwritten), then give
   it and the folder to whoever owns the data folder;
4. report the result to the page with that token; the gateway posts it in
   the chat.

What the passphrase protects: the archive's stored sign-ins and keys and its
operator secrets are encrypted with it, and the whole archive is signed with
it. The other records are readable by anyone holding the file
(``team_transfer.export_requests``), so the file must stay private.

Any failure after the request was taken is reported the same way, so the page
and the chat always say how the export ended: expected failures with their
own words, any other error as an unexpected one (its details go to the log).
A stop signal (the instance shutting down) is one of them: ``stop_on_signal``
turns SIGTERM into ``ExportStopped``, so the half-written archive is removed
(``export_team`` discards it) and the page says the instance stopped. The
passphrase lives only in this process's memory and is never logged, printed
or written.

Only two failures cannot be reported from here: the instance's settings or
the broker's address cannot be read (there is nowhere to report to), and the
process is killed. Both end the process with an error, and the supervisor
then reports it (``report_export_process_ended``) and removes the killed
process's half-written file (``remove_leftover_partials``).

File owner (owner's decision 2026-10-14): the container runs as root, so on a
Linux host a new file would belong to root and only root could copy it. The
export and ``exports/`` are given to the data folder's owner instead; the file
stays 0600. Once the archive is published, a failed ownership change does not
undo the export: the result stays a success with a note that copying the file
may need administrator rights.
"""

from __future__ import annotations

import logging
import os
import signal
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from access_control.people import TEAM_EXPORT_OWNER_REQUIRED
from secure_tool_broker.defaults import (
    TEAM_EXPORT_ENDED_ROUTE,
    TEAM_EXPORT_PASSPHRASE_ROUTE,
    TEAM_EXPORT_RESULT_ROUTE,
)
from team_transfer.broker_credentials import (
    BrokerEndpoint,
    BrokerUnavailable,
    broker_endpoint,
    call_broker,
    supervisor_broker_endpoint,
)
from team_transfer.container import PARTIAL_SUFFIX, TransferFileError
from team_transfer.crypto import TransferKeyError
from team_transfer.errors import TransferError
from team_transfer.export_requests import (
    EXPORT_FILE_SUFFIX,
    TeamExportPassphraseRequestV1,
    TeamExportPassphraseV1,
    TeamExportProcessEndedV1,
    TeamExportRequestV1,
    TeamExportResultV1,
    export_file_name,
    export_sections,
    short_error,
)
from team_transfer.sections import ExportSource
from team_transfer.service import ExportReport, active_owner_problem, export_source, export_team

logger = logging.getLogger(__name__)

# Failures with words of their own; any other error is reported as unexpected.
_FAILURES = (TransferError, TransferFileError, TransferKeyError, ValueError, OSError)
OWNER_NOT_SET_NOTE = (
    "The file could not be given to the data folder's owner, so it still belongs to the container's user: "
    "copying it may need administrator rights (sudo)."
)
REPORT_INCOMPLETE_NOTE = (
    "The file was written, but its size and parts could not be read afterwards. The launcher's log has details."
)
UNEXPECTED_FAILURE = "The export stopped on an unexpected error ({kind}). The launcher's log has details."
# The supervisor's report must not hold up its loop for long.
_ENDED_REPORT_TIMEOUT_SECONDS = 5.0


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

    source, endpoint = _setup(config_path, export_id)
    collection_token = ""
    try:
        problem = active_owner_problem(source.config, source.team_id, owner_person_id)
        if problem:
            raise TransferError(f"{problem} {TEAM_EXPORT_OWNER_REQUIRED}")
        collected = TeamExportPassphraseV1.model_validate(
            call_broker(
                endpoint,
                TEAM_EXPORT_PASSPHRASE_ROUTE,
                TeamExportPassphraseRequestV1(
                    export_id=export_id,
                    team_id=source.team_id,
                    owner_person_id=owner_person_id,
                    process_id=os.getpid(),
                ).model_dump(),
            )
        )
        collection_token = collected.collection_token
        exports_dir = _exports_folder(source.layout.exports_dir)
        out = exports_dir / export_file_name(source.instance_name, now or datetime.now(timezone.utc))
        report = export_team(source, out, collected.passphrase, owner_person_id=owner_person_id)
        del collected
    except BrokerUnavailable as exc:
        logger.warning("The broker did not answer during a requested export. event=team_export_broker_unavailable")
        result = _failed(
            export_id, source.team_id, owner_person_id, collection_token, f"The secure tool broker did not answer. {exc}"
        )
    except _FAILURES as exc:
        result = _failed(export_id, source.team_id, owner_person_id, collection_token, str(exc))
    except Exception as exc:
        # A defect or an environment problem (a storage or network error of
        # another kind): the page must still hear the export ended.
        logger.exception(
            "A requested team export failed unexpectedly. event=team_export_unexpected_failure export_id=%s "
            "error_type=%s",
            export_id,
            type(exc).__name__,
        )
        result = _failed(
            export_id, source.team_id, owner_person_id, collection_token, UNEXPECTED_FAILURE.format(kind=type(exc).__name__)
        )
    else:
        result = _published_or_noted(source, report, exports_dir, export_id, owner_person_id, collection_token)
    _report(endpoint, result)
    logger.info(
        "Requested team export ended. event=team_export_process_finished export_id=%s ok=%s file_name=%s",
        export_id,
        result.ok,
        result.file_name or "-",
    )
    return result


def _setup(config_path: str | Path, export_id: str) -> tuple[ExportSource, BrokerEndpoint]:
    """The instance and the broker's address; a failure here cannot reach the page (module docstring)."""

    try:
        source = export_source(config_path)
        return source, broker_endpoint(source.config, source.layout)
    except Exception as exc:
        logger.error(
            "A requested team export could not read the instance's settings or the broker's address; the "
            "supervisor reports the failed process. event=team_export_setup_failed export_id=%s error_type=%s "
            "error=%s",
            export_id,
            type(exc).__name__,
            short_error(str(exc))[:300],
        )
        raise TransferError(f"The export could not start: {short_error(str(exc))}") from exc


def _published_or_noted(
    source: ExportSource,
    report: ExportReport,
    exports_dir: Path,
    export_id: str,
    owner_person_id: str,
    collection_token: str,
) -> TeamExportResultV1:
    """The success result; any error while describing a published archive keeps it a success with a note."""

    try:
        return _published(source, report, exports_dir, export_id, owner_person_id, collection_token)
    except Exception as exc:  # a stop signal or a defect after publishing must not hide the file
        logger.exception(
            "The export was written but its report could not be completed. event=team_export_report_incomplete "
            "export_id=%s file_name=%s error_type=%s",
            export_id,
            report.path.name,
            type(exc).__name__,
        )
        return TeamExportResultV1(
            export_id=export_id,
            team_id=source.team_id,
            owner_person_id=owner_person_id,
            ok=True,
            collection_token=collection_token,
            file_name=report.path.name,
            note=REPORT_INCOMPLETE_NOTE,
        )


def _published(
    source: ExportSource,
    report: ExportReport,
    exports_dir: Path,
    export_id: str,
    owner_person_id: str,
    collection_token: str,
) -> TeamExportResultV1:
    """The success result for a published archive; nothing after publishing undoes it (module docstring)."""

    note = ""
    try:
        _give_to_data_folder_owner((exports_dir, report.path), source.layout.root)
    except Exception as exc:  # OSError mostly; a stop signal now must not hide a written file either
        logger.warning(
            "The export was written but not given to the data folder's owner. event=team_export_owner_not_set "
            "export_id=%s file_name=%s error_type=%s",
            export_id,
            report.path.name,
            type(exc).__name__,
        )
        note = OWNER_NOT_SET_NOTE
    return TeamExportResultV1(
        export_id=export_id,
        team_id=source.team_id,
        owner_person_id=owner_person_id,
        ok=True,
        collection_token=collection_token,
        file_name=report.path.name,
        size_bytes=_file_size(report.path, export_id),
        sections=export_sections(report.manifest),
        note=note,
    )


def _file_size(path: Path, export_id: str) -> int:
    """The published archive's size; 0 (logged) if it cannot be read, which never undoes the export."""

    try:
        return os.stat(path, follow_symlinks=False).st_size
    except OSError as exc:
        logger.warning(
            "The export's size could not be read. event=team_export_size_unreadable export_id=%s error_type=%s",
            export_id,
            type(exc).__name__,
        )
        return 0


def _failed(export_id: str, team_id: str, owner_person_id: str, collection_token: str, why: str) -> TeamExportResultV1:
    logger.warning(
        "Requested team export failed. event=team_export_failed export_id=%s owner_person_id=%s error=%s",
        export_id,
        owner_person_id,
        short_error(why)[:300],
    )
    return TeamExportResultV1(
        export_id=export_id,
        team_id=team_id,
        owner_person_id=owner_person_id,
        ok=False,
        collection_token=collection_token,
        error=short_error(why),
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
    """``exports/``, created 0700 when missing; it must be a real folder, never a link."""

    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise TransferFileError(
            f"{path.name}/ in the data folder is not a folder (it is a link or a file), so nothing was written "
            "there. Remove it, then run the export again."
        )
    # Opened without following a link, so the mode change lands on this folder.
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
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


# -- the supervisor's side (docker/launch_headless.py, TeamExportRunner) --------------


def report_export_process_ended(
    environment: Mapping[str, str], request: TeamExportRequestV1, exit_code: int, process_id: int
) -> str:
    """Tell the export's page that its process exited with an error; the broker's answer.

    The broker records it only while the export is still open (``recorded``),
    so an export that already reported keeps its own result (``ignored``),
    and for a collected export only when ``process_id`` is the process that
    collected.
    """

    reply = call_broker(
        supervisor_broker_endpoint(environment),
        TEAM_EXPORT_ENDED_ROUTE,
        TeamExportProcessEndedV1(
            export_id=request.export_id,
            team_id=request.team_id,
            owner_person_id=request.owner_person_id,
            exit_code=exit_code,
            process_id=process_id,
        ).model_dump(),
        timeout_seconds=_ENDED_REPORT_TIMEOUT_SECONDS,
    )
    return str(reply.get("status") or "")


def remove_leftover_partials(exports_dir: Path, pid: int) -> tuple[str, ...]:
    """Remove the half-written archives process ``pid`` left in ``exports/``; their names.

    Only that process's own temporary files (``team_transfer.container.partial_path``):
    another export's work and finished archives stay.
    """

    suffix = f"{EXPORT_FILE_SUFFIX}.{pid}{PARTIAL_SUFFIX}"
    try:
        entries = [
            entry.name
            for entry in os.scandir(exports_dir)
            if entry.name.startswith(".") and entry.name.endswith(suffix) and entry.is_file(follow_symlinks=False)
        ]
    except FileNotFoundError:
        return ()
    for name in entries:
        (exports_dir / name).unlink(missing_ok=True)
    return tuple(entries)


__all__ = [
    "OWNER_NOT_SET_NOTE",
    "REPORT_INCOMPLETE_NOTE",
    "UNEXPECTED_FAILURE",
    "ExportStopped",
    "remove_leftover_partials",
    "report_export_process_ended",
    "run_requested_export",
    "stop_on_signal",
]
