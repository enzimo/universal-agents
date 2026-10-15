"""Shared retention policy for rotating configuration-file backups."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


CONFIG_BACKUP_RETENTION_LIMIT = 5

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfigBackupPruneResult:
    """Observable outcome of pruning one target file's backup family."""

    target_path: Path
    retained: tuple[Path, ...]
    deleted: tuple[Path, ...]
    errors: tuple[tuple[Path, str], ...]


def prune_config_backups(
    target_path: str | Path,
    *,
    retention_limit: int = CONFIG_BACKUP_RETENTION_LIMIT,
    event_logger: logging.Logger | None = None,
    preserve_paths: Iterable[str | Path] = (),
) -> ConfigBackupPruneResult:
    """Keep the newest rotating backups for one exact configuration target.

    Only siblings named ``<target filename>.bak.*`` are eligible. Fixed-name
    one-copy backups such as ``file.yaml.bak`` and unrelated backup families
    are intentionally outside this policy. Newly-created backups supplied via
    ``preserve_paths`` sort ahead of equally recent siblings so coarse
    filesystem timestamps cannot prune the backup just returned to a caller.
    """

    if retention_limit < 1:
        raise ValueError("Configuration backup retention_limit must be at least 1.")

    target = Path(target_path).expanduser().resolve()
    preserved = {Path(path).expanduser().resolve() for path in preserve_paths}
    backup_prefix = f"{target.name}.bak."
    selected_logger = event_logger or logger
    candidates: list[tuple[bool, int, str, Path]] = []
    errors: list[tuple[Path, str]] = []

    try:
        siblings = tuple(target.parent.iterdir())
    except OSError as exc:
        errors.append((target.parent, str(exc)))
        selected_logger.warning(
            "event=config_backup_prune_directory_failed target_path=%s "
            "directory_path=%s error_type=%s error=%s",
            target,
            target.parent,
            type(exc).__name__,
            exc,
        )
        return ConfigBackupPruneResult(
            target_path=target,
            retained=(),
            deleted=(),
            errors=tuple(errors),
        )

    for backup_path in siblings:
        if (
            not backup_path.name.startswith(backup_prefix)
            or not backup_path.is_file()
        ):
            continue
        try:
            modified_ns = backup_path.stat().st_mtime_ns
        except OSError as exc:
            errors.append((backup_path, str(exc)))
            selected_logger.warning(
                "event=config_backup_prune_inspection_failed target_path=%s "
                "backup_path=%s error_type=%s error=%s",
                target,
                backup_path,
                type(exc).__name__,
                exc,
            )
            continue
        candidates.append(
            (
                backup_path.resolve() in preserved,
                modified_ns,
                backup_path.name,
                backup_path,
            )
        )

    candidates.sort(reverse=True)
    retained = tuple(item[3] for item in candidates[:retention_limit])
    deleted: list[Path] = []
    for _, _, _, backup_path in candidates[retention_limit:]:
        try:
            backup_path.unlink()
            deleted.append(backup_path)
        except FileNotFoundError:
            # Another updater applying the same policy already removed it.
            continue
        except OSError as exc:
            errors.append((backup_path, str(exc)))
            selected_logger.warning(
                "event=config_backup_prune_delete_failed target_path=%s "
                "backup_path=%s retention_limit=%s error_type=%s error=%s",
                target,
                backup_path,
                retention_limit,
                type(exc).__name__,
                exc,
            )

    if deleted:
        selected_logger.info(
            "event=config_backups_pruned target_path=%s retention_limit=%s "
            "deleted_count=%s deleted_paths=%s",
            target,
            retention_limit,
            len(deleted),
            [str(path) for path in deleted],
        )

    return ConfigBackupPruneResult(
        target_path=target,
        retained=retained,
        deleted=tuple(deleted),
        errors=tuple(errors),
    )
