"""The installation authority creates and keeps current the streams the contract's topology owner owns.

contracts/messages/v1/README.md ("Who sets up the streams", "Stream sizes", "Partitions"):
before it serves, the authority creates each team's work and dead-letter streams from the
contract (``custom_messages.wire.queues``: a stream per partition, the pool, edge,
team-services and dead-letter streams) with the byte limits, per-subject cap and replica
count of the instance's settings, the key-value buckets' streams (``custom_messages.wire.
buckets``: the installation's and each team's), and the consumers that exist once per team
(the team services' inbox and dead-letter notices). Agents' consumers follow placements
(``installation_authority.placements`` decides when; this module makes, inspects and
removes them), pools' and edges' come with the steps that name them. Nothing publishes to
the work streams or reads them yet.

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
  account allows, a stream taking more consumers than the account allows per stream
  (``StreamAccountRefused``), or more storage than its limit (``StreamStorageRefused``).

Then it lowers the streams whose limits go down, which gives back the room they set aside,
creates and enlarges the others, and creates or updates the team consumers. A start changes
only the streams whose settings differ in a field the contract sets, keeping metadata keys it
does not own, so a restart with the same settings asks NATS for no change at all. That
matters beyond speed: a NATS server whose storage is now smaller than what its streams set
aside (the bundled server measures its storage from the disk's free space at every start)
keeps serving the streams it has but refuses every new or changed stream, so a start that
asked for no change still serves there.

The server's refusals, and requests it does not answer, stop a start the same way as the
checks (``AuthorityStartRefused``, which the authority's process logs as
``installation_authority_start_refused``), naming the stream or consumer and the server's
error. One table decides the advice (``_server_refusal``): a setting or an account limit is
named only for the errors it fixes: storage (10047, and in a cluster a 10005 whose reasons
include "insufficient storage", since a cluster refuses a new stream too large for its
servers' storage that way and a raised limit of an existing one with 10047), 10027
streams, 10026 consumers, 10065 subjects another stream takes, and copies the servers
cannot hold (10074, and a 10005 that gives no reason). A request no JetStream answers, and
an account without JetStream (10039), say to enable it. Any other error is shown as the
server sent it, naming no setting.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from nats.errors import Error as NatsError
from nats.errors import NoRespondersError
from nats.js.api import ConsumerConfig, StorageType, StreamConfig
from nats.js.errors import APIError, NotFoundError, ServiceUnavailableError

from custom_messages.wire.buckets import TEAM_PLACEHOLDER, bucket_name, bucket_stream
from custom_messages.wire.queues import (
    PARTITION_COUNT_METADATA,
    QueueConsumer,
    QueueLimits,
    QueueStream,
    agent_consumers,
    consumer_agent,
    queue_stream_config,
    queue_streams_per_team,
    team_consumers,
    team_queue_streams,
)
from installation_authority.refusals import AuthorityStartRefused

_LOGGER = logging.getLogger(__name__)
# JetStream's errors whose cause a start names, measured on nats-server 2.15.1; every other
# error is shown as the server sent it (``QueueTopology._server_refusal``).
# A stream's limit does not fit the server's or the account's storage. A cluster answers it too,
# for a raised limit of an existing stream (a new stream gets NO_SUITABLE_PEERS there).
INSUFFICIENT_STORAGE = 10047
# The account (or its replica tier) holds as many streams as it allows.
MAXIMUM_STREAMS = 10027
# A stream's own consumer limit is above the account's, which caps each stream's consumers,
# or a stream holds as many consumers as it may.
MAXIMUM_CONSUMERS = 10026
# Another stream of the account takes some of the stream's subjects.
SUBJECTS_OVERLAP = 10065
# More than one copy of a stream on a server that is not in a cluster.
REPLICAS_WITHOUT_CLUSTER = 10074
# A cluster found too few servers to keep a stream's copies. Its text is NO_PEERS_TEXT and then
# the reasons servers were passed over (nats-server's selectPeerError: ", insufficient storage",
# ", peer offline" and others); with no reason, the cluster has fewer servers than the copies.
# Measured on three-server 2.15.1 clusters: a new stream too large for the servers' storage gets
# 10005 with "insufficient storage" (with one copy too), while raising an existing stream's limit
# beyond their room gets 10047, as on one server; five copies get the bare text.
NO_SUITABLE_PEERS = 10005
NO_PEERS_TEXT = "no suitable peers for placement"
NO_STORAGE_REASON = "insufficient storage"
# JetStream runs on the server, but not for the account the login belongs to (measured on 2.15.1).
JETSTREAM_NOT_ENABLED_FOR_ACCOUNT = 10039
# What a request about streams can meet besides success: a refusal (``APIError``), no answer in
# time (``TimeoutError``), or no JetStream to answer. ``NotFoundError`` is caught before it.
_SERVER_ERRORS = (NatsError, TimeoutError)
# What a refusal says the start left: nothing before the first change, the streams made after.
_NOTHING_CHANGED = "Nothing was changed."
_MADE_SO_FAR_STAY = "The streams made so far stay; once the cause is fixed, the next start finishes the team."
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
# The most consumers one consumer list reply holds (nats-server's API list limit).
CONSUMER_LIST_PAGE = 256
_STORAGE = {"file": StorageType.FILE, "memory": StorageType.MEMORY}


class StreamCountMismatch(AuthorityStartRefused):
    """A team's partition streams were created with another partition count than the setting's."""


class StreamCapWouldDrop(AuthorityStartRefused):
    """Lowering the partition streams' per-subject cap would drop messages a queue holds."""


class StreamStorageRefused(AuthorityStartRefused):
    """A stream's byte limit does not fit the account's storage limit or the server's storage."""


class StreamAccountRefused(AuthorityStartRefused):
    """The NATS account allows no more streams, fewer consumers per stream than a stream takes, or no replica tier."""


class StreamRefused(AuthorityStartRefused):
    """The server refused a stream or consumer for another reason, or did not answer; a setting is named only if it fixes it."""


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
    # Consumers per stream (measured on 2.15.1: a cap on each stream, not a total); 0 or below: none.
    consumer_limit: int = -1

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
                   stream_limit=int(limits.get("max_streams", -1)),
                   consumer_limit=int(limits.get("max_consumers", -1)))

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


@dataclass(frozen=True)
class AgentBacklog:
    """What an agent's inbox and control queues still hold.

    ``pending`` is waiting for delivery and ``ack_pending`` delivered but not acknowledged,
    both as the consumers report them; ``stored`` is what the partition stream holds on the
    queues' subjects. A message past its last delivery is neither pending nor waiting for an
    acknowledgement, yet stays in the work-queue stream (measured on nats-server 2.15.1), so
    only all three at 0 mean the queues hold nothing.
    """

    pending: int
    ack_pending: int
    stored: int

    @property
    def empty(self) -> bool:
        return self.pending == 0 and self.ack_pending == 0 and self.stored == 0


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
                try:
                    await self._ensure_consumer(consumer)
                except _SERVER_ERRORS as error:
                    raise self._server_refusal(f"create or update consumer {consumer.name} of stream {consumer.stream}",
                                               error, after=_MADE_SO_FAR_STAY) from None
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
            config: StreamConfig = (await self._js.stream_info(name)).config
        except NotFoundError:
            return None
        except _SERVER_ERRORS as error:
            raise self._server_refusal(f"read stream {name}", error, after=_NOTHING_CHANGED) from None
        return config

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
        try:
            info = await self._js.stream_info(stream.name, subjects_filter=">")
        except _SERVER_ERRORS as error:
            raise self._server_refusal(f"read what stream {stream.name} holds", error, after=_NOTHING_CHANGED) from None
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
        """Refuse when the account's limits do not allow what this start makes: its tier, streams, consumers or storage.

        An account without a storage limit of its own (``max_storage`` -1, the bundled server's
        global account) leaves the storage to the server, whose 10047 refusal is the check there
        (``_apply``). Asked only when something changes, so a restart with the same settings
        sends no request.
        """

        if not ordered:
            return
        account = await self._account_info(after=_NOTHING_CHANGED)
        room = AccountRoom.read(account, self.limits.replicas)
        entering = sum(room.enters(change) for change in ordered)
        if 0 < room.stream_limit < room.streams + entering:
            raise StreamAccountRefused(
                f"{_first_upper(room.where())} allows {room.stream_limit:,} streams and holds {room.streams:,}; this "
                f"start would add {entering:,}. {self._team_count()} The partition count is fixed when the "
                f"installation is created, so raise the account's stream limit to at least "
                f"{room.streams + entering:,}. Nothing was changed.")
        most = max(ordered, key=_consumers_of)
        if 0 < room.consumer_limit < _consumers_of(most):
            # Measured on nats-server 2.15.1: it refuses (10026) a stream whose own consumer
            # limit is above the account's, so the start would stop partway through.
            raise StreamAccountRefused(
                f"{_first_upper(room.where())} allows {room.consumer_limit:,} consumers per stream, and {most.what} "
                f"takes up to {_consumers_of(most):,}: NATS refuses a stream whose consumer limit is above the "
                f"account's. The contract sets how many consumers each stream takes, so raise the account's consumer "
                f"limit to at least {self.consumers_needed:,}, or remove it. Nothing was changed.")
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
                    f"{room.storage_limit:,} bytes: {self._totals(change.team, room)} {_lower(change)}{copies}, or "
                    f"raise the account's storage limit. Nothing was changed.")

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
        except _SERVER_ERRORS as error:
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
            self._logger.info("Created a queue consumer. event=protocol1_queue_consumer_created stream=%s consumer=%s",
                              consumer.stream, consumer.name)
            return
        changed = [name for name in _CONSUMER_FIELDS if not _same(getattr(info.config, name), getattr(config, name))]
        if changed:
            await self._js.add_consumer(consumer.stream, config=config)  # creates or updates a consumer of that name
            self._logger.info("Updated a queue consumer. event=protocol1_queue_consumer_updated stream=%s consumer=%s "
                              "changed=%s", consumer.stream, consumer.name, ",".join(changed))

    # -- agents' consumers (installation_authority.placements decides when) ----------------------

    async def ensure_agent_consumers(self, team: str, agent: str) -> None:
        """Create the agent's inbox and control consumers in its partition's stream, or update what differs."""

        for consumer in agent_consumers(self._transport, team, agent, self.partitions):
            await self._ensure_consumer(consumer)

    async def agent_backlog(self, team: str, agent: str) -> AgentBacklog:
        """What the agent's two queues hold; a consumer that does not exist holds nothing of its own."""

        pending = ack_pending = stored = 0
        for consumer in agent_consumers(self._transport, team, agent, self.partitions):
            try:
                info = await self._js.consumer_info(consumer.stream, consumer.name)
            except NotFoundError:
                pass
            else:
                pending += int(info.num_pending or 0)
                ack_pending += int(info.num_ack_pending or 0)
            held = await self._js.stream_info(consumer.stream, subjects_filter=consumer.config["filter_subject"])
            stored += sum((held.state.subjects or {}).values())
        return AgentBacklog(pending, ack_pending, stored)

    async def delete_agent_consumers(self, team: str, agent: str) -> int:
        """Delete the agent's two consumers; how many existed."""

        deleted = 0
        for consumer in agent_consumers(self._transport, team, agent, self.partitions):
            try:
                await self._js.delete_consumer(consumer.stream, consumer.name)
            except NotFoundError:
                continue
            deleted += 1
        return deleted

    async def agents_with_consumers(self, team: str) -> set[str]:
        """The agents with a consumer in one of ``team``'s partition streams (agent~ or control~ names)."""

        agents: set[str] = set()
        for stream in team_queue_streams(self._transport, team, self.partitions):
            if stream.partition is None:
                continue
            offset = 0
            while True:
                page = await self._js.consumers_info(stream.name, offset=offset)
                for info in page:
                    agent = consumer_agent(self._transport, info.name or "")
                    if agent is not None:
                        agents.add(agent)
                if len(page) < CONSUMER_LIST_PAGE:
                    break
                offset += len(page)
        return agents

    # -- what the refusals say ----------------------------------------------------------------------

    async def _refusal(self, change: StreamChange, error: BaseException, pending: int) -> AuthorityStartRefused:
        """The start refusal for the server's refusal of ``change``, or its missing answer."""

        action = "create" if change.current is None else "update"
        if isinstance(error, APIError) and _no_room_in_cluster(error):
            return await self._cluster_storage_refusal(change, action, error)
        if not isinstance(error, APIError) or error.err_code != INSUFFICIENT_STORAGE:
            return self._server_refusal(f"{action} {change.what}", error, after=_MADE_SO_FAR_STAY, change=change)
        room = AccountRoom.read(await self._account_info(after=_MADE_SO_FAR_STAY), self.limits.replicas)
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

    async def _cluster_storage_refusal(self, change: StreamChange, action: str,
                                       error: APIError) -> StreamStorageRefused:
        """The start refusal for a cluster's 10005 "insufficient storage": too few servers have room for the copies.

        A cluster places each copy of a stream on a server whose own storage has room for the
        stream's whole limit, so its totals are per server, and it refuses a new stream that fits
        nowhere with 10005. Fewer copies help when some servers have room. A raised limit of an
        existing stream is refused with 10047 instead, and gets the 10047 refusal (``_refusal``).
        """

        room = AccountRoom.read(await self._account_info(after=_MADE_SO_FAR_STAY), self.limits.replicas)
        copies = self.limits.replicas
        setting = f" ({change.setting})" if change.setting else ""
        fewer = f", lower {REPLICAS_SETTING} so fewer servers need that room" if copies > 1 else ""
        lacking = ("no server of the NATS cluster has room for it" if copies == 1
                   else f"too few servers of the NATS cluster have room for its {copies} copies")
        return StreamStorageRefused(
            f"NATS refused to {action} {change.what}: {lacking} (JetStream error {error.err_code}, "
            f"{error.description}). Each server that keeps a copy of a stream needs room for the stream's whole "
            f"limit in its own storage, here {_limit(change.config):,} bytes{setting}. "
            f"{self._totals(change.team, room)} {_lower(change)}{fewer}, or give the cluster's servers more storage (their max_file_store). {_MADE_SO_FAR_STAY}")

    def _server_refusal(self, doing: str, error: BaseException, *, after: str,
                        change: Optional[StreamChange] = None) -> AuthorityStartRefused:
        """The start refusal for ``error`` while trying ``doing`` ("create stream X"); storage has its own (``_refusal``).

        One table from JetStream error to advice (FA-3 of pull request 152's review): a setting
        or the account's limit is named only where changing it fixes the cause, and any other
        error is shown as the server sent it. ``after`` says what the start left;
        ``change`` is the stream being created or updated, when there is one.
        """

        if _no_jetstream_answered(error):
            return StreamRefused(
                f"No JetStream answered the request to {doing}: the NATS server runs without JetStream, or it is "
                f"still starting. Enable JetStream on the NATS server (a jetstream block in its configuration file), "
                f"then start again. {after}")
        if not isinstance(error, APIError):
            return StreamRefused(
                f"NATS did not answer the request to {doing} ({type(error).__name__}): its JetStream may be starting, "
                f"or a cluster choosing a leader. Start again once NATS answers. {after}")
        refused = f"JetStream error {error.err_code}, {error.description}"
        if error.err_code == JETSTREAM_NOT_ENABLED_FOR_ACCOUNT:
            return StreamRefused(
                f"NATS refused to {doing}: JetStream is not enabled for the NATS account the instance logs in to "
                f"({refused}). Enable JetStream for that account (jetstream: enabled in the account's block of the "
                f"server's configuration file), then start again. {after}")
        if error.err_code == MAXIMUM_STREAMS:
            return StreamAccountRefused(
                f"NATS refused to {doing}: the NATS account allows no more streams ({refused}). {self._team_count()} "
                f"Raise the account's stream limit. {after}")
        if error.err_code == MAXIMUM_CONSUMERS:
            takes = f"; {change.what} takes up to {_consumers_of(change):,}" if change is not None else ""
            return StreamAccountRefused(
                f"NATS refused to {doing}: the NATS account allows fewer consumers per stream than the instance's "
                f"streams take ({refused}){takes}. Raise the account's consumer limit to at least "
                f"{self.consumers_needed:,}, or remove it. {after}")
        if error.err_code == SUBJECTS_OVERLAP:
            subjects = f" ({', '.join(change.config.get('subjects') or ())})" if change is not None else ""
            return StreamRefused(
                f"NATS refused to {doing}: another stream in the NATS account takes some of its subjects{subjects} "
                f"({refused}). The instance needs these subjects to itself: remove the other stream or narrow its "
                f"subjects (nats stream ls lists the account's streams). {after}")
        if _too_many_copies(error):
            return StreamRefused(
                f"NATS refused to {doing}: the servers JetStream runs on cannot hold {self.limits.replicas} copies of "
                f"each stream ({refused}). Set {REPLICAS_SETTING} to at most the number of servers in the JetStream "
                f"cluster, or to 1 for a server that is not in a cluster. {after}")
        return StreamRefused(f"NATS refused to {doing}: {refused}. {after}")

    @property
    def consumers_needed(self) -> int:
        """The most consumers one of the instance's streams takes: the contract's largest ``max_consumers``, buckets too."""

        streams = [int(spec.get("max_consumers", 0)) for spec in self._transport["streams"].values()]
        return max([*streams, int(self._transport["key_value_stream"].get("max_consumers", 0))])

    async def _account_info(self, *, after: str) -> dict[str, Any]:
        """The account's information as the server sends it: nats-py's ``AccountInfo`` lacks ``reserved_storage``.

        ``after`` says what the start left, for the refusal when the server does not answer.
        """

        try:
            reply: dict[str, Any] = json.loads((await self._nc.request(ACCOUNT_INFO_SUBJECT, b"", timeout=5)).data)
            if "error" in reply:
                APIError.from_error(reply["error"])  # raises the error's kind
        except _SERVER_ERRORS as error:
            raise self._server_refusal("read the NATS account's JetStream information", error, after=after) from None
        return reply

    def _totals(self, team: Optional[str], room: AccountRoom) -> str:
        """What a team's streams set aside at the current settings, and what the account sets aside and allows."""

        copies = f", which with {room.copies} copies of each stream counts as {room.used:,}" if room.copies > 1 else ""
        if room.storage_limit >= 0:
            allowed = f"{',' if copies else ''} of its limit of {room.storage_limit:,}"
        else:
            allowed = " (it has no limit of its own; the server's storage applies)"
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


def _lower(change: StreamChange) -> str:
    """The advice to lower ``change``'s byte limit, or every one for a stream without a setting of its own."""

    if change.setting:
        return f"Lower {change.setting} or the other transport.queues.max_bytes entries"
    return "Lower the transport.queues.max_bytes entries"


def _placement_reasons(error: APIError) -> str:
    """The reasons a cluster's 10005 gives after NO_PEERS_TEXT ("insufficient storage", ...), "" for none."""

    text = (error.description or "").strip()
    if not text.startswith(NO_PEERS_TEXT):
        return text
    return text[len(NO_PEERS_TEXT):].lstrip(", ").strip()


def _no_room_in_cluster(error: APIError) -> bool:
    """A cluster's refusal of a stream too large for the storage of enough of its servers."""

    return error.err_code == NO_SUITABLE_PEERS and NO_STORAGE_REASON in _placement_reasons(error)


def _too_many_copies(error: APIError) -> bool:
    """More copies than the servers JetStream runs on: one server outside a cluster, or a cluster with too few."""

    return error.err_code == REPLICAS_WITHOUT_CLUSTER or (error.err_code == NO_SUITABLE_PEERS
                                                          and not _placement_reasons(error))


def _no_jetstream_answered(error: BaseException) -> bool:
    """Nothing serves the JetStream API to the login (measured on nats-server 2.15.1 without JetStream).

    nats-py raises ``NoRespondersError`` for a plain request (the account information) and a
    ``ServiceUnavailableError`` without codes for its own JetStream requests; a server that
    answers 503 sends its error code.
    """

    return isinstance(error, NoRespondersError) or (isinstance(error, ServiceUnavailableError)
                                                    and error.err_code is None)


def _consumers_of(change: StreamChange) -> int:
    """How many consumers ``change``'s stream takes, -1 for no limit of its own."""

    return int(change.config.get("max_consumers", -1))


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
