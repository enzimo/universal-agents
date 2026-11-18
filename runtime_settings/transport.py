"""Transport configuration and contracts."""

from __future__ import annotations
import logging
from typing import Any, Final, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator
from custom_messages.contracts.runtime_v1 import TRANSPORT_NATS
from default_values import MAX_QUEUE_PARTITIONS, MIN_QUEUE_BYTES, TRANSPORT_DEFAULTS

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


# The installation's partition count is fixed when the installation is created: another
# count puts every queued message in another stream (contracts/messages/v1/README.md,
# "Partitions"). So it does not travel with a team (team_transfer.settings), and the
# installation authority refuses to start with a count other than the one its teams'
# streams were created with.
PARTITION_COUNT_SETTING: Final = ("transport", "queues", "partitions")
# The size warning ends three points below its level (assistant_gateway.storage_capacity
# RELEASE_POINTS), so a level of 3 or less could never end; below 10 it would also be
# raised by a handful of messages.
MIN_WARN_AT_PERCENT: Final = 10
MAX_WARN_AT_PERCENT: Final = 99


def _queue_setting_number(value: Any, *, name: str, least: int, most: Optional[int] = None) -> int:
    """A whole number from ``least`` (to ``most``) for a ``transport.queues`` setting.

    Accepts an int or a string of digits (environment-style YAML values); refuses true/false
    and fractions, which pydantic's int coercion would otherwise accept or round.
    """

    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be a whole number.")
    if value < least or (most is not None and value > most):
        bound = f"from {least} to {most}" if most is not None else f"at least {least}"
        raise ValueError(f"{name} must be {bound}, not {value}.")
    return value


class QueueByteLimits(BaseModel):
    """``transport.queues.max_bytes``: one byte limit per size kind of the contract's streams.

    The field names are the contract's size kinds (``transport.yaml`` ``queues.size_kinds``).
    Each limit applies to each stream of its kind, each team's apart: every one of a team's
    partition streams has the ``partition`` limit. NATS sets the whole limit aside from its
    storage when it creates the stream, so these are also what the installation reserves.
    """

    model_config = ConfigDict(extra="forbid")

    partition: int = TRANSPORT_DEFAULTS.queues_max_bytes_partition.value
    pool: int = TRANSPORT_DEFAULTS.queues_max_bytes_pool.value
    edge: int = TRANSPORT_DEFAULTS.queues_max_bytes_edge.value
    team_services: int = TRANSPORT_DEFAULTS.queues_max_bytes_team_services.value
    dead_letter: int = TRANSPORT_DEFAULTS.queues_max_bytes_dead_letter.value

    @field_validator("partition", "pool", "edge", "team_services", "dead_letter", mode="before")
    @classmethod
    def _at_least_one_frame(cls, value: Any, info: Any) -> int:
        return _queue_setting_number(
            value, name=f"transport.queues.max_bytes.{info.field_name}", least=MIN_QUEUE_BYTES
        )


class QueueSettings(BaseModel):
    """``transport.queues``: the protocol 1 work and dead-letter streams of every team.

    The installation authority creates the streams with these limits at every start and the
    gateway warns administrators when one fills (contracts/messages/v1/README.md, "Stream
    sizes"). ``partitions`` is fixed when the installation is created
    (``PARTITION_COUNT_SETTING``). ``max_messages_per_subject`` caps one sender's waiting
    messages for one agent's inbox or control queue in a partition stream, so one agent's
    backlog leaves room for its own and its neighbours' control messages.
    """

    model_config = ConfigDict(extra="forbid")

    max_bytes: QueueByteLimits = Field(default_factory=QueueByteLimits)
    warn_at_percent: int = TRANSPORT_DEFAULTS.queues_warn_at_percent.value
    partitions: int = TRANSPORT_DEFAULTS.queues_partitions.value
    max_messages_per_subject: int = TRANSPORT_DEFAULTS.queues_max_messages_per_subject.value

    @field_validator("warn_at_percent", mode="before")
    @classmethod
    def _warn_level(cls, value: Any) -> int:
        return _queue_setting_number(
            value, name="transport.queues.warn_at_percent", least=MIN_WARN_AT_PERCENT, most=MAX_WARN_AT_PERCENT
        )

    @field_validator("partitions", mode="before")
    @classmethod
    def _partition_count(cls, value: Any) -> int:
        return _queue_setting_number(
            value, name="transport.queues.partitions", least=1, most=MAX_QUEUE_PARTITIONS
        )

    @field_validator("max_messages_per_subject", mode="before")
    @classmethod
    def _per_subject_cap(cls, value: Any) -> int:
        return _queue_setting_number(value, name="transport.queues.max_messages_per_subject", least=1)


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
    queues: QueueSettings = Field(default_factory=QueueSettings)

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        normalized = str(value or TRANSPORT_DEFAULTS.type.value).strip().lower()
        if normalized != TRANSPORT_NATS:
            raise ValueError("NATS/JetStream is the only supported transport.")
        return TRANSPORT_DEFAULTS.type.value
