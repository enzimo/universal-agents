"""What Settings > Team > File Retention shows and accepts (``assistant_gateway.file_retention``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from runtime_settings.governance import MAX_RETENTION_DAYS

# A save that sends no number of days for saved tool outputs keeps the saved
# one, so a page from before that setting existed can still save the others.
KEEP_SAVED_DAYS: Final = object()

ADMINISTRATORS_ONLY_TEXT = "Only team administrators can change how long files are kept."
CHANGED_MEANWHILE_TEXT = (
    "The settings file changed while this was being saved, so nothing was saved. "
    "Reload the page and try again."
)


class FileRetentionRefused(Exception):
    """The person may not change file retention."""


class FileRetentionInvalid(ValueError):
    """A number of days the settings cannot hold; nothing was saved."""


class FileRetentionConflict(Exception):
    """The settings file changed while saving; nothing was saved."""


@dataclass(frozen=True)
class FileRetention:
    """Days chat uploads, scratch files and saved tool outputs are kept; 0 keeps them forever."""

    upload_days: int
    scratch_days: int
    output_days: int


@dataclass(frozen=True)
class FileRetentionPage:
    retention: FileRetention
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


__all__ = [
    "ADMINISTRATORS_ONLY_TEXT",
    "CHANGED_MEANWHILE_TEXT",
    "KEEP_SAVED_DAYS",
    "MAX_RETENTION_DAYS",
    "FileRetention",
    "FileRetentionConflict",
    "FileRetentionInvalid",
    "FileRetentionPage",
    "FileRetentionRefused",
    "checked_days",
]
