"""Settings > Team > File Retention: how long files are kept, and when stored data growth is announced.

Settings of the saved instance configuration (``instance_config.yaml``):

- ``interactive_apps.plugins.builtin.internal_website.settings.attachments.retention_days``:
  days the gateway keeps files people send in any chat app, deleted by its
  cleanup (``assistant_gateway.upload_retention``) from its next run;
- ``tool_policy.scratch_retention_days``: days files stay in scratch/ folders,
  deleted by the manager's maintenance run (``custom_agents.scratch_retention``);
- ``tool_policy.output_retention_days``: days saved tool outputs are kept
  (oversized tool results, long tool output offloaded from conversations,
  research results and execution run folders), deleted by the manager's
  maintenance run (``custom_agents.output_retention``). The manager reads its
  settings when it starts, so a change to these two applies after the next
  restart;
- ``gateway.storage_warnings``: each person's storage notice step
  (``assistant_gateway.storage_usage``) and the Status page's two storage
  levels (``assistant_gateway.storage_capacity``). The gateway's running
  checks use new numbers from their next check, without a restart.

For days, 0 keeps files forever; a step of 0 turns people's storage notices
off. Every signed-in member sees the numbers. Changing them is a platform
settings change made in the gateway's own controls, so ``PersonChangeCheck``
decides it with the ``runtime_settings`` change kind (administrators with
authorization on; any active member with it off). Saving changes only these
fields of the saved file, keeps a backup of the previous file as the
configuration editor does, writes atomically, and refuses when the file
changed while it was being saved, so it never overwrites another settings
change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from assistant_gateway.application.file_retention_page import (
    ADMINISTRATORS_ONLY_TEXT,
    CHANGED_MEANWHILE_TEXT,
    KEEP_SAVED_DAYS,
    KEEP_SAVED_WARNING,
    FileRetention,
    FileRetentionConflict,
    FileRetentionPage,
    FileRetentionRefused,
    StorageWarnings,
    checked_days,
    checked_warnings,
)
from assistant_gateway.application.ports import GatewayPersonChangeCheck
from assistant_gateway.config import GatewayInstanceConfig, StorageWarningSettings
from assistant_gateway.storage_capacity import StorageCapacity
from assistant_gateway.storage_usage import StorageUsageNotices
from assistant_gateway.upload_retention import UploadRetention
from config_backup_retention import prune_config_backups
from config_file_updates import ConfigFileConflict
from custom_agents.runtime_settings_service import RUNTIME_SETTINGS_CHANGE_KIND

logger = logging.getLogger(__name__)


class GatewayFileRetention:
    """Reads and saves the retention and storage warning settings for the Settings > Team form."""

    def __init__(
        self,
        *,
        config_path: Path,
        person_changes: GatewayPersonChangeCheck,
        uploads: UploadRetention,
        storage_usage: StorageUsageNotices,
        storage_capacity: StorageCapacity,
    ) -> None:
        self._config_path = config_path
        self._person_changes = person_changes
        self._uploads = uploads
        self._storage_usage = storage_usage
        self._storage_capacity = storage_capacity
        # The file CAS protects other writers. This lock also keeps this
        # gateway's live values in the same order as its successful saves.
        self._save_lock = Lock()

    def page(self, *, person_id: str) -> FileRetentionPage:
        config = GatewayInstanceConfig.load_saved(self._config_path)
        return FileRetentionPage(
            retention=_retention(config),
            warnings=_warnings(config),
            can_change=self._person_changes.may_change(person_id, RUNTIME_SETTINGS_CHANGE_KIND),
        )

    def save(
        self,
        *,
        person_id: str,
        upload_days: object,
        scratch_days: object,
        output_days: object,
        person_step_gb: object = KEEP_SAVED_WARNING,
        nearing_limit_percent: object = KEEP_SAVED_WARNING,
        nearly_full_percent: object = KEEP_SAVED_WARNING,
    ) -> FileRetentionPage:
        """Save the numbers, preserving settings absent from an older open page."""

        with self._save_lock:
            if not self._person_changes.may_change(person_id, RUNTIME_SETTINGS_CHANGE_KIND):
                raise FileRetentionRefused(ADMINISTRATORS_ONLY_TEXT)
            config = GatewayInstanceConfig.load_saved(self._config_path)
            saved_warnings = config.gateway.storage_warnings
            wanted = FileRetentionPage(
                retention=FileRetention(
                    upload_days=checked_days(upload_days, what="chat uploads"),
                    scratch_days=checked_days(scratch_days, what="scratch files"),
                    output_days=config.tool_policy.output_retention_days if output_days is KEEP_SAVED_DAYS
                    else checked_days(output_days, what="saved tool outputs"),
                ),
                warnings=checked_warnings(
                    person_step_gb=saved_warnings.person_step_gb if person_step_gb is KEEP_SAVED_WARNING else person_step_gb,
                    nearing_limit_percent=saved_warnings.nearing_limit_percent if nearing_limit_percent is KEEP_SAVED_WARNING else nearing_limit_percent,
                    nearly_full_percent=saved_warnings.nearly_full_percent if nearly_full_percent is KEEP_SAVED_WARNING else nearly_full_percent,
                ),
                can_change=True,
            )
            return self._save(person_id, wanted, config)

    def _save(self, person_id: str, wanted: FileRetentionPage, config: GatewayInstanceConfig) -> FileRetentionPage:
        path = config.config_path
        before = (_retention(config), _warnings(config))
        config.interactive_apps.internal_website.attachments.retention_days = wanted.retention.upload_days
        config.tool_policy.scratch_retention_days = wanted.retention.scratch_days
        config.tool_policy.output_retention_days = wanted.retention.output_days
        # Through the settings model, so the saved file holds only what it accepts.
        config.gateway.storage_warnings = StorageWarningSettings.model_validate(
            {
                "person_step_gb": wanted.warnings.person_step_gb,
                "nearing_limit_percent": wanted.warnings.nearing_limit_percent,
                "nearly_full_percent": wanted.warnings.nearly_full_percent,
            }
        )
        backup = path.with_name(f"{path.name}.bak.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}")
        try:
            config.save(backup_path=backup)
        except ConfigFileConflict as exc:
            raise FileRetentionConflict(CHANGED_MEANWHILE_TEXT) from exc
        prune_config_backups(path, event_logger=logger, preserve_paths=(backup,))
        self._uploads.retention_days = wanted.retention.upload_days
        self._storage_usage.step_gb = wanted.warnings.person_step_gb
        self._storage_capacity.nearing_limit_percent = wanted.warnings.nearing_limit_percent
        self._storage_capacity.nearly_full_percent = wanted.warnings.nearly_full_percent
        logger.info(
            "File retention and storage warnings were changed. event=file_retention_changed person_id=%s "
            "upload_days=%s->%s scratch_days=%s->%s output_days=%s->%s person_step_gb=%s->%s nearing_limit_percent=%s->%s "
            "nearly_full_percent=%s->%s backup=%s",
            person_id,
            before[0].upload_days,
            wanted.retention.upload_days,
            before[0].scratch_days,
            wanted.retention.scratch_days,
            before[0].output_days,
            wanted.retention.output_days,
            before[1].person_step_gb,
            wanted.warnings.person_step_gb,
            before[1].nearing_limit_percent,
            wanted.warnings.nearing_limit_percent,
            before[1].nearly_full_percent,
            wanted.warnings.nearly_full_percent,
            backup.name,
        )
        return wanted


def _retention(config: GatewayInstanceConfig) -> FileRetention:
    return FileRetention(
        upload_days=config.interactive_apps.internal_website.attachments.retention_days,
        scratch_days=config.tool_policy.scratch_retention_days,
        output_days=config.tool_policy.output_retention_days,
    )


def _warnings(config: GatewayInstanceConfig) -> StorageWarnings:
    saved = config.gateway.storage_warnings
    return StorageWarnings(
        person_step_gb=saved.person_step_gb,
        nearing_limit_percent=saved.nearing_limit_percent,
        nearly_full_percent=saved.nearly_full_percent,
    )


__all__ = ["GatewayFileRetention"]
