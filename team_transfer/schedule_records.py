"""The record models of the ``schedules`` transfer section and helpers its planner and writer share.

The reasons for each choice are in ``team_transfer.schedules``. This module
has no dependency on the section or its writer, so both can import it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from pydantic import Field, JsonValue, model_validator

from team_transfer.format import TransferModel

if TYPE_CHECKING:
    from custom_messages.contracts.schedule_v1 import ScheduledMessage


class ScheduleRecordKind(StrEnum):
    SESSION = "session"
    SCHEDULE = "schedule"
    SCHEDULED_TARGET = "scheduled_target"
    HEARTBEAT_SOURCE = "heartbeat_source"
    HEARTBEAT_ITEM = "heartbeat_item"
    WORKER_BINDING = "worker_binding"
    POOL_LIMIT = "pool_limit"
    EXECUTION_JOB = "execution_job"
    MANAGED_SERVICE = "managed_service"


class SessionRouteV1(TransferModel):
    """A chat session's name and route, without its conversation."""

    session_key: str = Field(min_length=1, max_length=500)
    session_alias: Optional[str] = Field(default=None, max_length=500)
    manager_agent_id: Optional[str] = Field(default=None, max_length=500)
    channel_name: Optional[str] = Field(default=None, max_length=120)
    user_id: Optional[str] = Field(default=None, max_length=500)
    chat_id: Optional[str] = Field(default=None, max_length=500)
    thread_id: Optional[str] = Field(default=None, max_length=500)
    owner_person_id: Optional[str] = Field(default=None, max_length=240)
    status: str = Field(min_length=1, max_length=40)
    # Whether it was its chat's current session (``get_active_session_for_channel``).
    channel_current: bool = False

    @property
    def chat(self) -> Optional[tuple[str, str, str, str]]:
        """Its chat (channel, user, chat, thread) as the current-session record keys it; None without a full route."""

        if not (self.channel_name and self.user_id and self.chat_id):
            return None
        return (self.channel_name, self.user_id, self.chat_id, self.thread_id or "")


class CarriedScheduleV2(TransferModel):
    """Markdown is the schedule configuration, with separate immutable provenance."""

    remaining_count: Optional[int] = Field(default=None, ge=0)
    # The old instance's gateway key vouched for the saved context's person (module docstring).
    owner_vouched: bool = False
    markdown: str = Field(min_length=1)

    @property
    def document(self) -> dict[str, Any]:
        from team_transfer.schedule_markdown import parse_schedule

        return parse_schedule(self.markdown)


class ScheduledTargetV1(TransferModel):
    schedule_id: str = Field(min_length=1, max_length=400)
    session_key: str = Field(min_length=1, max_length=500)
    conversation_id: Optional[str] = Field(default=None, max_length=400)
    owner_person_id: Optional[str] = Field(default=None, max_length=240)


class HeartbeatSourceV1(TransferModel):
    source_id: str = Field(min_length=1, max_length=400)
    title: str = Field(max_length=500)
    enabled: bool


class HeartbeatItemV1(TransferModel):
    heartbeat_item_id: str = Field(min_length=1, max_length=400)
    source_id: str = Field(min_length=1, max_length=400)
    title: str = Field(max_length=500)
    instruction: str
    conversation_id: str = Field(min_length=1, max_length=400)
    target_agent_type: Optional[str] = Field(default=None, max_length=200)
    objective_id: Optional[str] = Field(default=None, max_length=400)
    enabled: bool
    source_key: Optional[str] = Field(default=None, max_length=500)


class ExecutionRecordV1(TransferModel):
    """An automation job or managed service, listed so the operator sets it up again."""

    record_id: str = Field(min_length=1, max_length=400)
    name: str = Field(min_length=1, max_length=240)
    owner_kind: str = Field(min_length=1, max_length=40)
    owner_id: str = Field(min_length=1, max_length=240)


# The field of ScheduleTransferRecordV2 each kind fills; every other one stays empty.
_KIND_FIELD: dict[ScheduleRecordKind, str] = {
    ScheduleRecordKind.SESSION: "session",
    ScheduleRecordKind.SCHEDULE: "schedule",
    ScheduleRecordKind.SCHEDULED_TARGET: "target",
    ScheduleRecordKind.HEARTBEAT_SOURCE: "heartbeat_source",
    ScheduleRecordKind.HEARTBEAT_ITEM: "heartbeat_item",
    # Versioned contracts: ScheduleWorkerBindingV1 and WorkerPoolLimitV1.
    ScheduleRecordKind.WORKER_BINDING: "document",
    ScheduleRecordKind.POOL_LIMIT: "document",
    ScheduleRecordKind.EXECUTION_JOB: "execution",
    ScheduleRecordKind.MANAGED_SERVICE: "execution",
}


class ScheduleTransferRecordV2(TransferModel):
    kind: ScheduleRecordKind
    record_id: str = Field(min_length=1, max_length=500)
    session: Optional[SessionRouteV1] = None
    schedule: Optional[CarriedScheduleV2] = None
    target: Optional[ScheduledTargetV1] = None
    heartbeat_source: Optional[HeartbeatSourceV1] = None
    heartbeat_item: Optional[HeartbeatItemV1] = None
    document: Optional[dict[str, JsonValue]] = None
    execution: Optional[ExecutionRecordV1] = None

    @model_validator(mode="after")
    def _one_payload(self) -> "ScheduleTransferRecordV2":
        expected = _KIND_FIELD[self.kind]
        filled = {
            name
            for name in set(_KIND_FIELD.values())
            if getattr(self, name) is not None
        }
        if filled != {expected}:
            raise ValueError(
                f"a {self.kind.value} record carries exactly its {expected} field"
            )
        return self

    @property
    def key(self) -> str:
        return record_key(self.kind, self.record_id)

    @property
    def payload(self) -> Any:
        return getattr(self, _KIND_FIELD[self.kind])


def record_key(kind: ScheduleRecordKind, record_id: str) -> str:
    return f"{kind.value}/{record_id}"


# -- schedule timing ------------------------------------------------------------------


def is_one_time(schedule: "ScheduledMessage") -> bool:
    return schedule.interval_ms is None and not schedule.cron_expression


def run_time_passed(schedule: "ScheduledMessage", now: datetime) -> bool:
    """Whether a one-time schedule's only run was due before ``now``."""

    if not is_one_time(schedule) or not schedule.next_fire_at:
        return False
    due = datetime.fromisoformat(schedule.next_fire_at.replace("Z", "+00:00"))
    return (due if due.tzinfo else due.replace(tzinfo=timezone.utc)) <= now


# -- the gateway signing key ---------------------------------------------------------


def gateway_signing_key(config: Any, layout: Any) -> Optional[str]:
    """The instance's gateway signing key value, from where its launcher reads secrets; None when unset.

    ``team.env`` (or the configured env file), or, for an instance whose
    secrets come from the environment, the environment or a Docker secret file
    (``ACCESS_GATEWAY_SIGNING_KEY_FILE``).
    """

    from access_control.identity import ENV_GATEWAY_SIGNING_KEY
    from internal_credentials import parse_env_file

    if config.launch.secrets_from_environment:
        source: dict[str, str] = dict(os.environ)
    else:
        source = parse_env_file(
            Path(config.launch.env_file_path or layout.team_env_path)
        )
    value = str(source.get(ENV_GATEWAY_SIGNING_KEY) or "").strip()
    secret_file = str(source.get(f"{ENV_GATEWAY_SIGNING_KEY}_FILE") or "").strip()
    if not value and secret_file:
        value = Path(secret_file).read_text(encoding="utf-8").strip()
    return value or None


__all__ = [
    "CarriedScheduleV2",
    "ExecutionRecordV1",
    "HeartbeatItemV1",
    "HeartbeatSourceV1",
    "ScheduleRecordKind",
    "ScheduleTransferRecordV2",
    "ScheduledTargetV1",
    "SessionRouteV1",
    "gateway_signing_key",
    "is_one_time",
    "record_key",
    "run_time_passed",
]
