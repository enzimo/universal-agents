"""Gateway request-lifecycle use cases over request and session ports."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from assistant_gateway.application.ports import GatewayClock
from assistant_gateway.domain.request_state import PendingQueryRecord, RequestRecord
from assistant_gateway.ports.repositories import (
    RequestRepository,
    RequestUnitOfWorkFactory,
)


TERMINAL_REQUEST_STATUSES = frozenset(
    {
        "completed",
        "manager_unavailable",
        "delivered",
        "deferred",
        "scheduled",
        "accepted",
        "rejected",
        "stale",
        "expired",
    }
)


class GatewayRequestService:
    """Own request persistence, status transitions, and pending-query state."""

    def __init__(
        self,
        *,
        repository: RequestRepository,
        unit_of_work_factory: RequestUnitOfWorkFactory,
        clock: GatewayClock,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

    def record(
        self,
        *,
        correlation_id: str,
        session_key: str,
        request_kind: str,
        request_text: str,
        conversation_id: str | None,
        followup_to_task_id: str | None,
        status: str = "pending",
        owner_canonical_user_id: str | None = None,
    ) -> None:
        self._repository.record_request(
            correlation_id=correlation_id,
            session_key=session_key,
            request_kind=request_kind,
            request_text=request_text,
            conversation_id=conversation_id,
            followup_to_task_id=followup_to_task_id,
            status=status,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def get(self, correlation_id: str) -> RequestRecord | None:
        return self._repository.get_request(correlation_id)

    def update_status(self, correlation_id: str, status: str) -> None:
        self._repository.update_request_status(correlation_id, status)

    def update_coordination(
        self,
        correlation_id: str,
        *,
        status: str,
        active_task_id: str,
    ) -> None:
        """Persist the current runtime task separately from its follow-up parent."""

        self._repository.update_request_coordination(
            correlation_id,
            status=status,
            active_task_id=active_task_id,
        )

    def mark_manager_unavailable(
        self,
        correlation_id: str,
    ) -> RequestRecord | None:
        request = self._repository.get_request(correlation_id)
        self._repository.update_request_status(
            correlation_id,
            "manager_unavailable",
        )
        return request

    def list_for_session(
        self,
        session_key: str,
        *,
        limit: int = 5,
    ) -> list[RequestRecord]:
        return self._repository.list_requests_for_session(
            session_key,
            limit=limit,
        )

    def session_keys_for(self, correlation_ids: tuple[str, ...]) -> frozenset[str]:
        """Sessions that recorded these requests; unrecorded correlations are skipped."""

        keys: set[str] = set()
        for correlation_id in correlation_ids:
            request = self._repository.get_request(correlation_id)
            if request is not None and request.session_key:
                keys.add(request.session_key)
        return frozenset(keys)

    def find_open_request(
        self,
        *,
        session_key: str,
        conversation_id: str | None,
        limit: int = 20,
    ) -> RequestRecord | None:
        for request in self.list_for_session(session_key, limit=limit):
            if (
                conversation_id is not None
                and request.conversation_id != conversation_id
            ):
                continue
            if request.status in TERMINAL_REQUEST_STATUSES:
                continue
            return request
        return None

    def expire_stale_pending_queries(
        self,
        *,
        timeout_seconds: float | None,
    ) -> int:
        if timeout_seconds is None:
            return 0
        cutoff = self._clock.now_utc() - timedelta(seconds=timeout_seconds)
        return self._repository.expire_pending_queries(cutoff_iso=cutoff.isoformat())

    def pending_query_is_expired(
        self,
        pending_query: PendingQueryRecord,
        *,
        timeout_seconds: float | None,
    ) -> bool:
        if timeout_seconds is None:
            return False
        try:
            created_at = self._parse_timestamp(pending_query.created_at)
        except ValueError:
            self._logger.warning(
                "Gateway request service treating pending query as expired. "
                "event=gateway_pending_query_invalid_created_at "
                "task_query_id=%s session_key=%s created_at=%r",
                pending_query.task_query_id,
                pending_query.session_key,
                pending_query.created_at,
            )
            return True
        return self._clock.now_utc() - created_at > timedelta(seconds=timeout_seconds)

    def get_pending_query(
        self,
        session_key: str,
    ) -> PendingQueryRecord | None:
        return self._repository.get_pending_query(session_key)

    def upsert_pending_query(
        self,
        *,
        session_key: str,
        correlation_id: str,
        task_query_id: str,
        parent_task_id: str | None,
        conversation_id: str | None,
        query_text: str,
        query_payload_json: str | None = None,
        status: str = "pending",
        owner_canonical_user_id: str | None = None,
    ) -> None:
        self._repository.upsert_pending_query(
            session_key=session_key,
            correlation_id=correlation_id,
            task_query_id=task_query_id,
            parent_task_id=parent_task_id,
            conversation_id=conversation_id,
            query_text=query_text,
            query_payload_json=query_payload_json,
            status=status,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def clear_pending_query(
        self,
        session_key: str,
        *,
        status: str = "completed",
    ) -> None:
        self._repository.clear_pending_query(
            session_key,
            status=status,
        )

    def transition_with_session_lineage(
        self,
        *,
        correlation_id: str,
        status: str,
        session_key: str,
        conversation_id: str | None,
        last_task_id: str | None,
        manager_agent_id: str | None,
    ) -> None:
        """Atomically update request state and its session task lineage."""

        with self._unit_of_work_factory.request_unit_of_work() as unit_of_work:
            if last_task_id:
                unit_of_work.request.update_request_coordination(
                    correlation_id,
                    status=status,
                    active_task_id=last_task_id,
                )
            else:
                unit_of_work.request.update_request_status(
                    correlation_id,
                    status,
                )
            unit_of_work.session.upsert_session(
                session_key=session_key,
                conversation_id=conversation_id,
                last_task_id=last_task_id,
                manager_agent_id=manager_agent_id,
            )
            unit_of_work.commit()

    @staticmethod
    def _parse_timestamp(raw_timestamp: str) -> datetime:
        timestamp = datetime.fromisoformat(raw_timestamp)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp
