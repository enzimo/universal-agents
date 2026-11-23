"""HTTP routes for Settings > Tasks > Brokered SSH.

GET returns enrolled SSH targets and active/recent delegated SSH sessions for
administrators (and ``can_manage: false`` with empty lists for non-admins so
hostnames, fingerprints, and session details stay private). POST revokes a
target or terminates a session immediately after validating the browser write
checks (same origin, fetch metadata, CSRF header) and ``PersonChangeCheck``
authorization inside ``assistant_gateway.execution_ssh``.

Like the other Web Chat Settings routes, these are person controls protected by
the signed-in session and write checks, not agent tools in the access catalog.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from aiohttp import web

from assistant_gateway.application.execution_ssh_page import (
    ExecutionSSHNotFound,
    ExecutionSSHPage,
    ExecutionSSHRefused,
)
from assistant_gateway.interactive_apps.website_handlers.access_inbox import (
    SignedInAccessHandlers,
)
from assistant_gateway.interactive_apps.website_handlers.off_loop import off_loop

LOGGER = logging.getLogger(__name__)


def execution_ssh_payload(page: ExecutionSSHPage) -> dict[str, Any]:
    """Serialize ``ExecutionSSHPage`` for ``execution_ssh_controls.mjs``."""

    return {
        "can_manage": page.can_manage,
        "ssh_enabled": page.ssh_enabled,
        "targets": [dataclasses.asdict(target) for target in page.targets],
        "sessions": [
            {
                **dataclasses.asdict(session),
                "capabilities": list(session.capabilities),
                "allowed_secret_recipes": list(session.allowed_secret_recipes),
            }
            for session in page.sessions
        ],
    }


class WebsiteExecutionSSHHandlers(SignedInAccessHandlers):
    refusal_event = "web_execution_ssh_refused"

    @off_loop
    def view(self, request: web.Request) -> web.Response:
        # Reads current roles via PersonChangeCheck and queries execution storage.
        person_id = self._person(request, write=False)
        page = self.application.execution_ssh.page(person_id=person_id)
        return self.operations._permission_json_response(
            {"status": "ok", **execution_ssh_payload(page)}
        )

    async def revoke_target(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=True)
        target_id = str(request.match_info.get("target_id") or "").strip()
        try:
            result = await self.application.execution_ssh.revoke_target(
                person_id=person_id, target_id=target_id
            )
        except ExecutionSSHRefused as exc:
            LOGGER.warning(
                "Unauthorized HTTP attempt to revoke an enrolled SSH target. "
                "event=%s action=revoke_target person_id=%s target_id=%s",
                self.refusal_event,
                person_id,
                target_id,
            )
            return self.operations._permission_json_response(
                {"status": "forbidden", "error": str(exc)}, status=403
            )
        except ExecutionSSHNotFound as exc:
            return self.operations._permission_json_response(
                {"status": "not_found", "error": str(exc)}, status=404
            )
        return self.operations._permission_json_response(
            {
                "status": "ok",
                "kind": result.kind,
                "resource_id": result.resource_id,
                "revoked": result.revoked,
                "broker_notified": result.broker_notified,
                "terminated_live": result.terminated_live,
                "message": result.message,
                **execution_ssh_payload(result.page),
            }
        )

    async def revoke_session(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=True)
        session_id = str(request.match_info.get("session_id") or "").strip()
        try:
            result = await self.application.execution_ssh.revoke_session(
                person_id=person_id, session_id=session_id
            )
        except ExecutionSSHRefused as exc:
            LOGGER.warning(
                "Unauthorized HTTP attempt to revoke an SSH session. "
                "event=%s action=revoke_session person_id=%s session_id=%s",
                self.refusal_event,
                person_id,
                session_id,
            )
            return self.operations._permission_json_response(
                {"status": "forbidden", "error": str(exc)}, status=403
            )
        except ExecutionSSHNotFound as exc:
            return self.operations._permission_json_response(
                {"status": "not_found", "error": str(exc)}, status=404
            )
        return self.operations._permission_json_response(
            {
                "status": "ok",
                "kind": result.kind,
                "resource_id": result.resource_id,
                "revoked": result.revoked,
                "broker_notified": result.broker_notified,
                "terminated_live": result.terminated_live,
                "message": result.message,
                **execution_ssh_payload(result.page),
            }
        )


__all__ = ["WebsiteExecutionSSHHandlers", "execution_ssh_payload"]
