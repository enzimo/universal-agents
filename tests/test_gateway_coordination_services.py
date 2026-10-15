from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from assistant_gateway.application.delivery_service import GatewayDeliveryService
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.store import GatewayStateStore


class _FixedClock:
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _services(
    tmp_path: Path,
) -> tuple[
    GatewayStateStore,
    GatewayRequestService,
    GatewayDeliveryService,
]:
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    requests = GatewayRequestService(
        repository=store.request_repository,
        unit_of_work_factory=store,
        clock=_FixedClock(),
    )
    deliveries = GatewayDeliveryService(
        repository=store.delivery_repository,
        unit_of_work_factory=store,
    )
    return store, requests, deliveries


def _create_session_and_request(
    store: GatewayStateStore,
    requests: GatewayRequestService,
) -> None:
    store.session_repository.create_session(
        session_key="session_1",
        conversation_id="conversation_1",
        manager_agent_id="manager_1",
    )
    requests.record(
        correlation_id="correlation_1",
        session_key="session_1",
        request_kind="interactive",
        request_text="hello",
        conversation_id="conversation_1",
        followup_to_task_id=None,
    )


def test_request_service_rolls_back_status_when_lineage_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, requests, _deliveries = _services(tmp_path)
    _create_session_and_request(store, requests)

    def fail_session_write(**_kwargs: object) -> None:
        raise RuntimeError("session lineage failed")

    monkeypatch.setattr(
        store._repository,  # noqa: SLF001 - storage failure injection
        "upsert_session",
        fail_session_write,
    )

    with pytest.raises(RuntimeError, match="session lineage failed"):
        requests.transition_with_session_lineage(
            correlation_id="correlation_1",
            status="completed",
            session_key="session_1",
            conversation_id="conversation_2",
            last_task_id="task_1",
            manager_agent_id="manager_1",
        )

    request = store.request_repository.get_request("correlation_1")
    session = store.session_repository.get_session("session_1")
    assert request is not None and request.status == "pending"
    assert session is not None and session.conversation_id == "conversation_1"


def test_delivery_service_rolls_back_defer_when_request_update_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, requests, deliveries = _services(tmp_path)
    _create_session_and_request(store, requests)

    def fail_status(_correlation_id: str, _status: str) -> None:
        raise RuntimeError("request status failed")

    monkeypatch.setattr(
        store._repository,  # noqa: SLF001 - storage failure injection
        "update_request_status",
        fail_status,
    )

    with pytest.raises(RuntimeError, match="request status failed"):
        deliveries.defer(
            session_key="session_1",
            origin_kind="autonomous",
            payload={"kind": "notice"},
            rendered_text="later",
            available_after="2026-07-27T12:00:00+00:00",
            correlation_id="correlation_1",
            conversation_id=None,
            last_task_id=None,
        )

    request = store.request_repository.get_request("correlation_1")
    assert request is not None and request.status == "pending"
    assert store.delivery_repository.list_pending_deliveries() == []


def test_request_service_maps_waiting_requests_to_their_sessions(
    tmp_path: Path,
) -> None:
    store, requests, _deliveries = _services(tmp_path)
    _create_session_and_request(store, requests)

    # Correlations without a request row (discovery, control) are skipped.
    assert requests.session_keys_for(("correlation_1", "discovery_1")) == frozenset(
        {"session_1"}
    )
    assert requests.session_keys_for(()) == frozenset()
