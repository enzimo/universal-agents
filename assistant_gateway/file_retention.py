"""Settings > Team > File Retention: how long chat uploads, scratch files and saved tool outputs are kept.

Three settings of the saved instance configuration (``instance_config.yaml``),
each a number of days, where 0 keeps files forever:

- ``interactive_apps.plugins.builtin.internal_website.settings.attachments.retention_days``:
  files people send in any chat app, deleted by the gateway's cleanup
  (``assistant_gateway.upload_retention``) from its next run;
- ``tool_policy.scratch_retention_days``: files in scratch/ folders, deleted by
  the manager's maintenance run (``custom_agents.scratch_retention``);
- ``tool_policy.output_retention_days``: saved tool outputs (oversized tool
  results, long tool output offloaded from conversations, research results
  and execution run folders), deleted by the manager's maintenance run
  (``custom_agents.output_retention``).

The manager reads its settings when it starts, so a change to the last two
applies after the next restart.

Every signed-in member sees the numbers. Changing them is a platform settings
change made in the gateway's own controls, so ``PersonChangeCheck`` decides it
with the ``runtime_settings`` change kind (administrators with authorization
on; any active member with it off). Saving changes only these three fields of
the saved file, keeps a backup of the previous file as the configuration
editor does, writes atomically, and refuses when the file changed while it
was being saved, so it never overwrites another settings change.
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
    FileRetention,
    FileRetentionConflict,
    FileRetentionPage,
    FileRetentionRefused,
    checked_days,
)
from assistant_gateway.application.ports import GatewayPersonChangeCheck
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.upload_retention import UploadRetention
from config_backup_retention import prune_config_backups
from config_file_updates import ConfigFileConflict
from custom_agents.runtime_settings_service import RUNTIME_SETTINGS_CHANGE_KIND

logger = logging.getLogger(__name__)


class GatewayFileRetention:
    """Reads and saves the three retention settings for the Settings > Team form."""

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
        # The file CAS protects other writers. This lock also keeps this
        # gateway's live value in the same order as its successful saves.
        self._save_lock = Lock()

    def page(self, *, person_id: str) -> FileRetentionPage:
        return FileRetentionPage(
            retention=_retention(GatewayInstanceConfig.load_saved(self._config_path)),
            can_change=self._person_changes.may_change(person_id, RUNTIME_SETTINGS_CHANGE_KIND),
        )

    def save(
        self, *, person_id: str, upload_days: object, scratch_days: object, output_days: object
    ) -> FileRetention:
        """Save the numbers; raises ``FileRetentionRefused``, ``FileRetentionInvalid`` or ``FileRetentionConflict``.

        ``output_days`` may be ``KEEP_SAVED_DAYS`` to keep the saved number.
        """

        with self._save_lock:
            return self._save(
                person_id=person_id, upload_days=upload_days, scratch_days=scratch_days, output_days=output_days
            )

    def _save(
        self, *, person_id: str, upload_days: object, scratch_days: object, output_days: object
    ) -> FileRetention:
        if not self._person_changes.may_change(person_id, RUNTIME_SETTINGS_CHANGE_KIND):
            raise FileRetentionRefused(ADMINISTRATORS_ONLY_TEXT)
        upload = checked_days(upload_days, what="chat uploads")
        scratch = checked_days(scratch_days, what="scratch files")
        output = None if output_days is KEEP_SAVED_DAYS else checked_days(output_days, what="saved tool outputs")
        config = GatewayInstanceConfig.load_saved(self._config_path)
        wanted = FileRetention(
            upload_days=upload,
            scratch_days=scratch,
            output_days=config.tool_policy.output_retention_days if output is None else output,
        )
        path = config.config_path
        before = _retention(config)
        config.interactive_apps.internal_website.attachments.retention_days = wanted.upload_days
        config.tool_policy.scratch_retention_days = wanted.scratch_days
        config.tool_policy.output_retention_days = wanted.output_days
        backup = path.with_name(f"{path.name}.bak.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}")
        try:
            config.save(backup_path=backup)
        except ConfigFileConflict as exc:
            raise FileRetentionConflict(CHANGED_MEANWHILE_TEXT) from exc
        prune_config_backups(path, event_logger=logger, preserve_paths=(backup,))
        self._uploads.retention_days = wanted.upload_days
        logger.info(
            "File retention was changed. event=file_retention_changed person_id=%s "
            "upload_days=%s->%s scratch_days=%s->%s output_days=%s->%s backup=%s",
            person_id,
            before.upload_days,
            wanted.upload_days,
            before.scratch_days,
            wanted.scratch_days,
            before.output_days,
            wanted.output_days,
            backup.name,
        )
        return wanted


def _retention(config: GatewayInstanceConfig) -> FileRetention:
    return FileRetention(
        upload_days=config.interactive_apps.internal_website.attachments.retention_days,
        scratch_days=config.tool_policy.scratch_retention_days,
        output_days=config.tool_policy.output_retention_days,
    )


__all__ = ["GatewayFileRetention"]
