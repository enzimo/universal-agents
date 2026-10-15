"""NATS subject and durable-name mapping for team routes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.envelope_v1 import GATEWAY_MANAGER_CONTROL_TYPES, MessageEnvelope
from custom_messages.enums import RoutingType
from custom_messages.enums import MessageMetaType

_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MANAGER_CONTROL_TYPES = GATEWAY_MANAGER_CONTROL_TYPES


def _is_manager_control_request(envelope: Optional[MessageEnvelope]) -> bool:
    if envelope is None or not isinstance(envelope.msg_content, _MANAGER_CONTROL_TYPES):
        return False
    return envelope.msg_content.command_type == MessageMetaType.REQUEST


def _logical_agent_route(suffix: str) -> Optional[str]:
    """Return an agent id from a direct work or control subject suffix."""

    for ending in (".control", ".work"):
        if suffix.startswith("agent.") and suffix.endswith(ending):
            return suffix[len("agent."):-len(ending)]
    return None


def encode_subject_token(value: str) -> str:
    """Return a NATS-safe subject token without losing human readability."""

    raw = str(value or "").strip()
    if not raw:
        return "_"
    token = _SAFE_TOKEN_RE.sub("_", raw).strip("_")
    return token or "_"


def encode_durable_name(value: str) -> str:
    """Return a NATS durable/consumer-safe name."""

    return encode_subject_token(value).replace(".", "_").replace("*", "_").replace(">", "_")


@dataclass(frozen=True)
class NatsSubjectMapper:
    """Map team routing keys to NATS subjects."""

    team_id: str
    subject_prefix: str = ""
    named_agent_type_queue_map: Mapping[str, str] | None = None
    user_queue_name: Optional[str] = None

    @property
    def team_token(self) -> str:
        return encode_subject_token(self.team_id)

    @property
    def prefix(self) -> str:
        configured = str(self.subject_prefix or "").strip()
        if not configured:
            configured = self.team_id
        return ".".join(
            encode_subject_token(part)
            for part in configured.split(".")
            if part
        )

    def route_subject(self, routing_key: str, *, envelope: Optional[MessageEnvelope] = None) -> str:
        route = str(routing_key or "").strip()
        if route == RoutingType.ALL_AGENTS_ROUTE:
            return self.broadcast_subject(envelope=envelope)
        if route == self.team_id:
            return self.lead_work_subject()
        if route == self.user_queue_name:
            return self.gateway_response_subject(route)
        if route.startswith(f"{self.team_id}_") and (
            "gateway" in route or "_user_interact_queue" in route
        ):
            return self.gateway_response_subject(route)
        if _is_manager_control_request(envelope):
            return self.agent_control_subject(route)
        target_named_agent_type = None
        if envelope is not None and envelope.msg_meta is not None:
            target_named_agent_type = envelope.msg_meta.to_named_agent_type
        if target_named_agent_type and route == f"{self.team_id}_{target_named_agent_type}":
            return self.named_type_work_subject(target_named_agent_type)
        for named_type, queue_name in dict(self.named_agent_type_queue_map or {}).items():
            if route == queue_name:
                return self.named_type_work_subject(named_type)
        return self.agent_work_subject(route)

    def logical_route_for_subject(self, subject: str) -> str:
        prefix = f"{self.prefix}."
        if not subject.startswith(prefix):
            return subject
        suffix = subject[len(prefix):]
        if suffix == "lead.work":
            return self.team_id
        if suffix.startswith("type.") and suffix.endswith(".work"):
            token = suffix[len("type."):-len(".work")]
            for named_type, queue_name in dict(self.named_agent_type_queue_map or {}).items():
                if encode_subject_token(named_type) == token:
                    return queue_name
            return f"{self.team_id}_{token}"
        if suffix.startswith("gateway.") and suffix.endswith(".response"):
            return self.user_queue_name or suffix[len("gateway."):-len(".response")]
        logical_agent_route = _logical_agent_route(suffix)
        if logical_agent_route is not None:
            return logical_agent_route
        if suffix in {"operational", "broadcast"} or suffix.startswith("presence."):
            return RoutingType.ALL_AGENTS_ROUTE.value
        return subject

    def agent_work_subject(self, agent_id: str) -> str:
        return f"{self.prefix}.agent.{encode_subject_token(agent_id)}.work"

    def agent_control_subject(self, agent_id: str) -> str:
        """Return the durable mailbox for execution-slot-exempt manager control."""

        return f"{self.prefix}.agent.{encode_subject_token(agent_id)}.control"

    def lead_work_subject(self) -> str:
        return f"{self.prefix}.lead.work"

    def run_context_request_subject(self) -> str:
        """Core NATS subject where the team lead answers requests for scheduled run contexts.

        Kept out of every JetStream stream: a request nobody answers now is
        not worth keeping, because the asking worker releases its claim and
        the schedule is retried (``custom_agents.scheduled_run_contexts``).
        """

        return f"{self.prefix}.lead.run_context"

    def run_context_reply_subject(self, agent_id: str) -> str:
        """Core NATS subject where one agent receives answers about its scheduled run contexts."""

        return f"{self.prefix}.agent.{encode_subject_token(agent_id)}.run_context"

    def named_type_work_subject(self, named_agent_type: str) -> str:
        return f"{self.prefix}.type.{encode_subject_token(named_agent_type)}.work"

    def gateway_response_subject(self, gateway_route: str) -> str:
        return f"{self.prefix}.gateway.{encode_subject_token(gateway_route)}.response"

    def operational_subject(self) -> str:
        return f"{self.prefix}.operational"

    def broadcast_subject(self, *, envelope: Optional[MessageEnvelope] = None) -> str:
        if envelope is not None and isinstance(envelope.msg_content, AgentStatus):
            source = envelope.msg_content.self_id or envelope.msg_header.from_agent_id or "unknown"
            return self.presence_subject(source)
        return f"{self.prefix}.broadcast"

    def presence_subject(self, agent_id: str) -> str:
        return f"{self.prefix}.presence.{encode_subject_token(agent_id)}"

    def work_stream_name(self) -> str:
        return encode_durable_name(f"{self.prefix}_WORK").upper()

    def presence_stream_name(self) -> str:
        return encode_durable_name(f"{self.prefix}_PRESENCE").upper()

    def dead_letter_stream_name(self) -> str:
        """Messages set aside after their last allowed delivery (``custom_transport.nats_pull``)."""

        return encode_durable_name(f"{self.prefix}_DEAD_LETTER").upper()

    def dead_letter_subjects(self) -> str:
        return f"{self.prefix}.deadletter.>"

    def dead_letter_raw_subject(self, copy_id: str) -> str:
        """Original bytes and headers, with no diagnostic overhead."""

        return f"{self.prefix}.deadletter.raw.{encode_subject_token(copy_id)}"

    def dead_letter_notice_subject(self, copy_id: str) -> str:
        """Small diagnostic record pointing to the retained raw message."""

        return f"{self.prefix}.deadletter.notice.{encode_subject_token(copy_id)}"

    def dead_letter_notice_subjects(self) -> str:
        return f"{self.prefix}.deadletter.notice.*"

    def durable_name(self, route: str) -> str:
        return encode_durable_name(f"{self.prefix}_{route}")
