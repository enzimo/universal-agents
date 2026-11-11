"""Deliberate stream reconciliation and read-only participant validation.

The gateway owns instance topology; the standalone team lead owns generic
launch topology. Connecting workers never alter shared retention or subjects.
Participants check once and never wait, so the headless launcher runs the
gateway's setup before it starts any service
(``docker.launch_headless.prepare_message_streams``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nats.errors import Error as NatsError
from nats.js.errors import NotFoundError
from custom_transport.subjects import NatsSubjectMapper, encode_durable_name

if TYPE_CHECKING:
    from custom_transport.nats_adapter import NatsTransportConfig


class NatsTopologyMismatch(RuntimeError):
    """Effective broker settings disagree with the participant contract."""


# How long a set-aside (dead-letter) message is kept for people to look at
# (owner's decision 2026-10-07). Fixed rather than a setting: it is long enough
# for an administrator to react to the notice they get at once
# (custom_agents.dead_letter_watch), and the copies hold message content.
DEAD_LETTER_MAX_AGE_SECONDS = 3 * 24 * 60 * 60
# The most the stream holds; the oldest copies go first. A flood of messages no
# receiver can handle must not fill a memory-backed broker.
DEAD_LETTER_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class NatsStreamTopology:
    config: NatsTransportConfig
    mapper: NatsSubjectMapper

    def specifications(self) -> list[Any]:
        try:
            from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Installed nats-py does not expose JetStream API classes.") from exc

        specifications = []
        storage = StorageType.FILE if str(self.config.storage).lower() == "file" else StorageType.MEMORY
        work_subjects = [
            f"{self.mapper.prefix}.agent.*.work",
            f"{self.mapper.prefix}.agent.*.control",
            f"{self.mapper.prefix}.lead.work",
            f"{self.mapper.prefix}.type.*.work",
            f"{self.mapper.prefix}.gateway.*.response",
        ]
        specifications.append(
            StreamConfig(
                name=self.mapper.work_stream_name(),
                subjects=work_subjects,
                retention=RetentionPolicy.WORK_QUEUE,
                storage=storage,
                num_replicas=int(self.config.replicas or 1),
                discard=DiscardPolicy.NEW,
                duplicate_window=float(self.config.duplicate_window_seconds),
                max_age=(
                    float(self.config.work_max_age_seconds)
                    if self.config.work_max_age_seconds
                    else None
                ),
            )
        )
        # Messages a receiver ended on their last allowed delivery because it
        # could not read or handle them (custom_transport.nats_pull). Without
        # this copy an ended message was gone and only log lines were left.
        specifications.append(
            StreamConfig(
                name=self.mapper.dead_letter_stream_name(),
                subjects=[self.mapper.dead_letter_subjects()],
                retention=RetentionPolicy.LIMITS,
                storage=storage,
                num_replicas=int(self.config.replicas or 1),
                discard=DiscardPolicy.OLD,
                duplicate_window=float(self.config.duplicate_window_seconds),
                max_age=float(DEAD_LETTER_MAX_AGE_SECONDS),
                max_bytes=DEAD_LETTER_MAX_BYTES,
                # A retry may publish the same raw copy without a dedup header
                # because adding one could exceed the accepted payload limit.
                # Stable copy subjects keep one complete record per copy.
                max_msgs_per_subject=1,
            )
        )
        return specifications

    def retired_stream_names(self) -> tuple[str, ...]:
        """Streams earlier versions created that nothing uses now; the topology owner deletes them.

        ``<PREFIX>_PRESENCE`` held the presence heartbeat until message contract step 7d
        (who is live comes from the team's signed agent records); deleting it also removes the
        gateway's durable presence consumer on it.
        """

        return (encode_durable_name(f"{self.mapper.prefix}_PRESENCE").upper(),)

    async def _delete_retired_streams(self, jetstream: Any, logger: logging.Logger) -> None:
        """Delete each retired stream that still exists; never stop the start for one.

        It is looked up first, so a fresh install sends no delete. Deleting is a one-time
        clean-up of an upgraded installation: a NATS login without the right to delete
        streams (an external server whose login may only create and update them) gets a
        warning naming the stream and how to remove it by hand, and the start goes on;
        nothing uses the stream. Such a login waits for the request to time out (about
        5 seconds) on every start until the stream is removed.
        """

        for name in self.retired_stream_names():
            try:
                await jetstream.stream_info(name)
                await jetstream.delete_stream(name)
            except NotFoundError:
                # Never created, or another owner deleted it meanwhile.
                continue
            except (NatsError, TimeoutError) as exc:
                logger.warning(
                    "Could not delete stream %s, which this version no longer uses; nothing reads or writes it. "
                    "Remove it with a NATS login that may delete streams, for example `nats stream rm %s`. "
                    "event=nats_retired_stream_not_deleted stream=%s error_type=%s",
                    name, name, name, type(exc).__name__,
                )
                continue
            logger.info("Deleted a stream this version no longer uses. event=nats_retired_stream_deleted stream=%s",
                        name)

    @staticmethod
    def differences(expected: Any, actual: Any) -> dict[str, tuple[Any, Any]]:
        fields = ("subjects", "retention", "storage", "num_replicas", "discard",
                  "duplicate_window", "max_age", "max_msgs_per_subject", "max_bytes")
        differences = {}
        for name in fields:
            wanted, found = getattr(expected, name), getattr(actual, name)
            if name == "duplicate_window" and not wanted:
                # Zero asks JetStream to choose its server default.
                continue
            if name == "subjects":
                wanted, found = sorted(wanted or []), sorted(found or [])
            elif name == "max_age":
                wanted, found = wanted or 0, found or 0
            elif wanted is None:
                continue
            if wanted != found:
                differences[name] = (wanted, found)
        return differences

    async def validate(self, jetstream: Any) -> None:
        for expected in self.specifications():
            try:
                actual = (await jetstream.stream_info(expected.name)).config
            except NotFoundError as exc:
                raise NatsTopologyMismatch(
                    f"Stream {expected.name} is missing; start the topology owner first."
                ) from exc
            differences = self.differences(expected, actual)
            if differences:
                raise NatsTopologyMismatch(
                    f"Stream {expected.name} configuration mismatch: {differences}"
                )

    async def reconcile(self, jetstream: Any, logger: logging.Logger) -> None:
        await self._delete_retired_streams(jetstream, logger)
        if str(self.config.storage).lower() != "file":
            # Flushing to disk (sync_interval: always) cannot keep what is only in memory.
            logger.warning(
                "NATS streams are kept in memory (transport.storage): queued work is lost when the NATS "
                "server restarts or the machine loses power. event=nats_streams_in_memory storage=%s",
                self.config.storage,
            )
        for expected in self.specifications():
            try:
                actual = (await jetstream.stream_info(expected.name)).config
            except NotFoundError:
                await jetstream.add_stream(expected)
            else:
                differences = self.differences(expected, actual)
                if differences:
                    logger.info("Reconciling NATS stream topology", extra={
                        "event": "nats_topology_reconcile", "stream": expected.name,
                        "changed_fields": sorted(differences),
                    })
                    await jetstream.update_stream(expected)
        # A successful API response is insufficient: prove effective settings.
        await self.validate(jetstream)
