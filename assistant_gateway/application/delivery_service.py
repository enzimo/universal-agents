"""Gateway delivery-state use cases over delivery coordination ports."""

from __future__ import annotations

from assistant_gateway.domain.delivery import (
    AttachmentRecord,
    DeliveryRecord,
    ScheduledTargetRecord,
)
from assistant_gateway.ports.repositories import (
    DeliveryRepository,
    DeliveryUnitOfWorkFactory,
)


class GatewayDeliveryService:
    """Own durable delivery outcomes, scheduling targets, and attachments."""

    def __init__(
        self,
        *,
        repository: DeliveryRepository,
        unit_of_work_factory: DeliveryUnitOfWorkFactory,
    ) -> None:
        self._repository = repository
        self._unit_of_work_factory = unit_of_work_factory

    def get_attachment(self, attachment_id: str) -> AttachmentRecord | None:
        return self._repository.get_attachment(attachment_id)

    def update_attachment_nats_ref(
        self,
        *,
        attachment_id: str,
        nats_bucket: str,
        nats_object_name: str,
    ) -> AttachmentRecord | None:
        return self._repository.update_attachment_nats_ref(
            attachment_id=attachment_id,
            nats_bucket=nats_bucket,
            nats_object_name=nats_object_name,
        )

    def get_due_deliveries(self) -> list[DeliveryRecord]:
        return self._repository.get_due_deliveries()

    def record_immediate_outcome(
        self,
        *,
        correlation_id: str | None,
        session_key: str,
        conversation_id: str | None,
        last_task_id: str | None,
    ) -> None:
        """Atomically record immediate delivery and conversation lineage."""

        with self._unit_of_work_factory.delivery_unit_of_work() as unit_of_work:
            if correlation_id:
                unit_of_work.request.update_request_status(
                    correlation_id,
                    "delivered",
                )
            if conversation_id:
                unit_of_work.session.register_conversation_session(
                    session_key=session_key,
                    conversation_id=conversation_id,
                    last_task_id=last_task_id,
                )
            unit_of_work.commit()

    def defer(
        self,
        *,
        session_key: str,
        origin_kind: str,
        payload: dict[str, object],
        rendered_text: str,
        available_after: str,
        correlation_id: str | None,
        conversation_id: str | None,
        last_task_id: str | None,
        owner_canonical_user_id: str | None = None,
    ) -> str:
        """Atomically enqueue a delivery and update related request state."""

        with self._unit_of_work_factory.delivery_unit_of_work() as unit_of_work:
            delivery_id = unit_of_work.delivery.add_deferred_delivery(
                session_key=session_key,
                origin_kind=origin_kind,
                payload=payload,
                rendered_text=rendered_text,
                available_after=available_after,
                correlation_id=correlation_id,
                owner_canonical_user_id=owner_canonical_user_id,
            )
            if correlation_id:
                unit_of_work.request.update_request_status(
                    correlation_id,
                    "deferred",
                )
            if conversation_id:
                unit_of_work.session.register_conversation_session(
                    session_key=session_key,
                    conversation_id=conversation_id,
                    last_task_id=last_task_id,
                    owner_canonical_user_id=owner_canonical_user_id,
                )
            unit_of_work.commit()
        return delivery_id

    def mark_deferred_delivery_delivered(
        self,
        *,
        delivery_id: str,
        correlation_id: str | None,
    ) -> None:
        """Atomically mark the delivery and its originating request delivered."""

        with self._unit_of_work_factory.delivery_unit_of_work() as unit_of_work:
            unit_of_work.delivery.mark_delivery_delivered(delivery_id)
            if correlation_id:
                unit_of_work.request.update_request_status(
                    correlation_id,
                    "delivered",
                )
            unit_of_work.commit()

    def register_scheduled_request(
        self,
        *,
        scheduled_job_id: str,
        correlation_id: str,
        session_key: str,
        request_kind: str,
        request_text: str,
        conversation_id: str | None,
        followup_to_task_id: str | None,
        owner_canonical_user_id: str | None = None,
    ) -> None:
        """Atomically persist the request and its proactive delivery target."""

        with self._unit_of_work_factory.delivery_unit_of_work() as unit_of_work:
            unit_of_work.request.record_request(
                correlation_id=correlation_id,
                session_key=session_key,
                request_kind=request_kind,
                request_text=request_text,
                conversation_id=conversation_id,
                followup_to_task_id=followup_to_task_id,
                status="scheduled",
                owner_canonical_user_id=owner_canonical_user_id,
            )
            unit_of_work.delivery.upsert_scheduled_target(
                scheduled_job_id=scheduled_job_id,
                session_key=session_key,
                conversation_id=conversation_id,
                owner_canonical_user_id=owner_canonical_user_id,
            )
            unit_of_work.commit()

    def upsert_scheduled_target(
        self,
        *,
        scheduled_job_id: str,
        session_key: str,
        conversation_id: str | None,
        owner_canonical_user_id: str | None = None,
    ) -> None:
        self._repository.upsert_scheduled_target(
            scheduled_job_id=scheduled_job_id,
            session_key=session_key,
            conversation_id=conversation_id,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def get_scheduled_target(
        self,
        scheduled_job_id: str,
    ) -> ScheduledTargetRecord | None:
        return self._repository.get_scheduled_target(scheduled_job_id)

    def get_scheduled_target_by_conversation_id(
        self,
        conversation_id: str,
    ) -> ScheduledTargetRecord | None:
        return self._repository.get_scheduled_target_by_conversation_id(conversation_id)

    def remove_scheduled_target(self, scheduled_job_id: str) -> bool:
        return self._repository.remove_scheduled_target(scheduled_job_id)
