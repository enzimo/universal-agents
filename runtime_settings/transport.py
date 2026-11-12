"""Transport configuration and contracts."""

from __future__ import annotations
import logging
from typing import Any, Final, Optional
from pydantic import BaseModel, Field, field_validator
from custom_messages.contracts.runtime_v1 import TRANSPORT_NATS
from default_values import TRANSPORT_DEFAULTS

logger = logging.getLogger(__name__)

# How long the presence stream kept agents' heartbeats. The stream was removed in message
# contract step 7d (who is live comes from the team's signed agent records), so the setting
# is gone: the configuration upgrade steps drop it (instance_config.yaml 12 -> 13,
# config.yaml 10 -> 11), and the team transfer drops it from settings exported by versions
# 5 to 9, main's version 9 included (team_transfer.settings). This is the one name for that
# path.
RETIRED_PRESENCE_SETTING: Final = ("transport", "presence_max_age_seconds")


def drop_retired_presence_setting(config: dict[str, Any]) -> bool:
    """Upgrade step: remove the presence stream's retention from a configuration being upgraded.

    Removed whatever its value: nothing reads it any more. Callers pass a shallow copy of the
    file's mapping, so the ``transport`` section is copied before it is changed. Returns
    whether the setting was there.
    """

    section, key = RETIRED_PRESENCE_SETTING
    transport = config.get(section)
    if not isinstance(transport, dict) or key not in transport:
        return False
    config[section] = {name: value for name, value in transport.items() if name != key}
    logger.info("Removed a setting this version no longer has from the configuration being upgraded. "
                "event=config_retired_setting_removed setting=%s", ".".join(RETIRED_PRESENCE_SETTING))
    return True


def refuse_retired_presence_setting(config: Any) -> None:
    """Refuse a current-version configuration that names the retired setting.

    The upgrade step removes it from older files, so a current file holding it was edited
    by hand; it is refused by name, like other removed settings, rather than ignored.
    """

    section, key = RETIRED_PRESENCE_SETTING
    transport = config.get(section) if isinstance(config, dict) else None
    if isinstance(transport, dict) and key in transport:
        raise ValueError(
            f"{'.'.join(RETIRED_PRESENCE_SETTING)} has been removed: the presence stream it sized is gone. "
            "Remove the setting from the file."
        )


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

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        normalized = str(value or TRANSPORT_DEFAULTS.type.value).strip().lower()
        if normalized != TRANSPORT_NATS:
            raise ValueError("NATS/JetStream is the only supported transport.")
        return TRANSPORT_DEFAULTS.type.value
