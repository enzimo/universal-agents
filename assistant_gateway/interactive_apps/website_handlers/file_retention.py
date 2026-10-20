"""The HTTP routes of Settings > Team > File Retention.

GET shows every signed-in member the saved numbers of days chat uploads and
scratch files are kept, and whether they may change them. PUT saves both;
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
    MAX_RETENTION_DAYS,
    FileRetention,
    FileRetentionConflict,
    FileRetentionInvalid,
    FileRetentionRefused,
)
from assistant_gateway.interactive_apps.website_handlers.access_inbox import SignedInAccessHandlers
from assistant_gateway.interactive_apps.website_handlers.off_loop import off_loop

LOGGER = logging.getLogger(__name__)


def retention_payload(retention: FileRetention) -> dict[str, Any]:
    """The numbers as the Team panel reads them (team_controls.mjs)."""

    return {
        "upload_days": retention.upload_days,
        "scratch_days": retention.scratch_days,
        "max_days": MAX_RETENTION_DAYS,
    }


class WebsiteFileRetentionHandlers(SignedInAccessHandlers):
    refusal_event = "web_file_retention_refused"

    @off_loop
    def view(self, request: web.Request) -> web.Response:
        # Reads the saved settings file and the person's roles; both are synchronous.
        person_id = self._person(request, write=False)
        page = self.application.file_retention.page(person_id=person_id)
        return self.operations._permission_json_response(
            {"status": "ok", **retention_payload(page.retention), "can_change": page.can_change}
        )

    async def save(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=True)
        payload = await self.operations._json_payload(request)
        try:
            saved = await asyncio.to_thread(
                self.application.file_retention.save,
                person_id=person_id,
                upload_days=payload.get("upload_days"),
                scratch_days=payload.get("scratch_days"),
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
        return self.operations._permission_json_response({"status": "ok", **retention_payload(saved), "can_change": True})


__all__ = ["WebsiteFileRetentionHandlers", "retention_payload"]
