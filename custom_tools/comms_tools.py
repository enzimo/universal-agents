"""NATS-backed structured agent communication helpers."""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_transport.base import TransportPublishOptions
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.subjects import NatsSubjectMapper
from default_values import TRANSPORT_DEFAULTS
from id_generation import new_id


def _process_host_runtime() -> Optional[str]:
    """This process's message host id (``custom_agents.agent_host``), or None without the launcher."""

    from custom_agents.agent_requests import process_host

    host = process_host()
    return None if host is None else host.profile.runtime


class TeamComms(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str = Field(default_factory=lambda: new_id("comms_tool"))
    team_id: Optional[str] = None
    named_agent_type_queue_map: dict[str, str] = Field(default_factory=dict)
    nats_url: str = Field(default=TRANSPORT_DEFAULTS.nats_url.value)
    nats_servers: list[str] = Field(default_factory=lambda: list(TRANSPORT_DEFAULTS.nats_servers.value))
    nats_name: Optional[str] = Field(default=TRANSPORT_DEFAULTS.nats_name.value)
    nats_jetstream_enabled: bool = Field(default=TRANSPORT_DEFAULTS.jetstream_enabled.value)
    nats_subject_prefix: str = Field(default=TRANSPORT_DEFAULTS.subject_prefix.value)
    nats_storage: str = Field(default=TRANSPORT_DEFAULTS.storage.value)
    nats_replicas: int = Field(default=TRANSPORT_DEFAULTS.replicas.value)
    nats_ack_wait_seconds: float = Field(default=TRANSPORT_DEFAULTS.ack_wait_seconds.value)
    nats_max_deliver: int = Field(default=TRANSPORT_DEFAULTS.max_deliver.value)
    nats_duplicate_window_seconds: float = Field(default=TRANSPORT_DEFAULTS.duplicate_window_seconds.value)
    nats_work_max_age_seconds: Optional[float] = Field(default=TRANSPORT_DEFAULTS.work_max_age_seconds.value)
    nats_process_consumer_inactive_threshold_seconds: float = Field(
        default=TRANSPORT_DEFAULTS.process_consumer_inactive_threshold_seconds.value,
        gt=0,
    )
    nats_user: Optional[str] = Field(default=TRANSPORT_DEFAULTS.nats_user.value)
    nats_password: Optional[str] = Field(default=TRANSPORT_DEFAULTS.nats_password.value)
    nats_credentials_file: Optional[str] = Field(default=TRANSPORT_DEFAULTS.credentials_file.value)
    nats_adapter: Optional[NatsTransportAdapter] = None
    _logger: logging.Logger = PrivateAttr(default_factory=lambda: logging.getLogger(__name__))

    def configure_transport(
        self,
        *,
        team_id: Optional[str],
        named_agent_type_queue_map: dict[str, str],
        nats_url: str,
        nats_name: Optional[str] = None,
        nats_servers: Optional[list[str]] = None,
        nats_jetstream_enabled: bool = TRANSPORT_DEFAULTS.jetstream_enabled.value,
        nats_subject_prefix: str = TRANSPORT_DEFAULTS.subject_prefix.value,
        nats_storage: str = TRANSPORT_DEFAULTS.storage.value,
        nats_replicas: int = TRANSPORT_DEFAULTS.replicas.value,
        nats_ack_wait_seconds: float = TRANSPORT_DEFAULTS.ack_wait_seconds.value,
        nats_max_deliver: int = TRANSPORT_DEFAULTS.max_deliver.value,
        nats_duplicate_window_seconds: float = TRANSPORT_DEFAULTS.duplicate_window_seconds.value,
        nats_work_max_age_seconds: Optional[float] = TRANSPORT_DEFAULTS.work_max_age_seconds.value,
        nats_process_consumer_inactive_threshold_seconds: float = (
            TRANSPORT_DEFAULTS.process_consumer_inactive_threshold_seconds.value
        ),
        nats_user: Optional[str] = TRANSPORT_DEFAULTS.nats_user.value,
        nats_password: Optional[str] = TRANSPORT_DEFAULTS.nats_password.value,
        nats_credentials_file: Optional[str] = TRANSPORT_DEFAULTS.credentials_file.value,
    ) -> None:
        self.team_id = team_id
        self.named_agent_type_queue_map = dict(named_agent_type_queue_map or {})
        self.nats_url = nats_url
        self.nats_servers = list(nats_servers or ([nats_url] if nats_url else TRANSPORT_DEFAULTS.nats_servers.value))
        self.nats_name = nats_name
        self.nats_jetstream_enabled = bool(nats_jetstream_enabled)
        self.nats_subject_prefix = nats_subject_prefix or TRANSPORT_DEFAULTS.subject_prefix.value
        self.nats_storage = nats_storage
        self.nats_replicas = int(nats_replicas or TRANSPORT_DEFAULTS.replicas.value)
        self.nats_ack_wait_seconds = float(nats_ack_wait_seconds or TRANSPORT_DEFAULTS.ack_wait_seconds.value)
        self.nats_max_deliver = int(nats_max_deliver or TRANSPORT_DEFAULTS.max_deliver.value)
        self.nats_duplicate_window_seconds = float(
            nats_duplicate_window_seconds or TRANSPORT_DEFAULTS.duplicate_window_seconds.value
        )
        self.nats_work_max_age_seconds = nats_work_max_age_seconds
        self.nats_process_consumer_inactive_threshold_seconds = float(
            nats_process_consumer_inactive_threshold_seconds
        )
        self.nats_user = nats_user
        self.nats_password = nats_password
        self.nats_credentials_file = nats_credentials_file
        if self.nats_adapter is not None:
            self.nats_adapter.mapper = self._nats_mapper(team_id)

    def _nats_mapper(self, team_id: Optional[str] = None) -> NatsSubjectMapper:
        return NatsSubjectMapper(
            team_id=team_id or self.team_id or "NO_TEAM",
            subject_prefix=self.nats_subject_prefix,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
        )

    def _ensure_nats_adapter(self, team_id: Optional[str] = None) -> NatsTransportAdapter:
        if self.nats_adapter is None:
            self.nats_adapter = NatsTransportAdapter(
                config=NatsTransportConfig(
                    servers=list(self.nats_servers or [self.nats_url]),
                    name=self.nats_name,
                    subject_prefix=self.nats_subject_prefix,
                    jetstream_enabled=self.nats_jetstream_enabled,
                    storage=self.nats_storage,
                    replicas=self.nats_replicas,
                    ack_wait_seconds=self.nats_ack_wait_seconds,
                    max_deliver=self.nats_max_deliver,
                    duplicate_window_seconds=self.nats_duplicate_window_seconds,
                    work_max_age_seconds=self.nats_work_max_age_seconds,
                    process_consumer_inactive_threshold_seconds=(
                        self.nats_process_consumer_inactive_threshold_seconds
                    ),
                    user=self.nats_user,
                    password=self.nats_password,
                    credentials_file=self.nats_credentials_file,
                    # Protocol 1 replies to this process's host arrive under its own inbox
                    # (_INBOX.<host>.): the host joins and opens sessions on this connection.
                    reply_inbox_runtime=_process_host_runtime(),
                ),
                mapper=self._nats_mapper(team_id),
                logger=self._logger,
            )
        return self.nats_adapter

    async def send_message(
        self,
        message: MessageEnvelope,
        routing_key: str | None = None,
        message_exchange=None,
        *,
        team_id: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
    ) -> Optional[int]:
        """Publish durably; return the work stream sequence the message got, if any."""
        _ = message_exchange
        if not routing_key:
            routing_key = message.msg_header.to_routing_path
        adapter = self._ensure_nats_adapter()
        previous_mapper = adapter.mapper
        if team_id and team_id != self.team_id:
            adapter.mapper = self._nats_mapper(team_id)
        try:
            if team_id and team_id != self.team_id and self.nats_jetstream_enabled:
                await adapter.validate_streams()
            ack = await adapter.publish(
                envelope=message,
                routing_key=str(routing_key),
                options=TransportPublishOptions(
                    durable=True,
                    ttl_seconds=ttl_seconds,
                    idempotency_key=message.msg_header.idempotency_key or message.msg_header.message_id,
                ),
            )
        finally:
            adapter.mapper = previous_mapper
        sequence = getattr(ack, "seq", None)
        return int(sequence) if isinstance(sequence, int) else None

    async def delete_work_message(self, sequence: int) -> bool:
        """Withdraw one message from the team's work stream (a task nobody took)."""

        return await self._ensure_nats_adapter().delete_work_message(sequence)

