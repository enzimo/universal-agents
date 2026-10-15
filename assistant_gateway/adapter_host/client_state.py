"""Durable client-side idempotency ledger for external platform deliveries."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from storage.sqlite_connection import connect_sqlite


@dataclass(frozen=True)
class AdapterClientRegistrationState:
    plugin_id: str
    adapter_instance_id: str
    registration_epoch: str | None
    inbound_committed_position: int
    outbound_received_position: int
    outbound_completed_position: int
    updated_at_utc: str


@dataclass(frozen=True)
class AdapterClientDeliveryRecord:
    event_id: str
    plugin_id: str
    adapter_instance_id: str
    stream_position: int
    payload_json: str
    status: str
    outcome: str | None
    provider_message_id: str | None
    error_code: str | None
    detail: str | None
    receipt_acknowledged: bool
    completion_acknowledged: bool
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True)
class AdapterClientInboundRecord:
    request_id: str
    idempotency_key: str
    plugin_id: str
    adapter_instance_id: str
    stream_position: int
    payload_json: str
    status: str
    correlation_id: str | None
    created_at_utc: str
    updated_at_utc: str


class AdapterClientStateConflict(RuntimeError):
    def __init__(
        self,
        error_code: str,
        detail: str,
        *,
        expected_stream_position: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail
        self.expected_stream_position = expected_stream_position


class AdapterHostClientStateStore:
    """SQLite ledger committed before and after every external side effect."""

    def __init__(
        self, db_path: str | Path, logger: logging.Logger | None = None
    ) -> None:
        self._db_path = str(Path(db_path).expanduser().resolve())
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(
            self._db_path, logger=self._logger, row_factory=sqlite3.Row
        )

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS adapter_host_client_registrations (
                    plugin_id TEXT NOT NULL,
                    adapter_instance_id TEXT NOT NULL,
                    registration_epoch TEXT,
                    inbound_committed_position INTEGER NOT NULL DEFAULT 0,
                    outbound_received_position INTEGER NOT NULL DEFAULT 0,
                    outbound_completed_position INTEGER NOT NULL DEFAULT 0,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (plugin_id, adapter_instance_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS adapter_host_client_inbox (
                    request_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    plugin_id TEXT NOT NULL,
                    adapter_instance_id TEXT NOT NULL,
                    stream_position INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    correlation_id TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    UNIQUE (plugin_id, adapter_instance_id, idempotency_key),
                    UNIQUE (plugin_id, adapter_instance_id, stream_position)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS adapter_host_client_deliveries (
                    event_id TEXT PRIMARY KEY,
                    plugin_id TEXT NOT NULL,
                    adapter_instance_id TEXT NOT NULL,
                    stream_position INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome TEXT,
                    provider_message_id TEXT,
                    error_code TEXT,
                    detail TEXT,
                    receipt_acknowledged INTEGER NOT NULL DEFAULT 0,
                    completion_acknowledged INTEGER NOT NULL DEFAULT 0,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    UNIQUE (plugin_id, adapter_instance_id, stream_position)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_adapter_host_client_delivery_recovery
                ON adapter_host_client_deliveries(
                    plugin_id, adapter_instance_id, status, stream_position
                )
                """
            )
            conn.commit()

    @staticmethod
    def _registration(
        row: sqlite3.Row | None,
    ) -> AdapterClientRegistrationState | None:
        if row is None:
            return None
        return AdapterClientRegistrationState(**dict(row))

    @staticmethod
    def _delivery(row: sqlite3.Row) -> AdapterClientDeliveryRecord:
        data = dict(row)
        data["receipt_acknowledged"] = bool(data["receipt_acknowledged"])
        data["completion_acknowledged"] = bool(data["completion_acknowledged"])
        return AdapterClientDeliveryRecord(**data)

    @staticmethod
    def _inbound(row: sqlite3.Row) -> AdapterClientInboundRecord:
        return AdapterClientInboundRecord(**dict(row))

    def registration_state(
        self, plugin_id: str, adapter_instance_id: str
    ) -> AdapterClientRegistrationState | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM adapter_host_client_registrations
                WHERE plugin_id = ? AND adapter_instance_id = ?
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
        return self._registration(row)

    def save_registration(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        registration_epoch: str,
        inbound_committed_position: int,
        outbound_received_position: int,
        outbound_completed_position: int,
        updated_at_utc: str,
    ) -> AdapterClientRegistrationState:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO adapter_host_client_registrations (
                    plugin_id, adapter_instance_id, registration_epoch,
                    inbound_committed_position, outbound_received_position,
                    outbound_completed_position, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (plugin_id, adapter_instance_id) DO UPDATE SET
                    registration_epoch = excluded.registration_epoch,
                    inbound_committed_position = excluded.inbound_committed_position,
                    outbound_received_position = excluded.outbound_received_position,
                    outbound_completed_position = excluded.outbound_completed_position,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    plugin_id,
                    adapter_instance_id,
                    registration_epoch,
                    inbound_committed_position,
                    outbound_received_position,
                    outbound_completed_position,
                    updated_at_utc,
                ),
            )
            conn.commit()
        state = self.registration_state(plugin_id, adapter_instance_id)
        if state is None:  # pragma: no cover - schema invariant
            raise RuntimeError("adapter registration state disappeared")
        return state

    def reset_epoch_acknowledgements(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        updated_at_utc: str,
    ) -> None:
        """Require the new epoch to re-establish receipt before pending completion."""

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE adapter_host_client_deliveries
                SET receipt_acknowledged = 0, updated_at_utc = ?
                WHERE plugin_id = ? AND adapter_instance_id = ?
                  AND completion_acknowledged = 0
                """,
                (updated_at_utc, plugin_id, adapter_instance_id),
            )
            conn.commit()

    def queue_inbound(
        self,
        *,
        request_id: str,
        idempotency_key: str,
        plugin_id: str,
        adapter_instance_id: str,
        registration_epoch: str,
        payload: Mapping[str, object],
        updated_at_utc: str,
    ) -> AdapterClientInboundRecord:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                """
                SELECT * FROM adapter_host_client_inbox
                WHERE plugin_id = ? AND adapter_instance_id = ?
                  AND idempotency_key = ?
                """,
                (plugin_id, adapter_instance_id, idempotency_key),
            ).fetchone()
            if duplicate is not None:
                conn.commit()
                return self._inbound(duplicate)
            row = conn.execute(
                """
                SELECT * FROM adapter_host_client_registrations
                WHERE plugin_id = ? AND adapter_instance_id = ?
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
            if row is None or row["registration_epoch"] != registration_epoch:
                raise AdapterClientStateConflict(
                    "stale_registration_epoch",
                    "client registration epoch is missing or stale",
                )
            assigned = conn.execute(
                """
                SELECT MAX(stream_position) AS max_position
                FROM adapter_host_client_inbox
                WHERE plugin_id = ? AND adapter_instance_id = ?
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
            position = (
                max(
                    int(row["inbound_committed_position"]),
                    int(assigned["max_position"] or 0),
                )
                + 1
            )
            conn.execute(
                """
                INSERT INTO adapter_host_client_inbox (
                    request_id, idempotency_key, plugin_id, adapter_instance_id,
                    stream_position, payload_json, status, correlation_id,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                """,
                (
                    request_id,
                    idempotency_key,
                    plugin_id,
                    adapter_instance_id,
                    position,
                    json.dumps(payload, sort_keys=True),
                    updated_at_utc,
                    updated_at_utc,
                ),
            )
            inserted = conn.execute(
                "SELECT * FROM adapter_host_client_inbox WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            conn.commit()
        if inserted is None:  # pragma: no cover - schema invariant
            raise RuntimeError("queued adapter inbound record disappeared")
        return self._inbound(inserted)

    def mark_inbound_accepted(
        self,
        request_id: str,
        *,
        correlation_id: str | None,
        updated_at_utc: str,
    ) -> AdapterClientInboundRecord:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM adapter_host_client_inbox WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"Unknown adapter client inbound request {request_id!r}."
                )
            registration = conn.execute(
                """
                SELECT * FROM adapter_host_client_registrations
                WHERE plugin_id = ? AND adapter_instance_id = ?
                """,
                (row["plugin_id"], row["adapter_instance_id"]),
            ).fetchone()
            expected = int(registration["inbound_committed_position"]) + 1
            if int(row["stream_position"]) != expected:
                raise AdapterClientStateConflict(
                    "inbound_completion_gap",
                    f"expected accepted inbound position {expected}",
                    expected_stream_position=expected,
                )
            conn.execute(
                """
                UPDATE adapter_host_client_inbox
                SET status = 'accepted', correlation_id = ?, updated_at_utc = ?
                WHERE request_id = ?
                """,
                (correlation_id, updated_at_utc, request_id),
            )
            conn.execute(
                """
                UPDATE adapter_host_client_registrations
                SET inbound_committed_position = ?, updated_at_utc = ?
                WHERE plugin_id = ? AND adapter_instance_id = ?
                """,
                (
                    int(row["stream_position"]),
                    updated_at_utc,
                    row["plugin_id"],
                    row["adapter_instance_id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM adapter_host_client_inbox WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            conn.commit()
        if updated is None:  # pragma: no cover - schema invariant
            raise RuntimeError("accepted adapter inbound record disappeared")
        return self._inbound(updated)

    def reconcile_inbound_cursor(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        gateway_committed_position: int,
        updated_at_utc: str,
    ) -> None:
        """Accept the gateway's durable cursor after an admission-response crash."""

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                """
                SELECT inbound_committed_position
                FROM adapter_host_client_registrations
                WHERE plugin_id = ? AND adapter_instance_id = ?
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
            if state is None:
                raise KeyError(f"Unknown adapter client registration {plugin_id!r}.")
            local = int(state["inbound_committed_position"])
            if gateway_committed_position < local:
                raise AdapterClientStateConflict(
                    "gateway_cursor_regressed",
                    "gateway inbound cursor regressed behind client durable state",
                )
            conn.execute(
                """
                UPDATE adapter_host_client_inbox
                SET status = 'accepted', updated_at_utc = ?
                WHERE plugin_id = ? AND adapter_instance_id = ?
                  AND stream_position <= ? AND status = 'pending'
                """,
                (
                    updated_at_utc,
                    plugin_id,
                    adapter_instance_id,
                    gateway_committed_position,
                ),
            )
            conn.execute(
                """
                UPDATE adapter_host_client_registrations
                SET inbound_committed_position = ?, updated_at_utc = ?
                WHERE plugin_id = ? AND adapter_instance_id = ?
                """,
                (
                    gateway_committed_position,
                    updated_at_utc,
                    plugin_id,
                    adapter_instance_id,
                ),
            )
            conn.commit()

    def pending_inbound(
        self, *, plugin_id: str, adapter_instance_id: str
    ) -> list[AdapterClientInboundRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM adapter_host_client_inbox
                WHERE plugin_id = ? AND adapter_instance_id = ?
                  AND status = 'pending'
                ORDER BY stream_position
                """,
                (plugin_id, adapter_instance_id),
            ).fetchall()
        return [self._inbound(row) for row in rows]

    def receive_delivery(
        self,
        *,
        event_id: str,
        plugin_id: str,
        adapter_instance_id: str,
        registration_epoch: str,
        stream_position: int,
        payload: Mapping[str, object],
        observed_at_utc: str,
    ) -> AdapterClientDeliveryRecord:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM adapter_host_client_deliveries WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return self._delivery(existing)
            registration = conn.execute(
                """
                SELECT * FROM adapter_host_client_registrations
                WHERE plugin_id = ? AND adapter_instance_id = ?
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
            if (
                registration is None
                or registration["registration_epoch"] != registration_epoch
            ):
                raise AdapterClientStateConflict(
                    "stale_registration_epoch", "delivery uses a stale client epoch"
                )
            expected = int(registration["outbound_received_position"]) + 1
            if stream_position != expected:
                raise AdapterClientStateConflict(
                    "outbound_sequence_gap",
                    f"expected outbound stream position {expected}",
                    expected_stream_position=expected,
                )
            conn.execute(
                """
                INSERT INTO adapter_host_client_deliveries (
                    event_id, plugin_id, adapter_instance_id, stream_position,
                    payload_json, status, outcome, provider_message_id,
                    error_code, detail, receipt_acknowledged,
                    completion_acknowledged, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, 'received', NULL, NULL, NULL, NULL, 0, 0, ?, ?)
                """,
                (
                    event_id,
                    plugin_id,
                    adapter_instance_id,
                    stream_position,
                    json.dumps(payload, sort_keys=True),
                    observed_at_utc,
                    observed_at_utc,
                ),
            )
            conn.execute(
                """
                UPDATE adapter_host_client_registrations
                SET outbound_received_position = ?, updated_at_utc = ?
                WHERE plugin_id = ? AND adapter_instance_id = ?
                """,
                (
                    stream_position,
                    observed_at_utc,
                    plugin_id,
                    adapter_instance_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM adapter_host_client_deliveries WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            conn.commit()
        if row is None:  # pragma: no cover - schema invariant
            raise RuntimeError("received adapter delivery record disappeared")
        return self._delivery(row)

    def mark_receipt_acknowledged(self, event_id: str, *, observed_at_utc: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE adapter_host_client_deliveries
                SET receipt_acknowledged = 1, updated_at_utc = ?
                WHERE event_id = ?
                """,
                (observed_at_utc, event_id),
            )
            conn.commit()

    def begin_delivery(self, event_id: str, *, observed_at_utc: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE adapter_host_client_deliveries
                SET status = 'delivering', updated_at_utc = ?
                WHERE event_id = ? AND status = 'received'
                """,
                (observed_at_utc, event_id),
            )
            conn.commit()
        return cursor.rowcount == 1

    def complete_delivery(
        self,
        event_id: str,
        *,
        outcome: str,
        provider_message_id: str | None,
        error_code: str | None,
        detail: str | None,
        observed_at_utc: str,
    ) -> AdapterClientDeliveryRecord:
        if outcome not in {"delivered", "failed", "outcome_unknown"}:
            raise ValueError(f"Unsupported adapter delivery outcome {outcome!r}.")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM adapter_host_client_deliveries WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown adapter client delivery {event_id!r}.")
            if row["status"] not in {"delivering", outcome}:
                raise AdapterClientStateConflict(
                    "delivery_not_in_progress",
                    "delivery must be durably marked in progress before completion",
                )
            conn.execute(
                """
                UPDATE adapter_host_client_deliveries
                SET status = ?, outcome = ?, provider_message_id = ?,
                    error_code = ?, detail = ?, updated_at_utc = ?
                WHERE event_id = ?
                """,
                (
                    outcome,
                    outcome,
                    provider_message_id,
                    error_code,
                    detail,
                    observed_at_utc,
                    event_id,
                ),
            )
            registration = conn.execute(
                """
                SELECT * FROM adapter_host_client_registrations
                WHERE plugin_id = ? AND adapter_instance_id = ?
                """,
                (row["plugin_id"], row["adapter_instance_id"]),
            ).fetchone()
            expected = int(registration["outbound_completed_position"]) + 1
            if int(row["stream_position"]) != expected:
                raise AdapterClientStateConflict(
                    "outbound_completion_gap",
                    f"expected completion for stream position {expected}",
                    expected_stream_position=expected,
                )
            conn.execute(
                """
                UPDATE adapter_host_client_registrations
                SET outbound_completed_position = ?, updated_at_utc = ?
                WHERE plugin_id = ? AND adapter_instance_id = ?
                """,
                (
                    int(row["stream_position"]),
                    observed_at_utc,
                    row["plugin_id"],
                    row["adapter_instance_id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM adapter_host_client_deliveries WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            conn.commit()
        if updated is None:  # pragma: no cover - schema invariant
            raise RuntimeError("completed adapter delivery record disappeared")
        return self._delivery(updated)

    def mark_completion_acknowledged(
        self, event_id: str, *, observed_at_utc: str
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE adapter_host_client_deliveries
                SET completion_acknowledged = 1, updated_at_utc = ?
                WHERE event_id = ? AND outcome IS NOT NULL
                """,
                (observed_at_utc, event_id),
            )
            conn.commit()

    def recover_interrupted_deliveries(self, *, observed_at_utc: str) -> int:
        """Mark ambiguous platform calls unknown instead of retrying blindly."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id FROM adapter_host_client_deliveries
                WHERE status = 'delivering'
                ORDER BY plugin_id, adapter_instance_id, stream_position
                """
            ).fetchall()
        recovered = 0
        for row in rows:
            self.complete_delivery(
                str(row["event_id"]),
                outcome="outcome_unknown",
                provider_message_id=None,
                error_code="external_delivery_outcome_unknown",
                detail=(
                    "The adapter host stopped after external delivery began; "
                    "the side effect was not retried."
                ),
                observed_at_utc=observed_at_utc,
            )
            recovered += 1
        return recovered

    def pending_acknowledgements(
        self, *, plugin_id: str, adapter_instance_id: str
    ) -> list[AdapterClientDeliveryRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM adapter_host_client_deliveries
                WHERE plugin_id = ? AND adapter_instance_id = ?
                  AND (receipt_acknowledged = 0
                       OR (outcome IS NOT NULL AND completion_acknowledged = 0))
                ORDER BY stream_position
                """,
                (plugin_id, adapter_instance_id),
            ).fetchall()
        return [self._delivery(row) for row in rows]

    def pending_deliveries(
        self, *, plugin_id: str, adapter_instance_id: str
    ) -> list[AdapterClientDeliveryRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM adapter_host_client_deliveries
                WHERE plugin_id = ? AND adapter_instance_id = ?
                  AND status = 'received'
                ORDER BY stream_position
                """,
                (plugin_id, adapter_instance_id),
            ).fetchall()
        return [self._delivery(row) for row in rows]


__all__ = [
    "AdapterClientDeliveryRecord",
    "AdapterClientInboundRecord",
    "AdapterClientRegistrationState",
    "AdapterClientStateConflict",
    "AdapterHostClientStateStore",
]
