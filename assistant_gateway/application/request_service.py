"""Gateway request-lifecycle use cases over request and session ports."""

from __future__ import annotations

import logging

from assistant_gateway.domain.request_state import (
    GUIDANCE_STATUS_HELD,
    REQUEST_KIND_TASK_STEERING,
    HeldGuidanceRecord,
    RequestRecord,
)
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
    """Own request persistence and status transitions."""

    def __init__(
        self,
        *,
        repository: RequestRepository,
        unit_of_work_factory: RequestUnitOfWorkFactory,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        self._unit_of_work_factory = unit_of_work_factory
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
        active_task_id: str | None = None,
        run_state: str | None = None,
    ) -> None:
        """Record a request row; ``run_state="running"`` may raise ``ThreadRequestRunning``."""

        existing = self._repository.get_request(correlation_id)
        if existing is not None and run_state is None and not existing.matches_task(followup_to_task_id):
            # Question and secure-form answers retain the transport
            # correlation. A sibling schedule's continuation must not
            # replace the original request's status, text or conversation.
            self._logger.debug(
                "A continuation keeps its own task. event=gateway_sibling_continuation "
                "correlation_id=%s request_task_id=%s reply_task_id=%s",
                correlation_id, existing.active_task_id, followup_to_task_id,
            )
            return
        self._repository.record_request(
            correlation_id=correlation_id,
            session_key=session_key,
            request_kind=request_kind,
            request_text=request_text,
            conversation_id=conversation_id,
            followup_to_task_id=followup_to_task_id,
            status=status,
            owner_canonical_user_id=owner_canonical_user_id,
            active_task_id=active_task_id,
            run_state=run_state,
        )

    def get(self, correlation_id: str) -> RequestRecord | None:
        return self._repository.get_request(correlation_id)

    def update_status(self, correlation_id: str, status: str) -> None:
        self._repository.update_request_status(correlation_id, status)

    def update_task_status(self, correlation_id: str, status: str, *, task_id: str | None) -> None:
        """A task's status changes only its own tracked request, including when no id was supplied."""

        record = self._repository.get_request(correlation_id)
        if record is not None and not record.matches_task(task_id):
            return
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

    def running_request(
        self,
        *,
        session_key: str,
        conversation_id: str | None,
    ) -> RequestRecord | None:
        """The request this thread runs now, read from durable state only."""

        return self._repository.get_running_request(
            session_key=session_key,
            conversation_id=conversation_id,
        )

    def finish(self, correlation_id: str) -> bool:
        """The thread's request ended; False when it was not running (repeat or other row)."""

        return self._repository.finish_request(correlation_id)

    def record_guidance(
        self,
        *,
        correlation_id: str,
        session_key: str,
        conversation_id: str | None,
        request_text: str,
        target: RequestRecord,
        held: HeldGuidanceRecord,
    ) -> None:
        """Record a guidance message and hold it for its request, in one transaction."""

        with self._unit_of_work_factory.request_unit_of_work() as unit_of_work:
            unit_of_work.request.record_request(
                correlation_id=correlation_id,
                session_key=session_key,
                request_kind=REQUEST_KIND_TASK_STEERING,
                request_text=request_text,
                conversation_id=conversation_id,
                followup_to_task_id=target.active_task_id,
                status=GUIDANCE_STATUS_HELD,
            )
            unit_of_work.request.hold_guidance(held)
            unit_of_work.commit()

    def held_guidance(self) -> list[HeldGuidanceRecord]:
        return self._repository.list_held_guidance()

    def get_held_guidance(self, correlation_id: str) -> HeldGuidanceRecord | None:
        return self._repository.get_held_guidance(correlation_id)

    def release_guidance(self, correlation_id: str, *, status: str) -> bool:
        """Stop holding guidance with its outcome; only the first caller gets True."""

        return self._repository.release_held_guidance(correlation_id, status=status)

    def transition_with_session_lineage(
        self,
        *,
        correlation_id: str,
        status: str,
        session_key: str,
        conversation_id: str | None,
        last_task_id: str | None,
        manager_agent_id: str | None,
        finished_task_id: str | None = None,
    ) -> None:
        """Atomically update request state and its session task lineage.

        The reply belongs to the session the request was asked in, which is
        not always the current chat session. Save lineage without changing
        the selected session. A final reply also ends the running request.
        """

        with self._unit_of_work_factory.request_unit_of_work() as unit_of_work:
            record = unit_of_work.request.get_request(correlation_id)
            if record is not None and not record.matches_task(finished_task_id or last_task_id):
                # A separate schedule conversation can keep its own history,
                # but cannot change the original thread's current task.
                if conversation_id and conversation_id != record.conversation_id:
                    unit_of_work.session.register_conversation_session(
                        session_key=session_key, conversation_id=conversation_id, last_task_id=last_task_id
                    )
                unit_of_work.commit()
                return
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
            unit_of_work.session.upsert_session_without_switching_chat(
                session_key=session_key,
                conversation_id=conversation_id,
                last_task_id=last_task_id,
                manager_agent_id=manager_agent_id,
            )
            if finished_task_id is not None:
                unit_of_work.request.finish_request(correlation_id)
            unit_of_work.commit()
