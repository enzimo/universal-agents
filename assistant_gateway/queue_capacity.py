"""The team's protocol 1 queue streams that are filling up, as problems on administrators' Status page.

Every team's work and dead-letter streams have a byte limit each (``transport.queues.max_bytes``,
one per size kind) and refuse new messages once full (``discard: new``, contract README "Stream
sizes"): a full work stream makes senders keep each refused message and send it again until it
expires, and a full dead-letter stream makes receivers hold what they would set aside. So
administrators must hear before a stream fills, like the storage warnings
(``assistant_gateway.storage_capacity``), which this follows:

- The Status page check (every ``status_page.NOTIFY_INTERVAL_SECONDS``) lists the streams of the
  gateway's team in one stream list request (``NatsTransportAdapter.jetstream_streams``,
  metadata only) and measures each work and dead-letter stream the contract names for the team
  against its own limit. Each gateway checks only its own team; nothing here assumes one gateway.
- At ``transport.queues.warn_at_percent`` (80 by default) a stream is a problem, so
  administrators get an Inbox notice (``StatusPage.notify_new_problems``). The finding id names
  the stream and when it entered the level (``queue_filling:<stream>:<entered at>``), never the
  percentage, so no notice repeats while the stream stays full.
- When the level was entered is kept with the storage levels (``StorageLevels.track_one``, the
  same object and file as the storage warnings', under keys of their own: the object holds the
  whole file, so two would overwrite each other), so a restart neither repeats nor forgets a
  notice. The
  warning ends only once the stream falls ``storage_capacity.RELEASE_POINTS`` percentage points
  below the level, so a stream moving around it does not announce it again on every crossing.
- A stream that is not listed (the authority has not created it, or the server lost it) is not
  judged here; a list that fails marks the message server unavailable on the page. So does a
  list that names one of the team's streams as missing (``unmeasured``; in a cluster, a stream
  whose servers are down): the streams it did measure are still judged, and the missing one's
  level and earlier problem are kept until it can be measured again, never shown as fine.
- The team's streams are named at the first check, not when the gateway is built: a team id
  protocol 1 refuses (over 24 characters, which the launcher's id check stops before any process
  starts) fails the check, logged and listed as unavailable on the page, instead of keeping the
  gateway from starting over its size warning.

The setting is a transport setting rather than a level of ``gateway.storage_warnings``: it
measures a stream against its own limit, not a disk or the server's storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Final, Mapping, Sequence

from assistant_gateway.storage_capacity import RELEASE_POINTS, StorageCapacity, StorageLevels
from assistant_gateway.storage_usage import readable_bytes
from custom_messages.contracts.status_v1 import StatusFindingV1, StatusSeverity
from custom_transport.nats_adapter import JetStreamStreamUsage
from custom_transport.protocol1.contract_files import transport as contract_transport
from runtime_settings.instance_layout import INSTANCE_CONFIG_FILENAME

if TYPE_CHECKING:
    from custom_messages.wire.queues import QueueStream

SOURCE: Final = "gateway"
# Finding ids and the keys in StorageLevels start with these.
FINDING_PREFIX: Final = "queue_filling:"
LEVEL_KEY_PREFIX: Final = "queue:"
WARN_SETTING: Final = "transport.queues.warn_at_percent"

# What each size kind's stream holds, in words ("Team team_a's message stream for ...").
_KIND_WORDS: Final[Mapping[str, str]] = {
    "partition": "the inboxes and control queues of agents in partition {partition}",
    "pool": "the worker pools",
    "edge": "people's chat routes and outside agents",
    "team_services": "the team services' copies of tasks, results and handbacks",
    "dead_letter": "set-aside messages (dead letters)",
}
_WORK_EFFECT: Final = (
    "When it is full, NATS refuses new messages for it: senders keep each one and send it again until it "
    "expires, so work waits and can fail."
)
_DEAD_LETTER_EFFECT: Final = (
    "When it is full, set-asides are refused while full: receivers keep each message they would set aside "
    "(without taking an execution slot) and try again until room returns."
)


@dataclass(frozen=True)
class FillingStream:
    """One of the team's queue streams and what the stream list reports about it."""

    stream: QueueStream
    usage: JetStreamStreamUsage

    @property
    def percent(self) -> float:
        limit = self.usage.max_bytes
        return 100.0 * self.usage.stored_bytes / limit if limit else 0.0


class QueueCapacity:
    """Turns the stream list into Status findings for the gateway's team's work and dead-letter streams."""

    def __init__(
        self,
        *,
        team: str,
        transport: Mapping[str, Any],
        partitions: int,
        levels: StorageLevels,
        warn_at_percent: int,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.team = team
        self._transport = transport
        self._partitions = partitions
        # The streams the contract names for the team, by name; named at the first check (module docstring).
        self._streams: dict[str, QueueStream] | None = None
        self._levels = levels
        self.warn_at_percent = warn_at_percent
        self._now = now

    def team_streams(self) -> dict[str, QueueStream]:
        """The team's work/dead-letter streams plus installation-wide object stores by name; raises for a bad id."""

        if self._streams is None:
            # Imported here: the stream names come from the contract's generated models, which
            # cost about half a second to import, and the gateway module that imports this one
            # also serves command-line runs that never check streams.
            from custom_messages.wire.queues import object_store_streams, team_queue_streams

            streams = (
                *team_queue_streams(self._transport, self.team, self._partitions),
                *object_store_streams(self._transport),
            )
            self._streams = {stream.name: stream for stream in streams}
        return self._streams

    def unmeasured(self, missing: Sequence[str]) -> list[str]:
        """The team's work and dead-letter streams among those the server could not measure."""

        streams = self.team_streams()
        return sorted(name for name in missing if name in streams)

    def findings(self, listed: Sequence[JetStreamStreamUsage]) -> list[StatusFindingV1]:
        """This check's findings; saves when a stream entered or left the level, so it runs off the gateway's loop."""

        streams = self.team_streams()
        measured = [
            FillingStream(streams[usage.name], usage)
            for usage in listed
            if usage.name in streams and usage.max_bytes
        ]
        since = self._levels.track_one(
            ((LEVEL_KEY_PREFIX + item.stream.name, item.percent) for item in measured),
            threshold=self.warn_at_percent,
            now=self._now(),
        )
        return [
            stream_finding(item, since=since[key], warn_at_percent=self.warn_at_percent)
            for item in measured
            if (key := LEVEL_KEY_PREFIX + item.stream.name) in since
        ]


def stream_finding(item: FillingStream, *, since: datetime, warn_at_percent: int) -> StatusFindingV1:
    """The problem for a stream at the level; its id stays the same while the level lasts."""

    stream, usage = item.stream, item.usage
    limit = usage.max_bytes or 0
    if stream.team is None or stream.kind == "blobs":
        title = f"The installation's blob store stream ({stream.name}) is {item.percent:.0f}% full"
        summary = (
            f"Stream {stream.name} holds {readable_bytes(usage.stored_bytes)} of its "
            f"{readable_bytes(limit)} limit. When it is full, NATS refuses new files and long text "
            "(such as large Knowledge pages and attachments) across every team until space is freed."
        )
        action = (
            "Check on this page that team services are running and clearing expired blobs. "
            f"To keep more data in the blob store, raise {stream.setting} in {INSTANCE_CONFIG_FILENAME} "
            "and restart the instance (NATS sets the whole limit aside when it creates the stream, so "
            "the disk needs room for the larger limit), or lower agent.blob_retention_days so older blobs "
            "expire sooner."
        )
    else:
        holds = _KIND_WORDS.get(stream.kind, "messages of kind " + stream.kind).format(partition=stream.partition)
        effect = _DEAD_LETTER_EFFECT if stream.kind == "dead_letter" else _WORK_EFFECT
        if stream.kind == "dead_letter":
            cause = "Set-aside messages wait here until someone deals with them; find out why so many were set aside."
        else:
            cause = (
                "Check on this page that the team's hosts are running and taking their work: a stopped reader "
                "leaves its queue to fill."
            )
        title = f"Team {stream.team}'s message stream for {holds} is {item.percent:.0f}% full"
        summary = (
            f"Stream {stream.name} holds {readable_bytes(usage.stored_bytes)} of its "
            f"{readable_bytes(limit)} limit. {effect}"
        )
        action = (
            f"{cause} If the team has more waiting than fits, raise {stream.setting} in {INSTANCE_CONFIG_FILENAME} "
            "and restart the instance. NATS sets a stream's whole limit aside when it creates it, for every "
            "stream of that kind, so the disk needs room for the larger limits."
        )
    return StatusFindingV1(
        finding_id=f"{FINDING_PREFIX}{stream.name}:{since.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
        severity=StatusSeverity.PROBLEM,
        title=title[:200],
        summary=summary[:2000],
        action=action[:1000],
        source=SOURCE,
        since=since,
        details=(
            f"Stream: {stream.name}\n"
            f"Stored: {usage.stored_bytes:,} bytes in {usage.messages:,} messages\n"
            f"Limit: {limit:,} bytes ({stream.setting})\n"
            f"Warning level: {warn_at_percent}% ({WARN_SETTING}); the warning ends below "
            f"{warn_at_percent - RELEASE_POINTS:g}%"
        ),
    )


def build_queue_capacity(config: Any, *, storage: StorageCapacity) -> QueueCapacity:
    """The gateway's check: its team's streams, from the contract, at the instance's partition count and warning level.

    It keeps its levels in the storage warnings' own record (``storage.levels``). The contract's
    transport document is read here rather than by the gateway, which reaches the message
    transport only through its manager runtime.
    """

    queues = config.transport.queues
    return QueueCapacity(
        team=config.manager.team_id,
        transport=contract_transport(),
        partitions=queues.partitions,
        levels=storage.levels,
        warn_at_percent=queues.warn_at_percent,
    )


__all__ = ["FINDING_PREFIX", "FillingStream", "QueueCapacity", "build_queue_capacity", "stream_finding"]
