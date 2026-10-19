"""Deferred delivery release and receipt publication application."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from assistant_gateway.application.delivery_service import GatewayDeliveryService
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.ports import GatewayClock, GatewayEventOutput
from assistant_gateway.delivery import summarize_delivery_batch
from assistant_gateway.delivery_receipts import (
    build_delivery_receipt_envelope,
    is_receiptable_delivery,
)
from assistant_gateway.domain.delivery import DeliveryRecord
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.ports.repositories import SessionRepository
from custom_messages.contracts.envelope_v1 import MessageEnvelope


@dataclass(frozen=True)
class GatewayDeferredDeliverySettings:
    """Stable gateway identity and polling settings for deferred delivery."""

    instance_name: str
    response_route: str
    poll_interval_seconds: float = 1.0


class GatewayDeferredDeliveryApplication:
    """Own digest release, durable outcomes, receipts, and polling lifecycle."""

    def __init__(
        self,
        *,
        settings: GatewayDeferredDeliverySettings,
        delivery: GatewayDeliveryService,
        sessions: SessionRepository,
        manager: GatewayManagerClient,
        event_output: GatewayEventOutput,
        clock: GatewayClock,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._delivery = delivery
        self._sessions = sessions
        self._manager = manager
        self._event_output = event_output
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

    async def release_due(self) -> list[str]:
        """Release every due delivery, grouped into one digest per session.

        Storage calls run on a worker thread: this runs every poll interval on
        the gateway's event loop, which also keeps chat connections alive (the
        Discord heartbeat), and a database locked by another process would
        otherwise stop that loop for as long as the lock wait lasts.
        """

        due = await asyncio.to_thread(self._delivery.get_due_deliveries)
        grouped = self._group_due_deliveries(due)
        rendered_batches: list[str] = []
        for session_key, items in grouped.items():
            digest = summarize_delivery_batch([item.rendered_text for item in items])
            rendered_batches.append(digest)
            session = await asyncio.to_thread(self._sessions.get_session, session_key)
            await self._event_output.emit(self._delivery_event(session_key, digest, session))
            delivered_at = self._clock.now_utc().isoformat()
            for item in items:
                await asyncio.to_thread(
                    self._delivery.mark_deferred_delivery_delivered,
                    delivery_id=item.delivery_id,
                    correlation_id=item.correlation_id,
                )
                envelope = self._restore_envelope(item)
                if envelope is not None:
                    await self.send_receipt(
                        envelope=envelope,
                        delivery_state="delivered",
                        delivery_via="digest",
                        delivered_at=delivered_at,
                    )
        return rendered_batches

    async def send_receipt(
        self,
        *,
        envelope: MessageEnvelope,
        delivery_state: str,
        delivery_via: str,
        delivered_at: str,
    ) -> None:
        """Queue the receipt for the lead, including while discovery is pending."""

        if not is_receiptable_delivery(envelope):
            return
        # The team route survives lead replacement and needs no live presence.
        # JetStream retains the receipt until a lead can consume it.
        manager_id = self._manager.settings.team_id
        receipt_envelope = build_delivery_receipt_envelope(
            envelope=envelope,
            instance_name=self._settings.instance_name,
            response_queue_name=self._settings.response_route,
            manager_agent_id=manager_id,
            delivery_state=delivery_state,
            delivery_via=delivery_via,
            delivered_at=delivered_at,
        )
        if receipt_envelope is not None:
            await self._manager.publish_durable(
                receipt_envelope,
                routing_key=manager_id,
            )

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll deferred state until the gateway lifecycle requests shutdown."""

        while not stop_event.is_set():
            try:
                await self.release_due()
                await asyncio.sleep(self._settings.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning(
                    "Gateway deferred-delivery loop failed. "
                    "event=gateway_deferred_delivery_loop_failed error=%s",
                    exc,
                )
                await asyncio.sleep(self._settings.poll_interval_seconds)

    @staticmethod
    def _group_due_deliveries(
        due: list[DeliveryRecord],
    ) -> dict[str, list[DeliveryRecord]]:
        grouped: dict[str, list[DeliveryRecord]] = {}
        for item in due:
            grouped.setdefault(item.session_key, []).append(item)
        return grouped

    @staticmethod
    def _delivery_event(
        session_key: str,
        digest: str,
        session: SessionRecord | None,
    ) -> GatewayDeliveryEvent:
        return GatewayDeliveryEvent(
            text=f"[Deferred Delivery:{session_key}]\n{digest}",
            session_key=session_key,
            channel_name=session.channel_name if session else None,
            user_id=session.user_id if session else None,
            chat_id=session.chat_id if session else None,
            thread_id=session.thread_id if session else None,
            conversation_id=session.conversation_id if session else None,
            scheduled_job_id=None,
            correlation_id=None,
            original_message_id=None,
        )

    def _restore_envelope(self, item: DeliveryRecord) -> MessageEnvelope | None:
        try:
            return MessageEnvelope.model_validate_json(item.payload_json)
        except Exception as exc:
            self._logger.warning(
                "Gateway failed to parse deferred payload for delivery receipt. "
                "event=gateway_deferred_delivery_payload_invalid "
                "delivery_id=%s error=%s",
                item.delivery_id,
                exc,
            )
            return None
