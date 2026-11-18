"""The installation authority creates and keeps current every team's protocol 1 work and dead-letter streams.

contracts/messages/v1/README.md ("Who sets up the streams", "Stream sizes", "Partitions"):
before it serves, the authority creates each team's streams from the contract
(``custom_messages.wire.queues``: a stream per partition, the pool, edge, team-services and
dead-letter streams) with the byte limits, per-subject cap and replica count of the
instance's settings, and the consumers that exist once per team (the team services' inbox
and dead-letter notices). Agents' consumers follow placements, pools' and edges' come with
the steps that name them. Nothing publishes to these streams or reads them yet.

``QueueTopology.ensure`` first reads every stream it wants and refuses the start, changing
nothing, when

- a team's partition streams were created with another partition count, which is fixed when
  the installation is created (``StreamCountMismatch``): streams of two layouts would put an
  agent's queues where its host does not read them;
- lowering the per-subject cap would drop queued messages (``StreamCapWouldDrop``): NATS
  keeps a lowered byte limit's messages but drops the oldest above a lowered
  ``max_msgs_per_subject`` (measured on nats-server 2.15.1);
- the new and enlarged limits do not fit the account's storage limit (``StreamStorageRefused``).

Then it lowers the streams whose limits go down, which gives back the room they set aside,
creates and enlarges the others, and creates or updates the team consumers. The server's
own storage refusal (JetStream error 10047) stops it the same way, naming the stream and
the setting; the streams already made stay, and the next start finishes the team. A start
changes only the streams whose settings differ in a field the contract sets, keeping
metadata keys it does not own, so a restart with the same settings changes nothing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from nats.js.api import ConsumerConfig, StreamConfig
from nats.js.errors import APIError, NotFoundError

from custom_messages.wire.queues import (
    PARTITION_COUNT_METADATA,
    QueueConsumer,
    QueueLimits,
    QueueStream,
    queue_stream_config,
    team_consumers,
    team_queue_streams,
)
from installation_authority.refusals import AuthorityStartRefused

_LOGGER = logging.getLogger(__name__)
# JetStream's error when a stream's limit does not fit the server's or the account's storage.
INSUFFICIENT_STORAGE = 10047
# The account information request ($JS.API.INFO): what the account sets aside and its limit.
ACCOUNT_INFO_SUBJECT = "$JS.API.INFO"
CAP_SETTING = "transport.queues.max_messages_per_subject"
COUNT_SETTING = "transport.queues.partitions"
# Metadata keys the server writes itself (_nats.level and others); never sent back.
_SERVER_METADATA_PREFIX = "_nats."
# Consumer settings the contract sets, compared to decide whether a consumer needs an update.
_CONSUMER_FIELDS = ("filter_subject", "ack_wait", "max_deliver", "ack_policy")


class StreamCountMismatch(AuthorityStartRefused):
    """A team's partition streams were created with another partition count than the setting's."""


class StreamCapWouldDrop(AuthorityStartRefused):
    """Lowering the partition streams' per-subject cap would drop messages a queue holds."""


class StreamStorageRefused(AuthorityStartRefused):
    """A stream's byte limit does not fit the account's storage limit or the server's storage."""


class StreamRefused(AuthorityStartRefused):
    """The server refused to create or update a stream for another reason, such as more replicas than it has servers."""


@dataclass(frozen=True)
class StreamChange:
    """One stream to create (``current`` is None) or update, with the fields that change and the room it sets aside."""

    stream: QueueStream
    config: dict[str, Any]
    current: Optional[StreamConfig]
    changed: tuple[str, ...]

    @property
    def reserve(self) -> int:
        """The bytes this change sets aside: its limit for a new stream, the difference for an update (below 0: given back)."""

        before = 0 if self.current is None else max(int(self.current.max_bytes or 0), 0)
        return int(self.config["max_bytes"]) - before


class QueueTopology:
    """Every team's work and dead-letter streams and team consumers on one NATS connection, as the contract describes."""

    def __init__(self, nc: Any, js: Any, *, transport: Mapping[str, Any], limits: QueueLimits, partitions: int,
                 logger: logging.Logger = _LOGGER) -> None:
        self._nc, self._js = nc, js
        self._transport = transport
        self.limits = limits
        self.partitions = partitions
        self._logger = logger

    async def ensure(self, teams: Iterable[str]) -> None:
        """Create or update every stream and team consumer of ``teams``; ``AuthorityStartRefused`` when it cannot."""

        teams = sorted(set(teams))
        changes: list[StreamChange] = []
        for team in teams:
            changes.extend(await self._team_changes(team))
        # Lowered streams first: they give back the room the others may need.
        ordered = [change for change in changes if change.reserve < 0] + [c for c in changes if c.reserve >= 0]
        await self._check_account_storage(ordered)
        for change in ordered:
            await self._apply(change)
        for team in teams:
            for consumer in team_consumers(self._transport, team):
                await self._ensure_consumer(consumer)
        self._logger.info("Every team's message streams are set up. event=protocol1_queue_streams_ready teams=%s "
                          "created=%s updated=%s", ",".join(teams),
                          sum(change.current is None for change in changes),
                          sum(change.current is not None for change in changes))

    # -- reading what is there ------------------------------------------------------------------

    async def _team_changes(self, team: str) -> list[StreamChange]:
        changes: list[StreamChange] = []
        for stream in team_queue_streams(self._transport, team, self.partitions):
            config = queue_stream_config(self._transport, stream, self.limits)
            try:
                info = await self._js.stream_info(stream.name)
            except NotFoundError:
                changes.append(StreamChange(stream, config, None, ()))
                continue
            current = info.config
            if stream.partition is not None:
                self._check_count(stream, current)
            changed = _changed_fields(config, current)
            if not changed:
                continue
            if "max_msgs_per_subject" in changed:
                await self._check_cap(stream, current, int(config["max_msgs_per_subject"]))
            changes.append(StreamChange(stream, config, current, changed))
        return changes

    def _check_count(self, stream: QueueStream, current: StreamConfig) -> None:
        """Refuse a partition stream created with another count (only ``ua.partitions``; the server adds keys of its own)."""

        recorded = (current.metadata or {}).get(PARTITION_COUNT_METADATA)
        if recorded == str(self.partitions):
            return
        if recorded is None:
            raise StreamCountMismatch(
                f"Stream {stream.name} records no partition count ({PARTITION_COUNT_METADATA} in its metadata), so the "
                f"installation authority did not create it and cannot tell which layout it holds. Nothing was changed. "
                f"Delete it once it holds nothing you need (nats stream rm {stream.name}), then start again.")
        raise StreamCountMismatch(
            f"Team {stream.team}'s partition streams were created with partition count {recorded} (stream "
            f"{stream.name}), but {COUNT_SETTING} is {self.partitions}. The count is fixed when the installation is "
            f"created: set {COUNT_SETTING} back to {recorded}. Nothing was changed.")

    async def _check_cap(self, stream: QueueStream, current: StreamConfig, cap: int) -> None:
        """Refuse to lower the per-subject cap below what one of the stream's subjects holds: NATS would drop the oldest.

        The stream is read just before it is changed. Until senders publish to these streams
        (step 6) nothing can arrive in between; from then on a sender at the old cap between
        the read and the update could lose its oldest message, which the step that adds the
        senders weighs.
        """

        before = int(current.max_msgs_per_subject if current.max_msgs_per_subject is not None else -1)
        if 0 < before <= cap:
            return
        info = await self._js.stream_info(stream.name, subjects_filter=">")
        held = info.state.subjects or {}
        over = sorted(((count, subject) for subject, count in held.items() if count > cap), reverse=True)
        if over:
            count, subject = over[0]
            keep = f"Keep {CAP_SETTING} at {before}" if before > 0 else "Keep the stream without a cap"
            raise StreamCapWouldDrop(
                f"Lowering {CAP_SETTING} to {cap} would drop queued messages: stream {stream.name} holds {count} "
                f"messages on {subject}, and {len(over)} of its subjects hold more than {cap}. NATS drops the oldest "
                f"messages above a lowered per-subject cap. {keep} until those queues drain, then lower it. Nothing "
                f"was changed.")

    async def _check_account_storage(self, ordered: list[StreamChange]) -> None:
        """Refuse when the account has a storage limit and the new and enlarged limits do not fit beside what it sets aside.

        An account without a limit of its own (``limits.max_storage`` -1, the bundled server's
        global account) reports nothing to check against: the server's 10047 refusal is the
        check there (``_apply``). A replicated account may keep its limits per replica tier
        instead; those are left to the server's refusal too.
        """

        if sum(change.reserve for change in ordered) <= 0:
            return
        account = await self._account_info()
        limit = int(account.get("limits", {}).get("max_storage", -1))
        if limit < 0:
            return
        reserved = int(account.get("reserved_storage", 0))
        for change in ordered:
            reserved += change.reserve
            if reserved > limit:
                raise StreamStorageRefused(
                    f"Stream {change.stream.name} does not fit the NATS account's storage limit of {limit:,} bytes: "
                    f"{self._totals(change.stream.team, account)} Lower {change.stream.setting} or the other "
                    f"transport.queues.max_bytes entries, or raise the account's storage limit. Nothing was changed.")

    # -- changing it --------------------------------------------------------------------------------

    async def _apply(self, change: StreamChange) -> None:
        config = StreamConfig(**change.config)
        if change.current is not None:
            # Keep the metadata keys this step does not own; the server writes its own again.
            config.metadata = {**{key: value for key, value in (change.current.metadata or {}).items()
                                  if not key.startswith(_SERVER_METADATA_PREFIX)},
                               **change.config.get("metadata", {})} or None
        try:
            if change.current is None:
                await self._js.add_stream(config=config)
            else:
                await self._js.update_stream(config=config)
        except APIError as error:
            if error.err_code != INSUFFICIENT_STORAGE:
                action = "create" if change.current is None else "update"
                raise StreamRefused(
                    f"NATS refused to {action} stream {change.stream.name}: JetStream error {error.err_code}, "
                    f"{error.description}. Check transport.replicas against the servers JetStream runs on, and the "
                    f"server's JetStream settings. The streams made so far stay; the next start finishes the "
                    f"team.") from error
            account = await self._account_info()
            raise StreamStorageRefused(
                f"NATS has no room to set aside {change.config['max_bytes']:,} bytes for stream {change.stream.name} "
                f"({change.stream.setting}): JetStream error {INSUFFICIENT_STORAGE}, {error.description}. "
                f"{self._totals(change.stream.team, account)} Lower {change.stream.setting} or the other "
                f"transport.queues.max_bytes entries, or give NATS more storage (its max_file_store, or more disk). "
                f"The streams made so far stay; the next start finishes the team.") from None
        if change.current is None:
            self._logger.info("Created a message stream. event=protocol1_queue_stream_created stream=%s max_bytes=%s "
                              "replicas=%s", change.stream.name, change.config["max_bytes"], change.config["num_replicas"])
        else:
            self._logger.info("Updated a message stream. event=protocol1_queue_stream_updated stream=%s changed=%s",
                              change.stream.name, ",".join(
                                  f"{name}:{_shown(getattr(change.current, name))}->{_shown(change.config[name])}"
                                  for name in change.changed))

    async def _ensure_consumer(self, consumer: QueueConsumer) -> None:
        config = ConsumerConfig(**consumer.config)
        try:
            info = await self._js.consumer_info(consumer.stream, consumer.name)
        except NotFoundError:
            await self._js.add_consumer(consumer.stream, config=config)
            self._logger.info("Created a team consumer. event=protocol1_queue_consumer_created stream=%s consumer=%s",
                              consumer.stream, consumer.name)
            return
        changed = [name for name in _CONSUMER_FIELDS if not _same(getattr(info.config, name), getattr(config, name))]
        if changed:
            await self._js.add_consumer(consumer.stream, config=config)  # creates or updates a consumer of that name
            self._logger.info("Updated a team consumer. event=protocol1_queue_consumer_updated stream=%s consumer=%s "
                              "changed=%s", consumer.stream, consumer.name, ",".join(changed))

    # -- what the refusals say ----------------------------------------------------------------------

    async def _account_info(self) -> dict[str, Any]:
        """The account's information as the server sends it: nats-py's ``AccountInfo`` lacks ``reserved_storage``."""

        reply: dict[str, Any] = json.loads((await self._nc.request(ACCOUNT_INFO_SUBJECT, b"", timeout=5)).data)
        if "error" in reply:
            APIError.from_error(reply["error"])  # raises the error's kind
        return reply

    def _totals(self, team: str, account: Mapping[str, Any]) -> str:
        """What a team's streams set aside at the current settings, and what the account sets aside and allows."""

        streams = team_queue_streams(self._transport, team, self.partitions)
        total = sum(self.limits.max_bytes[stream.kind] for stream in streams)
        partition = self.limits.max_bytes["partition"]
        others = ", ".join(f"{stream.setting} {self.limits.max_bytes[stream.kind]:,}"
                           for stream in streams if stream.partition is None)
        limit = int(account.get("limits", {}).get("max_storage", -1))
        allowed = f" of its limit of {limit:,}" if limit >= 0 else " (it has no limit of its own; the server's storage applies)"
        return (f"Team {team}'s streams set aside {total:,} bytes at the current settings ({self.partitions} partition "
                f"streams of {partition:,} bytes, and {others}); the account sets aside "
                f"{int(account.get('reserved_storage', 0)):,} bytes now{allowed}.")


def _changed_fields(config: Mapping[str, Any], current: StreamConfig) -> tuple[str, ...]:
    """The fields of ``config`` the existing stream holds differently; metadata is checked apart (``_check_count``)."""

    return tuple(name for name, value in config.items()
                 if name not in {"name", "metadata"} and not _same(getattr(current, name), value))


def _same(current: Any, wanted: Any) -> bool:
    """Whether the server's value equals the wanted one: enums equal their strings, times compare as numbers."""

    if isinstance(current, (int, float)) and isinstance(wanted, (int, float)) and not isinstance(wanted, bool):
        return float(current) == float(wanted)
    if isinstance(current, list) and isinstance(wanted, list):
        return sorted(current) == sorted(wanted)
    return bool(current == wanted)


def _shown(value: Any) -> str:
    return getattr(value, "value", str(value))
