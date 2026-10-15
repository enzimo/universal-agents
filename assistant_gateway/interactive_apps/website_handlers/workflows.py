"""Authenticated HTTP routes for the access workflow page.

Reads need the signed-in browser session; decisions also need the write
checks every permission action uses (same origin, fetch metadata and the
CSRF header). The person is always the session's user, and every decision
goes through the access Inbox (``assistant_gateway.access_workflows_page``).
"""

from __future__ import annotations

import asyncio

import logging
from dataclasses import asdict
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from access_control.decisions import HandDownRefused
from assistant_gateway.application.access_workflows import WorkflowDecisionsV2, WorkflowPageRefused
from assistant_gateway.application.website_application import WebsiteGatewayApplication
from assistant_gateway.interactive_apps.website_handlers.operations import (
    WebsiteSignedInHandlersOperations,
)
from assistant_gateway.interactive_apps.website_support import (
    _render_operator_ui_shell,
    _WEB_CHAT_CSP,
)

LOGGER = logging.getLogger(__name__)
ACTION_DIAGNOSTICS = "diagnostics"
ACTION_REVIEW = "review"
ACTION_DECISIONS = "decisions"
# The person marks a setup step they did outside Universal Agents as done.
ACTION_SETUP_DONE = "setup_done"
UNEXPECTED = "The workflow could not be read or changed. Refresh the page and try again."
BAD_SELECTION = "The decisions were not in the expected form. Refresh the page and try again."


class WebsiteWorkflowHandlers:
    def __init__(
        self,
        *,
        application: WebsiteGatewayApplication,
        operations: WebsiteSignedInHandlersOperations,
    ):
        self.application = application
        self.operations = operations

    async def page(self, request: web.Request) -> web.Response:
        return web.Response(
            text=_render_operator_ui_shell("workflow.html"),
            content_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": _WEB_CHAT_CSP,
            },
        )

    def _credential_actions(self, owner: str, workflow: dict[str, Any]) -> list[dict[str, Any]]:
        """Account-link forms offered in the workflow's own conversation (transient, owner-scoped)."""

        return [
            asdict(item.action)
            for item in self.application.transcripts.list_credential_actions(owner_canonical_user_id=owner)
            if item.session_key == workflow["source_session_key"]
            and item.correlation_id == workflow["source_correlation_id"]
        ]

    async def _get(self, request: web.Request, owner: str, workflow_id: str, action: str) -> dict[str, Any]:
        # The page refreshes every 5 seconds while open: each read runs in a
        # worker thread, off the loop the chat apps share (website_handlers.off_loop).
        service = self.application.workflows
        if not workflow_id:
            return await asyncio.to_thread(
                lambda: service.list(
                    owner_id=owner,
                    offset=max(0, int(request.query.get("offset", "0"))),
                    include_terminal=request.query.get("status") == "resolved",
                )
            )
        if action == ACTION_DIAGNOSTICS:
            return await asyncio.to_thread(lambda: service.diagnostics(workflow_id, owner_id=owner))
        result = await service.read(
            workflow_id, owner_id=owner, event_offset=max(0, int(request.query.get("event_offset", "0")))
        )
        result["credential_actions"] = await asyncio.to_thread(self._credential_actions, owner, result["workflow"])
        return result

    async def _post(self, request: web.Request, owner: str, workflow_id: str, action: str) -> dict[str, Any]:
        service = self.application.workflows
        if action == ACTION_REVIEW:
            return await service.prepare(workflow_id, owner_id=owner)
        if action == ACTION_DECISIONS:
            payload = await self.operations._json_payload(request)
            selection = WorkflowDecisionsV2.model_validate({"decisions": payload.get("decisions")})
            return await service.decide(workflow_id, owner_id=owner, selection=selection)
        if action == ACTION_SETUP_DONE:
            payload = await self.operations._json_payload(request)
            setup_request_id = str(payload.get("setup_request_id") or "").strip()[:160]
            return await service.mark_setup_done(workflow_id, owner_id=owner, setup_request_id=setup_request_id)
        raise ValueError("Unknown workflow action.")

    def _refused(self, text: str, *, status: int, workflow_id: str, action: str) -> web.Response:
        LOGGER.warning(
            "A workflow page action was refused. event=workflow_page_action_refused workflow_id=%s action=%s "
            "reason=%s",
            workflow_id,
            action or "read",
            text,
        )
        return self.operations._permission_json_response({"status": "refused", "error": text}, status=status)

    async def handle(self, request: web.Request) -> web.Response:
        write = request.method == "POST"
        session = (
            self.operations._require_permission_write_authority(request)
            if write
            else self.operations._require_permission_browser_session(request)
        )
        owner = str(session.canonical_user_id or "").strip()
        if not owner:
            raise web.HTTPForbidden(text="Sign in with a registered account to follow workflows.")
        workflow_id = request.match_info.get("workflow_id", "")
        action = request.match_info.get("action", "")
        try:
            result = await (self._post if write else self._get)(request, owner, workflow_id, action)
        except ValidationError:
            return self._refused(BAD_SELECTION, status=400, workflow_id=workflow_id, action=action)
        except (HandDownRefused, WorkflowPageRefused) as exc:
            # Written for people (access service refusals and the page's own).
            return self._refused(str(exc), status=409, workflow_id=workflow_id, action=action)
        except PermissionError as exc:
            # "Workflow not found": the same answer whether it is missing or someone else's.
            return self._refused(str(exc), status=404, workflow_id=workflow_id, action=action)
        except (RuntimeError, ValueError):
            LOGGER.exception(
                "A workflow page action failed. event=workflow_page_action_failed workflow_id=%s action=%s",
                workflow_id,
                action or "read",
            )
            return self.operations._permission_json_response({"status": "error", "error": UNEXPECTED}, status=500)
        response = self.operations._permission_json_response({"status": "ok", **result})
        if action == ACTION_DIAGNOSTICS:
            response.headers["Content-Disposition"] = 'attachment; filename="workflow-diagnostics.json"'
        return response
