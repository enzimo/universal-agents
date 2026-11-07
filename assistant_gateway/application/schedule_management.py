"""Gateway-owned user schedule creation, and schedule lists and controls through the team services.

Creating a schedule still publishes a durable ``ScheduledMessage`` to the team lead (queued
work moves in phase 3). Listing, pausing, resuming, turning off and removing go to the team
services as ``schedules.*`` commands for the signed-in person (``team_services.schedules``),
which decide whose schedules they are and who may change system schedules. A chat's own
list names the chat by its opaque delivery reference (``GatewayDeliveryRefs``). A change
names the listed version (``seen_version``); a chat command that names only a schedule id
looks the current version up first, so it acts on the schedule as named, as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from assistant_gateway.application.delivery_service import GatewayDeliveryService
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.ports import GatewayIdGenerator, GatewayPersonChangeCheck
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
)
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from assistant_gateway.application.team_commands import GatewayTeamCommandService
from custom_messages.contracts.schedule_v1 import (
    SYSTEM_SCHEDULE_CHANGE_KIND,
    ScheduledMessage,
    ScheduleTaskMemory,
)
from custom_messages.wire.encoding import format_timestamp
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    ScheduleManagementClass,
)


@dataclass(frozen=True)
class GatewayScheduleSettings:
    """Stable gateway settings required for user schedule operations."""

    instance_name: str
    response_route: str
    user_display_name: str
    request_timezone: str
    default_session_key: str
    default_owner_canonical_user_id: str
    manager_unavailable_text: str


@dataclass(frozen=True)
class GatewayScheduleCreateRequest:
    """One normalized request to create a user-owned recurring schedule."""

    name: str
    instruction: str
    session_key: str | None = None
    interval_seconds: int | None = None
    cron_expression: str | None = None
    route: GatewayRequestRoute = GatewayRequestRoute()


class GatewayDeliveryRefs(Protocol):
    """Opaque delivery references of the gateway's sessions (``custom_transport.protocol1.delivery_refs``)."""

    def ref(self, session_key: str) -> str: ...


@dataclass(frozen=True)
class GatewayScheduleView:
    """A schedule as the team services list it (the contract's ``schedules.list`` row)."""

    schedule_id: str
    schedule_name: str | None
    management_class: ScheduleManagementClass
    system_schedule_kind: str | None
    objective_id: str | None
    enabled: bool
    paused_until: str | None
    next_fire_at: str | None
    instruction_preview: str
    seen_version: str
    worker: dict[str, Any]
    # The chat the list was asked for; None for a person's list across chats.
    session_key: str | None = None

    @classmethod
    def from_wire(cls, row: dict[str, Any], *, session_key: str | None = None) -> "GatewayScheduleView":
        return cls(
            schedule_id=row["schedule_id"], schedule_name=row.get("name"),
            management_class=ScheduleManagementClass(row["management_class"]),
            system_schedule_kind=row.get("system_schedule_kind"), objective_id=row.get("objective_id"),
            enabled=bool(row["enabled"]), paused_until=row.get("paused_until"), next_fire_at=row.get("next_fire_at"),
            instruction_preview=str(row.get("instruction_preview") or ""), seen_version=row["seen_version"],
            worker=dict(row["worker"]), session_key=session_key,
        )


@dataclass(frozen=True)
class GatewayScheduleListing:
    """The team services' answer to a schedule list."""

    success: bool
    message: str = ""
    schedules: tuple[GatewayScheduleView, ...] = ()


# The contract's page size for schedule lists; longer lists are read page by page.
SCHEDULE_PAGE_SIZE = 50


class GatewayScheduleApplication:
    """Own user schedule envelopes, targets, and control operations."""

    def __init__(
        self,
        *,
        settings: GatewayScheduleSettings,
        sessions: GatewaySessionService,
        request_context: GatewayRequestContextService,
        delivery: GatewayDeliveryService,
        manager: GatewayManagerClient,
        team_commands: GatewayTeamCommandService,
        id_generator: GatewayIdGenerator,
        person_changes: GatewayPersonChangeCheck | None = None,
        delivery_refs: GatewayDeliveryRefs | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._request_context = request_context
        self._delivery = delivery
        self._manager = manager
        # Lists and controls go to the team services (module docstring).
        self._team_commands = team_commands
        self._delivery_refs = delivery_refs
        self._id_generator = id_generator
        # Decides who may change schedules the runtime manages; without it
        # nobody may (the manager refuses those changes).
        self._person_changes = person_changes

    def may_change_system_schedules(self, route: GatewayRequestRoute = GatewayRequestRoute()) -> bool:
        """Whether the signed-in person may change schedules the runtime manages."""

        return self._may_change_system_schedules(self._request_context.canonical_user_id(route))

    def may_change_schedule(self, schedule: GatewayScheduleView, route: GatewayRequestRoute = GatewayRequestRoute()) -> bool:
        """Whether the signed-in person may change this listed schedule of theirs.

        A person's own schedule, yes; a schedule the runtime manages only when
        they may make ``system_schedule`` changes.
        """

        if schedule.management_class != ScheduleManagementClass.SYSTEM:
            return True
        return self.may_change_system_schedules(route)

    @staticmethod
    def listing_digest(schedule: GatewayScheduleView) -> str:
        """The digest a control on this listed schedule binds to (the list's ``seen_version``).

        The Schedules page and the chat and CLI commands send it as
        ``expected_record_digest``; the team services' store checks the same digest.
        """

        return schedule.seen_version

    def _may_change_system_schedules(self, person_id: str | None) -> bool:
        if self._person_changes is None or not person_id:
            return False
        return bool(self._person_changes.may_change(person_id, SYSTEM_SCHEDULE_CHANGE_KIND))

    async def add(self, request: GatewayScheduleCreateRequest) -> str:
        """Create and durably publish one user-managed recurring schedule."""

        manager_id = self._require_manager_id()
        session = self._resolve_session(request.session_key, request.route)
        schedule_id = self._id_generator.new_scheduled_job_id()
        correlation_id = self._id_generator.new_correlation_id()
        schedule_conversation_id = self._id_generator.new_conversation_id()
        resolved_route = self._resolved_route(request.route, session)
        payload_header = self._manager_header(manager_id)
        # The person's own time zone: the manager reads the schedule's cron
        # expression in it (the manager fills the schedule's cron_timezone from it).
        person_timezone = self._request_context.requester_presentation_for_session(session, resolved_route).timezone
        payload_meta = MessageMeta(
            from_agent_name=f"{self._settings.instance_name}_gateway",
            msg_correlation_id=correlation_id,
            conversation_id=schedule_conversation_id,
            request_timezone=person_timezone or self._settings.request_timezone,
            requester_type=RequesterType.TIMER_REQUESTER,
            is_response_requested=True,
            gateway_context=self._request_context.build_gateway_context(
                session_key=session.session_key,
                delivery_mode="cron",
                scheduled_job_id=schedule_id,
                route=resolved_route,
                manager_agent_id=manager_id,
            ),
        )
        schedule_message = ScheduledMessage(
            schedule_id=schedule_id,
            schedule_name=request.name,
            management_class=ScheduleManagementClass.USER,
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement=request.instruction.strip(),
            ),
            payload_header=payload_header,
            payload_meta=payload_meta,
            interval_ms=(
                request.interval_seconds * 1000
                if request.interval_seconds is not None
                else None
            ),
            cron_expression=request.cron_expression,
            repeat_count=None,
            schedule_memory=ScheduleTaskMemory(),
        )
        envelope = MessageEnvelope(
            msg_content=schedule_message,
            msg_meta=MessageMeta(
                from_agent_name=self._settings.user_display_name,
                msg_correlation_id=correlation_id,
                conversation_id=session.conversation_id,
                request_timezone=self._settings.request_timezone,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=False,
                gateway_context=self._request_context.build_gateway_context(
                    session_key=session.session_key,
                    delivery_mode="cron",
                    scheduled_job_id=schedule_id,
                    route=resolved_route,
                    manager_agent_id=manager_id,
                ),
            ),
            msg_header=self._manager_header(manager_id),
        )
        self._delivery.register_scheduled_request(
            scheduled_job_id=schedule_id,
            correlation_id=correlation_id,
            session_key=session.session_key,
            request_kind="cron",
            request_text=request.instruction,
            conversation_id=schedule_conversation_id,
            followup_to_task_id=None,
        )
        await self._manager.publish_durable(envelope, routing_key=manager_id)
        return schedule_id

    async def list(
        self,
        *,
        session_key: str | None = None,
        route: GatewayRequestRoute = GatewayRequestRoute(),
        owner_wide: bool = False,
    ) -> list[GatewayScheduleView]:
        response = await self.list_with_worker_assignments(
            session_key=session_key, route=route, owner_wide=owner_wide,
        )
        return list(response.schedules) if response.success else []

    async def list_with_worker_assignments(
        self,
        *,
        session_key: str | None = None,
        route: GatewayRequestRoute = GatewayRequestRoute(),
        owner_wide: bool = False,
    ) -> GatewayScheduleListing:
        """The person's schedules, with their workers, from the team services; one chat's unless ``owner_wide``."""

        session = self._resolve_session(session_key, route)
        person = self._person(route, session)
        body: dict[str, Any] = {}
        if not owner_wide:
            if self._delivery_refs is None:
                return GatewayScheduleListing(False, "This gateway cannot name its chats to the team services.")
            body["delivery_ref"] = self._delivery_refs.ref(session.session_key)
        listed: list[GatewayScheduleView] = []
        offset = 0
        while True:
            reply = await self._team_commands.request(
                "schedules.list", {**body, "offset": offset, "limit": SCHEDULE_PAGE_SIZE}, person_id=person
            )
            if reply.get("outcome") != "ok":
                return GatewayScheduleListing(False, str(reply.get("message") or reply.get("outcome")))
            listed.extend(
                GatewayScheduleView.from_wire(row, session_key=None if owner_wide else session.session_key)
                for row in reply["schedules"]
            )
            if "next_offset" not in reply:
                return GatewayScheduleListing(True, "schedule_listed", tuple(listed))
            offset = int(reply["next_offset"])

    async def pause(
        self,
        schedule_id: str,
        *,
        paused_until: str,
        session_key: str | None = None,
        expected_record_digest: str | None = None,
    ) -> bool:
        return await self._change("pause", schedule_id, session_key=session_key,
                                  expected_record_digest=expected_record_digest,
                                  paused_until=format_timestamp(_moment(paused_until)))

    async def resume(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
        expected_record_digest: str | None = None,
    ) -> bool:
        return await self._change("resume", schedule_id, session_key=session_key,
                                  expected_record_digest=expected_record_digest)

    async def disable(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
        expected_record_digest: str | None = None,
    ) -> bool:
        return await self._change("disable", schedule_id, session_key=session_key,
                                  expected_record_digest=expected_record_digest)

    async def remove(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
        expected_record_digest: str | None = None,
    ) -> bool:
        removed = await self._change("remove", schedule_id, session_key=session_key,
                                     expected_record_digest=expected_record_digest)
        if removed:
            self._delivery.remove_scheduled_target(schedule_id)
        return removed

    async def _change(
        self,
        action: str,
        schedule_id: str,
        *,
        session_key: str | None,
        expected_record_digest: str | None,
        paused_until: str | None = None,
    ) -> bool:
        """One change at the team services for the signed-in person; False when refused or gone."""

        route = GatewayRequestRoute()
        session = self._resolve_session(session_key, route)
        person = self._person(route, session)
        seen = expected_record_digest
        if seen is None:
            # A chat command names the schedule only: act on it as it is now (module docstring).
            listing = await self.list_with_worker_assignments(session_key=session_key, route=route, owner_wide=True)
            seen = next((item.seen_version for item in listing.schedules if item.schedule_id == schedule_id), None)
            if seen is None:
                return False
        body: dict[str, Any] = {"schedule_id": schedule_id, "seen_version": seen}
        if paused_until is not None:
            body["paused_until"] = paused_until
        reply = await self._team_commands.request(f"schedules.{action}", body, person_id=person)
        return reply.get("outcome") == "ok"

    def _person(self, route: GatewayRequestRoute, session: SessionRecord) -> str:
        """The signed-in person; an unregistered route is refused (``PermissionError``)."""

        return self._request_context.manager_principal(self._resolved_route(route, session)).canonical_user_id

    def _resolve_session(
        self,
        session_key: str | None,
        route: GatewayRequestRoute,
    ) -> SessionRecord:
        owner_canonical_user_id = self._request_context.canonical_user_id(route)
        return self._sessions.resolve_session(
            requested_session_key=session_key,
            default_session_key=self._settings.default_session_key,
            manager_agent_id=self._manager.manager_id,
            owner_canonical_user_id=owner_canonical_user_id,
            default_owner_canonical_user_id=(
                self._settings.default_owner_canonical_user_id
            ),
            route=GatewaySessionRoute(
                channel_name=route.channel_name,
                user_id=route.user_id,
                chat_id=route.chat_id,
                thread_id=route.thread_id,
            ),
            enforce_owner_scope=self._request_context.users_enabled,
        )

    @staticmethod
    def _resolved_route(
        route: GatewayRequestRoute,
        session: SessionRecord,
    ) -> GatewayRequestRoute:
        return GatewayRequestRoute(
            channel_name=route.channel_name or session.channel_name,
            user_id=route.user_id or session.user_id,
            chat_id=route.chat_id or session.chat_id,
            chat_scope=route.chat_scope,
            thread_id=route.thread_id or session.thread_id,
            original_message_id=route.original_message_id,
            canonical_user_id=(
                session.owner_canonical_user_id or route.canonical_user_id
            ),
        )

    def _manager_header(self, manager_id: str) -> MessageHeader:
        return MessageHeader(
            from_agent_id=self._settings.response_route,
            to_routing_path=manager_id,
            response_routing_key=self._settings.response_route,
            agent_signature="",
        )

    def _require_manager_id(self) -> str:
        manager_id = self._manager.manager_id
        if manager_id is None:
            raise RuntimeError(self._settings.manager_unavailable_text)
        return manager_id


def _moment(value: str) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

