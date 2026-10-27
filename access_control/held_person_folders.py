"""Hold, restore and purge the folder of a person removed from the team.

When an administrator removes a person (``/user remove``), their private
folder (``workspaces/users/<folder>``, including their Python environments and
published pages) is not deleted at once. It moves to
``state/held_people/<folder>`` (``InstanceLayout.held_people_dir``), where no
agent tool or sandboxed code can reach it, and stays there until an
administrator restores the person (``/user restore``) or the retention time
(``gateway.users.removed_data_retention_days``) passes and a maintenance run
purges it (``custom_agents.person_departure_runtime``).

Why a move and not a copy: one ``rename`` of the folder entry is atomic and
recoverable (moving it back restores exactly what was there), and nothing
inside the folder is opened, so a link an agent planted inside is moved as a
link and never followed (``access_control.safe_open.move_directory``). The
purge deletes the held folder with ``shutil.rmtree``, which on Linux walks by
file descriptor and removes links without following them.

The gateway holds and restores (it applies ``/user remove`` and
``/user restore``); the manager and the gateway purge. Both derive every path
from the instance layout.

A held conversation's long texts stay in their files in
``dbs/conversation_artifacts`` while their records are held. Both purges
delete those files with the records (``purge_held_artifact_files``) before the
records go, so a failed purge keeps the records that name the files and the
next purge tries again, instead of leaving files no record names.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from access_control import safe_open
from old_file_cleanup import OldFilesRemoved, remove_listed_file
from runtime_settings.instance_layout import (
    HELD_PEOPLE_DIRNAME,
    STATE_DIRNAME,
    USERS_DIRNAME,
    WORKSPACES_DIRNAME,
    InstanceLayout,
    person_folder_name,
)

logger = logging.getLogger(__name__)


def _person_parts(person_id: str) -> tuple[str, ...]:
    return (WORKSPACES_DIRNAME, USERS_DIRNAME, person_folder_name(person_id))


def _held_parts(person_id: str) -> tuple[str, ...]:
    return (STATE_DIRNAME, HELD_PEOPLE_DIRNAME, person_folder_name(person_id))


def _exists(layout: InstanceLayout, parts: tuple[str, ...]) -> bool:
    try:
        return safe_open.entry_status(layout.root, parts) is not None
    except safe_open.UnsafePath:
        return False


def hold_folder(layout: InstanceLayout, person_id: str) -> bool:
    """Move the person's folder to the holding area; False when they have no folder.

    Raises ``safe_open.UnsafePath`` when the move is refused (a link where the
    folder should be, a folder already held for them, another file system).
    """

    if not _exists(layout, _person_parts(person_id)):
        return False
    safe_open.move_directory(layout.root, _person_parts(person_id), layout.root, _held_parts(person_id))
    logger.info(
        "Moved a removed person's folder to the holding area. event=person_folder_held person_id=%s folder=%s",
        person_id,
        person_folder_name(person_id),
    )
    return True


def restore_folder(layout: InstanceLayout, person_id: str) -> bool:
    """Move the held folder back to ``workspaces/users``; False when nothing is held.

    Raises ``safe_open.UnsafePath`` when the person's folder exists again and
    is not empty, so nothing is overwritten.
    """

    if not _exists(layout, _held_parts(person_id)):
        return False
    safe_open.move_directory(layout.root, _held_parts(person_id), layout.root, _person_parts(person_id))
    logger.info(
        "Moved a restored person's folder back. event=person_folder_restored person_id=%s folder=%s",
        person_id,
        person_folder_name(person_id),
    )
    return True


def held_folder_path(layout: InstanceLayout, person_id: str) -> Path:
    return layout.root.joinpath(*_held_parts(person_id))


def purge_folder(layout: InstanceLayout, person_id: str) -> bool:
    """Delete the held folder for good; False when nothing is held."""

    # Opens every parent folder without following links, so a link planted in
    # the path can never redirect the deletion (access_control.safe_open).
    if not safe_open.remove_directory(layout.root, _held_parts(person_id)):
        return False
    logger.info(
        "Deleted a removed person's held folder. event=person_folder_purged person_id=%s folder=%s",
        person_id,
        person_folder_name(person_id),
    )
    return True


def purge_held_artifact_files(layout: InstanceLayout, person_id: str, paths: Iterable[str]) -> OldFilesRemoved:
    """Delete the files of the person's held conversation artifacts, only inside ``dbs/conversation_artifacts``.

    ``paths`` are the files the held artifact records name
    (``PersonDepartureRepository.held_artifact_paths``). A path outside that
    folder or through a link is left alone and logged; a file already gone is
    not counted.
    """

    counts = OldFilesRemoved()
    for path in paths:
        remove_listed_file(layout.conversation_artifacts_dir, Path(path), counts)
    if counts.files_removed or counts.skipped:
        logger.info(
            "Deleted a removed person's held conversation artifact files. event=person_artifact_files_purged "
            "person_id=%s files_removed=%s bytes_removed=%s skipped=%s",
            person_id,
            counts.files_removed,
            counts.bytes_removed,
            counts.skipped,
        )
    return counts


__all__ = ["held_folder_path", "hold_folder", "purge_folder", "purge_held_artifact_files", "restore_folder"]
