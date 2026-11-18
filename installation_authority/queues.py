"""The installation authority creates and keeps current the streams the contract's topology owner owns.

contracts/messages/v1/README.md ("Who sets up the streams", "Stream sizes", "Partitions"):
before it serves, the authority creates each team's work and dead-letter streams from the
contract (``custom_messages.wire.queues``: a stream per partition, the pool, edge,
team-services and dead-letter streams) with the byte limits, per-subject cap and replica
count of the instance's settings, the key-value buckets' streams (``custom_messages.wire.
buckets``: the installation's and each team's), and the consumers that exist once per team
(the team services' inbox and dead-letter notices). Agents' consumers follow placements,
pools' and edges' come with the steps that name them. Nothing publishes to the work streams
or reads them yet.

``QueueTopology.ensure`` first reads every stream it wants and refuses the start, changing
nothing, when

- a team's partition streams were created with another partition count, which is fixed when
  the installation is created (``StreamCountMismatch``): streams of two layouts would put an
  agent's queues where its host does not read them;
- lowering the per-subject cap would drop queued messages (``StreamCapWouldDrop``): NATS
  keeps a lowered byte limit's messages but drops the oldest above a lowered
  ``max_msgs_per_subject`` (measured on nats-server 2.15.1);
- the NATS account's limits do not allow what this start would make: no limits for the
  replica count on an account that keeps them per replica tier, more streams than the
  account allows (``StreamAccountRefused``), or more storage than its limit
  (``StreamStorageRefused``).

Then it lowers the streams whose limits go down, which gives back the room they set aside,
creates and enlarges the others, and creates or updates the team consumers. A start changes
only the streams whose settings differ in a field the contract sets, keeping metadata keys it
does not own, so a restart with the same settings asks NATS for no change at all. That
matters beyond speed: a NATS server whose storage is now smaller than what its streams set
aside (the bundled server measures its storage from the disk's free space at every start)
keeps serving the streams it has but refuses every new or changed stream, so a start that
asked for no change still serves there. The server's refusals (JetStream errors 10047 and
10027) stop a start the same way as the checks, naming the stream and what to do.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from nats.js.api import ConsumerConfig, StorageType, StreamConfig
from nats.js.errors import APIError, NotFoundError

from custom_messages.wire.buckets import TEAM_PLACEHOLDER, bucket_name, bucket_stream
from custom_messages.wire.queues import (
    PARTITION_COUNT_METADATA,
    QueueConsumer,
    QueueLimits,
    QueueStream,
    queue_stream_config,
    queue_streams_per_team,
    team_consumers,
    team_queue_streams,
)
from installation_authority.refusals import AuthorityStartRefused

_LOGGER = logging.getLogger(__name__)
# JetStream's error when a stream's limit does not fit the server's or the account's storage.
INSUFFICIENT_STORAGE = 10047
# JetStream's error when the account (or its replica tier) holds as many streams as it allows.
MAXIMUM_STREAMS = 10027
# The account information request ($JS.API.INFO): what the account sets aside, its limits and tiers.
ACCOUNT_INFO_SUBJECT = "$JS.API.INFO"
CAP_SETTING = "transport.queues.max_messages_per_subject"
COUNT_SETTING = "transport.queues.partitions"
REPLICAS_SETTING = "transport.replicas"
# A nats-server whose configuration sets no max_file_store, as the bundled server's does not,
# gives JetStream three quarters of the disk's free space, measured each time it starts
# (measured on 2.15.1: 0.750). So the disk needs a third more free space than the streams set
# aside, at every start of NATS, not only the first.
BUNDLED_STORAGE_SHARE = 3 / 4
# Metadata keys the server writes itself (_nats.level and others); never sent back.
_SERVER_METADATA_PREFIX = "_nats."
# Consumer settings the contract sets, compared to decide whether a consumer needs an update.
_CONSUMER_FIELDS = ("filter_subject", "ack_wait", "max_deliver", "ack_policy")
_STORAGE = {"file": StorageType.FILE, "memory": StorageType.MEMORY}


class StreamCountMismatch(AuthorityStartRefused):
    """A team's partition streams were created with another partition count than the setting's."""


class StreamCapWouldDrop(AuthorityStartRefused):
    """Lowering the partition streams' per-subject cap would drop messages a queue holds."""


class StreamStorageRefused(AuthorityStartRefused):
    """A stream's byte limit does not fit the account's storage limit or the server's storage."""


class StreamAccountRefused(AuthorityStartRefused):
    """The NATS account allows no more streams, or has no limits for the replica count."""


class StreamRefused(AuthorityStartRefused):
    """The server refused to create or update a stream for another reason, such as more replicas than it has servers."""


@dataclass(frozen=True)
class StreamChange:
    """One stream to create (``current`` is None) or update, with the fields that change.

    ``team`` is None for a bucket the installation's teams share; ``setting`` names the
    setting that holds the stream's byte limit, and ``bucket`` the bucket a bucket's stream
    keeps (its stream has no byte limit).
    """

    name: str
    team: Optional[str]
    config: dict[str, Any]
    current: Optional[StreamConfig]
    changed: tuple[str, ...]
    setting: Optional[str] = None
    bucket: Optional[str] = None

    @property
    def reserve(self) -> int:
        """The bytes one copy of this change sets aside: its limit for a new stream, the difference for an update."""

        before = 0 if self.current is None else max(int(self.current.max_bytes or 0), 0)
        return _limit(self.config) - before

    @property
    def what(self) -> str:
        return f"bucket {self.bucket}" if self.bucket else f"stream {self.name}"


@dataclass(frozen=True)
class AccountRoom:
    """What the NATS account allows the streams of this start, from its information (``$JS.API.INFO``).

    Measured on nats-server 2.15.1 (one server, and three in a cluster): an account with one
    flat set of limits counts every copy of a stream against its storage limit, but reports
    what its streams set aside once per stream. Every stream in the installation's account
    has ``transport.replicas`` copies (the authority's, the buckets' and the older streams'),
    so what it uses is the reported figure times that count; streams of others in the same
    account with fewer copies would make this check stricter than the server. An account
    whose limits are kept per replica tier (``R1``, ``R3``, as hosted NATS services set
    them) reports each tier on its own, with top-level limits of 0 that say nothing, and
    counts a stream once in the tier of its replica count.
    """

    replicas: int
    tier: Optional[str]
    storage_limit: int  # below 0: none
    reported: int  # what the account (or the tier) reports its streams set aside, once per stream
    streams: int
    stream_limit: int  # 0 or below: none

    @property
    def copies(self) -> int:
        """How many times the limit counts a stream's byte limit: each copy on a flat account, once in a tier."""

        return 1 if self.tier else self.replicas

    @property
    def used(self) -> int:
        """What the account's streams take of its limit, each with ``replicas`` copies."""

        return self.reported * self.copies

    @classmethod
    def read(cls, reply: Mapping[str, Any], replicas: int) -> "AccountRoom":
        """The room for streams of ``replicas`` copies; ``StreamAccountRefused`` when the account has no tier for them."""

        tiers = reply.get("tiers") or {}
        tier = None
        if tiers:
            tier = f"R{replicas}"
            if tier not in tiers:
                raise StreamAccountRefused(
                    f"The NATS account keeps its JetStream limits per replica tier ({', '.join(sorted(tiers))}) and "
                    f"has none for {tier}, the {REPLICAS_SETTING} setting of {replicas}. Set {REPLICAS_SETTING} to "
                    f"a replica count the account has a tier for, or ask the account's operator for limits in tier "
                    f"{tier}. Nothing was changed.")
        part = tiers[tier] if tier else reply
        limits = part.get("limits") or {}
        return cls(replicas=replicas, tier=tier, storage_limit=int(limits.get("max_storage", -1)),
                   reported=int(part.get("reserved_storage", 0)), streams=int(part.get("streams", 0)),
                   stream_limit=int(limits.get("max_streams", -1)))

    def reserve_of(self, change: StreamChange) -> int:
        """What ``change`` adds to ``used``: each copy on a flat account; a stream entering the tier adds its limit."""

        if self.tier is None:
            return change.reserve * self.replicas
        if self.enters(change):
            return _limit(change.config)
        return change.reserve

    def enters(self, change: StreamChange) -> bool:
        """Whether ``change`` adds a stream to the account's (or the tier's) stream count."""

        if change.current is None:
            return True
        return self.tier is not None and int(change.current.num_replicas or 1) != self.replicas

    def where(self) -> str:
        return f"the NATS account's tier {self.tier}" if self.tier else "the NATS account"


class QueueTopology:
    """The topology owner's streams on one NATS connection: every team's work and dead-letter streams, the buckets'
    streams and the team consumers, as the contract describes."""

    def __init__(self, nc: Any, js: Any, *, transport: Mapping[str, Any], limits: QueueLimits, partitions: int,
                 logger: logging.Logger = _LOGGER) -> None:
        self._nc, self._js = nc, js
        self._transport = transport
        self.limits = limits
        self.partitions = partitions
        self._logger = logger

    async def ensure(self, teams: Iterable[str]) -> None:
        """Create or update every stream, bucket and team consumer of ``teams``; ``AuthorityStartRefused`` when it cannot."""

        teams = sorted(set(teams))
        changes: list[StreamChange] = []
        for team in teams:
            changes.extend(await self._team_changes(team))
        changes.extend(await self._bucket_changes(teams))
        # Lowered streams first: they give back the room the others may need.
        ordered = [change for change in changes if change.reserve < 0] + [c for c in changes if c.reserve >= 0]
        await self._check_account(ordered)
        pending = sum(change.reserve for change in ordered)
        for change in ordered:
            await self._apply(change, pending=pending)
            pending -= change.reserve
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
            current = await self._current(stream.name)
            if current is not None and stream.partition is not None:
                self._check_count(stream, current)
            changed = () if current is None else _changed_fields(config, current)
            if current is not None and not changed:
                continue
            if "max_msgs_per_subject" in changed and current is not None:
                await self._check_cap(stream, current, int(config["max_msgs_per_subject"]))
            changes.append(StreamChange(stream.name, team, config, current, changed, setting=stream.setting))
        return changes

    async def _bucket_changes(self, teams: list[str]) -> list[StreamChange]:
        """The buckets' streams to create or update: the installation's once, each team's per team.

        The client's bucket-creation helper enables rollup headers, which would let a writer of
        one key erase other keys, so the stream is made with the contract's restrictions and
        bound as a bucket afterwards (``custom_messages.wire.buckets``).
        """

        changes: list[StreamChange] = []
        for bucket in self._transport["key_value"]:
            for team in teams if TEAM_PLACEHOLDER in bucket else (None,):
                config = bucket_stream(self._transport, bucket, team)
                config["storage"] = _STORAGE[config["storage"]]
                config["num_replicas"] = self.limits.replicas
                current = await self._current(config["name"])
                changed = () if current is None else _changed_fields(config, current)
                if current is None or changed:
                    changes.append(StreamChange(config["name"], team, config, current, changed,
                                                bucket=bucket_name(bucket, team)))
        return changes

    async def _current(self, name: str) -> Optional[StreamConfig]:
        try:
            return (await self._js.stream_info(name)).config
        except NotFoundError:
            return None

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

    async def _check_account(self, ordered: list[StreamChange]) -> None:
        """Refuse when the account's limits do not allow what this start makes: its tier, stream count or storage.

        An account without a storage limit of its own (``max_storage`` -1, the bundled server's
        global account) leaves the storage to the server, whose 10047 refusal is the check there
        (``_apply``). Asked only when something changes, so a restart with the same settings
        sends no request.
        """

        if not ordered:
            return
        account = await self._account_info()
        room = AccountRoom.read(account, self.limits.replicas)
        entering = sum(room.enters(change) for change in ordered)
        if 0 < room.stream_limit < room.streams + entering:
            raise StreamAccountRefused(
                f"{_first_upper(room.where())} allows {room.stream_limit:,} streams and holds {room.streams:,}; this "
                f"start would add {entering:,}. {self._team_count()} The partition count is fixed when the "
                f"installation is created, so raise the account's stream limit to at least "
                f"{room.streams + entering:,}. Nothing was changed.")
        if room.storage_limit < 0:
            return
        copies_change = any(change.current is not None and int(change.current.num_replicas or 1) != room.replicas
                            for change in ordered)
        if room.used > room.storage_limit and not copies_change:
            # Measured on nats-server 2.15.1: such an account refuses every new or changed stream,
            # an unchanged update and a lowered limit included. (While transport.replicas changes,
            # ``used`` counts the copies the streams are getting, so the loop below speaks.)
            raise StreamStorageRefused(
                f"{_first_upper(room.where())} sets aside {room.used:,} bytes, more than its storage limit of "
                f"{room.storage_limit:,}: its limit was lowered below what its streams had set aside. NATS refuses "
                f"every new or changed stream until it is raised, a lowered limit too, so lowering "
                f"transport.queues.max_bytes cannot help. Raise the account's storage limit. Nothing was changed.")
        used = room.used
        copies = f", {REPLICAS_SETTING}" if room.copies > 1 else ""
        for change in ordered:
            used += room.reserve_of(change)
            if used > room.storage_limit:
                raise StreamStorageRefused(
                    f"{_first_upper(change.what)} does not fit the storage limit of {room.where()}, "
                    f"{room.storage_limit:,} bytes: {self._totals(change.team, room)} Lower "
                    f"{change.setting or 'the transport.queues.max_bytes entries'}"
                    f"{' or the other transport.queues.max_bytes entries' if change.setting else ''}{copies}, or raise "
                    f"the account's storage limit. Nothing was changed.")

    # -- changing it --------------------------------------------------------------------------------

    async def _apply(self, change: StreamChange, *, pending: int) -> None:
        """Create or update one stream; ``pending`` is what this change and the later ones set aside, one copy each."""

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
            raise await self._refusal(change, error, pending) from None
        kind = "bucket" if change.bucket else "queue_stream"
        if change.current is None:
            self._logger.info("Created a %s. event=protocol1_%s_created stream=%s max_bytes=%s replicas=%s",
                              "key-value bucket" if change.bucket else "message stream", kind, change.name,
                              change.config.get("max_bytes", -1), change.config["num_replicas"])
        else:
            self._logger.info("Updated a %s. event=protocol1_%s_updated stream=%s changed=%s",
                              "key-value bucket" if change.bucket else "message stream", kind, change.name, ",".join(
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

    async def _refusal(self, change: StreamChange, error: APIError, pending: int) -> AuthorityStartRefused:
        """The start refusal for the server's refusal of ``change``."""

        action = "create" if change.current is None else "update"
        if error.err_code == MAXIMUM_STREAMS:
            return StreamAccountRefused(
                f"NATS refused to create {change.what}: the NATS account allows no more streams (JetStream error "
                f"{MAXIMUM_STREAMS}, {error.description}). {self._team_count()} Raise the account's stream limit. The "
                f"streams made so far stay; once the account allows them, the next start finishes the team.")
        if error.err_code != INSUFFICIENT_STORAGE:
            return StreamRefused(
                f"NATS refused to {action} {change.what}: JetStream error {error.err_code}, {error.description}. "
                f"Check {REPLICAS_SETTING} against the servers JetStream runs on, and the server's JetStream "
                f"settings. The streams made so far stay; the next start finishes the team.")
        room = AccountRoom.read(await self._account_info(), self.limits.replicas)
        refused = f"JetStream error {INSUFFICIENT_STORAGE}, {error.description}."
        if change.reserve <= 0:
            # A lowering or a change of no size can be refused only when what is set aside already
            # exceeds the storage NATS has: it then refuses every new or changed stream.
            return StreamStorageRefused(
                f"NATS refused to {'lower the limit of' if change.reserve < 0 else action} {change.what}: {refused} "
                f"NATS refuses every new or changed stream while its streams set aside more than the storage it has, "
                f"which happens when NATS started with less room than they had set aside, or the account's limit was "
                f"lowered below it. Lowering a limit cannot help then. {self._totals(change.team, room)}"
                f"{self._bundled_room(room, room.used)} Free disk space and restart the instance (the bundled server "
                f"measures its storage when it starts), or give an external server more storage (its "
                f"max_file_store) or raise its account's limit. Nothing else was changed by this start's refused "
                f"step; once NATS has the room, the next start finishes.")
        return StreamStorageRefused(
            f"NATS has no room to set aside {_limit(change.config):,} bytes for {change.what} "
            f"({change.setting}): {refused} {self._totals(change.team, room)}"
            f"{self._bundled_room(room, room.used + pending * room.copies)} Lower "
            f"{change.setting} or the other transport.queues.max_bytes entries, or give NATS more storage (free disk "
            f"space and restart the instance, or an external server's max_file_store). If NATS started with less "
            f"room than its streams had already set aside, it refuses every change, a lowered limit too, until it "
            f"has more. The streams made so far stay; once the limits fit, the next start finishes the team.")

    async def _account_info(self) -> dict[str, Any]:
        """The account's information as the server sends it: nats-py's ``AccountInfo`` lacks ``reserved_storage``."""

        reply: dict[str, Any] = json.loads((await self._nc.request(ACCOUNT_INFO_SUBJECT, b"", timeout=5)).data)
        if "error" in reply:
            APIError.from_error(reply["error"])  # raises the error's kind
        return reply

    def _totals(self, team: Optional[str], room: AccountRoom) -> str:
        """What a team's streams set aside at the current settings, and what the account sets aside and allows."""

        if room.storage_limit >= 0:
            allowed = f" of its limit of {room.storage_limit:,}"
        else:
            allowed = " (it has no limit of its own; the server's storage applies)"
        copies = (f", which with {room.copies} copies of each stream counts as {room.used:,}," if room.copies > 1
                  else "")
        account = f"{room.where()} sets aside {room.reported:,} bytes now{copies}{allowed}."
        if team is None:
            return _first_upper(account)
        streams = team_queue_streams(self._transport, team, self.partitions)
        total = sum(self.limits.max_bytes[stream.kind] for stream in streams)
        partition = self.limits.max_bytes["partition"]
        others = ", ".join(f"{stream.setting} {self.limits.max_bytes[stream.kind]:,}"
                           for stream in streams if stream.partition is None)
        return (f"Team {team}'s streams set aside {total:,} bytes at the current settings ({self.partitions} partition "
                f"streams of {partition:,} bytes, and {others}); {account}")

    @staticmethod
    def _bundled_room(room: AccountRoom, set_aside: int) -> str:
        """What the bundled server needs on its disk for ``set_aside`` bytes; nothing for an account with a limit.

        The bundled server's account has no storage limit of its own, so the server's storage,
        measured from the disk when it starts, is what refuses.
        """

        if room.storage_limit >= 0:
            return ""
        return (f" On the bundled NATS server JetStream gets three quarters of the disk's free space, measured each "
                f"time NATS starts, so for {set_aside:,} bytes set aside the disk needs about "
                f"{math.ceil(set_aside / BUNDLED_STORAGE_SHARE):,} bytes free at every start.")

    def _team_count(self) -> str:
        """How many streams a team needs at this partition count, buckets included."""

        team_buckets = sum(TEAM_PLACEHOLDER in bucket for bucket in self._transport["key_value"])
        shared = len(self._transport["key_value"]) - team_buckets
        others = queue_streams_per_team(self._transport, self.partitions) - self.partitions
        return (f"A team needs {self.partitions + others + team_buckets:,} streams at partition count "
                f"{self.partitions} ({self.partitions} partition streams, {others} more and {team_buckets} buckets), "
                f"and the installation {shared} buckets of its own.")


def _first_upper(text: str) -> str:
    """``text`` starting with a capital, the rest as it is (stream names keep their case)."""

    return text[:1].upper() + text[1:]


def _limit(config: Mapping[str, Any]) -> int:
    """A stream configuration's byte limit, 0 for none (a bucket's stream)."""

    return max(int(config.get("max_bytes", -1)), 0)


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
