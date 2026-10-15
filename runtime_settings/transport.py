"""Transport configuration and contracts."""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field, field_validator
from custom_messages.contracts.runtime_v1 import TRANSPORT_NATS
from default_values import TRANSPORT_DEFAULTS


class TransportSettings(BaseModel):
    type: str = TRANSPORT_DEFAULTS.type.value
    nats_url: str = TRANSPORT_DEFAULTS.nats_url.value
    nats_servers: list[str] = Field(
        default_factory=lambda: list(TRANSPORT_DEFAULTS.nats_servers.value)
    )
    nats_name: Optional[str] = TRANSPORT_DEFAULTS.nats_name.value
    jetstream_enabled: bool = TRANSPORT_DEFAULTS.jetstream_enabled.value
    subject_prefix: str = TRANSPORT_DEFAULTS.subject_prefix.value
    storage: str = TRANSPORT_DEFAULTS.storage.value
    replicas: int = TRANSPORT_DEFAULTS.replicas.value
    ack_wait_seconds: float = TRANSPORT_DEFAULTS.ack_wait_seconds.value
    max_deliver: int = TRANSPORT_DEFAULTS.max_deliver.value
    duplicate_window_seconds: float = TRANSPORT_DEFAULTS.duplicate_window_seconds.value
    work_max_age_seconds: Optional[float] = (
        TRANSPORT_DEFAULTS.work_max_age_seconds.value
    )
    presence_max_age_seconds: Optional[float] = (
        TRANSPORT_DEFAULTS.presence_max_age_seconds.value
    )
    process_consumer_inactive_threshold_seconds: float = Field(
        default=TRANSPORT_DEFAULTS.process_consumer_inactive_threshold_seconds.value,
        gt=0,
    )
    nats_user: Optional[str] = TRANSPORT_DEFAULTS.nats_user.value
    nats_password: Optional[str] = TRANSPORT_DEFAULTS.nats_password.value
    credentials_file: Optional[str] = TRANSPORT_DEFAULTS.credentials_file.value

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        normalized = str(value or TRANSPORT_DEFAULTS.type.value).strip().lower()
        if normalized != TRANSPORT_NATS:
            raise ValueError("NATS/JetStream is the only supported transport.")
        return TRANSPORT_DEFAULTS.type.value
