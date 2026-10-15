from __future__ import annotations

import asyncio
from .off_loop import off_loop
from .pagination import list_page_query
from assistant_gateway.interactive_apps.website_support import require_website_runtime
from typing import Literal, cast
from aiohttp import web
from assistant_gateway.application.website_application import (
    WebAppearanceV1,
    WebMCPLifecycleRequestV1,
    WebMCPConnectionDecisionRequestV1,
    WebMCPConnectionListRequestV1,
    WebHostedMCPServerListRequestV1,
    WebMemoryListRequestV1,
    WebMemoryCreateRequestV1,
    WebMemoryMutationRequestV1,
    WebMemoryConflictError,
    WebObjectiveControlRequestV1,
    WebTaskListRequestV1,
    WebTaskControlRequestV1,
    WebScheduleControlRequestV1,
    WebsiteGatewayApplication,
)
from runtime_settings.appearance import OnboardingAppearanceSettings
from assistant_gateway.interactive_apps.host_ports import (
    InteractiveAppRuntimeContext,
)
from .operations import WebsiteControlsHandlersOperations


class WebsiteControlsHandlers:
    """Own controls HTTP behavior with explicit state and bound operations."""

    def __init__(
        self,
        *,
        application: WebsiteGatewayApplication,
        runtime: InteractiveAppRuntimeContext | None,
    ) -> None:
        self.operations: WebsiteControlsHandlersOperations
        self.application = application
        self.runtime = runtime

    async def _handle_web_chat_appearance(
        self,
        request: web.Request,
    ) -> web.Response:
        """Read or mutate the signed-in canonical user's appearance override."""

        web_session = (
            self.operations._require_permission_browser_session(request)
            if request.method == "GET"
            else self.operations._require_permission_write_authority(request)
        )
        canonical_user_id = str(web_session.canonical_user_id or "").strip()
        if not canonical_user_id:
            raise web.HTTPForbidden(
                text="Sign in with a registered user account to save appearance settings."
            )

        # Database work runs in a worker thread (website_handlers.off_loop).
        identity = self.application.identity
        if request.method == "GET":
            appearance = await asyncio.to_thread(identity.get_web_chat_appearance, canonical_user_id)
        elif request.method == "DELETE":
            await asyncio.to_thread(identity.reset_web_chat_appearance, canonical_user_id)
            appearance = None
        else:
            payload = await self.operations._json_payload(request)
            raw_appearance = payload.get("appearance")
            if not isinstance(raw_appearance, dict):
                raise web.HTTPBadRequest(text="appearance must be a JSON object.")
            try:
                normalized = OnboardingAppearanceSettings.model_validate(
                    raw_appearance
                ).model_dump(mode="json")
                appearance = await asyncio.to_thread(
                    lambda: identity.save_web_chat_appearance(
                        canonical_user_id=canonical_user_id,
                        appearance=WebAppearanceV1.model_validate(normalized),
                    )
                )
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc)) from exc

        response = web.json_response(
            {
                "status": "ok",
                "appearance": (
                    appearance.model_dump(
                        mode="json",
                        exclude={"schema_version"},
                    )
                    if appearance is not None
                    else None
                ),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @off_loop
    def _handle_web_chat_team(self, request: web.Request) -> web.Response:
        web_session = self.operations._require_permission_browser_session(request)
        session_key = str(request.query.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        context = self.operations._permission_api_context(
            request,
            web_session,
            session_key=session_key,
        )
        team = self.application.operations.list_web_chat_team_members()
        return web.json_response(
            {
                "status": "ok",
                "authorization_presentation_mode": (
                    require_website_runtime(
                        self.runtime
                    ).authorization_presentation_mode.value
                ),
                **team.model_dump(mode="json"),
                "mcp_connections": self.application.operations.list_web_chat_mcp_connections(
                    WebMCPConnectionListRequestV1(context=context)
                ).model_dump(mode="json")["connections"],
                "hosted_mcp_servers": self.application.operations.list_web_chat_hosted_mcp_servers(
                    WebHostedMCPServerListRequestV1(context=context)
                ).model_dump(mode="json")["servers"],
            }
        )

    async def _handle_web_chat_memories(
        self,
        request: web.Request,
    ) -> web.Response:
        web_session = self.operations._require_permission_browser_session(request)
        session_key = str(request.query.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        scope = str(request.query.get("scope") or "personal").strip().lower()
        if scope not in {"personal", "team"}:
            raise web.HTTPBadRequest(text="scope must be personal or team.")
        typed_scope = cast(Literal["personal", "team"], scope)
        try:
            page = list_page_query(request)
            context = await asyncio.to_thread(
            self.operations._permission_api_context,
                request,
                web_session,
                session_key=session_key,
            )
            result = await self.application.operations.list_web_chat_memories(
                WebMemoryListRequestV1(
                    context=context,
                    scope=typed_scope,
                    **page,
                )
            )
        except PermissionError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except RuntimeError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return self.operations._permission_json_response(
            {"status": "ok", **result.model_dump(mode="json")}
        )

    async def _handle_web_chat_memory_mutation(
        self,
        request: web.Request,
    ) -> web.Response:
        web_session = self.operations._require_permission_write_authority(request)
        payload = await self.operations._json_payload(request)
        session_key = str(payload.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        scope = str(request.match_info.get("scope") or "").strip().lower()
        if scope not in {"personal", "team"}:
            raise web.HTTPBadRequest(text="scope must be personal or team.")
        typed_scope = cast(Literal["personal", "team"], scope)
        creating = request.method == "POST"
        memory_id_text = str(request.match_info.get("memory_id") or "").strip()
        try:
            memory_id = None if creating else int(memory_id_text)
        except ValueError as exc:
            raise web.HTTPBadRequest(text="memory_id must be an integer.") from exc
        action = "edit" if request.method in {"PATCH", "POST"} else "delete"
        tags_payload = payload.get("tags")
        if action == "edit" and (
            not isinstance(tags_payload, list)
            or any(not isinstance(tag, str) for tag in tags_payload)
        ):
            raise web.HTTPBadRequest(text="tags must be an array of strings.")
        context = await asyncio.to_thread(
            self.operations._permission_api_context,
            request,
            web_session,
            session_key=session_key,
        )
        try:
            if creating:
                result = await self.application.operations.create_web_chat_memory(
                    WebMemoryCreateRequestV1(
                        context=context,
                        scope=typed_scope,
                        fact_text=str(payload.get("fact_text") or "").strip(),
                        tags=tuple(cast(list[str], tags_payload)),
                        invalid_after_utc=payload.get("invalid_after_utc"),
                    )
                )
                return self.operations._permission_json_response(
                    {
                        "status": "ok",
                        **result.model_dump(mode="json", exclude_none=True),
                    }
                )
            assert memory_id is not None
            result = await self.application.operations.mutate_web_chat_memory(
                WebMemoryMutationRequestV1(
                    context=context,
                    scope=typed_scope,
                    memory_id=memory_id,
                    action=action,
                    expected_record_digest=str(
                        payload.get("record_digest") or ""
                    ).strip(),
                    fact_text=(
                        str(payload.get("fact_text") or "").strip()
                        if action == "edit"
                        else None
                    ),
                    tags=(
                        tuple(cast(list[str], tags_payload))
                        if action == "edit"
                        else None
                    ),
                    invalid_after_utc=(
                        payload.get("invalid_after_utc") if action == "edit" else None
                    ),
                )
            )
        except WebMemoryConflictError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        except PermissionError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except RuntimeError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return self.operations._permission_json_response(
            {"status": "ok", **result.model_dump(mode="json", exclude_none=True)}
        )

    async def _handle_web_chat_tasks(self, request: web.Request) -> web.Response:
        """Read bounded live tasks or apply a CSRF-bound cancellation."""
        listing = request.method == "GET"
        web_session = (
            self.operations._require_permission_browser_session(request) if listing
            else self.operations._require_permission_write_authority(request)
        )
        payload = request.query if listing else await self.operations._json_payload(request)
        session_key = str(payload.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(text="Open a conversation before managing tasks.")
        context = await asyncio.to_thread(self.operations._permission_api_context, request, web_session, session_key=session_key)
        try:
            if listing:
                result = await self.application.operations.list_web_chat_tasks(
                    WebTaskListRequestV1(context=context, agent_id=payload.get("agent_id"), view=payload.get("view", "active"), **list_page_query(request))
                )
            else:
                result = await self.application.operations.control_web_chat_tasks(
                    WebTaskControlRequestV1(
                        context=context, action=payload.get("action"), agent_id=payload.get("agent_id"),
                        task_id=payload.get("task_id"), inventory_digest=payload.get("inventory_digest"),
                    )
                )
        except PermissionError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except TimeoutError as exc:
            raise web.HTTPGatewayTimeout(text=str(exc)) from exc
        except (RuntimeError, ValueError, LookupError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return self.operations._permission_json_response(
            {"status": "ok", **result.model_dump(mode="json")}
        )

    async def _handle_web_chat_objectives(
        self,
        request: web.Request,
    ) -> web.Response:
        """Return authoritative Objective state owned by the browser user."""
        web_session = self.operations._require_permission_browser_session(request)
        session_key = str(request.query.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        context = await asyncio.to_thread(
            self.operations._permission_api_context,
            request,
            web_session,
            session_key=session_key,
        )
        try:
            result = await self.application.operations.list_web_chat_objectives(context)
        except PermissionError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except TimeoutError as exc:
            raise web.HTTPGatewayTimeout(text=str(exc)) from exc
        except (RuntimeError, ValueError, LookupError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return self.operations._permission_json_response(
            {"status": "ok", **result.model_dump(mode="json")}
        )

    async def _handle_web_chat_objective_control(
        self,
        request: web.Request,
    ) -> web.Response:
        """Apply a CSRF-bound lifecycle action to one exact Objective version."""
        web_session = self.operations._require_permission_write_authority(request)
        payload = await self.operations._json_payload(request)
        session_key = str(payload.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        context = await asyncio.to_thread(
            self.operations._permission_api_context,
            request,
            web_session,
            session_key=session_key,
        )
        try:
            result = await self.application.operations.control_web_chat_objective(
                WebObjectiveControlRequestV1(
                    context=context,
                    objective_id=str(
                        request.match_info.get("objective_id") or ""
                    ).strip(),
                    action=str(payload.get("action") or "").strip(),
                    expected_record_digest=str(
                        payload.get("expected_record_digest") or ""
                    ).strip(),
                )
            )
        except PermissionError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except TimeoutError as exc:
            raise web.HTTPGatewayTimeout(text=str(exc)) from exc
        except (RuntimeError, ValueError, LookupError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return self.operations._permission_json_response(
            {"status": "ok", **result.model_dump(mode="json")}
        )

    async def _handle_web_chat_schedules(
        self,
        request: web.Request,
    ) -> web.Response:
        """Return owner-wide schedules visible to the authenticated user."""
        web_session = self.operations._require_permission_browser_session(request)
        session_key = str(request.query.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        context = await asyncio.to_thread(
            self.operations._permission_api_context,
            request,
            web_session,
            session_key=session_key,
        )
        try:
            result = await self.application.operations.list_web_chat_schedules(context)
        except PermissionError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except TimeoutError as exc:
            raise web.HTTPGatewayTimeout(text=str(exc)) from exc
        except (RuntimeError, ValueError, LookupError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return self.operations._permission_json_response(
            {"status": "ok", **result.model_dump(mode="json")}
        )

    async def _handle_web_chat_schedule_control(
        self,
        request: web.Request,
    ) -> web.Response:
        """Apply a CSRF-bound schedule action with optimistic concurrency."""
        web_session = self.operations._require_permission_write_authority(request)
        payload = await self.operations._json_payload(request)
        session_key = str(payload.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        context = await asyncio.to_thread(
            self.operations._permission_api_context,
            request,
            web_session,
            session_key=session_key,
        )
        try:
            result = await self.application.operations.control_web_chat_schedule(
                WebScheduleControlRequestV1(
                    context=context,
                    schedule_id=str(
                        request.match_info.get("schedule_id") or ""
                    ).strip(),
                    action=str(payload.get("action") or "").strip(),
                    expected_record_digest=str(
                        payload.get("expected_record_digest") or ""
                    ).strip(),
                    paused_until=payload.get("paused_until"),
                )
            )
        except PermissionError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except TimeoutError as exc:
            raise web.HTTPGatewayTimeout(text=str(exc)) from exc
        except (RuntimeError, ValueError, LookupError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return self.operations._permission_json_response(
            {"status": "ok", **result.model_dump(mode="json")}
        )

    async def _handle_web_chat_mcp_lifecycle(
        self,
        request: web.Request,
    ) -> web.Response:
        """Apply a recoverable lifecycle action to one exact MCP resource."""
        web_session = self.operations._require_permission_write_authority(request)
        payload = await self.operations._json_payload(request)
        session_key = str(payload.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        context = await asyncio.to_thread(
            self.operations._permission_api_context,
            request,
            web_session,
            session_key=session_key,
        )
        try:
            result = await self.application.operations.control_web_chat_mcp_lifecycle(
                WebMCPLifecycleRequestV1(
                    context=context,
                    resource_kind=str(
                        request.match_info.get("resource_kind") or ""
                    ).strip(),
                    resource_id=str(
                        request.match_info.get("resource_id") or ""
                    ).strip(),
                    action=str(payload.get("action") or "").strip(),
                    expected_digest=str(payload.get("expected_digest") or "").strip(),
                    expected_lifecycle=str(
                        payload.get("expected_lifecycle") or ""
                    ).strip(),
                )
            )
        except PermissionError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except TimeoutError as exc:
            raise web.HTTPGatewayTimeout(text=str(exc)) from exc
        except (RuntimeError, ValueError, LookupError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return self.operations._permission_json_response(
            {"status": "ok", **result.model_dump(mode="json")}
        )

    async def _handle_web_chat_mcp_connection_decision(
        self,
        request: web.Request,
    ) -> web.Response:
        web_session = self.operations._require_permission_write_authority(request)
        payload = await self.operations._json_payload(request)
        session_key = str(payload.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        context = await asyncio.to_thread(
            self.operations._permission_api_context,
            request,
            web_session,
            session_key=session_key,
        )
        try:
            result = self.application.operations.decide_web_chat_mcp_connection(
                WebMCPConnectionDecisionRequestV1(
                    context=context,
                    connection_id=str(request.match_info.get("connection_id") or ""),
                    expected_record_digest=str(
                        payload.get("expected_record_digest") or ""
                    ),
                    decision=str(payload.get("decision") or ""),
                    reason=str(payload.get("reason") or ""),
                )
            )
        except (PermissionError, RuntimeError, ValueError) as exc:
            return self.operations._authorization_error_response(
                exc,
                operation="save this MCP server decision",
            )
        return self.operations._permission_json_response(
            result.model_dump(mode="json", exclude_none=True)
        )
