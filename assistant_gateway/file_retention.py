"""Settings > Team > File Retention: how long chat uploads and scratch files are kept.

Two settings of the saved instance configuration (``instance_config.yaml``),
each a number of days, where 0 keeps files forever:

- ``interactive_apps.plugins.builtin.internal_website.settings.attachments.retention_days``:
  files people send in any chat app, deleted by the gateway's cleanup
  (``assistant_gateway.upload_retention``) from its next run;
- ``tool_policy.scratch_retention_days``: files in scratch/ folders, deleted by
  the manager's maintenance run (``custom_agents.scratch_retention``). The
  manager reads its settings when it starts, so a change applies after the
  next restart.

Every signed-in member sees the numbers. Changing them is a platform settings
change made in the gateway's own controls, so ``PersonChangeCheck`` decides it
with the ``runtime_settings`` change kind (administrators with authorization
on; any active member with it off). Saving changes only these two fields of
the saved file, keeps a backup of the previous file as the configuration
editor does, writes atomically, and refuses when the file changed while it
was being saved, so it never overwrites another settings change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from assistant_gateway.application.file_retention_page import (
    ADMINISTRATORS_ONLY_TEXT,
    CHANGED_MEANWHILE_TEXT,
    FileRetention,
    FileRetentionConflict,
    FileRetentionPage,
    FileRetentionRefused,
    checked_days,
)
from assistant_gateway.application.ports import GatewayPersonChangeCheck
from assistant_gateway.config import GatewayInstanceConfig, dump_instance_config_yaml
from assistant_gateway.upload_retention import UploadRetention
from config_backup_retention import prune_config_backups
from custom_agents.governance.authorized_operations import atomic_write_bytes
from custom_agents.runtime_settings_service import RUNTIME_SETTINGS_CHANGE_KIND

logger = logging.getLogger(__name__)


class GatewayFileRetention:
    """Reads and saves the two retention settings for the Settings > Team form."""

    def __init__(
        self,
        *,
        config_path: Path,
        person_changes: GatewayPersonChangeCheck,
        uploads: UploadRetention,
    ) -> None:
        self._config_path = config_path
        self._person_changes = person_changes
        self._uploads = uploads

    def page(self, *, person_id: str) -> FileRetentionPage:
        return FileRetentionPage(
            retention=_retention(GatewayInstanceConfig.load_saved(self._config_path)),
            can_change=self._person_changes.may_change(person_id, RUNTIME_SETTINGS_CHANGE_KIND),
        )

    def save(self, *, person_id: str, upload_days: object, scratch_days: object) -> FileRetention:
        """Save both numbers; raises ``FileRetentionRefused``, ``FileRetentionInvalid`` or ``FileRetentionConflict``."""

        if not self._person_changes.may_change(person_id, RUNTIME_SETTINGS_CHANGE_KIND):
            raise FileRetentionRefused(ADMINISTRATORS_ONLY_TEXT)
        wanted = FileRetention(
            upload_days=checked_days(upload_days, what="chat uploads"),
            scratch_days=checked_days(scratch_days, what="scratch files"),
        )
        # Read the bytes before loading them: a change after this read is
        # caught by the comparison just before writing.
        path = GatewayInstanceConfig.load_saved(self._config_path).config_path
        previous = path.read_bytes()
        config = GatewayInstanceConfig.load(path)
        before = _retention(config)
        config.interactive_apps.internal_website.attachments.retention_days = wanted.upload_days
        config.tool_policy.scratch_retention_days = wanted.scratch_days
        content = dump_instance_config_yaml(config.to_yaml_dict()).encode("utf-8")
        if path.read_bytes() != previous:
            raise FileRetentionConflict(CHANGED_MEANWHILE_TEXT)
        backup = path.with_name(f"{path.name}.bak.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}")
        atomic_write_bytes(backup, previous)
        atomic_write_bytes(path, content)
        prune_config_backups(path, event_logger=logger, preserve_paths=(backup,))
        self._uploads.retention_days = wanted.upload_days
        logger.info(
            "File retention was changed. event=file_retention_changed person_id=%s "
            "upload_days=%s->%s scratch_days=%s->%s backup=%s",
            person_id,
            before.upload_days,
            wanted.upload_days,
            before.scratch_days,
            wanted.scratch_days,
            backup.name,
        )
        return wanted


def _retention(config: GatewayInstanceConfig) -> FileRetention:
    return FileRetention(
        upload_days=config.interactive_apps.internal_website.attachments.retention_days,
        scratch_days=config.tool_policy.scratch_retention_days,
    )


__all__ = ["GatewayFileRetention"]
