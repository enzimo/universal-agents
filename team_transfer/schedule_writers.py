"""Write the ``schedules`` section's accepted records into the new instance.

Kept apart from ``team_transfer.schedules`` (the records, export and plan) so
each file stays focused; the reasons for every choice are in that module's
docstring. Every write goes through the owning store or repository, in the
order the plan checked them: sessions, schedules, delivery targets, the
heartbeat list, worker bindings, then pool sizes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional

from team_transfer.schedule_records import (
    CarriedScheduleV2,
    HeartbeatItemV1,
    HeartbeatSourceV1,
    ScheduledTargetV1,
    ScheduleRecordKind,
    ScheduleTransferRecordV2,
    SessionRouteV1,
    gateway_signing_key,
    is_one_time,
    run_time_passed,
)
from team_transfer.sections import ImportContext

if TYPE_CHECKING:
    from access_control.identity import ContextSigner
    from custom_messages.contracts.schedule_v1 import ScheduledMessage

logger = logging.getLogger(__name__)

# Recorded as who set a carried pool size when its history names nobody.
_TRANSFER_ACTOR = "team_transfer"


def _signer(config: Any, context: ImportContext) -> Optional["ContextSigner"]:
    from access_control.identity import ISSUER_GATEWAY, ContextSigner

    value = gateway_signing_key(config, context.layout)
    if value is None:
        logger.warning(
            "The new instance has no gateway signing key, so people's schedules are imported unsigned and run "
            "without their owners' roles. event=team_transfer_schedules_no_signing_key root=%s",
            context.layout.root,
        )
        return None
    return ContextSigner.from_value(ISSUER_GATEWAY, value)


@dataclass
class ScheduleWriter:
    team_id: str
    store: Any
    gateway: Any
    heartbeat: Any
    pools: Any
    signer: Optional["ContextSigner"]
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def open(cls, context: ImportContext) -> "ScheduleWriter":
        from custom_agents.heartbeat_store import HeartbeatStore
        from custom_agents.schedule_store import ScheduleStore
        from storage.gateway_state_repository import create_gateway_state_repository
        from storage.worker_pool_repository import WorkerPoolRepository
        from team_transfer.settings import planned_config

        config = planned_config(context)
        storage = config.manager_storage_settings
        return cls(
            team_id=context.reader.manifest.team_id,
            store=ScheduleStore(
                storage_settings=storage, default_cron_timezone=config.user.timezone
            ),
            gateway=create_gateway_state_repository(config.state_storage_settings),
            heartbeat=HeartbeatStore(storage_settings=storage),
            pools=WorkerPoolRepository(storage),
            signer=_signer(config, context),
        )

    def write(self, record: ScheduleTransferRecordV2) -> None:
        writers: dict[ScheduleRecordKind, Callable[[Any], None]] = {
            ScheduleRecordKind.SESSION: self._session,
            ScheduleRecordKind.SCHEDULE: self._schedule,
            ScheduleRecordKind.SCHEDULED_TARGET: self._target,
            ScheduleRecordKind.HEARTBEAT_SOURCE: self._heartbeat_source,
            ScheduleRecordKind.HEARTBEAT_ITEM: self._heartbeat_item,
            ScheduleRecordKind.WORKER_BINDING: self._binding,
            ScheduleRecordKind.POOL_LIMIT: self._pool_limit,
        }
        writer = writers.get(record.kind)
        if writer is None:
            # Execution records are always refused by the plan.
            raise ValueError(
                f"The import has no writer for {record.kind.value} records."
            )
        writer(record.payload)

    # -- sessions and delivery targets ------------------------------------------------

    def _session(self, route: SessionRouteV1) -> None:
        """Write a carried session; only the session that was its chat's current one becomes current.

        Sessions are written in key order and a person's older session (one a
        schedule still delivers through) can sort after their current one, so
        writing a session must never switch its chat to it.
        """

        # The session's conversation and last task are history (module docstring).
        self.gateway.upsert_session_without_switching_chat(
            session_key=route.session_key,
            session_alias=route.session_alias,
            conversation_id=None,
            last_task_id=None,
            manager_agent_id=route.manager_agent_id,
            channel_name=route.channel_name,
            user_id=route.user_id,
            chat_id=route.chat_id,
            thread_id=route.thread_id,
            status=route.status,
            owner_canonical_user_id=route.owner_person_id,
        )
        if not route.channel_current or route.chat is None:
            return
        channel_name, user_id, chat_id, thread_id = route.chat
        current = self.gateway.set_active_session_for_channel_if_unset(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            session_key=route.session_key,
        )
        if current != route.session_key:
            # Only a later batch gets here; its plan lists the session (``_plan_session``).
            logger.warning(
                "A chat already has a current session in this instance, which stays current; the imported "
                "session that was current in the export is not. event=team_transfer_session_current_kept "
                "session_key=%s current_session_key=%s channel_name=%s",
                route.session_key,
                current,
                route.channel_name,
            )

    def _target(self, target: ScheduledTargetV1) -> None:
        self.gateway.upsert_scheduled_target(
            scheduled_job_id=target.schedule_id,
            session_key=target.session_key,
            conversation_id=target.conversation_id,
            owner_canonical_user_id=target.owner_person_id,
        )

    # -- schedules ----------------------------------------------------------------------

    def _schedule(self, entry: CarriedScheduleV2) -> None:
        from custom_messages.contracts.schedule_v1 import ScheduledMessage

        schedule = ScheduledMessage.model_validate(entry.document)
        if is_one_time(schedule):
            if schedule.enabled and run_time_passed(schedule, self.now):
                schedule.enabled = False
                logger.warning(
                    "A one-time schedule's run was due during the move; it is imported turned off. "
                    "event=team_transfer_schedule_run_missed schedule_id=%s due=%s",
                    schedule.schedule_id,
                    schedule.next_fire_at,
                )
        else:
            # Computed from now by the store, so runs missed during the move are skipped.
            schedule.next_fire_at = None
        self._sign_again(schedule, vouched=entry.owner_vouched)
        self.store.add(schedule, remaining_count_override=entry.remaining_count)

    def _sign_again(self, schedule: "ScheduledMessage", *, vouched: bool) -> None:
        """Sign a person's schedule context with the new gateway key (``team_transfer.schedules`` docstring)."""

        from custom_agents.schedule_management import is_system_managed_schedule
        from custom_agents.schedule_store import owner_canonical_user_id_for_schedule

        gateway_context = getattr(schedule.payload_meta, "gateway_context", None)
        owner = owner_canonical_user_id_for_schedule(schedule)
        if gateway_context is None or is_system_managed_schedule(schedule) or not owner:
            return
        if vouched and self.signer is not None:
            gateway_context.task_context = self.signer.issue(
                team_id=self.team_id,
                person_id=owner,
                person_roles=self._roles(owner),
                schedule_id=schedule.schedule_id,
            )
        elif gateway_context.task_context is not None:
            # Signed with the old key, it would only fail every check; say so once here.
            gateway_context.task_context = None
            logger.warning(
                "A person's schedule is imported without a signed context, so its runs carry no roles. "
                "event=team_transfer_schedule_unsigned schedule_id=%s owner=%s vouched=%s",
                schedule.schedule_id,
                owner,
                vouched,
            )

    def _roles(self, person_id: str) -> tuple[str, ...]:
        from access_control.people import MEMBERSHIP_STATUS_ACTIVE, effective_roles

        return effective_roles(
            membership.role
            for membership in self.gateway.list_team_memberships(person_id)
            if membership.team_id == self.team_id
            and membership.status == MEMBERSHIP_STATUS_ACTIVE
        )

    # -- heartbeat list -----------------------------------------------------------------

    def _heartbeat_source(self, source: HeartbeatSourceV1) -> None:
        self.heartbeat.upsert_source(
            source_id=source.source_id, title=source.title, enabled=source.enabled
        )

    def _heartbeat_item(self, item: HeartbeatItemV1) -> None:
        self.heartbeat.add_item(
            source_id=item.source_id,
            title=item.title,
            instruction=item.instruction,
            conversation_id=item.conversation_id,
            target_agent_type=item.target_agent_type,
            objective_id=item.objective_id,
            enabled=item.enabled,
            heartbeat_item_id=item.heartbeat_item_id,
            source_key=item.source_key,
        )

    # -- worker setup -------------------------------------------------------------------

    def _binding(self, document: dict[str, Any]) -> None:
        """Through the repository's guarded ``configure``, as the first revision, not waiting for review."""

        from custom_agents.schedule_management import schedule_control_state_digest
        from custom_messages.contracts.schedule_worker_v1 import ScheduleWorkerBindingV1

        binding = ScheduleWorkerBindingV1.model_validate(document).model_copy(
            update={
                "revision": 1,
                "blocked_reason": None,
                "blocked_since_utc": None,
                "next_review_at_utc": None,
                "review_generation": 0,
            }
        )
        stored = self.store.get_scheduled_message(binding.schedule_id)
        self.store.worker_records.configure(
            binding,
            stored,
            expected_revision=0,
            expected_control_digest=schedule_control_state_digest(stored),
        )

    def _pool_limit(self, document: dict[str, Any]) -> None:
        from storage.worker_pool_repository import WorkerPoolLimitV1

        limit = WorkerPoolLimitV1.model_validate(document)
        if limit.lasting_max_workers is not None:
            self._set_limit(limit, limit.lasting_max_workers, None)
        if limit.temporary_active(self.now) and limit.temporary_max_workers is not None:
            self._set_limit(
                limit, limit.temporary_max_workers, limit.temporary_until_utc
            )

    def _set_limit(
        self, limit: Any, max_workers: int, until_utc: Optional[datetime]
    ) -> None:
        change = next(
            (item for item in reversed(limit.history) if item.until_utc == until_utc),
            None,
        )
        self.pools.set_limit(
            team_id=limit.team_id,
            scope=limit.scope,
            max_workers=max_workers,
            until_utc=until_utc,
            set_by=change.set_by if change is not None else _TRANSFER_ACTOR,
            reason=f"Carried by the team transfer. {change.reason if change is not None else ''}".strip(),
        )


__all__ = ["ScheduleWriter"]
