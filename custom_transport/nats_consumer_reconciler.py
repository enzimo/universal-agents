"""Inspection and offline cleanup for Universal Agents JetStream consumers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from custom_transport.nats_consumer_state import consumer_listening_state
from custom_transport.nats_pull import CONSUMER_METADATA_SCHEMA
from custom_transport.subjects import NatsSubjectMapper


GENERATED_AGENT_ID_PATTERN = r"agent_id_[0-9a-f]{12}"


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


@dataclass(frozen=True)
class ConsumerReconciliationResult:
    """Summary and decisions returned by one reconciliation pass."""

    decisions: tuple[ConsumerReconciliationDecision, ...]
    applied: bool
    account_consumer_count: int | None = None
    account_max_consumers: int | None = None
    deleted_consumer_count: int = 0

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
    """Classify and delete abandoned process-owned consumers for one team."""

    def __init__(
        self,
        *,
        jetstream: Any,
        mapper: NatsSubjectMapper,
        stable_agent_ids: Iterable[str] = (),
        logger: logging.Logger | None = None,
    ) -> None:
        self._js = jetstream
        self._mapper = mapper
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
    ) -> ConsumerReconciliationResult:
        """Scan every team consumer, then optionally delete safe candidates."""

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
        account_consumer_count, account_max_consumers = await self._account_capacity()
        deleted_count = await self._apply_decisions(decisions) if apply else 0
        return ConsumerReconciliationResult(
            decisions=decisions,
            applied=apply,
            account_consumer_count=account_consumer_count,
            account_max_consumers=account_max_consumers,
            deleted_consumer_count=deleted_count,
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
    ) -> int:
        deleted_count = 0
        for decision in decisions:
            if not decision.delete:
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

    async def _account_capacity(self) -> tuple[int | None, int | None]:
        account_info = getattr(self._js, "account_info", None)
        if account_info is None:
            return None, None
        try:
            info = await account_info()
        except Exception as exc:
            self._logger.warning(
                "Could not read JetStream consumer capacity. "
                "event=nats_consumer_capacity_unavailable error=%s",
                exc,
            )
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
        listening = consumer_listening_state(info)
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
            candidate_kind = "legacy" if owner_agent_id else None

        reason = "unmanaged_or_persistent"
        delete = False
        if candidate_kind is not None:
            if listening is None:
                reason = "listening_unknown"
            elif listening:
                reason = "live_subscription"
            elif owner_agent_id in self._stable_agent_ids:
                reason = "configured_stable_agent"
            elif not self._old_enough(
                info,
                minimum_age_seconds=minimum_age_seconds,
                observed_at=observed_at,
            ):
                reason = "younger_than_minimum_age"
            else:
                reason = f"abandoned_{candidate_kind}_process_consumer"
                delete = True

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
        )

    def _managed_identity(
        self,
        *,
        metadata: dict[str, str],
        stream_name: str,
        consumer_name: str,
        filter_subject: str,
    ) -> tuple[str | None, str | None, str | None]:
        if (
            metadata.get("ua.managed") != "true"
            or metadata.get("ua.schema") != CONSUMER_METADATA_SCHEMA
            or metadata.get("ua.team_id") != self._mapper.team_id
            or metadata.get("ua.consumer_scope") != "process"
        ):
            return None, None, None
        owner_agent_id = str(metadata.get("ua.owner_agent_id") or "").strip()
        consumer_role = str(metadata.get("ua.consumer_role") or "").strip()
        expected_route = {
            "agent_direct": (
                self._mapper.work_stream_name(),
                self._mapper.agent_work_subject(owner_agent_id),
            ),
            "presence_snapshot": (
                self._mapper.presence_stream_name(),
                f"{self._mapper.prefix}.presence.*",
            ),
        }.get(consumer_role)
        if not owner_agent_id or expected_route != (stream_name, filter_subject):
            return None, None, None
        if (
            consumer_role == "agent_direct"
            and consumer_name != self._mapper.durable_name(f"agent_{owner_agent_id}")
        ):
            return None, None, None
        return owner_agent_id, consumer_role, "managed"

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
                    return agent_id, "agent_direct"

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
