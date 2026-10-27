"""The HTTP routes of Settings > Team > File Retention.

GET shows every signed-in member the saved numbers of days chat uploads,
scratch files and saved tool outputs are kept, and when people and
administrators hear that stored data grows (``gateway.storage_warnings``),
and whether they may change them. PUT saves them all;
who may is decided by ``PersonChangeCheck`` in
``assistant_gateway.file_retention`` (administrators with authorization on),
from the person's current roles, never from the browser.

Like the other Web Chat routes it has no tool catalog entry: it is a person's
control, protected by the signed-in session and the write check, not an agent
operation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from assistant_gateway.application.file_retention_page import (
    KEEP_SAVED_DAYS,
    KEEP_SAVED_WARNING,
    MAX_RETENTION_DAYS,
    MAX_STORAGE_WARNING_STEP_GB,
    FileRetentionConflict,
    FileRetentionInvalid,
    FileRetentionPage,
    FileRetentionRefused,
)
from assistant_gateway.interactive_apps.website_handlers.access_inbox import SignedInAccessHandlers
from assistant_gateway.interactive_apps.website_handlers.off_loop import off_loop

LOGGER = logging.getLogger(__name__)


def retention_payload(page: FileRetentionPage) -> dict[str, Any]:
    """The numbers as the Team panel reads them (file_retention_controls.mjs)."""

    return {
        "upload_days": page.retention.upload_days,
        "scratch_days": page.retention.scratch_days,
        "output_days": page.retention.output_days,
        "max_days": MAX_RETENTION_DAYS,
        "person_step_gb": page.warnings.person_step_gb,
        "nearing_limit_percent": page.warnings.nearing_limit_percent,
        "nearly_full_percent": page.warnings.nearly_full_percent,
        "max_step_gb": MAX_STORAGE_WARNING_STEP_GB,
        "can_change": page.can_change,
    }


class WebsiteFileRetentionHandlers(SignedInAccessHandlers):
    refusal_event = "web_file_retention_refused"

    @off_loop
    def view(self, request: web.Request) -> web.Response:
        # Reads the saved settings file and the person's roles; both are synchronous.
        person_id = self._person(request, write=False)
        page = self.application.file_retention.page(person_id=person_id)
        return self.operations._permission_json_response({"status": "ok", **retention_payload(page)})

    async def save(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=True)
        payload = await self.operations._json_payload(request)
        try:
            saved = await asyncio.to_thread(
                self.application.file_retention.save,
                person_id=person_id,
                upload_days=payload.get("upload_days"),
                scratch_days=payload.get("scratch_days"),
                # Missing (a page from before this setting): keep the saved number.
                output_days=payload["output_days"] if "output_days" in payload else KEEP_SAVED_DAYS,
                person_step_gb=payload.get("person_step_gb", KEEP_SAVED_WARNING),
                nearing_limit_percent=payload.get("nearing_limit_percent", KEEP_SAVED_WARNING),
                nearly_full_percent=payload.get("nearly_full_percent", KEEP_SAVED_WARNING),
            )
        except FileRetentionRefused as exc:
            LOGGER.warning(
                "Someone who may not change file retention tried to. event=%s person_id=%s",
                self.refusal_event,
                person_id,
            )
            return self.operations._permission_json_response({"status": "forbidden", "error": str(exc)}, status=403)
        except FileRetentionInvalid as exc:
            return self.operations._permission_json_response({"status": "invalid", "error": str(exc)}, status=400)
        except FileRetentionConflict as exc:
            return self._refused(exc, action="save_file_retention", person_id=person_id)
        return self.operations._permission_json_response({"status": "ok", **retention_payload(saved)})


__all__ = ["WebsiteFileRetentionHandlers", "retention_payload"]
