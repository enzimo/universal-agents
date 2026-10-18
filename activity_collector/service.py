"""Standalone HTTP activity collector service with user/admin dashboard views."""

from __future__ import annotations

import asyncio
import json
import logging
import hmac
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from aiohttp import BasicAuth, web

from activity_collector.settings import CollectorRuntimeSettings
from runtime_hosting.aiohttp import start_aiohttp_site
from runtime_hosting.page_icon import favicon_link_tag
from default_values import AGENT_DEFAULTS
from custom_messages.contracts.activity_v1 import TaskActivityEvent
from custom_messages.enums import TaskActivityStage
from storage.activity_collector_repository import (
    ActivityCollectorHealth,
    ActivityCollectorRepository,
    create_activity_collector_repository,
)

INGEST_TOKEN_HEADER = "X-Activity-Collector-Token"
ACTIVITY_COLLECTOR_HTTP_MAX_BODY_BYTES = 1_048_576


@dataclass
class CollectorMetrics:
    ingest_requests: int = 0
    ingested_events: int = 0
    duplicate_or_ignored_events: int = 0
    last_ingest_at: Optional[str] = None


@dataclass
class CollectorAgentView:
    agent_id: str
    name: Optional[str] = None
    named_agent_type: Optional[str] = None
    is_team_lead: bool = False
    is_executing: Optional[bool] = None
    has_execution_capacity: Optional[bool] = None
    is_paused: Optional[bool] = None
    is_exiting: bool = False
    top_level_queue_depth: Optional[int] = None
    waiting_top_level_task_count: Optional[int] = None
    deferred_message_count: Optional[int] = None
    diagnostic: dict[str, Any] | None = None
    last_seen_at: Optional[str] = None
    latest_stage: Optional[str] = None
    latest_activity_type: Optional[str] = None
    active_task_ids: set[str] | None = None


def _new_objective_state(event: TaskActivityEvent) -> dict[str, Any]:
    return {
        "objective_id": event.objective_id,
        "objective_title": event.objective_title,
        "last_updated_at": event.created_at,
        "latest_summary": event.summary,
        "latest_activity_type": event.activity_type.value,
        "latest_stage": event.stage.value,
        "task_ids": set(),
        "event_count": 0,
        "objective": None,
        "plan": None,
        "work_policy": None,
        "work_sessions": [],
        "checkpoints": [],
        "subgoals": [],
    }


def _apply_objective_snapshot(state: dict[str, Any], event: TaskActivityEvent) -> None:
    details = event.details if isinstance(event.details, dict) else {}
    objective = details.get("objective")
    if not isinstance(objective, dict) and details.get("message_class") == "Objective":
        objective = details
    if isinstance(objective, dict):
        state["objective"] = objective
        state["objective_title"] = objective.get("title") or state.get("objective_title")

    plan = details.get("plan")
    if not isinstance(plan, dict) and details.get("message_class") == "Plan":
        plan = details
    if isinstance(plan, dict):
        state["plan"] = plan

    work = details.get("objective_work")
    if isinstance(work, dict):
        state["work_policy"] = work.get("policy")
        state["work_sessions"] = list(work.get("sessions") or [])
        state["checkpoints"] = list(work.get("checkpoints") or [])


def _finalize_objective_state(state: dict[str, Any]) -> None:
    state["task_ids"] = sorted(state["task_ids"])
    objective = state.get("objective") or {}
    plan = state.get("plan") or {}
    state["status"] = objective.get("status") or state["latest_stage"]
    state["horizon"] = objective.get("horizon")
    state["priority"] = objective.get("priority")
    state["desired_outcome"] = objective.get("desired_outcome")
    state["why"] = objective.get("why")
    state["progress_summary"] = objective.get("progress_summary") or state["latest_summary"]
    state["current_focus"] = objective.get("current_focus") or plan.get("current_focus")
    state["success_criteria"] = list(objective.get("success_criteria") or [])
    state["subgoals"] = list(plan.get("plan_steps") or plan.get("plan_items") or [])


class SseBroadcaster:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = {
            "user": set(),
            "admin": set(),
        }

    def subscribe(self, audience: str) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=10)
        self._subscribers[audience].add(queue)
        return queue

    def unsubscribe(self, audience: str, queue: asyncio.Queue[str]) -> None:
        self._subscribers.get(audience, set()).discard(queue)

    def publish(self, audience: str, payload: dict[str, Any]) -> None:
        message = json.dumps(payload)
        for queue in list(self._subscribers.get(audience, set())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                continue


class ActivityCollectorService:
    def __init__(
        self,
        config: CollectorRuntimeSettings,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self._logger = logger or logging.getLogger(__name__)
        self._user_token = config.activity_collector.require_user_token()
        self._admin_token = config.activity_collector.require_admin_token()
        self._ingest_token = config.activity_collector.require_ingest_token()
        self._repository: ActivityCollectorRepository = create_activity_collector_repository(
            config.storage,
            logger=self._logger,
        )
        self._metrics = CollectorMetrics()
        self._broadcaster = SseBroadcaster()

    def build_app(self) -> web.Application:
        app = web.Application(
            middlewares=[self._auth_middleware],
            client_max_size=ACTIVITY_COLLECTOR_HTTP_MAX_BODY_BYTES,
        )
        app.router.add_post("/api/activity/events", self._handle_single_event)
        app.router.add_post("/api/activity/batch", self._handle_batch_events)
        app.router.add_get(self.config.activity_collector.user_url_path, self._handle_user_page)
        app.router.add_get(self.config.activity_collector.admin_url_path, self._handle_admin_page)
        app.router.add_get("/api/user/team-summary", self._handle_user_summary)
        app.router.add_get("/api/user/recent-activity", self._handle_user_recent_activity)
        app.router.add_get("/api/user/objectives", self._handle_user_objectives)
        app.router.add_get("/api/admin/agents", self._handle_admin_agents)
        app.router.add_get("/api/admin/tasks", self._handle_admin_tasks)
        app.router.add_get("/api/admin/objectives", self._handle_admin_objectives)
        app.router.add_get("/api/admin/activity", self._handle_admin_activity)
        app.router.add_get("/api/admin/health", self._handle_admin_health)
        app.router.add_get("/api/stream/user", self._handle_user_stream)
        app.router.add_get("/api/stream/admin", self._handle_admin_stream)
        return app

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        audience = self._classify_audience(request.path)
        if audience is not None:
            if audience == "admin":
                expected_token = self._admin_token
                supplied = self._extract_token(request, include_basic=True)
            elif audience == "ingest":
                expected_token = self._ingest_token
                supplied = self._extract_token(
                    request,
                    include_basic=False,
                    extra_header=INGEST_TOKEN_HEADER,
                    include_shared_header=False,
                )
            else:
                expected_token = self._user_token
                supplied = self._extract_token(request, include_basic=True)
            if not hmac.compare_digest(
                (supplied or "").encode("utf-8"), expected_token.encode("utf-8")
            ):
                headers = (
                    {"WWW-Authenticate": f'Basic realm="Activity collector {audience}", charset="UTF-8"'}
                    if audience in {"admin", "user"}
                    else {}
                )
                raise web.HTTPUnauthorized(
                    text="Missing or invalid shared-secret token.", headers=headers
                )
        return await handler(request)

    def _classify_audience(self, path: str) -> Optional[str]:
        if path.startswith("/api/activity/"):
            return "ingest"
        if path == self.config.activity_collector.admin_url_path or path.startswith("/api/admin") or path.startswith("/api/stream/admin"):
            return "admin"
        if path == self.config.activity_collector.user_url_path or path.startswith("/api/user") or path.startswith("/api/stream/user"):
            return "user"
        return None

    @staticmethod
    def _extract_token(
        request: web.Request,
        *,
        include_basic: bool,
        extra_header: str | None = None,
        include_shared_header: bool = True,
    ) -> Optional[str]:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            return auth_header.split(" ", 1)[1].strip() or None
        # Native browser authentication keeps dashboard and EventSource tokens
        # in headers. URL query parameters are never credential channels.
        if include_basic and auth_header.lower().startswith("basic "):
            try:
                return BasicAuth.decode(auth_header, encoding="utf-8").password
            except ValueError:
                return None
        if extra_header and request.headers.get(extra_header):
            return request.headers.get(extra_header)
        if include_shared_header and request.headers.get("X-Shared-Token"):
            return request.headers.get("X-Shared-Token")
        return None

    async def _handle_single_event(self, request: web.Request) -> web.Response:
        payload = await request.json()
        event = TaskActivityEvent.model_validate(payload)
        inserted = self._repository.upsert_events([event])
        self._record_ingest(inserted=inserted, received=1)
        self._broadcast_refresh()
        return web.json_response({"accepted": 1, "inserted": inserted}, status=202)

    async def _handle_batch_events(self, request: web.Request) -> web.Response:
        payload = await request.json()
        raw_events = list(payload.get("events") or [])
        batch_limit = self.config.activity_collector.batch_max_events
        if len(raw_events) > batch_limit:
            self._logger.warning(
                "Rejecting oversized activity collector batch received=%s limit=%s remote=%s",
                len(raw_events),
                batch_limit,
                request.remote,
            )
            raise web.HTTPRequestEntityTooLarge(
                max_size=batch_limit,
                actual_size=len(raw_events),
                text=f"Activity event batch exceeds limit of {batch_limit}.",
            )
        events = [TaskActivityEvent.model_validate(item) for item in raw_events]
        inserted = self._repository.upsert_events(events)
        self._record_ingest(inserted=inserted, received=len(events))
        self._broadcast_refresh()
        return web.json_response({"accepted": len(events), "inserted": inserted}, status=202)

    def _record_ingest(self, *, inserted: int, received: int) -> None:
        self._metrics.ingest_requests += 1
        self._metrics.ingested_events += inserted
        self._metrics.duplicate_or_ignored_events += max(0, received - inserted)
        self._metrics.last_ingest_at = datetime.now(timezone.utc).isoformat()

    def _broadcast_refresh(self) -> None:
        payload = {"type": "refresh", "timestamp": datetime.now(timezone.utc).isoformat()}
        self._broadcaster.publish("user", payload)
        self._broadcaster.publish("admin", payload)

    def _list_events(
        self,
        *,
        limit: int = 200,
        user_visible: Optional[bool] = None,
        objective_id: Optional[str] = None,
    ) -> list[TaskActivityEvent]:
        return list(
            reversed(
                self._repository.list_recent_events(
                    limit=limit,
                    team_id=self.config.team_id,
                    user_visible=user_visible,
                    objective_id=objective_id,
                )
            )
        )

    def _list_agents(self, events: list[TaskActivityEvent]) -> list[CollectorAgentView]:
        agents: dict[str, CollectorAgentView] = {}

        def ensure_agent(
            *,
            agent_id: Optional[str],
            name: Optional[str],
            named_agent_type: Optional[str],
            is_team_lead: bool,
        ) -> Optional[CollectorAgentView]:
            stable_id = str(agent_id or "").strip()
            if not stable_id:
                return None
            agent = agents.get(stable_id)
            if agent is None:
                agent = CollectorAgentView(
                    agent_id=stable_id,
                    name=name,
                    named_agent_type=named_agent_type,
                    is_team_lead=is_team_lead,
                    active_task_ids=set(),
                )
                agents[stable_id] = agent
            agent.name = name or agent.name
            agent.named_agent_type = named_agent_type or agent.named_agent_type
            agent.is_team_lead = agent.is_team_lead or is_team_lead
            return agent

        for event in events:
            manager = ensure_agent(
                agent_id=event.manager_agent_id,
                name=event.manager_name,
                named_agent_type=AGENT_DEFAULTS.named_agent_type.value,
                is_team_lead=True,
            )
            emitter = ensure_agent(
                agent_id=event.agent_id,
                name=event.agent_name,
                named_agent_type=event.named_agent_type,
                is_team_lead=bool(event.manager_agent_id and event.agent_id == event.manager_agent_id),
            )
            for agent in (manager, emitter):
                if agent is None:
                    continue
                agent.last_seen_at = event.created_at
                agent.latest_stage = event.stage.value
                agent.latest_activity_type = event.activity_type.value
                task_id = event.top_level_task_id or event.task_id
                if task_id and agent.active_task_ids is not None:
                    if event.stage in {
                        TaskActivityStage.COMPLETED,
                        TaskActivityStage.FAILED,
                        TaskActivityStage.CANCELLED,
                    }:
                        agent.active_task_ids.discard(task_id)
                    else:
                        agent.active_task_ids.add(task_id)

        for agent in agents.values():
            active_task_count = len(agent.active_task_ids or set())
            agent.is_executing = active_task_count > 0
            agent.has_execution_capacity = active_task_count == 0
            agent.diagnostic = {
                "activity_source": "collector_events",
                "active_task_count": active_task_count,
                "latest_stage": agent.latest_stage,
                "latest_activity_type": agent.latest_activity_type,
            }
        return list(agents.values())

    def _build_task_states(self, events: list[TaskActivityEvent]) -> list[dict[str, Any]]:
        task_states: dict[str, dict[str, Any]] = {}
        for event in events:
            top_level_task_id = event.top_level_task_id or event.task_id
            if not top_level_task_id:
                continue
            state = task_states.setdefault(
                top_level_task_id,
                {
                    "top_level_task_id": top_level_task_id,
                    "current_summary": event.summary,
                    "current_detail": event.detail,
                    "stage": event.stage.value,
                    "manager_name": event.manager_name,
                    "manager_agent_id": event.manager_agent_id,
                    "objective_id": event.objective_id,
                    "objective_title": event.objective_title,
                    "last_updated_at": event.created_at,
                    "latest_activity_type": event.activity_type.value,
                    "latest_agent_name": event.agent_name,
                    "latest_agent_id": event.agent_id,
                    "events": [],
                },
            )
            state["current_summary"] = event.summary
            state["current_detail"] = event.detail
            state["stage"] = event.stage.value
            state["manager_name"] = event.manager_name or state.get("manager_name")
            state["manager_agent_id"] = event.manager_agent_id or state.get("manager_agent_id")
            state["objective_id"] = event.objective_id or state.get("objective_id")
            state["objective_title"] = event.objective_title or state.get("objective_title")
            state["last_updated_at"] = event.created_at
            state["latest_activity_type"] = event.activity_type.value
            state["latest_agent_name"] = event.agent_name
            state["latest_agent_id"] = event.agent_id
            state["events"].append(
                {
                    "created_at": event.created_at,
                    "summary": event.summary,
                    "detail": event.detail,
                    "activity_type": event.activity_type.value,
                    "stage": event.stage.value,
                    "agent_name": event.agent_name,
                    "agent_id": event.agent_id,
                    "objective_id": event.objective_id,
                    "objective_title": event.objective_title,
                    "trace_id": event.trace_id,
                    "trace_url": self._trace_url(event.trace_id),
                    "details": event.details,
                }
            )
        ordered = sorted(task_states.values(), key=lambda item: item["last_updated_at"], reverse=True)
        for item in ordered:
            item["is_terminal"] = item["stage"] in {
                TaskActivityStage.COMPLETED.value,
                TaskActivityStage.FAILED.value,
                TaskActivityStage.CANCELLED.value,
            }
        return ordered

    def _build_objective_states(self, events: list[TaskActivityEvent]) -> list[dict[str, Any]]:
        objective_states: dict[str, dict[str, Any]] = {}
        for event in events:
            if not event.objective_id:
                continue
            state = objective_states.setdefault(event.objective_id, _new_objective_state(event))
            state["objective_title"] = event.objective_title or state.get("objective_title")
            state["last_updated_at"] = event.created_at
            state["latest_summary"] = event.summary
            state["latest_activity_type"] = event.activity_type.value
            state["latest_stage"] = event.stage.value
            state["event_count"] += 1
            _apply_objective_snapshot(state, event)
            task_id = event.top_level_task_id or event.task_id
            if task_id:
                state["task_ids"].add(task_id)
        ordered = sorted(objective_states.values(), key=lambda item: item["last_updated_at"], reverse=True)
        for state in ordered:
            _finalize_objective_state(state)
        return ordered

    def _build_team_summary(self, agents: list[CollectorAgentView], events: list[TaskActivityEvent]) -> dict[str, Any]:
        active_agents = [status for status in agents if not status.is_exiting]
        latest_event = events[-1] if events else None
        manager = next((status for status in active_agents if status.is_team_lead), None)
        manager_name = manager.name if manager else (latest_event.manager_name if latest_event else None)
        manager_agent_id = manager.agent_id if manager else (latest_event.manager_agent_id if latest_event else None)

        by_type: dict[str, int] = defaultdict(int)
        for status in active_agents:
            by_type[status.named_agent_type or "unknown"] += 1

        recently_spawned = [
            {
                "created_at": event.created_at,
                "summary": event.summary,
                "agent_name": event.agent_name,
                "named_agent_type": event.named_agent_type,
            }
            for event in events
            if event.activity_type.value in {"delegated", "worker_claimed", "worker_reclaimed"}
        ][-12:]

        return {
            "team_id": self.config.team_id,
            "manager_name": manager_name,
            "manager_agent_id": manager_agent_id,
            "active_agent_count": len(active_agents),
            "team_composition": dict(sorted(by_type.items())),
            "active_workers": [
                {
                    "agent_id": status.agent_id,
                    "name": status.name,
                    "named_agent_type": status.named_agent_type,
                    "is_executing": status.is_executing,
                    "is_paused": status.is_paused,
                    "top_level_queue_depth": status.top_level_queue_depth,
                    "waiting_top_level_task_count": status.waiting_top_level_task_count,
                    "deferred_message_count": status.deferred_message_count,
                }
                for status in sorted(active_agents, key=lambda item: (item.is_team_lead is not True, item.name or ""))
            ],
            "spawned_or_reclaimed_workers": recently_spawned,
        }

    def _build_user_payload(self) -> dict[str, Any]:
        events = self._list_events(limit=400, user_visible=True)
        agents = self._list_agents(events)
        task_states = self._build_task_states(events)
        objective_states = self._build_objective_states(events)
        active_tasks = [task for task in task_states if not task["is_terminal"]]
        recent_activity = [
            {
                "activity_event_id": event.activity_event_id,
                "created_at": event.created_at,
                "summary": event.summary,
                "detail": event.detail,
                "activity_type": event.activity_type.value,
                "stage": event.stage.value,
                "agent_name": event.agent_name,
                "session_key": event.session_key,
                "conversation_id": event.conversation_id,
                "top_level_task_id": event.top_level_task_id,
                "objective_id": event.objective_id,
                "objective_title": event.objective_title,
                "user_visible": event.user_visible,
                "details": event.details,
            }
            for event in events[-30:]
        ]
        return {
            "team": self._build_team_summary(agents, events),
            "objectives": objective_states,
            "tasks_in_progress": active_tasks,
            "recent_activity": recent_activity,
        }

    def _build_admin_payload(self) -> dict[str, Any]:
        events = self._list_events(limit=600, user_visible=None)
        agents = self._list_agents(events)
        task_states = self._build_task_states(events)
        objective_states = self._build_objective_states(events)
        return {
            "agents": [
                {
                    "agent_id": status.agent_id,
                    "name": status.name,
                    "named_agent_type": status.named_agent_type,
                    "is_team_lead": status.is_team_lead,
                    "is_executing": status.is_executing,
                    "has_execution_capacity": status.has_execution_capacity,
                    "is_paused": status.is_paused,
                    "is_exiting": status.is_exiting,
                    "top_level_queue_depth": status.top_level_queue_depth,
                    "waiting_top_level_task_count": status.waiting_top_level_task_count,
                    "deferred_message_count": status.deferred_message_count,
                    "diagnostic": status.diagnostic or {},
                    "last_seen_at": status.last_seen_at,
                    "recent_activity": [
                        {
                            "created_at": event.created_at,
                            "summary": event.summary,
                            "activity_type": event.activity_type.value,
                            "stage": event.stage.value,
                            "trace_id": event.trace_id,
                            "trace_url": self._trace_url(event.trace_id),
                        }
                        for event in events
                        if event.agent_id == status.agent_id
                    ][-12:],
                }
                for status in sorted(agents, key=lambda item: (item.is_team_lead is not True, item.name or ""))
            ],
            "objectives": objective_states,
            "tasks": task_states,
            "activity": [
                {
                    "created_at": event.created_at,
                    "summary": event.summary,
                    "detail": event.detail,
                    "activity_type": event.activity_type.value,
                    "stage": event.stage.value,
                    "agent_name": event.agent_name,
                    "agent_id": event.agent_id,
                    "top_level_task_id": event.top_level_task_id,
                    "objective_id": event.objective_id,
                    "objective_title": event.objective_title,
                    "trace_id": event.trace_id,
                    "trace_url": self._trace_url(event.trace_id),
                    "details": event.details,
                    "user_visible": event.user_visible,
                }
                for event in events[-80:]
            ],
        }

    def _trace_url(self, trace_id: Optional[str]) -> Optional[str]:
        base_url = self.config.activity_collector.otel_trace_base_url
        if not trace_id or not base_url:
            return None
        if "{trace_id}" in base_url:
            return base_url.format(trace_id=trace_id)
        return f"{base_url.rstrip('/')}/{trace_id}"

    async def _handle_user_summary(self, request: web.Request) -> web.Response:
        return web.json_response(self._build_user_payload())

    async def _handle_user_recent_activity(self, request: web.Request) -> web.Response:
        payload = self._build_user_payload()
        return web.json_response({"recent_activity": payload["recent_activity"]})

    async def _handle_user_objectives(self, request: web.Request) -> web.Response:
        payload = self._build_user_payload()
        return web.json_response({"objectives": payload["objectives"]})

    async def _handle_admin_agents(self, request: web.Request) -> web.Response:
        return web.json_response({"agents": self._build_admin_payload()["agents"]})

    async def _handle_admin_tasks(self, request: web.Request) -> web.Response:
        return web.json_response({"tasks": self._build_admin_payload()["tasks"]})

    async def _handle_admin_objectives(self, request: web.Request) -> web.Response:
        return web.json_response({"objectives": self._build_admin_payload()["objectives"]})

    async def _handle_admin_activity(self, request: web.Request) -> web.Response:
        return web.json_response({"activity": self._build_admin_payload()["activity"]})

    async def _handle_admin_health(self, request: web.Request) -> web.Response:
        repository_health: ActivityCollectorHealth = self._repository.get_health()
        return web.json_response(
            {
                "repository": asdict(repository_health),
                "collector": asdict(self._metrics),
                "team_id": self.config.team_id,
            }
        )

    async def _handle_user_page(self, request: web.Request) -> web.Response:
        return web.Response(text=self._render_user_html(), content_type="text/html")

    async def _handle_admin_page(self, request: web.Request) -> web.Response:
        return web.Response(text=self._render_admin_html(), content_type="text/html")

    async def _handle_user_stream(self, request: web.Request) -> web.StreamResponse:
        return await self._stream_events(request, audience="user")

    async def _handle_admin_stream(self, request: web.Request) -> web.StreamResponse:
        return await self._stream_events(request, audience="admin")

    async def _stream_events(self, request: web.Request, *, audience: str) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)
        queue = self._broadcaster.subscribe(audience)
        try:
            await response.write(b"data: {\"type\":\"ready\"}\n\n")
            while True:
                payload = await queue.get()
                await response.write(f"data: {payload}\n\n".encode("utf-8"))
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        finally:
            self._broadcaster.unsubscribe(audience, queue)

    def _render_user_html(self) -> str:
        return self._render_html(
            title="Team Activity",
            summary_endpoint="/api/user/team-summary",
            stream_endpoint="/api/stream/user",
            mode="user",
        )

    def _render_admin_html(self) -> str:
        return self._render_html(
            title="Team Admin",
            summary_endpoint="/api/admin/activity",
            stream_endpoint="/api/stream/admin",
            mode="admin",
        )

    def _render_html(self, *, title: str, summary_endpoint: str, stream_endpoint: str, mode: str) -> str:
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  {favicon_link_tag()}
  <style>
    :root {{
      --bg: #f2f5f7;
      --fg: #17212b;
      --muted: #637083;
      --panel: rgba(255,255,255,.88);
      --line: #d8e0e7;
      --accent: #176b87;
      --good: #1f7a55;
      --warn: #a56012;
    }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; background: radial-gradient(circle at top left, #fff, var(--bg)); color: var(--fg); }}
    main {{ max-width: 1360px; margin: 0 auto; padding: 24px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
    .panel {{ background: var(--panel); backdrop-filter: blur(14px); border: 1px solid var(--line); border-radius: 14px; padding: 16px; box-shadow: 0 10px 30px rgba(18,37,54,.05); }}
    .wide {{ grid-column: 1 / -1; }}
    .objective-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 12px; }}
    .objective {{ border: 1px solid var(--line); border-radius: 12px; padding: 14px; background: rgba(255,255,255,.72); }}
    .row {{ display: flex; gap: 8px; align-items: center; justify-content: space-between; flex-wrap: wrap; }}
    .badge {{ display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px; font-size: 12px; color: var(--muted); }}
    .budget {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 10px; }}
    .metric {{ background: #edf3f6; border-radius: 8px; padding: 7px; font-size: 12px; }}
    .subgoals {{ list-style: none; padding: 0; }}
    .subgoals li {{ border-left: 3px solid var(--line); padding-left: 9px; }}
    .subgoals li[data-status="completed"] {{ border-color: var(--good); }}
    .subgoals li[data-status="in_progress"] {{ border-color: var(--accent); }}
    .muted {{ color: var(--muted); }}
    .mono {{ font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px; }}
    ul {{ padding-left: 18px; margin: 8px 0 0; }}
    li {{ margin: 6px 0; }}
    a {{ color: var(--accent); }}
    pre {{ white-space: pre-wrap; word-break: break-word; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <p class="muted">Live team activity dashboard. Refreshes automatically when new curated events arrive.</p>
    <div id="content" class="grid"></div>
  </main>
  <script>
    const mode = {json.dumps(mode)};
    const summaryEndpoint = {json.dumps(summary_endpoint)};
    const streamEndpoint = {json.dumps(stream_endpoint)};
    function apiUrl(path) {{
      return new URL(path, window.location.origin).toString();
    }}

    function escapeHtml(value) {{
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }}

    function safeTraceLink(value) {{
      if (!value) return "";
      try {{
        const url = new URL(String(value), window.location.origin);
        if (url.protocol !== "http:" && url.protocol !== "https:") return "";
        return `<div><a href="${{escapeHtml(url.toString())}}" target="_blank" rel="noreferrer">trace</a></div>`;
      }} catch (_error) {{
        return "";
      }}
    }}

    function renderObjectives(objectives) {{
      return `<section class="panel wide"><div class="row"><h2>Objectives</h2><span class="muted">goals, subgoals, evidence, and bounded runs</span></div>
        <div class="objective-grid">${{(objectives || []).map(item => {{
          const policy = item.work_policy || null;
          const sessions = item.work_sessions || [];
          const latest = sessions[0] || null;
          const budget = latest?.budget || policy?.budget || {{}};
          const checkpointToolReserve = Number(budget.checkpoint_reserve_tool_calls || 0);
          const materialToolAllowance = Math.max(0, Number(budget.tool_calls || 0) - checkpointToolReserve);
          const checkpoints = item.checkpoints || [];
          return `<article class="objective">
            <div class="row"><strong>${{escapeHtml(item.objective_title || item.objective_id)}}</strong><span class="badge">${{escapeHtml(item.status || "unknown")}}</span></div>
            <div class="muted mono">${{escapeHtml(item.objective_id)}}</div>
            <p>${{escapeHtml(item.desired_outcome || item.latest_summary || "No desired outcome snapshot yet.")}}</p>
            <div><strong>Progress</strong>: ${{escapeHtml(item.progress_summary || "No evidence delta recorded.")}}</div>
            <div class="muted">Focus: ${{escapeHtml(item.current_focus || "-")}}</div>
            <ul class="subgoals">${{(item.subgoals || []).map(step => `<li data-status="${{escapeHtml(step.status || "planned")}}"><strong>${{escapeHtml(step.title || step.description || "Subgoal")}}</strong><div class="muted">${{escapeHtml(step.status || "planned")}}${{step.result_summary ? " • " + escapeHtml(step.result_summary) : ""}}</div></li>`).join("") || "<li>No current subgoals.</li>"}}</ul>
            ${{policy ? `<div class="row"><span class="badge">time-boxed: ${{escapeHtml(policy.profile_name)}} (${{escapeHtml(policy.profile_source || "configured")}})</span><span class="muted">next: ${{escapeHtml(policy.next_run_at || "scheduler pending")}}</span></div>` : `<span class="badge">continuous execution</span>`}}
            ${{latest ? `<div class="budget"><div class="metric">${{escapeHtml(latest.status)}}<br><span class="muted">session</span></div><div class="metric">${{escapeHtml(latest.total_tokens_used)}} / ${{escapeHtml(budget.total_tokens)}}<br><span class="muted">session tokens</span></div><div class="metric">${{escapeHtml(latest.turns_used)}} / ${{escapeHtml(budget.turns)}}<br><span class="muted">session turns</span></div><div class="metric">${{escapeHtml(latest.tool_calls_used)}} / ${{escapeHtml(budget.tool_calls)}}<br><span class="muted">session tool calls</span><br><span class="muted">${{escapeHtml(materialToolAllowance)}} work + ${{escapeHtml(checkpointToolReserve)}} checkpoint-only</span></div></div>` : ""}}
            ${{checkpoints[0] ? `<p class="muted"><strong>Latest checkpoint:</strong> ${{escapeHtml(checkpoints[0].health)}} — ${{escapeHtml(checkpoints[0].next_subgoal || checkpoints[0].act)}}</p>` : ""}}
          </article>`;
        }}).join("") || "<p>No Objective snapshots have been published.</p>"}}</div></section>`;
    }}

    function renderUser(payload) {{
      const team = payload.team || {{}};
      return `
        ${{renderObjectives(payload.objectives)}}
        <section class="panel">
          <h2>Team</h2>
          <div class="muted">Team: <span class="mono">${{escapeHtml(team.team_id || "-")}}</span></div>
          <div>Manager: ${{escapeHtml(team.manager_name || "-")}}</div>
          <div class="muted mono">${{escapeHtml(team.manager_agent_id || "")}}</div>
          <ul>${{Object.entries(team.team_composition || {{}}).map(([name,count]) => `<li>${{escapeHtml(name)}}: ${{escapeHtml(count)}}</li>`).join("")}}</ul>
        </section>
        <section class="panel">
          <h2>Tasks In Progress</h2>
          <ul>${{(payload.tasks_in_progress || []).map(task => `<li><strong>${{escapeHtml(task.current_summary)}}</strong><div class="muted">${{escapeHtml(task.stage)}} • ${{escapeHtml(task.last_updated_at)}}</div><div>${{escapeHtml(task.current_detail || "")}}</div></li>`).join("") || "<li>No active tasks.</li>"}}</ul>
        </section>
        <section class="panel">
          <h2>Workers</h2>
          <ul>${{(team.active_workers || []).map(worker => `<li><strong>${{escapeHtml(worker.name || worker.agent_id)}}</strong> (${{escapeHtml(worker.named_agent_type || "unknown")}})<div class="muted">executing=${{escapeHtml(worker.is_executing)}} queue=${{escapeHtml(worker.top_level_queue_depth ?? "-")}} waiting=${{escapeHtml(worker.waiting_top_level_task_count ?? "-")}}</div></li>`).join("") || "<li>No live workers.</li>"}}</ul>
        </section>
        <section class="panel">
          <h2>Recent Activity</h2>
          <ul>${{(payload.recent_activity || []).map(item => `<li><strong>${{escapeHtml(item.summary)}}</strong><div class="muted">${{escapeHtml(item.created_at)}} • ${{escapeHtml(item.agent_name || "-")}}</div><div>${{escapeHtml(item.detail || "")}}</div></li>`).join("") || "<li>No recent activity.</li>"}}</ul>
        </section>
      `;
    }}

    function renderAdmin(payload) {{
      return `
        ${{renderObjectives(payload.objectives)}}
        <section class="panel">
          <h2>Recent Admin Activity</h2>
          <ul>${{(payload.activity || []).map(item => `<li><strong>${{escapeHtml(item.summary)}}</strong><div class="muted">${{escapeHtml(item.created_at)}} • ${{escapeHtml(item.agent_name || "-")}} • ${{escapeHtml(item.stage)}}</div>${{safeTraceLink(item.trace_url)}}<div>${{escapeHtml(item.detail || "")}}</div></li>`).join("") || "<li>No activity.</li>"}}</ul>
        </section>
        <section class="panel">
          <h2>Agents</h2>
          <ul id="admin-agents"></ul>
        </section>
        <section class="panel">
          <h2>Tasks</h2>
          <ul id="admin-tasks"></ul>
        </section>
        <section class="panel">
          <h2>Health</h2>
          <pre id="admin-health"></pre>
        </section>
      `;
    }}

    async function render() {{
      const content = document.getElementById("content");
      const response = await fetch(apiUrl(summaryEndpoint));
      const payload = await response.json();
      content.innerHTML = mode === "user" ? renderUser(payload) : renderAdmin(payload);
      if (mode === "admin") {{
        const [agents, tasks, health] = await Promise.all([
          fetch(apiUrl("/api/admin/agents")).then(r => r.json()),
          fetch(apiUrl("/api/admin/tasks")).then(r => r.json()),
          fetch(apiUrl("/api/admin/health")).then(r => r.json()),
        ]);
        document.getElementById("admin-agents").innerHTML = (agents.agents || []).map(agent => `<li><strong>${{escapeHtml(agent.name || agent.agent_id)}}</strong> (${{escapeHtml(agent.named_agent_type || "unknown")}})<div class="muted">${{escapeHtml(agent.agent_id)}} • executing=${{escapeHtml(agent.is_executing)}} • paused=${{escapeHtml(agent.is_paused)}}</div></li>`).join("") || "<li>No agents.</li>";
        document.getElementById("admin-tasks").innerHTML = (tasks.tasks || []).map(task => `<li><strong>${{escapeHtml(task.current_summary)}}</strong><div class="muted">${{escapeHtml(task.top_level_task_id)}} • ${{escapeHtml(task.stage)}}</div></li>`).join("") || "<li>No tasks.</li>";
        document.getElementById("admin-health").textContent = JSON.stringify(health, null, 2);
      }}
    }}

    render();
    const events = new EventSource(apiUrl(streamEndpoint));
    events.onmessage = () => render();
  </script>
</body>
</html>"""


async def run_activity_collector(config: CollectorRuntimeSettings) -> None:
    service = ActivityCollectorService(config)
    app = service.build_app()
    hosted_site = await start_aiohttp_site(
        app,
        host=config.activity_collector.bind_host,
        port=config.activity_collector.bind_port,
        access_log=None,
        web_module=web,
    )
    logging.getLogger(__name__).info(
        "Activity collector listening on %s%s and %s",
        hosted_site.base_url,
        config.activity_collector.user_url_path,
        config.activity_collector.admin_url_path,
    )
    try:
        await asyncio.Event().wait()
    finally:
        await hosted_site.cleanup()
