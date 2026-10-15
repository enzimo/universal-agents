"""SQLite/Postgres persistence for the authenticated adapter-host boundary."""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import logging
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, TypeVar

from assistant_gateway.adapter_host.domain import (
    AdapterHostHealthSnapshot,
    AdapterInboxRecord,
    AdapterOutboxRecord,
    AdapterRegistrationRecord,
    AdapterStreamRecord,
    AdapterUploadRecord,
)
from assistant_gateway.adapter_host.repository import AdapterHostRepository
from storage.backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from storage.config import StorageSettings
from storage.postgres_connection import (
    PostgresConnection,
    connect_postgres,
    ensure_storage_parent_dir,
    validate_postgres_connection,
)
from storage.runtime import describe_storage_capabilities, require_repository_backend
from storage.sqlite_connection import connect_sqlite


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AdapterHostRepositoryConflict(RuntimeError):
    """Stable storage-level conflict that the application maps to protocol errors."""

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


_RecordT = TypeVar("_RecordT")


def _record(record_type: type[_RecordT], row: Any | None) -> _RecordT | None:
    return record_type(**dict(row)) if row is not None else None


def _required_record(record_type: type[_RecordT], row: Any | None) -> _RecordT:
    record = _record(record_type, row)
    if record is None:  # pragma: no cover - repository write invariant
        raise RuntimeError(f"{record_type.__name__} disappeared after persistence")
    return record


class _RelationalAdapterHostRepository(AdapterHostRepository):
    """One-writer durable ledger for registrations, requests, and deliveries."""

    _dialect: SqlDialect


    @staticmethod
    def _begin(conn: sqlite3.Connection | PostgresConnection) -> None:
        conn.begin_write()

    def _init_schema(self) -> None:
        ensure_storage_parent_dir(self._db_path)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_adapter_registrations (
                    authenticated_host_id TEXT NOT NULL,
                    plugin_id TEXT NOT NULL,
                    adapter_instance_id TEXT NOT NULL,
                    registration_id TEXT NOT NULL,
                    registration_epoch TEXT NOT NULL,
                    configuration_digest TEXT NOT NULL,
                    plugin_api_version TEXT NOT NULL,
                    implementation_version TEXT NOT NULL,
                    channel_names_json TEXT NOT NULL,
                    offered_capabilities_json TEXT NOT NULL,
                    negotiated_capabilities_json TEXT NOT NULL,
                    negotiated_limits_json TEXT NOT NULL,
                    lease_expires_at_utc TEXT NOT NULL,
                    last_heartbeat_at_utc TEXT NOT NULL,
                    state TEXT NOT NULL,
                    last_inbound_position INTEGER NOT NULL DEFAULT 0,
                    last_outbound_received_position INTEGER NOT NULL DEFAULT 0,
                    last_outbound_completed_position INTEGER NOT NULL DEFAULT 0,
                    reconnect_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    detail TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (plugin_id, adapter_instance_id),
                    UNIQUE (registration_epoch)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_adapter_inbox (
                    authenticated_host_id TEXT NOT NULL,
                    plugin_id TEXT NOT NULL,
                    adapter_instance_id TEXT NOT NULL,
                    registration_epoch TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    stream_position INTEGER NOT NULL,
                    message_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    admission_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    detail TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (
                        authenticated_host_id, plugin_id, adapter_instance_id,
                        registration_epoch, idempotency_key
                    ),
                    UNIQUE (
                        authenticated_host_id, plugin_id, adapter_instance_id,
                        registration_epoch, stream_position
                    )
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_adapter_inbox_recovery
                ON gateway_adapter_inbox(status, created_at_utc)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_adapter_streams (
                    plugin_id TEXT NOT NULL,
                    adapter_instance_id TEXT NOT NULL,
                    next_outbound_position INTEGER NOT NULL,
                    retained_from_position INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (plugin_id, adapter_instance_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_adapter_outbox (
                    event_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    plugin_id TEXT NOT NULL,
                    adapter_instance_id TEXT NOT NULL,
                    stream_position INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    received_epoch TEXT,
                    completed_epoch TEXT,
                    outcome TEXT,
                    provider_message_id TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    detail TEXT,
                    created_at_utc TEXT NOT NULL,
                    received_at_utc TEXT,
                    completed_at_utc TEXT,
                    updated_at_utc TEXT NOT NULL,
                    UNIQUE (plugin_id, adapter_instance_id, idempotency_key),
                    UNIQUE (plugin_id, adapter_instance_id, stream_position)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_adapter_outbox_delivery
                ON gateway_adapter_outbox(
                    plugin_id, adapter_instance_id, status, stream_position
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_adapter_uploads (
                    upload_id TEXT PRIMARY KEY,
                    upload_request_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    authenticated_host_id TEXT NOT NULL,
                    plugin_id TEXT NOT NULL,
                    adapter_instance_id TEXT NOT NULL,
                    registration_epoch TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_reference TEXT NOT NULL UNIQUE,
                    local_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at_utc TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    completed_at_utc TEXT,
                    UNIQUE (
                        authenticated_host_id, plugin_id, adapter_instance_id,
                        registration_epoch, idempotency_key
                    )
                )
                """
            )
            conn.commit()

    def get_registration(
        self, plugin_id: str, adapter_instance_id: str
    ) -> AdapterRegistrationRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_registrations
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
        return _record(AdapterRegistrationRecord, row)

    def save_registration(self, record: AdapterRegistrationRecord) -> None:
        values = asdict(record)
        columns = tuple(values)
        assignments = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"plugin_id", "adapter_instance_id", "created_at_utc"}
        )
        placeholders = ", ".join(f"{self._dialect.bind}" for _ in columns)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_adapter_registrations ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT (plugin_id, adapter_instance_id) DO UPDATE SET
                    {assignments}
                """,
                tuple(values[column] for column in columns),
            )
            conn.commit()

    def get_inbox_by_idempotency(
        self,
        *,
        authenticated_host_id: str,
        plugin_id: str,
        adapter_instance_id: str,
        registration_epoch: str,
        idempotency_key: str,
    ) -> AdapterInboxRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_inbox
                WHERE authenticated_host_id = {self._dialect.bind} AND plugin_id = {self._dialect.bind}
                  AND adapter_instance_id = {self._dialect.bind} AND registration_epoch = {self._dialect.bind}
                  AND idempotency_key = {self._dialect.bind}
                """,
                (
                    authenticated_host_id,
                    plugin_id,
                    adapter_instance_id,
                    registration_epoch,
                    idempotency_key,
                ),
            ).fetchone()
        return _record(AdapterInboxRecord, row)

    def admit_inbox(
        self,
        record: AdapterInboxRecord,
        *,
        expected_previous_position: int,
    ) -> AdapterInboxRecord:
        with self._lock, self._connect() as conn:
            self._begin(conn)
            duplicate = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_inbox
                WHERE authenticated_host_id = {self._dialect.bind} AND plugin_id = {self._dialect.bind}
                  AND adapter_instance_id = {self._dialect.bind} AND registration_epoch = {self._dialect.bind}
                  AND idempotency_key = {self._dialect.bind}
                """,
                (
                    record.authenticated_host_id,
                    record.plugin_id,
                    record.adapter_instance_id,
                    record.registration_epoch,
                    record.idempotency_key,
                ),
            ).fetchone()
            if duplicate is not None:
                conn.commit()
                return _required_record(AdapterInboxRecord, duplicate)

            registration = conn.execute(
                f"""
                SELECT authenticated_host_id, registration_epoch,
                       last_inbound_position
                FROM gateway_adapter_registrations
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                """,
                (record.plugin_id, record.adapter_instance_id),
            ).fetchone()
            if registration is None:
                raise AdapterHostRepositoryConflict(
                    "adapter_not_registered", "adapter registration does not exist"
                )
            if registration["authenticated_host_id"] != record.authenticated_host_id:
                raise AdapterHostRepositoryConflict(
                    "authenticated_host_mismatch",
                    "adapter registration belongs to a different authenticated host",
                )
            if registration["registration_epoch"] != record.registration_epoch:
                raise AdapterHostRepositoryConflict(
                    "stale_registration_epoch", "registration epoch is stale"
                )
            actual_previous = int(registration["last_inbound_position"])
            expected_position = actual_previous + 1
            if (
                actual_previous != expected_previous_position
                or record.stream_position != expected_position
            ):
                raise AdapterHostRepositoryConflict(
                    "inbound_sequence_gap",
                    f"expected inbound stream position {expected_position}",
                    expected_stream_position=expected_position,
                )

            values = asdict(record)
            columns = tuple(values)
            try:
                conn.execute(
                    f"""
                    INSERT INTO gateway_adapter_inbox ({", ".join(columns)})
                    VALUES ({", ".join(f"{self._dialect.bind}" for _ in columns)})
                    """,
                    tuple(values[column] for column in columns),
                )
            except Exception as exc:
                raise AdapterHostRepositoryConflict(
                    "inbound_sequence_conflict",
                    "inbound stream position is already owned by another request",
                    expected_stream_position=expected_position,
                ) from exc
            conn.execute(
                f"""
                UPDATE gateway_adapter_registrations
                SET last_inbound_position = {self._dialect.bind}, updated_at_utc = {self._dialect.bind}
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                  AND registration_epoch = {self._dialect.bind}
                """,
                (
                    record.stream_position,
                    record.updated_at_utc,
                    record.plugin_id,
                    record.adapter_instance_id,
                    record.registration_epoch,
                ),
            )
            conn.commit()
        return record

    def mark_inbox_processing(self, request_id: str) -> bool:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE gateway_adapter_inbox
                SET status = 'processing', updated_at_utc = {self._dialect.bind}
                WHERE request_id = {self._dialect.bind} AND status = 'accepted'
                """,
                (now, request_id),
            )
            conn.commit()
        return cursor.rowcount == 1

    def complete_inbox(
        self,
        request_id: str,
        *,
        status: str,
        result_json: str | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE gateway_adapter_inbox
                SET status = {self._dialect.bind}, result_json = {self._dialect.bind}, error_code = {self._dialect.bind}, detail = {self._dialect.bind},
                    updated_at_utc = {self._dialect.bind}
                WHERE request_id = {self._dialect.bind}
                """,
                (status, result_json, error_code, detail, utc_now_iso(), request_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown adapter inbox request {request_id!r}.")
            conn.commit()

    def list_inbox_for_recovery(self, *, limit: int = 100) -> list[AdapterInboxRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_inbox
                WHERE status = 'accepted'
                ORDER BY created_at_utc, request_id
                LIMIT {self._dialect.bind}
                """,
                (limit,),
            ).fetchall()
        return [_required_record(AdapterInboxRecord, row) for row in rows]

    def mark_interrupted_inbox_outcome_unknown(self) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE gateway_adapter_inbox
                SET status = 'outcome_unknown',
                    error_code = 'gateway_effect_outcome_unknown',
                    detail = 'The gateway process stopped after dispatch began; the effect was not retried.',
                    updated_at_utc = {self._dialect.bind}
                WHERE status = 'processing'
                """,
                (utc_now_iso(),),
            )
            conn.commit()
        return cursor.rowcount

    def enqueue_outbox(
        self,
        *,
        event_id: str,
        idempotency_key: str,
        plugin_id: str,
        adapter_instance_id: str,
        payload_json: str,
        max_pending_events: int,
    ) -> AdapterOutboxRecord:
        with self._lock, self._connect() as conn:
            self._begin(conn)
            existing = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_outbox
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                  AND idempotency_key = {self._dialect.bind}
                """,
                (plugin_id, adapter_instance_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return _required_record(AdapterOutboxRecord, existing)
            pending = conn.execute(
                f"""
                SELECT COUNT(*) AS pending_count
                FROM gateway_adapter_outbox
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                  AND status IN ('pending', 'received')
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
            if pending is None:  # pragma: no cover - aggregate query invariant
                raise RuntimeError("adapter outbox count query returned no row")
            if int(pending["pending_count"]) >= max_pending_events:
                raise AdapterHostRepositoryConflict(
                    "outbox_backpressure",
                    f"adapter outbox reached its bounded capacity of {max_pending_events}",
                )

            now = utc_now_iso()
            stream = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_streams
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
            if stream is None:
                position = 1
                conn.execute(
                    f"""
                    INSERT INTO gateway_adapter_streams (
                        plugin_id, adapter_instance_id, next_outbound_position,
                        retained_from_position, updated_at_utc
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, 2, 1, {self._dialect.bind})
                    """,
                    (plugin_id, adapter_instance_id, now),
                )
            else:
                position = int(stream["next_outbound_position"])
                conn.execute(
                    f"""
                    UPDATE gateway_adapter_streams
                    SET next_outbound_position = {self._dialect.bind}, updated_at_utc = {self._dialect.bind}
                    WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                    """,
                    (position + 1, now, plugin_id, adapter_instance_id),
                )
            record = AdapterOutboxRecord(
                event_id=event_id,
                idempotency_key=idempotency_key,
                plugin_id=plugin_id,
                adapter_instance_id=adapter_instance_id,
                stream_position=position,
                payload_json=payload_json,
                status="pending",
                received_epoch=None,
                completed_epoch=None,
                outcome=None,
                provider_message_id=None,
                attempt_count=0,
                error_code=None,
                detail=None,
                created_at_utc=now,
                received_at_utc=None,
                completed_at_utc=None,
                updated_at_utc=now,
            )
            values = asdict(record)
            columns = tuple(values)
            conn.execute(
                f"""
                INSERT INTO gateway_adapter_outbox ({", ".join(columns)})
                VALUES ({", ".join(f"{self._dialect.bind}" for _ in columns)})
                """,
                tuple(values[column] for column in columns),
            )
            conn.commit()
        return record

    def list_outbox_for_delivery(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        after_position: int,
        limit: int,
    ) -> list[AdapterOutboxRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_outbox
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                  AND stream_position > {self._dialect.bind}
                  AND status IN ('pending', 'received')
                ORDER BY stream_position
                LIMIT {self._dialect.bind}
                """,
                (plugin_id, adapter_instance_id, after_position, limit),
            ).fetchall()
        return [_required_record(AdapterOutboxRecord, row) for row in rows]

    def acknowledge_outbox(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        registration_epoch: str,
        event_id: str,
        stream_position: int,
        stage: str,
        outcome: str | None,
        provider_message_id: str | None,
        error_code: str | None,
        detail: str | None,
        observed_at_utc: str,
    ) -> AdapterOutboxRecord:
        with self._lock, self._connect() as conn:
            self._begin(conn)
            registration = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_registrations
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
            if registration is None:
                raise AdapterHostRepositoryConflict(
                    "adapter_not_registered", "adapter registration does not exist"
                )
            if registration["registration_epoch"] != registration_epoch:
                raise AdapterHostRepositoryConflict(
                    "stale_registration_epoch",
                    "delivery acknowledgement uses a stale epoch",
                )
            row = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_outbox
                WHERE event_id = {self._dialect.bind} AND plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                """,
                (event_id, plugin_id, adapter_instance_id),
            ).fetchone()
            if row is None or int(row["stream_position"]) != stream_position:
                raise AdapterHostRepositoryConflict(
                    "delivery_identity_mismatch",
                    "event id and stream position do not identify the same delivery",
                )

            if stage == "received":
                previous = int(registration["last_outbound_received_position"])
                if stream_position > previous + 1:
                    raise AdapterHostRepositoryConflict(
                        "outbound_receipt_gap",
                        f"expected receipt for stream position {previous + 1}",
                        expected_stream_position=previous + 1,
                    )
                if stream_position <= previous:
                    conn.execute(
                        f"""
                        UPDATE gateway_adapter_outbox
                        SET received_epoch = {self._dialect.bind}, updated_at_utc = {self._dialect.bind}
                        WHERE event_id = {self._dialect.bind}
                        """,
                        (registration_epoch, observed_at_utc, event_id),
                    )
                else:
                    conn.execute(
                        f"""
                        UPDATE gateway_adapter_outbox
                        SET status = 'received', received_epoch = {self._dialect.bind},
                            received_at_utc = COALESCE(received_at_utc, {self._dialect.bind}),
                            attempt_count = attempt_count + 1, updated_at_utc = {self._dialect.bind}
                        WHERE event_id = {self._dialect.bind}
                        """,
                        (
                            registration_epoch,
                            observed_at_utc,
                            observed_at_utc,
                            event_id,
                        ),
                    )
                    conn.execute(
                        f"""
                        UPDATE gateway_adapter_registrations
                        SET last_outbound_received_position = {self._dialect.bind}, updated_at_utc = {self._dialect.bind}
                        WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                        """,
                        (
                            stream_position,
                            observed_at_utc,
                            plugin_id,
                            adapter_instance_id,
                        ),
                    )
            elif stage == "completed":
                previous = int(registration["last_outbound_completed_position"])
                if stream_position > previous + 1:
                    raise AdapterHostRepositoryConflict(
                        "outbound_completion_gap",
                        f"expected completion for stream position {previous + 1}",
                        expected_stream_position=previous + 1,
                    )
                if row["received_epoch"] != registration_epoch:
                    raise AdapterHostRepositoryConflict(
                        "delivery_receipt_required",
                        "the current epoch must durably acknowledge receipt before completion",
                    )
                if stream_position > previous:
                    status = {
                        "delivered": "delivered",
                        "failed": "failed",
                        "outcome_unknown": "outcome_unknown",
                    }.get(str(outcome))
                    if status is None:
                        raise AdapterHostRepositoryConflict(
                            "invalid_delivery_outcome", "unsupported delivery outcome"
                        )
                    conn.execute(
                        f"""
                        UPDATE gateway_adapter_outbox
                        SET status = {self._dialect.bind}, completed_epoch = {self._dialect.bind}, outcome = {self._dialect.bind},
                            provider_message_id = {self._dialect.bind}, error_code = {self._dialect.bind}, detail = {self._dialect.bind},
                            completed_at_utc = {self._dialect.bind}, updated_at_utc = {self._dialect.bind}
                        WHERE event_id = {self._dialect.bind}
                        """,
                        (
                            status,
                            registration_epoch,
                            outcome,
                            provider_message_id,
                            error_code,
                            detail,
                            observed_at_utc,
                            observed_at_utc,
                            event_id,
                        ),
                    )
                    conn.execute(
                        f"""
                        UPDATE gateway_adapter_registrations
                        SET last_outbound_completed_position = {self._dialect.bind}, updated_at_utc = {self._dialect.bind},
                            error_code = {self._dialect.bind}, detail = {self._dialect.bind}
                        WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                        """,
                        (
                            stream_position,
                            observed_at_utc,
                            error_code if status != "delivered" else None,
                            detail if status != "delivered" else None,
                            plugin_id,
                            adapter_instance_id,
                        ),
                    )
            else:
                raise AdapterHostRepositoryConflict(
                    "invalid_acknowledgement_stage", "unsupported acknowledgement stage"
                )
            updated = conn.execute(
                f"SELECT * FROM gateway_adapter_outbox WHERE event_id = {self._dialect.bind}",
                (event_id,),
            ).fetchone()
            conn.commit()
        return _required_record(AdapterOutboxRecord, updated)

    def get_stream(
        self, plugin_id: str, adapter_instance_id: str
    ) -> AdapterStreamRecord:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_streams
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
        if row is None:
            return AdapterStreamRecord(
                plugin_id=plugin_id,
                adapter_instance_id=adapter_instance_id,
                next_outbound_position=1,
                retained_from_position=1,
                updated_at_utc=utc_now_iso(),
            )
        return _required_record(AdapterStreamRecord, row)

    def update_heartbeat(
        self,
        *,
        authenticated_host_id: str,
        plugin_id: str,
        adapter_instance_id: str,
        registration_epoch: str,
        state: str,
        lease_expires_at_utc: str,
        observed_at_utc: str,
        inbound_committed_position: int,
        outbound_received_position: int,
        outbound_completed_position: int,
        error_code: str | None,
        detail: str | None,
    ) -> AdapterRegistrationRecord:
        with self._lock, self._connect() as conn:
            self._begin(conn)
            row = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_registrations
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
            if row is None:
                raise AdapterHostRepositoryConflict(
                    "adapter_not_registered", "adapter registration does not exist"
                )
            if row["authenticated_host_id"] != authenticated_host_id:
                raise AdapterHostRepositoryConflict(
                    "authenticated_host_mismatch",
                    "adapter registration belongs to a different authenticated host",
                )
            if row["registration_epoch"] != registration_epoch:
                raise AdapterHostRepositoryConflict(
                    "stale_registration_epoch", "heartbeat uses a stale epoch"
                )
            gateway_positions = (
                int(row["last_inbound_position"]),
                int(row["last_outbound_received_position"]),
                int(row["last_outbound_completed_position"]),
            )
            reported_positions = (
                inbound_committed_position,
                outbound_received_position,
                outbound_completed_position,
            )
            if any(
                reported > gateway
                for reported, gateway in zip(reported_positions, gateway_positions)
            ):
                raise AdapterHostRepositoryConflict(
                    "cursor_ahead_of_gateway",
                    "adapter reported a cursor beyond gateway durable state",
                )
            conn.execute(
                f"""
                UPDATE gateway_adapter_registrations
                SET state = {self._dialect.bind}, lease_expires_at_utc = {self._dialect.bind},
                    last_heartbeat_at_utc = {self._dialect.bind}, error_code = {self._dialect.bind}, detail = {self._dialect.bind},
                    updated_at_utc = {self._dialect.bind}
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                """,
                (
                    state,
                    lease_expires_at_utc,
                    observed_at_utc,
                    error_code,
                    detail,
                    observed_at_utc,
                    plugin_id,
                    adapter_instance_id,
                ),
            )
            updated = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_registrations
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
            conn.commit()
        return _required_record(AdapterRegistrationRecord, updated)

    def health_snapshot(
        self, plugin_id: str, adapter_instance_id: str
    ) -> AdapterHostHealthSnapshot | None:
        with self._connect() as conn:
            registration = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_registrations
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
            if registration is None:
                return None
            counts = conn.execute(
                f"""
                SELECT
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                    SUM(CASE WHEN status = 'received' THEN 1 ELSE 0 END) AS inflight_count,
                    MIN(CASE WHEN status IN ('pending', 'received') THEN created_at_utc END)
                        AS oldest_pending_at_utc
                FROM gateway_adapter_outbox
                WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                """,
                (plugin_id, adapter_instance_id),
            ).fetchone()
            if counts is None:  # pragma: no cover - aggregate query invariant
                raise RuntimeError("adapter health count query returned no row")
        return AdapterHostHealthSnapshot(
            authenticated_host_id=str(registration["authenticated_host_id"]),
            plugin_id=plugin_id,
            adapter_instance_id=adapter_instance_id,
            registration_epoch=str(registration["registration_epoch"]),
            state=str(registration["state"]),
            lease_expires_at_utc=str(registration["lease_expires_at_utc"]),
            last_heartbeat_at_utc=str(registration["last_heartbeat_at_utc"]),
            last_inbound_position=int(registration["last_inbound_position"]),
            last_outbound_received_position=int(
                registration["last_outbound_received_position"]
            ),
            last_outbound_completed_position=int(
                registration["last_outbound_completed_position"]
            ),
            pending_event_count=int(counts["pending_count"] or 0),
            in_flight_event_count=int(counts["inflight_count"] or 0),
            oldest_pending_at_utc=counts["oldest_pending_at_utc"],
            reconnect_count=int(registration["reconnect_count"]),
            error_code=registration["error_code"],
            detail=registration["detail"],
        )

    def save_upload(self, record: AdapterUploadRecord) -> AdapterUploadRecord:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                f"""
                SELECT * FROM gateway_adapter_uploads
                WHERE authenticated_host_id = {self._dialect.bind} AND plugin_id = {self._dialect.bind}
                  AND adapter_instance_id = {self._dialect.bind} AND registration_epoch = {self._dialect.bind}
                  AND idempotency_key = {self._dialect.bind}
                """,
                (
                    record.authenticated_host_id,
                    record.plugin_id,
                    record.adapter_instance_id,
                    record.registration_epoch,
                    record.idempotency_key,
                ),
            ).fetchone()
            if existing is not None:
                return _required_record(AdapterUploadRecord, existing)
            values = asdict(record)
            columns = tuple(values)
            conn.execute(
                f"""
                INSERT INTO gateway_adapter_uploads ({", ".join(columns)})
                VALUES ({", ".join(f"{self._dialect.bind}" for _ in columns)})
                """,
                tuple(values[column] for column in columns),
            )
            conn.commit()
        return record

    def get_upload(self, upload_id: str) -> AdapterUploadRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM gateway_adapter_uploads WHERE upload_id = {self._dialect.bind}",
                (upload_id,),
            ).fetchone()
        return _record(AdapterUploadRecord, row)

    def complete_upload(self, upload_id: str, *, completed_at_utc: str) -> None:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE gateway_adapter_uploads
                SET status = 'completed', completed_at_utc = {self._dialect.bind}
                WHERE upload_id = {self._dialect.bind} AND status = 'pending'
                """,
                (completed_at_utc, upload_id),
            )
            if cursor.rowcount != 1:
                raise AdapterHostRepositoryConflict(
                    "upload_not_pending",
                    "upload ticket is missing, expired, or already used",
                )
            conn.commit()

    def prune(self, *, cutoff_utc: str) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            self._begin(conn)
            terminal = conn.execute(
                f"""
                SELECT plugin_id, adapter_instance_id, MAX(stream_position) AS max_position
                FROM gateway_adapter_outbox
                WHERE status IN ('delivered', 'failed', 'outcome_unknown')
                  AND updated_at_utc < {self._dialect.bind}
                GROUP BY plugin_id, adapter_instance_id
                """,
                (cutoff_utc,),
            ).fetchall()
            outbox = conn.execute(
                f"""
                DELETE FROM gateway_adapter_outbox
                WHERE status IN ('delivered', 'failed', 'outcome_unknown')
                  AND updated_at_utc < {self._dialect.bind}
                """,
                (cutoff_utc,),
            ).rowcount
            for row in terminal:
                conn.execute(
                    f"""
                    UPDATE gateway_adapter_streams
                    SET retained_from_position = CASE
                        WHEN retained_from_position < {self._dialect.bind} THEN {self._dialect.bind}
                        ELSE retained_from_position END,
                        updated_at_utc = {self._dialect.bind}
                    WHERE plugin_id = {self._dialect.bind} AND adapter_instance_id = {self._dialect.bind}
                    """,
                    (
                        int(row["max_position"]) + 1,
                        int(row["max_position"]) + 1,
                        utc_now_iso(),
                        row["plugin_id"],
                        row["adapter_instance_id"],
                    ),
                )
            inbox = conn.execute(
                f"""
                DELETE FROM gateway_adapter_inbox
                WHERE status IN ('completed', 'failed', 'outcome_unknown')
                  AND updated_at_utc < {self._dialect.bind}
                """,
                (cutoff_utc,),
            ).rowcount
            uploads = conn.execute(
                f"""
                DELETE FROM gateway_adapter_uploads
                WHERE expires_at_utc < {self._dialect.bind}
                """,
                (cutoff_utc,),
            ).rowcount
            conn.commit()
        return {"inbox": inbox, "outbox": outbox, "uploads": uploads}


class SqliteAdapterHostRepository(_RelationalAdapterHostRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: logging.Logger | None = None) -> None:
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._init_schema()


    def _connect(self) -> sqlite3.Connection | PostgresConnection:
        return connect_sqlite(
            self._db_path,
            logger=self._logger,
            row_factory=sqlite3.Row,
        )


class PostgresAdapterHostRepository(_RelationalAdapterHostRepository):
    """Postgres implementation using the repository layer's SQL compatibility."""

    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: logging.Logger | None = None) -> None:
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        validate_postgres_connection(dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)


def create_adapter_host_repository(
    storage_settings: StorageSettings,
    *,
    logger: logging.Logger | None = None,
) -> AdapterHostRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="AdapterHostRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if (
        storage_settings.backend == STORAGE_BACKEND_SQLITE
        and storage_settings.sqlite_path
    ):
        return SqliteAdapterHostRepository(storage_settings.sqlite_path, logger=logger)
    if (
        storage_settings.backend == STORAGE_BACKEND_POSTGRES
        and storage_settings.postgres_dsn
    ):
        return PostgresAdapterHostRepository(
            storage_settings.postgres_dsn, logger=logger
        )
    raise NotImplementedError(
        f"AdapterHostRepository has no implementation for backend {storage_settings.backend!r}."
    )


__all__ = [
    "AdapterHostRepositoryConflict",
    "PostgresAdapterHostRepository",
    "SqliteAdapterHostRepository",
    "create_adapter_host_repository",
]
