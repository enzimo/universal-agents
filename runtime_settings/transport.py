"""Transport configuration and contracts."""

from __future__ import annotations
import logging
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
from custom_messages.contracts.runtime_v1 import TRANSPORT_NATS
from default_values import TRANSPORT_DEFAULTS

logger = logging.getLogger(__name__)
# Settings earlier versions read that no longer exist; an old configuration still loads.
RETIRED_TRANSPORT_SETTINGS = {
    "presence_max_age_seconds": "the presence stream it kept was removed (who is live comes from the "
                                "team's signed agent records)",
}


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
    process_consumer_inactive_threshold_seconds: float = Field(
        default=TRANSPORT_DEFAULTS.process_consumer_inactive_threshold_seconds.value,
        gt=0,
    )
    nats_user: Optional[str] = TRANSPORT_DEFAULTS.nats_user.value
    nats_password: Optional[str] = TRANSPORT_DEFAULTS.nats_password.value
    credentials_file: Optional[str] = TRANSPORT_DEFAULTS.credentials_file.value

    @model_validator(mode="before")
    @classmethod
    def _set_aside_retired_settings(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        retired = sorted(key for key in RETIRED_TRANSPORT_SETTINGS if key in value)
        if not retired:
            return value
        for key in retired:
            logger.warning("Ignoring transport.%s: %s. Remove it from the configuration to stop this warning. "
                           "event=retired_transport_setting_ignored", key, RETIRED_TRANSPORT_SETTINGS[key])
        return {key: item for key, item in value.items() if key not in RETIRED_TRANSPORT_SETTINGS}

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        normalized = str(value or TRANSPORT_DEFAULTS.type.value).strip().lower()
        if normalized != TRANSPORT_NATS:
            raise ValueError("NATS/JetStream is the only supported transport.")
        return TRANSPORT_DEFAULTS.type.value
