"""A team's protocol 1 work and dead-letter streams and their consumers, as the topology owner creates them.

contracts/messages/v1/README.md ("Who sets up the streams", "Stream sizes", "Partitions"):
the installation authority creates every stream of every team it serves from
``transport.yaml`` ``streams``, each with its size kind's byte limit from the instance's
settings (``transport.queues``), and the consumers ``transport.yaml`` ``consumers`` names.
This module only fills the contract's templates and opens no connection, so the authority,
the gateway's size warning and the tests read the contract one way (like ``buckets``).

Configurations use NATS's field names as nats-py's ``StreamConfig`` and ``ConsumerConfig``
spell them, with the API's own strings for policies (``workqueue``, ``new``, ``file``,
``explicit``), which nats-py's enums equal; times are in seconds, as nats-py takes them.
A contract field this module does not know is refused rather than left out, so a later
contract change cannot create streams that silently lack a setting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from pydantic import TypeAdapter, ValidationError

from custom_messages.wire.models import AgentId, TeamId
from custom_messages.wire.partitions import agent_partition, check_partition_count

# A partition stream records the count it was created with, so the authority can refuse a
# start with another count instead of mixing two layouts (transport.queues.partitions).
PARTITION_COUNT_METADATA = "ua.partitions"

_TEAM, _PARTITION, _AGENT = "team", "partition", "agent"
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_TEAM_ID: TypeAdapter[str] = TypeAdapter(TeamId)
_AGENT_ID: TypeAdapter[str] = TypeAdapter(AgentId)

# Contract stream fields and the NATS field each becomes; the size kind, the cap's setting and
# the subjects are filled separately (queue_stream_config).
_STREAM_FIELDS = {
    "retention": "retention",
    "storage": "storage",
    "max_age_seconds": "max_age",
    "duplicate_window_seconds": "duplicate_window",
    "max_consumers": "max_consumers",
    "max_message_bytes": "max_msg_size",
    "discard": "discard",
    "discard_new_per_subject": "discard_new_per_subject",
    "max_msgs_per_subject": "max_msgs_per_subject",
}
_FILLED_STREAM_FIELDS = {"subjects", "size_kind", "max_msgs_per_subject_setting"}
_CONSUMER_FIELDS = {"ack_wait_seconds": "ack_wait", "max_deliveries": "max_deliver", "ack": "ack_policy"}
_FILLED_CONSUMER_FIELDS = {"stream", "name", "filter", "mode"}


@dataclass(frozen=True)
class QueueLimits:
    """What sizes a team's streams: the instance's ``transport.queues`` settings and the replica count.

    ``max_bytes`` holds one byte limit per size kind (``transport.queues.max_bytes``);
    ``max_messages_per_subject`` is the partition streams' per-subject cap;
    ``replicas`` is the operator's JetStream replica count (``transport.replicas``).
    The settings model checks the values and how they relate; this checks only what NATS
    needs to accept them.
    """

    partitions: int
    max_bytes: Mapping[str, int]
    max_messages_per_subject: int
    replicas: int = 1

    def __post_init__(self) -> None:
        check_partition_count(self.partitions)
        for name, value in (("max_messages_per_subject", self.max_messages_per_subject), ("replicas", self.replicas),
                            *((f"max_bytes.{kind}", limit) for kind, limit in self.max_bytes.items())):
            if type(value) is not int or value < 1:
                raise ValueError(f"transport.queues {name} is a whole number of at least 1, not {value!r}")


@dataclass(frozen=True)
class QueueStream:
    """One of a team's work or dead-letter streams: ``UA1_PART_team_a~7`` from ``UA1_PART_{team}~{partition}``."""

    name: str
    template: str
    team: str
    kind: str
    partition: Optional[int] = None

    @property
    def setting(self) -> str:
        """The setting that holds this stream's byte limit, for messages that tell an administrator what to change."""

        return f"transport.queues.max_bytes.{self.kind}"


@dataclass(frozen=True)
class QueueConsumer:
    """One consumer of a team's streams: the stream it reads and its NATS configuration (``config["name"]``)."""

    stream: str
    config: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.config["name"])


def team_queue_streams(transport: Mapping[str, Any], team: str, count: int) -> tuple[QueueStream, ...]:
    """Every work and dead-letter stream of ``team``: a ``{partition}`` stream once per partition.

    ``count`` is the installation's partition count. The streams are those the contract's
    queued classes name (``classes.*.streams``), which are every stream of ``streams``, in
    the generated contract's order, a partition stream's partitions in ascending order.
    """

    _check_id(_TEAM_ID, team, "team id")
    numbers = range(check_partition_count(count))
    streams: list[QueueStream] = []
    for template in _queued_streams(transport):
        kind = transport["streams"][template]["size_kind"]
        for partition in numbers if "{" + _PARTITION + "}" in template else (None,):
            streams.append(QueueStream(_fill(template, team=team, partition=partition), template, team, kind, partition))
    return tuple(streams)


def queue_stream_config(transport: Mapping[str, Any], stream: QueueStream, limits: QueueLimits) -> dict[str, Any]:
    """The NATS configuration of ``stream`` with ``limits``.

    Every contract field is carried over; the byte limit comes from the stream's size kind,
    the per-subject cap from ``max_messages_per_subject`` where the contract names that
    setting, and a partition stream records the count in its metadata
    (``PARTITION_COUNT_METADATA``).
    """

    spec = transport["streams"][stream.template]
    unknown = set(spec) - set(_STREAM_FIELDS) - _FILLED_STREAM_FIELDS
    if unknown:
        raise ValueError(f"stream {stream.template} has contract fields this version cannot apply: {sorted(unknown)}")
    if stream.kind not in limits.max_bytes:
        raise ValueError(f"no byte limit for {stream.name}: {stream.setting} is missing")
    config: dict[str, Any] = {
        "name": stream.name,
        "subjects": [_fill(subject, team=stream.team, partition=stream.partition) for subject in spec["subjects"]],
        **{_STREAM_FIELDS[field]: value for field, value in spec.items() if field in _STREAM_FIELDS},
        "max_bytes": limits.max_bytes[stream.kind],
        "num_replicas": limits.replicas,
    }
    if "max_msgs_per_subject_setting" in spec:
        config["max_msgs_per_subject"] = limits.max_messages_per_subject
    if stream.partition is not None:
        config["metadata"] = {PARTITION_COUNT_METADATA: str(limits.partitions)}
    return config


def team_consumers(transport: Mapping[str, Any], team: str) -> tuple[QueueConsumer, ...]:
    """The consumers that exist once per team: those whose names hold no id but the team's.

    These are the team services' inbox and dead-letter notice consumers. Pool and edge
    consumers name a pool or an edge, and agents' consumers an agent, so they are created
    when those exist (README, "Who sets up the streams").
    """

    _check_id(_TEAM_ID, team, "team id")
    return tuple(_consumer(spec, team=team) for spec in transport["consumers"].values()
                 if _placeholders(spec["name"], spec["filter"], spec["stream"]) == {_TEAM})


def agent_consumers(transport: Mapping[str, Any], team: str, agent: str, count: int) -> tuple[QueueConsumer, ...]:
    """The inbox and control consumers of ``agent`` in its partition's stream of ``team``."""

    _check_id(_TEAM_ID, team, "team id")
    _check_id(_AGENT_ID, agent, "agent id")
    partition = agent_partition(team, agent, count)
    return tuple(_consumer(spec, team=team, agent=agent, partition=partition) for spec in _agent_consumer_specs(transport))


def consumer_agent(transport: Mapping[str, Any], name: str) -> Optional[str]:
    """The agent an agent consumer's name holds (``agent~<agent>``, ``control~<agent>``); None for any other name."""

    for spec in _agent_consumer_specs(transport):
        before, _, after = spec["name"].partition("{" + _AGENT + "}")
        if name.startswith(before) and name.endswith(after) and len(name) > len(before) + len(after):
            agent = name[len(before):len(name) - len(after)]
            try:
                return _check_id(_AGENT_ID, agent, "agent id")
            except ValueError:
                return None
    return None


def _queued_streams(transport: Mapping[str, Any]) -> list[str]:
    return [stream for spec in transport["classes"].values() for stream in spec.get("streams", [])]


def _agent_consumer_specs(transport: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [spec for spec in transport["consumers"].values() if _AGENT in _placeholders(spec["name"])]


def _consumer(spec: Mapping[str, Any], **values: Any) -> QueueConsumer:
    unknown = set(spec) - set(_CONSUMER_FIELDS) - _FILLED_CONSUMER_FIELDS
    if unknown or spec["mode"] != "pull":
        raise ValueError(f"consumer {spec['name']} has contract settings this version cannot apply: "
                         f"{sorted(unknown) or spec['mode']}")
    name = _fill(spec["name"], **values)
    config = {
        "name": name,
        "durable_name": name,
        "filter_subject": _fill(spec["filter"], **values),
        **{_CONSUMER_FIELDS[field]: value for field, value in spec.items() if field in _CONSUMER_FIELDS},
    }
    return QueueConsumer(_fill(spec["stream"], **values), config)


def _placeholders(*templates: str) -> set[str]:
    return {name for template in templates for name in _PLACEHOLDER.findall(template)}


def _fill(template: str, **values: Any) -> str:
    """``template`` with each ``{name}`` replaced; a placeholder without a value is refused."""

    def value(match: re.Match[str]) -> str:
        filled = values.get(match.group(1))
        if filled is None:
            raise ValueError(f"{template} needs a {match.group(1)}")
        return str(filled)

    return _PLACEHOLDER.sub(value, template)


def _check_id(adapter: TypeAdapter[str], value: str, what: str) -> str:
    """``value`` when it is a contract id: one subject token, so a filled subject never reaches other teams or agents."""

    try:
        return adapter.validate_python(value)
    except ValidationError:
        raise ValueError(f"{value!r} is not a protocol 1 {what}") from None
