"""What Settings > Team > File Retention shows and accepts (``assistant_gateway.file_retention``).

The page holds how long files are kept and, beside it, when people and
administrators hear that stored data grows (``gateway.storage_warnings``):
both decide how the team's stored data is kept in bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from default_values import MAX_STORAGE_WARNING_STEP_GB
from runtime_settings.governance import MAX_RETENTION_DAYS

# A save that sends no number of days for saved tool outputs keeps the saved
# one, so a page from before that setting existed can still save the others.
KEEP_SAVED_DAYS: Final = object()
# The same rule applies when an open page predates the warning controls.
KEEP_SAVED_WARNING: Final = object()

ADMINISTRATORS_ONLY_TEXT = "Only team administrators can change how long files are kept."
CHANGED_MEANWHILE_TEXT = (
    "The settings file changed while this was being saved, so nothing was saved. "
    "Reload the page and try again."
)


class FileRetentionRefused(Exception):
    """The person may not change file retention."""


class FileRetentionInvalid(ValueError):
    """A number the settings cannot hold; nothing was saved."""


class FileRetentionConflict(Exception):
    """The settings file changed while saving; nothing was saved."""


@dataclass(frozen=True)
class FileRetention:
    """Days chat uploads, scratch files and saved tool outputs are kept; 0 keeps them forever."""

    upload_days: int
    scratch_days: int
    output_days: int


@dataclass(frozen=True)
class StorageWarnings:
    """When people and administrators hear that stored data grows (``gateway.storage_warnings``).

    ``person_step_gb``: each person's notice step in GB, 0 for none.
    ``nearing_limit_percent``: a disk or the message storage is a warning on
    the Status page, and administrators are told. ``nearly_full_percent``: a
    problem, and they are told again.
    """

    person_step_gb: int
    nearing_limit_percent: int
    nearly_full_percent: int


@dataclass(frozen=True)
class FileRetentionPage:
    retention: FileRetention
    warnings: StorageWarnings
    can_change: bool


def checked_days(value: object, *, what: str) -> int:
    """A whole number of days from 0 to ``MAX_RETENTION_DAYS``, or ``FileRetentionInvalid``."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise FileRetentionInvalid(f"Days to keep {what} must be a whole number.")
    if not 0 <= value <= MAX_RETENTION_DAYS:
        raise FileRetentionInvalid(
            f"Days to keep {what} must be from 0 (keep forever) to {MAX_RETENTION_DAYS}."
        )
    return value


def checked_warnings(*, person_step_gb: object, nearing_limit_percent: object, nearly_full_percent: object) -> StorageWarnings:
    """The three storage warning numbers, or ``FileRetentionInvalid`` saying what to correct."""

    step = _whole(
        person_step_gb,
        0,
        MAX_STORAGE_WARNING_STEP_GB,
        f"The storage notice step must be a whole number of GB from 0 (no notices) to {MAX_STORAGE_WARNING_STEP_GB}.",
    )
    nearing = _whole(nearing_limit_percent, 1, 100, "The warning level must be a whole percent from 1 to 100.")
    full = _whole(nearly_full_percent, 1, 100, "The problem level must be a whole percent from 1 to 100.")
    if nearing >= full:
        raise FileRetentionInvalid(
            "The warning level must be below the problem level, so administrators hear before storage is nearly full."
        )
    return StorageWarnings(person_step_gb=step, nearing_limit_percent=nearing, nearly_full_percent=full)


def _whole(value: object, low: int, high: int, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise FileRetentionInvalid(message)
    return value


__all__ = [
    "ADMINISTRATORS_ONLY_TEXT",
    "CHANGED_MEANWHILE_TEXT",
    "KEEP_SAVED_DAYS",
    "KEEP_SAVED_WARNING",
    "MAX_RETENTION_DAYS",
    "MAX_STORAGE_WARNING_STEP_GB",
    "FileRetention",
    "FileRetentionConflict",
    "FileRetentionInvalid",
    "FileRetentionPage",
    "FileRetentionRefused",
    "StorageWarnings",
    "checked_days",
    "checked_warnings",
]
