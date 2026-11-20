"""Inspection and offline cleanup for Universal Agents JetStream consumers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from custom_transport.base import AGENT_CONSUMER_ROLES, AGENT_CONTROL_ROLE, AGENT_DIRECT_ROLE
from custom_transport.nats_consumer_state import (
    StreamConsumerCapacity,
    consumer_has_open_request,
    consumer_listening_state,
    stream_consumer_capacity,
)
from custom_transport.nats_pull import CONSUMER_METADATA_SCHEMA, PUSH_CONSUMER_METADATA_SCHEMA
from custom_transport.subjects import NatsSubjectMapper


GENERATED_AGENT_ID_PATTERN = r"agent_id_[0-9a-f]{12}"

# The schemas an agent's own mailbox and control consumers may carry. From
# 2026-09-13, when a started worker's id began to be set by ``spawn_agent``,
# until pull delivery on 2026-10-05, every started worker got lasting push
# consumers with the push schema and the same ownership metadata, names and
# filter subjects as today's. An old consumer is replaced only when one with
# its name is created again, which never happens for a gone worker, so teams
# that ran those releases can still hold them (checked in the history before
# the repository restart of 2026-10-15).
AGENT_CONSUMER_SCHEMAS = frozenset({CONSUMER_METADATA_SCHEMA, PUSH_CONSUMER_METADATA_SCHEMA})
# The setting that gives an agent a lasting id (``strands_main``). The other
# source, ``--agent-id`` on the command line, is never used by the supervisor
# and cannot be read here; an agent run by hand with it is not recognised
# (``custom_agents.worker_consumer_cleanup``).
CONFIGURED_AGENT_ID_SETTING = "agent.agent_id"
# What kind of abandoned consumer a candidate is (``ConsumerReconciliationDecision.kind``).
KIND_MANAGED_PROCESS = "managed"  # process-owned, with ownership metadata
KIND_LEGACY_PROCESS = "legacy"  # a generated agent id's consumer from before the metadata
# A lasting agent mailbox or control consumer whose owner is not configured:
# what releases before 2026-11-19 left for every started worker.
KIND_LASTING_AGENT = "lasting_agent"
_ABANDONED_REASONS = {
    KIND_MANAGED_PROCESS: "abandoned_managed_process_consumer",
    KIND_LEGACY_PROCESS: "abandoned_legacy_process_consumer",
    KIND_LASTING_AGENT: "abandoned_lasting_agent_consumer",
}


def configured_agent_ids(config: Any) -> frozenset[str]:
    """Agent ids set in a runtime configuration (anything with ``get(path)``).

    Their mailbox and control consumers are lasting routes that must survive
    every process, so the team lead's cleanup and the offline script keep them.
    """

    configured = str(config.get(CONFIGURED_AGENT_ID_SETTING) or "").strip()
    return frozenset({configured}) if configured else frozenset()


@dataclass(frozen=True)
class AgentConsumer:
    """One agent's mailbox or control consumer found on the work stream."""

    stream_name: str
    consumer_name: str
    owner_agent_id: str
    consumer_role: str
    consumer_scope: str
    num_pending: int | None
    num_ack_pending: int | None
    # ``ua.schema``: this release's, or the push-delivery releases'.
    schema: str
    # A receiver has a request open right now (``consumer_has_open_request``).
    asking: bool


@dataclass(frozen=True)
class ConsumerReconciliationDecision:
    """One consumer classification from a reconciliation scan."""

    stream_name: str
    consumer_name: str
    filter_subject: str
    owner_agent_id: str | None
    consumer_role: str | None
    delete: bool
    reason: str
    listening: bool | None
    num_pending: int | None
    num_ack_pending: int | None
    # Which kind of candidate (``KIND_*``), or None when it is not one.
    kind: str | None = None


@dataclass(frozen=True)
class ConsumerReconciliationResult:
    """Summary and decisions returned by one reconciliation pass."""

    decisions: tuple[ConsumerReconciliationDecision, ...]
    applied: bool
    account_consumer_count: int | None = None
    account_max_consumers: int | None = None
    deleted_consumer_count: int = 0
    # The work stream's count and enforced limit, read after the deletions.
    stream_capacity: StreamConsumerCapacity | None = None

    @property
    def scanned_count(self) -> int:
        return len(self.decisions)

    @property
    def candidate_count(self) -> int:
        return sum(decision.delete for decision in self.decisions)

    @property
    def deleted_count(self) -> int:
        return self.deleted_consumer_count


class NatsConsumerReconciler:
    """Classify and delete one team's abandoned consumers with the runtime stopped.

    Candidates are process-owned agent consumers, legacy generated-id ones and,
    since 2026-11-20, lasting agent mailbox and control consumers whose owner is
    not configured (``KIND_LASTING_AGENT``). The team lead's own removal while
    the runtime runs is ``agent_consumers``/``delete_agent_consumers`` with the
    liveness rule (``custom_agents.worker_consumer_cleanup``).
    """

    def __init__(
        self,
        *,
        jetstream: Any,
        mapper: NatsSubjectMapper,
        stable_agent_ids: Iterable[str] = (),
        logger: logging.Logger | None = None,
        server_version: tuple[int, int] | None = None,
    ) -> None:
        self._js = jetstream
        self._mapper = mapper
        # (major, minor) of the NATS server, which decides its default stream limit.
        self._server_version = server_version
        self._stable_agent_ids = {
            str(agent_id).strip()
            for agent_id in stable_agent_ids
            if str(agent_id).strip()
        }
        self._logger = logger or logging.getLogger(__name__)

    async def reconcile(
        self,
        *,
        apply: bool,
        minimum_age_seconds: float,
        now: datetime | None = None,
        delete_kinds: frozenset[str] | None = None,
    ) -> ConsumerReconciliationResult:
        """Scan every team consumer, then optionally delete safe candidates.

        ``delete_kinds`` limits deletion to candidates of those kinds; the
        others are only reported. The launcher removes only lasting agent
        leftovers this way before any agent starts (``run_nats_consumer_preflight``).
        """

        observed_at = now or datetime.now(timezone.utc)
        infos = await self._consumer_infos()
        decisions = tuple(
            self._classify(
                stream_name=stream_name,
                info=info,
                minimum_age_seconds=minimum_age_seconds,
                observed_at=observed_at,
            )
            for stream_name, info in infos
        )
        deleted_count = await self._apply_decisions(decisions, delete_kinds) if apply else 0
        # Capacity after the deletions: the state this pass leaves behind. The
        # account is read once for both uses, so a failed read is logged once.
        account_info = await self._account_info()
        account_consumer_count, account_max_consumers = self._account_capacity(account_info)
        return ConsumerReconciliationResult(
            decisions=decisions,
            applied=apply,
            account_consumer_count=account_consumer_count,
            account_max_consumers=account_max_consumers,
            deleted_consumer_count=deleted_count,
            stream_capacity=await self._stream_capacity(account_info),
        )

    async def _consumer_infos(self) -> list[tuple[str, Any]]:
        infos: list[tuple[str, Any]] = []
        for stream_name in self._stream_names():
            infos.extend(
                (stream_name, info) for info in await self._list_consumers(stream_name)
            )
        return infos

    async def _apply_decisions(
        self,
        decisions: tuple[ConsumerReconciliationDecision, ...],
        delete_kinds: frozenset[str] | None = None,
    ) -> int:
        deleted_count = 0
        for decision in decisions:
            if not decision.delete or (delete_kinds is not None and decision.kind not in delete_kinds):
                continue
            try:
                await self._js.delete_consumer(
                    decision.stream_name,
                    decision.consumer_name,
                )
            except Exception as exc:
                if self._is_consumer_not_found(exc):
                    continue
                raise
            deleted_count += 1
            self._log_deleted(decision)
        return deleted_count

    def _log_deleted(self, decision: ConsumerReconciliationDecision) -> None:
        self._logger.info(
            "Deleted abandoned NATS consumer. "
            "event=nats_consumer_reconciler_deleted stream_name=%s "
            "consumer_name=%s consumer_role=%s owner_agent_id=%s "
            "num_pending=%s num_ack_pending=%s reason=%s",
            decision.stream_name,
            decision.consumer_name,
            decision.consumer_role,
            decision.owner_agent_id,
            decision.num_pending,
            decision.num_ack_pending,
            decision.reason,
        )

    async def stream_capacity(self) -> StreamConsumerCapacity | None:
        """The team work stream's consumer count and the limit NATS enforces on it.

        A stream that does not exist yet (a new team) gives no count but still
        the limit and server version, with no warning. None only when the
        stream cannot be read (logged).
        """

        return await self._stream_capacity(await self._account_info())

    async def _stream_capacity(self, account_info: Any) -> StreamConsumerCapacity | None:
        stream_name = self._mapper.work_stream_name()
        read_stream = getattr(self._js, "stream_info", None)
        if read_stream is None:
            return None
        try:
            stream_info = await read_stream(stream_name)
        except Exception as exc:
            if not self._is_stream_not_found(exc):
                self._capacity_unavailable(exc)
                return None
            stream_info = None
        return stream_consumer_capacity(stream_name, stream_info, account_info, self._server_version)

    def _capacity_unavailable(self, exc: Exception) -> None:
        self._logger.warning(
            "Could not read JetStream consumer capacity. "
            "event=nats_consumer_capacity_unavailable error=%s",
            exc,
        )

    async def _account_info(self) -> Any:
        account_info = getattr(self._js, "account_info", None)
        if account_info is None:
            return None
        try:
            return await account_info()
        except Exception as exc:
            self._capacity_unavailable(exc)
            return None

    def _account_capacity(self, info: Any) -> tuple[int | None, int | None]:
        if info is None:
            return None, None
        limits = getattr(info, "limits", None)
        count = self._optional_int(getattr(info, "consumers", None))
        maximum = self._optional_int(getattr(limits, "max_consumers", None))
        if maximum is not None and maximum < 0:
            maximum = None
        return count, maximum

    def _stream_names(self) -> tuple[str, ...]:
        return (
            self._mapper.work_stream_name(),
            self._mapper.presence_stream_name(),
        )

    async def _list_consumers(self, stream_name: str) -> list[Any]:
        consumers: list[Any] = []
        offset = 0
        while True:
            try:
                page = list(await self._js.consumers_info(stream_name, offset=offset))
            except Exception as exc:
                if self._is_stream_not_found(exc):
                    return consumers
                raise
            if not page:
                return consumers
            consumers.extend(page)
            offset += len(page)

    def _classify(
        self,
        *,
        stream_name: str,
        info: Any,
        minimum_age_seconds: float,
        observed_at: datetime,
    ) -> ConsumerReconciliationDecision:
        config = getattr(info, "config", None)
        consumer_name = str(getattr(info, "name", "") or "")
        filter_subject = str(getattr(config, "filter_subject", "") or "")
        metadata = dict(getattr(config, "metadata", None) or {})
        owner_agent_id, consumer_role, candidate_kind = self._managed_identity(
            metadata=metadata,
            stream_name=stream_name,
            consumer_name=consumer_name,
            filter_subject=filter_subject,
        )
        has_managed_metadata = any(str(key).startswith("ua.") for key in metadata)
        if candidate_kind is None and not has_managed_metadata:
            owner_agent_id, consumer_role = self._legacy_identity(
                stream_name=stream_name,
                consumer_name=consumer_name,
                filter_subject=filter_subject,
            )
            candidate_kind = KIND_LEGACY_PROCESS if owner_agent_id else None
        # A lasting consumer outlives its receiver with the messages it held
        # still pending, so only an open request proves a receiver for it.
        listening: bool | None = (
            consumer_has_open_request(info)
            if candidate_kind == KIND_LASTING_AGENT
            else consumer_listening_state(info)
        )

        reason = self._candidate_reason(
            candidate_kind,
            listening=listening,
            owner_agent_id=owner_agent_id,
            old_enough=self._old_enough(info, minimum_age_seconds=minimum_age_seconds, observed_at=observed_at),
        )
        delete = candidate_kind is not None and reason == _ABANDONED_REASONS[candidate_kind]

        return ConsumerReconciliationDecision(
            stream_name=stream_name,
            consumer_name=consumer_name,
            filter_subject=filter_subject,
            owner_agent_id=owner_agent_id,
            consumer_role=consumer_role,
            delete=delete,
            reason=reason,
            listening=listening,
            num_pending=self._optional_int(getattr(info, "num_pending", None)),
            num_ack_pending=self._optional_int(getattr(info, "num_ack_pending", None)),
            kind=candidate_kind,
        )

    def _candidate_reason(
        self, candidate_kind: str | None, *, listening: bool | None, owner_agent_id: str | None, old_enough: bool
    ) -> str:
        """Why a consumer is kept, or the ``_ABANDONED_REASONS`` entry when it is deleted."""

        if candidate_kind is None:
            return "unmanaged_or_persistent"
        if listening is None:
            return "listening_unknown"
        if listening:
            return "live_subscription"
        if owner_agent_id in self._stable_agent_ids:
            return "configured_stable_agent"
        if not old_enough:
            return "younger_than_minimum_age"
        return _ABANDONED_REASONS[candidate_kind]

    def _managed_identity(
        self,
        *,
        metadata: dict[str, str],
        stream_name: str,
        consumer_name: str,
        filter_subject: str,
    ) -> tuple[str | None, str | None, str | None]:
        """Owner, role and candidate kind of a consumer with ownership metadata, or Nones.

        Process-owned consumers of this release (agent mailboxes and control
        queues of both schemas, presence snapshots) and lasting agent mailbox
        and control consumers of both schemas are candidates; every other
        consumer (team-lead routes, worker queues, gateway routes) is not.
        """

        schema = metadata.get("ua.schema")
        owner_agent_id = str(metadata.get("ua.owner_agent_id") or "").strip()
        consumer_role = str(metadata.get("ua.consumer_role") or "").strip()
        scope = metadata.get("ua.consumer_scope")
        if (
            metadata.get("ua.managed") != "true"
            or metadata.get("ua.team_id") != self._mapper.team_id
            or not owner_agent_id
        ):
            return None, None, None
        kind: str | None
        if consumer_role in AGENT_CONSUMER_ROLES:
            kind = {"route": KIND_LASTING_AGENT, "process": KIND_MANAGED_PROCESS}.get(str(scope))
            matches = (
                kind is not None
                and schema in AGENT_CONSUMER_SCHEMAS
                and self._is_agent_consumer(
                    stream_name=stream_name,
                    consumer_name=consumer_name,
                    filter_subject=filter_subject,
                    owner_agent_id=owner_agent_id,
                    consumer_role=consumer_role,
                )
            )
        else:
            kind = KIND_MANAGED_PROCESS
            matches = (
                consumer_role == "presence_snapshot"
                and scope == "process"
                and schema == CONSUMER_METADATA_SCHEMA
                and (stream_name, filter_subject)
                == (self._mapper.presence_stream_name(), f"{self._mapper.prefix}.presence.*")
            )
        if not matches:
            return None, None, None
        return owner_agent_id, consumer_role, kind

    def _is_agent_consumer(
        self,
        *,
        stream_name: str,
        consumer_name: str,
        filter_subject: str,
        owner_agent_id: str,
        consumer_role: str,
    ) -> bool:
        """Whether a consumer is exactly the named agent's mailbox or control consumer."""

        if consumer_role == AGENT_DIRECT_ROLE:
            expected = (
                self._mapper.agent_work_subject(owner_agent_id),
                self._mapper.durable_name(f"agent_{owner_agent_id}"),
            )
        elif consumer_role == AGENT_CONTROL_ROLE:
            expected = (
                self._mapper.agent_control_subject(owner_agent_id),
                self._mapper.durable_name(f"agent_{owner_agent_id}_control"),
            )
        else:
            return False
        return stream_name == self._mapper.work_stream_name() and (
            filter_subject,
            consumer_name,
        ) == expected

    async def agent_consumers(self) -> tuple[AgentConsumer, ...]:
        """Every agent's mailbox and control consumer of this team on the work stream.

        Lasting and process-owned ones alike, of this release and of the
        push-delivery releases (``AGENT_CONSUMER_SCHEMAS``), read from the
        consumers' ownership metadata and checked against the names and subjects
        the agent would use, so nothing else on the stream is ever listed.
        Whether an owner is gone is not decided here
        (``custom_agents.worker_consumer_cleanup``).
        """

        stream_name = self._mapper.work_stream_name()
        found: list[AgentConsumer] = []
        for info in await self._list_consumers(stream_name):
            config = getattr(info, "config", None)
            metadata = dict(getattr(config, "metadata", None) or {})
            schema = str(metadata.get("ua.schema") or "")
            if (
                metadata.get("ua.managed") != "true"
                or schema not in AGENT_CONSUMER_SCHEMAS
                or metadata.get("ua.team_id") != self._mapper.team_id
            ):
                continue
            owner_agent_id = str(metadata.get("ua.owner_agent_id") or "").strip()
            consumer_role = str(metadata.get("ua.consumer_role") or "").strip()
            consumer_name = str(getattr(info, "name", "") or "")
            if consumer_role not in AGENT_CONSUMER_ROLES or not self._is_agent_consumer(
                stream_name=stream_name,
                consumer_name=consumer_name,
                filter_subject=str(getattr(config, "filter_subject", "") or ""),
                owner_agent_id=owner_agent_id,
                consumer_role=consumer_role,
            ):
                continue
            found.append(
                AgentConsumer(
                    stream_name=stream_name,
                    consumer_name=consumer_name,
                    owner_agent_id=owner_agent_id,
                    consumer_role=consumer_role,
                    consumer_scope=str(metadata.get("ua.consumer_scope") or ""),
                    num_pending=self._optional_int(getattr(info, "num_pending", None)),
                    num_ack_pending=self._optional_int(getattr(info, "num_ack_pending", None)),
                    schema=schema,
                    asking=consumer_has_open_request(info),
                )
            )
        return tuple(found)

    async def delete_agent_consumers(
        self, consumers: Iterable[AgentConsumer], *, reason: str
    ) -> int:
        """Delete these agent consumers; one already gone is skipped. Returns how many were deleted.

        Messages a deleted mailbox still held stay in the work stream (work-queue
        retention keeps unacknowledged messages), and a consumer created again
        with the same name and filter delivers them.
        """

        deleted = 0
        for consumer in consumers:
            try:
                await self._js.delete_consumer(consumer.stream_name, consumer.consumer_name)
            except Exception as exc:
                if self._is_consumer_not_found(exc):
                    continue
                raise
            deleted += 1
            self._logger.info(
                "Deleted a gone agent's NATS consumer. "
                "event=nats_agent_consumer_deleted stream_name=%s consumer_name=%s "
                "consumer_role=%s consumer_scope=%s schema=%s owner_agent_id=%s num_pending=%s "
                "num_ack_pending=%s reason=%s",
                consumer.stream_name,
                consumer.consumer_name,
                consumer.consumer_role,
                consumer.consumer_scope,
                consumer.schema,
                consumer.owner_agent_id,
                consumer.num_pending,
                consumer.num_ack_pending,
                reason,
            )
        return deleted

    def _legacy_identity(
        self,
        *,
        stream_name: str,
        consumer_name: str,
        filter_subject: str,
    ) -> tuple[str | None, str | None]:
        if stream_name == self._mapper.work_stream_name():
            subject_match = re.fullmatch(
                re.escape(f"{self._mapper.prefix}.agent.")
                + f"({GENERATED_AGENT_ID_PATTERN})"
                + re.escape(".work"),
                filter_subject,
            )
            if subject_match:
                agent_id = subject_match.group(1)
                if consumer_name == self._mapper.durable_name(f"agent_{agent_id}"):
                    return agent_id, AGENT_DIRECT_ROLE

        for role, route_prefix, expected_stream, expected_filter in (
            (
                "presence_snapshot",
                "presence",
                self._mapper.presence_stream_name(),
                f"{self._mapper.prefix}.presence.*",
            ),
        ):
            if stream_name != expected_stream or filter_subject != expected_filter:
                continue
            name_match = re.fullmatch(
                self._durable_name_pattern(route_prefix),
                consumer_name,
            )
            if name_match:
                return name_match.group(1), role
        return None, None

    def _durable_name_pattern(self, route_prefix: str) -> str:
        marker = "UAIDTOKEN"
        template = self._mapper.durable_name(f"{route_prefix}_{marker}")
        prefix, suffix = template.split(marker, maxsplit=1)
        return re.escape(prefix) + f"({GENERATED_AGENT_ID_PATTERN})" + re.escape(suffix)

    @staticmethod
    def _old_enough(
        info: Any,
        *,
        minimum_age_seconds: float,
        observed_at: datetime,
    ) -> bool:
        if minimum_age_seconds <= 0:
            return True
        created = getattr(info, "created", None)
        if not isinstance(created, datetime):
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (observed_at - created).total_seconds() >= minimum_age_seconds

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None:
            return None
        return int(value)

    @staticmethod
    def _is_consumer_not_found(exc: Exception) -> bool:
        if exc.__class__.__name__ in {"NotFoundError", "ConsumerNotFoundError"}:
            return True
        return "consumer not found" in str(exc).lower()

    @staticmethod
    def _is_stream_not_found(exc: Exception) -> bool:
        if exc.__class__.__name__ in {"NotFoundError", "StreamNotFoundError"}:
            return True
        return "stream not found" in str(exc).lower()
