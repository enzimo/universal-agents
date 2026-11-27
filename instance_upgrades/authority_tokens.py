"""One-time first-start conversion of stored 0.9 signed task contexts and schedule permits.

Up to 0.9.x, who a task or scheduled run worked for was stored as an Ed25519-signed
``SignedTaskContextV1`` or ``SignedSchedulePermitV1`` checked with the shared keys in
``team.env`` (``ACCESS_GATEWAY_SIGNING_KEY``, ``ACCESS_SCHEDULER_SIGNING_KEY``,
``ACCESS_CONTEXT_VERIFY_KEYS``). From 0.10, only protocol 1 ``AuthorityToken`` values
signed by host certificates issued by the installation authority are accepted
(``access_control.task_identity.resolve_task_identity``), and the old signing-key
variables leave ``team.env`` and are refused at startup.

This module runs once at the first start of 0.10 (plan 13C.9, decision 90):

- **First start without the marker, no legacy rows in any store:** writes the marker
  (``InstanceLayout.authority_tokens_upgrade_path``) without touching NATS. If any
  removed 0.9 signing-key variable (or ``*_FILE`` form) is set, it is accepted once on
  this first start (the launcher withholds it from every runtime service) and startup
  logs a warning naming the variables to remove before the next restart. From the next
  start on, once the marker exists, removed signing-key variables are refused immediately.
- **Upgraded 0.9.x installs (marker missing, legacy rows present):** the headless
  launcher runs ``assistant_cli.py upgrade authority-tokens --config <path>`` after
  ``installation-authority`` is ready and before ``team-services``, ``secure_tool_broker``,
  ``manager``, ``startup_agents``, and ``gateway`` start. No model-running process ever
  receives a signing key (decision 58). The step checks each legacy context or permit
  once with a self-contained 0.9 verifier, re-signs it with the active ``gateway`` or
  ``team-services`` host certificate (``issued_at = conversion_time``,
  ``expires_at = min(old_end, conversion_time + AUTHORITY_TOKEN_MAX_SECONDS)``), converts
  a schedule's vouched context or permit into ``owner_vouched = 1`` plus a fresh
  scheduler-signed permit, backfills ``change_requests.token_digest`` for converted root
  tasks (C26), and writes the marker.
- **Unverifiable or already-expired records (C03):** never silently dropped. The row is
  kept intact with only its unparseable legacy ``task_context`` / ``schedule_run`` /
  ``run_permit_json`` cleared to ``NULL`` (and ``owner_vouched = 0`` for unverifiable
  schedules), logged prominently at startup, and reported to administrators in one
  durable Access Inbox notice (``AccessNoticeKind.AUTHORITY_TOKEN_UPGRADE``).
  Expired saved gateway contexts on ``scheduled_messages`` are the deliberate exception:
  they still prove vouching (``check_expiry=False``), set ``owner_vouched = 1``, and get
  a fresh scheduler-signed permit.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from access_control.people import PERSON_ROLE_ADMIN
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    AccessNoticeV1,
    PersonStanding,
    SCHEDULE_RUN_ID_PREFIX,
    TeamPersonV1,
)
from custom_messages.wire.authority_bindings import AuthorityTokenRecord
from custom_messages.wire.kinds import AUTHORITY_TOKEN_MAX_SECONDS
from custom_transport.protocol1.authority_tokens import root_task_token, schedule_permit_token
from custom_transport.protocol1.identity import HostIdentity
from env_secrets import get_env_with_file_fallback
from runtime_settings.security import present_removed_signing_key_env_vars
from storage.access_repository import AccessRepository
from storage.config import StorageSettings
from storage.runtime_snapshot_layout import canonical_json, payload_digest
from storage.sql_dialect import POSTGRES, SQLITE, SqlDialect
from storage.table_columns import ensure_column, table_column_names

if TYPE_CHECKING:
    from assistant_gateway.config import GatewayInstanceConfig

LOGGER = logging.getLogger(__name__)

_LEGACY_CONTEXT_DOMAIN: Final = b"ua-task-context-v1\x00"
_LEGACY_PERMIT_DOMAIN: Final = b"ua-schedule-permit-v1\x00"
_ENV_GATEWAY_SIGNING_KEY: Final = "ACCESS_GATEWAY_SIGNING_KEY"
_ENV_SCHEDULER_SIGNING_KEY: Final = "ACCESS_SCHEDULER_SIGNING_KEY"
_ENV_VERIFY_KEYS: Final = "ACCESS_CONTEXT_VERIFY_KEYS"
_PERMIT_RENEW_LEAD: Final = timedelta(days=2)
_SYSTEM_WORK_KINDS: Final = frozenset({"internal_improvement", "memory_review_cleanup", "after_action_report_review"})


class MissingLegacyVerifyKeysError(RuntimeError):
    """Legacy signed records exist in storage, but no 0.9 verification keys were supplied."""


@dataclass(frozen=True)
class SigningIdentities:
    """Active host identities used during the one-shot conversion to sign protocol 1 tokens."""

    gateway: HostIdentity
    scheduler: HostIdentity


@dataclass(frozen=True)
class ConversionIssue:
    """One stored legacy context or permit that could not be converted into a token."""

    category: Literal["unverifiable", "expired"]
    table: str
    record_id: str
    reason: str
    detail: str = ""


@dataclass
class ConversionCounts:
    """Counts of stored records inspected and updated during the one-shot conversion."""

    converted_contexts: int = 0
    converted_schedule_runs: int = 0
    converted_permits: int = 0
    vouched_schedules: int = 0
    fresh_permits_issued: int = 0
    ledger_digests_backfilled: int = 0
    expired: int = 0
    unverifiable: int = 0
    expiry_capped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "converted_contexts": self.converted_contexts,
            "converted_schedule_runs": self.converted_schedule_runs,
            "converted_permits": self.converted_permits,
            "vouched_schedules": self.vouched_schedules,
            "fresh_permits_issued": self.fresh_permits_issued,
            "ledger_digests_backfilled": self.ledger_digests_backfilled,
            "expired": self.expired,
            "unverifiable": self.unverifiable,
            "expiry_capped": self.expiry_capped,
        }


@dataclass(frozen=True)
class AuthorityTokenUpgradeResult:
    """Outcome of running ``upgrade_authority_tokens``."""

    already_done: bool = False
    had_legacy_records: bool = False
    counts: ConversionCounts = field(default_factory=ConversionCounts)
    issues: tuple[ConversionIssue, ...] = ()
    notice_stored: bool = False
    removed_keys_still_set: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Self-contained 0.9 legacy models and Ed25519 verifier
# ---------------------------------------------------------------------------


class _LegacyTaskContext(BaseModel):
    """Exact 0.9.x ``TaskContextV1`` shape (before 0.10 added ``issuer_roles``)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1] = 1
    context_id: str = Field(min_length=1, max_length=128)
    issuer: Literal["gateway", "scheduler"]
    team_id: str = Field(min_length=1, max_length=120)
    person_id: str | None = Field(default=None, max_length=240)
    person_roles: tuple[str, ...] = ()
    root_task_id: str | None = Field(default=None, max_length=240)
    conversation_id: str | None = Field(default=None, max_length=240)
    schedule_id: str | None = Field(default=None, max_length=240)
    issued_at_utc: datetime
    expires_at_utc: datetime


class _LegacySignedTaskContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context: _LegacyTaskContext
    key_id: str = Field(min_length=1, max_length=120)
    signature: str = Field(min_length=1, max_length=400)


class _LegacySchedulePermit(BaseModel):
    """Exact 0.9.x ``SchedulePermitV1`` shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    record_kind: Literal["schedule_permit"] = "schedule_permit"
    permit_id: str = Field(min_length=1, max_length=120)
    issuer: Literal["scheduler"]
    team_id: str = Field(min_length=1, max_length=120)
    schedule_id: str = Field(min_length=1, max_length=240)
    person_id: str | None = Field(default=None, max_length=240)
    issued_at_utc: datetime
    expires_at_utc: datetime


class _LegacySignedSchedulePermit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    permit: _LegacySchedulePermit
    key_id: str = Field(min_length=1, max_length=120)
    signature: str = Field(min_length=1, max_length=400)


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _canonical_legacy_bytes(domain: bytes, model: BaseModel) -> bytes:
    return domain + json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")


class _LegacyVerifyError(ValueError):
    """Raised when a legacy 0.9 context or permit cannot be cryptographically verified."""


@dataclass(frozen=True)
class _LegacyKeyring:
    """Public verification keys loaded from the 0.9 signing/verify environment variables."""

    keys: Mapping[str, tuple[str, Ed25519PublicKey]]

    @property
    def is_empty(self) -> bool:
        return not self.keys

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "_LegacyKeyring":
        source = os.environ if env is None else env
        keys: dict[str, tuple[str, Ed25519PublicKey]] = {}
        raw_verify = str(get_env_with_file_fallback(_ENV_VERIFY_KEYS, env=source) or "").strip()
        if raw_verify:
            try:
                entries = json.loads(raw_verify)
            except ValueError as exc:
                raise ValueError(f"{_ENV_VERIFY_KEYS} is not valid JSON.") from exc
            if not isinstance(entries, list):
                raise ValueError(f"{_ENV_VERIFY_KEYS} must be a JSON array.")
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise ValueError(f"{_ENV_VERIFY_KEYS} entries must be objects.")
                key_id = str(entry.get("key_id") or "").strip()
                issuer = str(entry.get("issuer") or "").strip()
                pub_b64 = str(entry.get("public_key") or "").strip()
                if not key_id or not issuer or not pub_b64:
                    raise ValueError(f"{_ENV_VERIFY_KEYS} entry is missing key_id, issuer, or public_key.")
                keys[key_id] = (issuer, Ed25519PublicKey.from_public_bytes(_unb64(pub_b64)))
        for issuer, env_name in (("gateway", _ENV_GATEWAY_SIGNING_KEY), ("scheduler", _ENV_SCHEDULER_SIGNING_KEY)):
            raw_priv = str(get_env_with_file_fallback(env_name, env=source) or "").strip()
            if not raw_priv:
                continue
            key_id, _, seed_b64 = raw_priv.partition(":")
            if not key_id or not seed_b64:
                raise ValueError(f"{env_name} is not in '<key_id>:<base64>' form.")
            priv = Ed25519PrivateKey.from_private_bytes(_unb64(seed_b64))
            pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            keys[key_id] = (issuer, Ed25519PublicKey.from_public_bytes(pub_raw))
        return cls(keys=keys)

    def verify_context(self, raw: Mapping[str, Any], *, team_id: str) -> _LegacyTaskContext:
        try:
            envelope = _LegacySignedTaskContext.model_validate(raw)
        except ValidationError as exc:
            raise _LegacyVerifyError(f"malformed legacy task context ({exc.error_count()} validation error(s))") from exc
        known = self.keys.get(envelope.key_id)
        if known is None:
            raise _LegacyVerifyError(f"unknown signing key {envelope.key_id!r}")
        issuer, public_key = known
        if envelope.context.issuer != issuer:
            raise _LegacyVerifyError("context issuer does not match signing key issuer")
        self._verify_sig(public_key, envelope.signature, _canonical_legacy_bytes(_LEGACY_CONTEXT_DOMAIN, envelope.context), "task context")
        if envelope.context.team_id != team_id:
            raise _LegacyVerifyError(f"context belongs to another team ({envelope.context.team_id!r})")
        if envelope.context.issuer == "gateway" and envelope.context.schedule_id is not None:
            raise _LegacyVerifyError("gateway context unexpectedly names a schedule")
        return envelope.context

    def verify_permit(self, raw: Mapping[str, Any], *, team_id: str, expected_schedule_id: str | None = None) -> _LegacySchedulePermit:
        try:
            envelope = _LegacySignedSchedulePermit.model_validate(raw)
        except ValidationError as exc:
            raise _LegacyVerifyError(f"malformed legacy schedule permit ({exc.error_count()} validation error(s))") from exc
        known = self.keys.get(envelope.key_id)
        if known is None:
            raise _LegacyVerifyError(f"unknown signing key {envelope.key_id!r}")
        issuer, public_key = known
        if issuer != "scheduler" or envelope.permit.issuer != "scheduler":
            raise _LegacyVerifyError("only the scheduler key signs schedule permits")
        self._verify_sig(public_key, envelope.signature, _canonical_legacy_bytes(_LEGACY_PERMIT_DOMAIN, envelope.permit), "schedule permit")
        if envelope.permit.team_id != team_id:
            raise _LegacyVerifyError(f"permit belongs to another team ({envelope.permit.team_id!r})")
        if expected_schedule_id is not None and envelope.permit.schedule_id != expected_schedule_id:
            raise _LegacyVerifyError(f"permit names schedule {envelope.permit.schedule_id!r}, expected {expected_schedule_id!r}")
        return envelope.permit

    @staticmethod
    def _verify_sig(public_key: Ed25519PublicKey, signature: str, payload: bytes, what: str) -> None:
        try:
            public_key.verify(_unb64(signature), payload)
        except (InvalidSignature, ValueError) as exc:
            raise _LegacyVerifyError(f"invalid {what} signature") from exc


# ---------------------------------------------------------------------------
# Legacy payload detection (never accepts protocol 1 tokens as legacy)
# ---------------------------------------------------------------------------


def _is_legacy_signed_context(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and "token_id" not in value
        and isinstance(value.get("context"), Mapping)
        and isinstance(value.get("key_id"), str)
        and isinstance(value.get("signature"), str)
    )


def _is_legacy_signed_permit(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and "token_id" not in value
        and isinstance(value.get("permit"), Mapping)
        and isinstance(value.get("key_id"), str)
        and isinstance(value.get("signature"), str)
    )


def _is_legacy_schedule_run(value: Any) -> bool:
    return isinstance(value, Mapping) and _is_legacy_signed_permit(value.get("permit"))


def _contains_legacy_payload(value: Any) -> bool:
    if isinstance(value, Mapping):
        if _is_legacy_signed_context(value) or _is_legacy_signed_permit(value) or _is_legacy_schedule_run(value):
            return True
        return any(_contains_legacy_payload(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_legacy_payload(item) for item in value)
    return False


def _raw_json_has_legacy_payload(raw: Any) -> bool:
    if not isinstance(raw, str) or not raw.strip():
        return False
    # Fast text pre-filter: every legacy context or permit carries "key_id" and "signature".
    if '"key_id"' not in raw or '"signature"' not in raw:
        return False
    try:
        parsed = json.loads(raw)
    except ValueError:
        return False
    return _contains_legacy_payload(parsed)


# ---------------------------------------------------------------------------
# Database helpers (SQLite + PostgreSQL)
# ---------------------------------------------------------------------------


def _dialect_for(storage: StorageSettings) -> SqlDialect:
    return POSTGRES if storage.postgres_dsn else SQLITE


def _same_storage(a: StorageSettings | None, b: StorageSettings | None) -> bool:
    if a is None or b is None:
        return False
    if a.postgres_dsn or b.postgres_dsn:
        return bool(a.postgres_dsn and b.postgres_dsn and a.postgres_dsn == b.postgres_dsn)
    if a.sqlite_path and b.sqlite_path:
        return Path(a.sqlite_path).resolve() == Path(b.sqlite_path).resolve()
    return False


def _open_connection_if_exists(storage: StorageSettings | None) -> Callable[[], Any] | None:
    if storage is None:
        return None
    if storage.postgres_dsn:
        from storage.postgres_connection import connect_postgres

        dsn = storage.postgres_dsn
        return lambda: connect_postgres(dsn)
    path = storage.sqlite_path
    if not path or not Path(path).is_file():
        return None
    from storage.sqlite_connection import connect_sqlite

    resolved = str(path)
    return lambda: connect_sqlite(resolved)


def _row_values(row: Any) -> tuple[Any, ...]:
    """Return positional column values for both SQLite rows/tuples and PostgreSQL ``DatabaseRow`` mappings."""

    return tuple(row[i] for i in range(len(row)))


def _table_exists(conn: Any, dialect: SqlDialect, table: str) -> bool:
    if dialect is POSTGRES:
        row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()
        return bool(row and row[0])
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


def _resolve_storages(
    config: "GatewayInstanceConfig", env: Mapping[str, str] | None = None
) -> tuple[StorageSettings | None, StorageSettings | None]:
    """Resolve manager and gateway-state storage settings using ``env`` (so ``POSTGRES_DSN`` from team.env works)."""

    source_env = os.environ if env is None else env
    resolve_mgr = getattr(config, "resolve_manager_storage_settings", None)
    manager = resolve_mgr(env=source_env) if callable(resolve_mgr) else getattr(config, "manager_storage_settings", None)
    resolve_state = getattr(config, "resolve_state_storage_settings", None)
    state = resolve_state(env=source_env) if callable(resolve_state) else getattr(config, "state_storage_settings", None)
    return manager, state


def _distinct_storages(
    config: "GatewayInstanceConfig", env: Mapping[str, str] | None = None
) -> list[tuple[str, StorageSettings]]:
    """Manager and gateway-state storage settings, deduplicated when both point to the same database."""

    storages: list[tuple[str, StorageSettings]] = []
    manager, gateway = _resolve_storages(config, env=env)
    if manager is not None:
        storages.append(("manager", manager))
    if gateway is not None and not _same_storage(manager, gateway):
        storages.append(("gateway", gateway))
    return storages


# Tables and JSON columns that can hold legacy SignedTaskContextV1 or SignedSchedulePermitV1.
_SCAN_SPEC: Final[tuple[tuple[str, tuple[str, ...], str | None], ...]] = (
    ("active_task_runtime", ("runtime_state_json",), "team_id"),
    ("active_task_runtime_records", ("payload_json",), None),
    ("retired_task_runtime", ("runtime_state_json", "records_json"), "team_id"),
    ("agent_deferred_messages", ("messages_json",), "team_id"),
    ("worker_task_handbacks", ("payload_json",), "team_id"),
    ("scheduled_messages", ("schedule_json", "payload_json", "run_permit_json"), "claim_team_id"),
    ("schedule_worker_bindings", ("pending_schedule_json",), "team_id"),
    ("conversation_task_links", ("gateway_context_json",), None),
    ("person_held_rows", ("row_json",), "team_id"),
    ("gateway_deliveries", ("payload_json",), None),
    ("gateway_held_guidance", ("steering_json",), None),
)


def has_legacy_authority_records(
    config: "GatewayInstanceConfig", *, env: Mapping[str, str] | None = None
) -> bool:
    """Read-only check: whether any store for ``config`` still contains a 0.9 signed context or permit."""

    from storage.read_only import read_only_repositories

    team_id = str(config.manager.team_id)
    for _role, storage in _distinct_storages(config, env=env):
        connect = _open_connection_if_exists(storage)
        if connect is None:
            continue
        dialect = _dialect_for(storage)
        with read_only_repositories(), connect() as conn:
            if _db_has_legacy_records(conn, dialect, team_id):
                return True
    return False


def _db_has_legacy_records(conn: Any, dialect: SqlDialect, team_id: str) -> bool:
    p = dialect.bind
    for table, columns, team_col in _SCAN_SPEC:
        if not _table_exists(conn, dialect, table):
            continue
        present_cols = set(table_column_names(conn, table))
        target_cols = [col for col in columns if col in present_cols]
        if not target_cols:
            continue
        if table == "active_task_runtime_records" and _table_exists(conn, dialect, "active_task_runtime"):
            sql = (
                f"SELECT {', '.join(target_cols)} FROM active_task_runtime_records r "
                f"WHERE NOT EXISTS (SELECT 1 FROM active_task_runtime s "
                f"WHERE s.owner_agent_id = r.owner_agent_id AND s.team_id IS NOT NULL AND s.team_id <> {p})"
            )
            rows = conn.execute(sql, (team_id,)).fetchall()
        elif team_col and team_col in present_cols:
            sql = f"SELECT {', '.join(target_cols)} FROM {table} WHERE ({team_col} = {p} OR {team_col} IS NULL)"
            rows = conn.execute(sql, (team_id,)).fetchall()
        else:
            rows = conn.execute(f"SELECT {', '.join(target_cols)} FROM {table}").fetchall()
        for row in rows:
            if any(_raw_json_has_legacy_payload(cell) for cell in _row_values(row)):
                return True
    return False


# ---------------------------------------------------------------------------
# Conversion engine
# ---------------------------------------------------------------------------


@dataclass
class _ConversionContext:
    team_id: str
    now: datetime
    keyring: _LegacyKeyring
    identities: SigningIdentities | None
    people: Mapping[str, TeamPersonV1]
    system_work_schedule_ids: frozenset[str]
    counts: ConversionCounts = field(default_factory=ConversionCounts)
    issues: list[ConversionIssue] = field(default_factory=list)
    ledger_backfills: list[tuple[str, str, str, str]] = field(default_factory=list)  # (team_id, request_id, person_id, token_digest)

    def _require_identities(self) -> SigningIdentities:
        if self.identities is None:
            raise RuntimeError("Signing identities are required to re-sign verifiable 0.9 records.")
        return self.identities

    def add_issue(self, category: Literal["unverifiable", "expired"], table: str, record_id: str, reason: str, detail: str = "") -> None:
        self.issues.append(ConversionIssue(category=category, table=table, record_id=record_id, reason=reason, detail=detail))
        if category == "expired":
            self.counts.expired += 1
        else:
            self.counts.unverifiable += 1

    def bounded_expiry(self, old_end: datetime, *, table: str, record_id: str) -> datetime:
        cap = self.now + timedelta(seconds=AUTHORITY_TOKEN_MAX_SECONDS)
        end = _utc(old_end)
        if end > cap:
            self.counts.expiry_capped += 1
            LOGGER.info(
                "Capped converted authority token expiry to %s seconds. "
                "event=authority_token_conversion_expiry_capped table=%s record_id=%s old_expires_at=%s new_expires_at=%s",
                AUTHORITY_TOKEN_MAX_SECONDS,
                table,
                record_id,
                end.isoformat(),
                cap.isoformat(),
            )
            return cap
        return end

    def resolve_person_ref(self, person_id: str | None) -> tuple[str | None, str | None]:
        """Return ``(person_ref, error_reason)`` for ``person_id`` (None for system work)."""

        if person_id is None:
            return None, None
        person = self.people.get(person_id)
        if person is None:
            return None, f"person {person_id!r} is missing from access_people"
        return person.person_ref, None

    def convert_signed_context(
        self,
        raw: Mapping[str, Any],
        *,
        table: str,
        record_id: str,
        task_id_hint: str | None = None,
    ) -> tuple[dict[str, Any] | None, _LegacyTaskContext | None]:
        """Convert one legacy ``SignedTaskContextV1`` dict into a protocol 1 ``AuthorityToken`` dict.

        Returns ``(new_token_or_None, verified_legacy_context_or_None)``.
        """

        try:
            legacy = self.keyring.verify_context(raw, team_id=self.team_id)
        except _LegacyVerifyError as exc:
            self.add_issue("unverifiable", table, record_id, str(exc))
            return None, None
        if legacy.person_id is None and legacy.schedule_id is None:
            self.add_issue("unverifiable", table, record_id, "legacy context names neither a person nor a schedule")
            return None, None
        person_ref, person_err = self.resolve_person_ref(legacy.person_id)
        if person_err is not None:
            self.add_issue("unverifiable", table, record_id, person_err)
            return None, None
        if _utc(legacy.expires_at_utc) <= self.now:
            self.add_issue("expired", table, record_id, f"expired at {_utc(legacy.expires_at_utc).isoformat()}")
            return None, None
        expires_at = self.bounded_expiry(legacy.expires_at_utc, table=table, record_id=record_id)
        identities = self._require_identities()
        identity = (
            identities.gateway
            if legacy.issuer == "gateway" and legacy.schedule_id is None
            else identities.scheduler
        )
        root_task_id = legacy.root_task_id or task_id_hint or legacy.context_id
        try:
            token = root_task_token(
                identity,
                team=self.team_id,
                root_task_id=root_task_id,
                request_id=legacy.context_id,
                issued=self.now,
                person_ref=person_ref,
                schedule_id=legacy.schedule_id,
                expires_at=expires_at,
            )
        except ValueError as exc:
            self.add_issue("unverifiable", table, record_id, f"could not sign converted token: {exc}")
            return None, None
        self.counts.converted_contexts += 1
        return token, legacy

    def convert_schedule_run(
        self,
        raw: Mapping[str, Any],
        *,
        table: str,
        record_id: str,
    ) -> dict[str, Any] | None:
        """Convert one legacy ``ScheduleRunIdentityV1`` dict into one carrying a protocol 1 permit token."""

        raw_permit = raw.get("permit")
        if not isinstance(raw_permit, Mapping):
            self.add_issue("unverifiable", table, record_id, "schedule_run is missing its permit")
            return None
        context_id = str(raw.get("context_id") or "").strip()
        if not context_id.startswith(f"{SCHEDULE_RUN_ID_PREFIX}_"):
            self.add_issue("unverifiable", table, record_id, f"invalid schedule_run context_id {context_id!r}")
            return None
        try:
            permit = self.keyring.verify_permit(raw_permit, team_id=self.team_id)
        except _LegacyVerifyError as exc:
            self.add_issue("unverifiable", table, record_id, str(exc))
            return None
        person_ref, person_err = self.resolve_person_ref(permit.person_id)
        if person_err is not None:
            self.add_issue("unverifiable", table, record_id, person_err)
            return None
        if _utc(permit.expires_at_utc) <= self.now:
            self.add_issue("expired", table, record_id, f"schedule run permit expired at {_utc(permit.expires_at_utc).isoformat()}")
            return None
        expires_at = self.bounded_expiry(permit.expires_at_utc, table=table, record_id=record_id)
        is_owner_permit = permit.person_id is not None and permit.schedule_id in self.system_work_schedule_ids
        try:
            token = schedule_permit_token(
                self._require_identities().scheduler,
                team=self.team_id,
                schedule_id=permit.schedule_id,
                issued=self.now,
                person_ref=person_ref,
                owner_permit=is_owner_permit,
                expires_at=expires_at,
            )
        except ValueError as exc:
            self.add_issue("unverifiable", table, record_id, f"could not sign converted schedule run permit: {exc}")
            return None
        updated = dict(raw)
        updated["permit"] = token
        self.counts.converted_schedule_runs += 1
        return updated


def _convert_json_tree(
    node: Any,
    *,
    ctx: _ConversionContext,
    table: str,
    record_id: str,
    task_id_hint: str | None = None,
    on_root_gateway_token: Callable[[dict[str, Any], _LegacyTaskContext], None] | None = None,
) -> tuple[Any, bool]:
    """Recursively convert any embedded legacy ``task_context`` or ``schedule_run`` inside JSON data."""

    if isinstance(node, list):
        changed = False
        new_list: list[Any] = []
        for idx, item in enumerate(node):
            item_task_hint = _extract_task_id(item) or task_id_hint
            converted, item_changed = _convert_json_tree(
                item,
                ctx=ctx,
                table=table,
                record_id=f"{record_id}[{idx}]",
                task_id_hint=item_task_hint,
                on_root_gateway_token=None,
            )
            new_list.append(converted)
            changed = changed or item_changed
        return (new_list, True) if changed else (node, False)

    if not isinstance(node, dict):
        return node, False

    local_task_hint = _extract_task_id(node) or task_id_hint
    changed = False
    result = dict(node)

    if "task_context" in result and _is_legacy_signed_context(result["task_context"]):
        new_token, legacy = ctx.convert_signed_context(
            result["task_context"],
            table=table,
            record_id=record_id,
            task_id_hint=local_task_hint,
        )
        result["task_context"] = new_token
        changed = True
        if new_token is not None and legacy is not None and on_root_gateway_token is not None:
            on_root_gateway_token(new_token, legacy)

    if "schedule_run" in result and _is_legacy_schedule_run(result["schedule_run"]):
        result["schedule_run"] = ctx.convert_schedule_run(
            result["schedule_run"],
            table=table,
            record_id=record_id,
        )
        changed = True

    for key, val in list(result.items()):
        if key in ("task_context", "schedule_run"):
            continue
        if isinstance(val, (dict, list)):
            converted, child_changed = _convert_json_tree(
                val,
                ctx=ctx,
                table=table,
                record_id=record_id,
                task_id_hint=local_task_hint,
                on_root_gateway_token=on_root_gateway_token if key in ("meta", "gateway_context") else None,
            )
            if child_changed:
                result[key] = converted
                changed = True

    return (result, True) if changed else (node, False)


def _extract_task_id(node: Any) -> str | None:
    if not isinstance(node, Mapping):
        return None
    direct = node.get("task_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for nested_key in ("task", "msg_content", "handed_back"):
        nested = node.get(nested_key)
        if isinstance(nested, Mapping):
            candidate = nested.get("task_id")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _is_system_work_schedule_doc(schedule_doc: Mapping[str, Any] | None) -> bool:
    if not isinstance(schedule_doc, Mapping):
        return False
    kind = str(schedule_doc.get("system_schedule_kind") or "").strip().lower()
    return kind in _SYSTEM_WORK_KINDS


def _load_system_work_schedule_ids(conn: Any, dialect: SqlDialect, team_id: str) -> frozenset[str]:
    if not _table_exists(conn, dialect, "scheduled_messages"):
        return frozenset()
    p = dialect.bind
    rows = conn.execute(
        f"SELECT schedule_id, schedule_json FROM scheduled_messages WHERE (claim_team_id = {p} OR claim_team_id IS NULL)",
        (team_id,),
    ).fetchall()
    ids: set[str] = set()
    for row in rows:
        schedule_id, schedule_json = _row_values(row)
        if not isinstance(schedule_json, str) or not schedule_json.strip():
            continue
        try:
            doc = json.loads(schedule_json)
        except ValueError:
            continue
        if _is_system_work_schedule_doc(doc):
            ids.add(str(schedule_id))
    return frozenset(ids)


# ---------------------------------------------------------------------------
# Per-table converters
# ---------------------------------------------------------------------------


def _convert_active_task_runtime(conn: Any, dialect: SqlDialect, ctx: _ConversionContext) -> None:
    p = dialect.bind
    if _table_exists(conn, dialect, "active_task_runtime"):
        rows = conn.execute(
            f"SELECT owner_agent_id, runtime_state_json FROM active_task_runtime "
            f"WHERE (team_id = {p} OR team_id IS NULL)",
            (ctx.team_id,),
        ).fetchall()
        for row in rows:
            owner_agent_id, raw_state = _row_values(row)
            if not _raw_json_has_legacy_payload(raw_state):
                continue
            doc = json.loads(raw_state)
            updated, changed = _convert_json_tree(
                doc, ctx=ctx, table="active_task_runtime", record_id=str(owner_agent_id)
            )
            if changed:
                conn.execute(
                    f"UPDATE active_task_runtime SET runtime_state_json = {p} WHERE owner_agent_id = {p}",
                    (canonical_json(updated), owner_agent_id),
                )

    if not _table_exists(conn, dialect, "active_task_runtime_records"):
        return
    if _table_exists(conn, dialect, "active_task_runtime"):
        rows = conn.execute(
            f"SELECT r.owner_agent_id, r.section, r.record_id, r.payload_json "
            f"FROM active_task_runtime_records r "
            f"WHERE NOT EXISTS (SELECT 1 FROM active_task_runtime s "
            f"WHERE s.owner_agent_id = r.owner_agent_id AND s.team_id IS NOT NULL AND s.team_id <> {p})",
            (ctx.team_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT owner_agent_id, section, record_id, payload_json FROM active_task_runtime_records"
        ).fetchall()
    for row in rows:
        owner_agent_id, section, record_id, raw_payload = _row_values(row)
        if not _raw_json_has_legacy_payload(raw_payload):
            continue
        doc = json.loads(raw_payload)
        is_root_task = (
            section == "tasks"
            and isinstance(doc, dict)
            and not (doc.get("task") or {}).get("parent_task_id")
            and bool(doc.get("owner_person_id"))
        )
        owner_person_id = str(doc.get("owner_person_id") or "") if is_root_task else ""

        def _capture_ledger_digest(
            new_token: dict[str, Any],
            legacy: _LegacyTaskContext,
            *,
            _owner: str = owner_person_id,
        ) -> None:
            if legacy.issuer == "gateway" and legacy.schedule_id is None and _owner and legacy.person_id == _owner:
                token_json = AuthorityTokenRecord.model_validate(new_token).model_dump_json()
                digest = hashlib.sha256(token_json.encode("utf-8")).hexdigest()
                ctx.ledger_backfills.append((ctx.team_id, legacy.context_id, _owner, digest))

        updated, changed = _convert_json_tree(
            doc,
            ctx=ctx,
            table=f"active_task_runtime_records:{section}",
            record_id=str(record_id),
            task_id_hint=str(record_id) if section == "tasks" else None,
            on_root_gateway_token=_capture_ledger_digest if is_root_task else None,
        )
        if changed:
            serialized = canonical_json(updated)
            conn.execute(
                f"UPDATE active_task_runtime_records SET payload_json = {p}, payload_digest = {p} "
                f"WHERE owner_agent_id = {p} AND section = {p} AND record_id = {p}",
                (serialized, payload_digest(serialized), owner_agent_id, section, record_id),
            )


def _apply_ledger_backfills(conn: Any, dialect: SqlDialect, ctx: _ConversionContext) -> None:
    """Backfill ``change_requests.token_digest`` for converted root tasks (C26)."""

    if not ctx.ledger_backfills or not _table_exists(conn, dialect, "change_requests"):
        return
    ensure_column(conn, "change_requests", "token_digest", "TEXT NOT NULL DEFAULT ''", logger=LOGGER)
    p = dialect.bind
    for team_id, request_id, person_id, digest in ctx.ledger_backfills:
        cursor = conn.execute(
            f"UPDATE change_requests SET token_digest = {p} "
            f"WHERE team_id = {p} AND request_id = {p} AND person_id = {p}",
            (digest, team_id, request_id, person_id),
        )
        if getattr(cursor, "rowcount", 0) and cursor.rowcount > 0:
            ctx.counts.ledger_digests_backfilled += int(cursor.rowcount)


def _convert_retired_task_runtime(conn: Any, dialect: SqlDialect, ctx: _ConversionContext) -> None:
    if not _table_exists(conn, dialect, "retired_task_runtime"):
        return
    p = dialect.bind
    rows = conn.execute(
        f"SELECT archive_id, runtime_state_json, records_json FROM retired_task_runtime "
        f"WHERE (team_id = {p} OR team_id IS NULL)",
        (ctx.team_id,),
    ).fetchall()
    for row in rows:
        archive_id, raw_state, raw_records = _row_values(row)
        state_changed = False
        records_changed = False
        new_state = raw_state
        new_records = raw_records
        if _raw_json_has_legacy_payload(raw_state):
            updated, state_changed = _convert_json_tree(
                json.loads(raw_state), ctx=ctx, table="retired_task_runtime", record_id=str(archive_id)
            )
            if state_changed:
                new_state = canonical_json(updated)
        if _raw_json_has_legacy_payload(raw_records):
            updated, records_changed = _convert_json_tree(
                json.loads(raw_records), ctx=ctx, table="retired_task_runtime", record_id=str(archive_id)
            )
            if records_changed:
                new_records = canonical_json(updated)
        if state_changed or records_changed:
            conn.execute(
                f"UPDATE retired_task_runtime SET runtime_state_json = {p}, records_json = {p} WHERE archive_id = {p}",
                (new_state, new_records, archive_id),
            )


def _convert_simple_json_table(
    conn: Any,
    dialect: SqlDialect,
    ctx: _ConversionContext,
    *,
    table: str,
    pk_column: str,
    json_column: str,
    team_column: str | None = None,
) -> None:
    if not _table_exists(conn, dialect, table):
        return
    present = set(table_column_names(conn, table))
    if pk_column not in present or json_column not in present:
        return
    p = dialect.bind
    if team_column and team_column in present:
        rows = conn.execute(
            f"SELECT {pk_column}, {json_column} FROM {table} WHERE ({team_column} = {p} OR {team_column} IS NULL)",
            (ctx.team_id,),
        ).fetchall()
    else:
        rows = conn.execute(f"SELECT {pk_column}, {json_column} FROM {table}").fetchall()
    for row in rows:
        pk_value, raw_json = _row_values(row)
        if not _raw_json_has_legacy_payload(raw_json):
            continue
        doc = json.loads(raw_json)
        updated, changed = _convert_json_tree(doc, ctx=ctx, table=table, record_id=str(pk_value))
        if changed:
            conn.execute(
                f"UPDATE {table} SET {json_column} = {p} WHERE {pk_column} = {p}",
                (json.dumps(updated), pk_value),
            )


def _convert_conversation_task_links(conn: Any, dialect: SqlDialect, ctx: _ConversionContext) -> None:
    if not _table_exists(conn, dialect, "conversation_task_links"):
        return
    present = set(table_column_names(conn, "conversation_task_links"))
    if not {"conversation_id", "task_id", "gateway_context_json"} <= present:
        return
    p = dialect.bind
    rows = conn.execute(
        "SELECT conversation_id, task_id, gateway_context_json FROM conversation_task_links"
    ).fetchall()
    for row in rows:
        conversation_id, task_id, raw_json = _row_values(row)
        if not _raw_json_has_legacy_payload(raw_json):
            continue
        doc = json.loads(raw_json)
        updated, changed = _convert_json_tree(
            doc,
            ctx=ctx,
            table="conversation_task_links",
            record_id=f"{conversation_id}:{task_id}",
            task_id_hint=str(task_id),
        )
        if changed:
            conn.execute(
                f"UPDATE conversation_task_links SET gateway_context_json = {p} "
                f"WHERE conversation_id = {p} AND task_id = {p}",
                (json.dumps(updated), conversation_id, task_id),
            )


def _convert_person_held_rows(conn: Any, dialect: SqlDialect, ctx: _ConversionContext) -> None:
    if not _table_exists(conn, dialect, "person_held_rows"):
        return
    p = dialect.bind
    rows = conn.execute(
        f"SELECT held_row_id, source_table, row_json FROM person_held_rows WHERE team_id = {p}",
        (ctx.team_id,),
    ).fetchall()
    for row in rows:
        held_row_id, source_table, raw_row_json = _row_values(row)
        if not raw_row_json or '"key_id"' not in raw_row_json:
            continue
        try:
            row_doc = json.loads(raw_row_json)
        except ValueError:
            continue
        if not isinstance(row_doc, dict):
            continue
        changed = False
        for col, val in list(row_doc.items()):
            if isinstance(val, str) and _raw_json_has_legacy_payload(val):
                updated, col_changed = _convert_json_tree(
                    json.loads(val),
                    ctx=ctx,
                    table=f"person_held_rows:{source_table}",
                    record_id=str(held_row_id),
                )
                if col_changed:
                    row_doc[col] = json.dumps(updated)
                    changed = True
            elif isinstance(val, (dict, list)) and _contains_legacy_payload(val):
                updated, col_changed = _convert_json_tree(
                    val,
                    ctx=ctx,
                    table=f"person_held_rows:{source_table}",
                    record_id=str(held_row_id),
                )
                if col_changed:
                    row_doc[col] = updated
                    changed = True
        if changed:
            conn.execute(
                f"UPDATE person_held_rows SET row_json = {p} WHERE held_row_id = {p}",
                (json.dumps(row_doc), held_row_id),
            )


def _convert_scheduled_messages(conn: Any, dialect: SqlDialect, ctx: _ConversionContext) -> None:
    if not _table_exists(conn, dialect, "scheduled_messages"):
        return
    ensure_column(conn, "scheduled_messages", "owner_vouched", "INTEGER NOT NULL DEFAULT 0", logger=LOGGER)
    ensure_column(conn, "scheduled_messages", "owner_permit_json", "TEXT", logger=LOGGER)
    ensure_column(conn, "scheduled_messages", "run_permit_refusal", "TEXT", logger=LOGGER)
    ensure_column(conn, "scheduled_messages", "delivery_ref", "TEXT", logger=LOGGER)

    p = dialect.bind
    rows = conn.execute(
        f"SELECT schedule_id, schedule_name, owner_canonical_user_id, enabled, schedule_json, payload_json, "
        f"run_permit_json, owner_permit_json, owner_vouched "
        f"FROM scheduled_messages WHERE (claim_team_id = {p} OR claim_team_id IS NULL)",
        (ctx.team_id,),
    ).fetchall()
    for row in rows:
        _convert_one_schedule_row(conn, dialect, ctx, row)


def _convert_one_schedule_row(conn: Any, dialect: SqlDialect, ctx: _ConversionContext, row: Any) -> None:
    (
        schedule_id,
        schedule_name,
        owner_id,
        enabled,
        raw_schedule_json,
        raw_payload_json,
        raw_run_permit_json,
        raw_owner_permit_json,
        existing_vouched,
    ) = _row_values(row)
    schedule_id = str(schedule_id)
    schedule_label = f"schedule_name={schedule_name or '-'} owner={owner_id or '-'}"

    has_legacy_sched = _raw_json_has_legacy_payload(raw_schedule_json)
    has_legacy_payload = _raw_json_has_legacy_payload(raw_payload_json)
    has_legacy_permit = _raw_json_has_legacy_payload(raw_run_permit_json)
    if not (has_legacy_sched or has_legacy_payload or has_legacy_permit):
        return

    sched_doc = json.loads(raw_schedule_json) if isinstance(raw_schedule_json, str) and raw_schedule_json.strip() else None
    is_system_work = _is_system_work_schedule_doc(sched_doc)

    vouched_by_context = False
    vouched_by_permit = False
    context_unverifiable = False
    permit_unverifiable = False

    new_schedule_json = raw_schedule_json
    verified_context_person_id: str | None = None
    if has_legacy_sched and isinstance(sched_doc, dict):
        sched_doc, vouched_by_context, context_unverifiable, verified_context_person_id = _convert_schedule_doc(
            sched_doc,
            ctx=ctx,
            schedule_id=schedule_id,
            owner_id=owner_id,
            schedule_label=schedule_label,
        )
        new_schedule_json = json.dumps(sched_doc)

    new_payload_json = raw_payload_json
    if has_legacy_payload:
        updated_payload, _ = _convert_json_tree(
            json.loads(raw_payload_json),
            ctx=ctx,
            table="scheduled_messages:payload",
            record_id=schedule_id,
        )
        new_payload_json = json.dumps(updated_payload)

    new_run_permit_dict: dict[str, Any] | None = None
    new_run_permit_expires: datetime | None = None
    permit_expired = False
    verified_permit_person_id: str | None = None
    if has_legacy_permit:
        raw_permit_obj = json.loads(raw_run_permit_json)
        (
            new_run_permit_dict,
            new_run_permit_expires,
            vouched_by_permit,
            permit_expired,
            permit_unverifiable,
            verified_permit_person_id,
        ) = _convert_schedule_row_permit(
            raw_permit_obj,
            ctx=ctx,
            schedule_id=schedule_id,
            owner_id=owner_id,
            schedule_label=schedule_label,
        )

    effective_owner_id = owner_id or verified_context_person_id or verified_permit_person_id
    final_vouched = bool(existing_vouched)
    new_owner_permit_dict: dict[str, Any] | None = None
    if context_unverifiable:
        final_vouched = False
        new_run_permit_dict = None
        new_owner_permit_dict = None
    elif vouched_by_context or vouched_by_permit:
        final_vouched = True
        ctx.counts.vouched_schedules += 1
        scheduler_id = ctx._require_identities().scheduler
        owner_person = ctx.people.get(str(effective_owner_id)) if effective_owner_id else None
        if is_system_work:
            if new_run_permit_dict is None and enabled:
                new_run_permit_dict = schedule_permit_token(
                    scheduler_id,
                    team=ctx.team_id,
                    schedule_id=schedule_id,
                    issued=ctx.now,
                    person_ref=None,
                    owner_permit=False,
                )
                new_run_permit_expires = ctx.now + timedelta(seconds=AUTHORITY_TOKEN_MAX_SECONDS)
                ctx.counts.fresh_permits_issued += 1
            if owner_person is not None and owner_person.standing is PersonStanding.ACTIVE and not raw_owner_permit_json:
                new_owner_permit_dict = schedule_permit_token(
                    scheduler_id,
                    team=ctx.team_id,
                    schedule_id=schedule_id,
                    issued=ctx.now,
                    person_ref=owner_person.person_ref,
                    owner_permit=True,
                )
        elif new_run_permit_dict is None and owner_person is not None and owner_person.standing is PersonStanding.ACTIVE:
            new_run_permit_dict = schedule_permit_token(
                scheduler_id,
                team=ctx.team_id,
                schedule_id=schedule_id,
                issued=ctx.now,
                person_ref=owner_person.person_ref,
                owner_permit=False,
            )
            new_run_permit_expires = ctx.now + timedelta(seconds=AUTHORITY_TOKEN_MAX_SECONDS)
            ctx.counts.fresh_permits_issued += 1
    elif permit_unverifiable:
        final_vouched = False
        new_run_permit_dict = None
        new_owner_permit_dict = None
    elif permit_expired and is_system_work and enabled:
        new_run_permit_dict = schedule_permit_token(
            ctx._require_identities().scheduler,
            team=ctx.team_id,
            schedule_id=schedule_id,
            issued=ctx.now,
            person_ref=None,
            owner_permit=False,
        )
        new_run_permit_expires = ctx.now + timedelta(seconds=AUTHORITY_TOKEN_MAX_SECONDS)
        ctx.counts.fresh_permits_issued += 1
    elif permit_expired:
        ctx.add_issue("expired", "scheduled_messages", schedule_id, "stored schedule permit was already expired", schedule_label)

    new_run_permit_json = json.dumps(new_run_permit_dict) if new_run_permit_dict is not None else None
    new_owner_permit_json = (
        json.dumps(new_owner_permit_dict)
        if new_owner_permit_dict is not None
        else (None if context_unverifiable or permit_unverifiable else raw_owner_permit_json)
    )
    recheck_at = (
        max(ctx.now, new_run_permit_expires - _PERMIT_RENEW_LEAD).isoformat()
        if new_run_permit_expires is not None
        else None
    )
    run_permit_owner = effective_owner_id if new_run_permit_json is not None and not is_system_work else None

    p = dialect.bind
    conn.execute(
        f"UPDATE scheduled_messages "
        f"SET schedule_json = {p}, payload_json = {p}, run_permit_json = {p}, owner_permit_json = {p}, "
        f"owner_vouched = {p}, run_permit_owner = {p}, run_permit_recheck_at = {p}, run_permit_refusal = NULL "
        f"WHERE schedule_id = {p}",
        (
            new_schedule_json,
            new_payload_json,
            new_run_permit_json,
            new_owner_permit_json,
            1 if final_vouched else 0,
            run_permit_owner,
            recheck_at,
            schedule_id,
        ),
    )


def _convert_schedule_doc(
    sched_doc: dict[str, Any],
    *,
    ctx: _ConversionContext,
    schedule_id: str,
    owner_id: str | None,
    schedule_label: str,
) -> tuple[dict[str, Any], bool, bool, str | None]:
    """Convert saved gateway ``task_context`` inside ``scheduled_messages.schedule_json``.

    Per plan section 5, a schedule's saved gateway context proves vouching with
    ``check_expiry=False`` (so even an expired saved context vouches the schedule).
    Returns ``(updated_doc, vouched, unverifiable, verified_person_id)``.
    """

    vouched = False
    unverifiable = False
    verified_person_id: str | None = None
    meta = sched_doc.get("payload_meta")
    gw = meta.get("gateway_context") if isinstance(meta, dict) else None
    if isinstance(gw, dict) and _is_legacy_signed_context(gw.get("task_context")):
        raw_tc = gw["task_context"]
        try:
            legacy = ctx.keyring.verify_context(raw_tc, team_id=ctx.team_id)
        except _LegacyVerifyError as exc:
            ctx.add_issue("unverifiable", "scheduled_messages", schedule_id, str(exc), schedule_label)
            gw["task_context"] = None
            unverifiable = True
        else:
            if legacy.issuer != "gateway":
                ctx.add_issue(
                    "unverifiable",
                    "scheduled_messages",
                    schedule_id,
                    f"saved schedule context was issued by {legacy.issuer!r}, expected 'gateway'",
                    schedule_label,
                )
                gw["task_context"] = None
                unverifiable = True
            elif owner_id and legacy.person_id != owner_id:
                ctx.add_issue(
                    "unverifiable",
                    "scheduled_messages",
                    schedule_id,
                    f"saved schedule context names person {legacy.person_id!r}, expected owner {owner_id!r}",
                    schedule_label,
                )
                gw["task_context"] = None
                unverifiable = True
            else:
                person_ref, person_err = ctx.resolve_person_ref(legacy.person_id)
                if person_err is not None or person_ref is None:
                    ctx.add_issue(
                        "unverifiable",
                        "scheduled_messages",
                        schedule_id,
                        person_err or "saved schedule context names no person",
                        schedule_label,
                    )
                    gw["task_context"] = None
                    unverifiable = True
                else:
                    vouched = True
                    verified_person_id = legacy.person_id
                    if _utc(legacy.expires_at_utc) > ctx.now:
                        expires_at = ctx.bounded_expiry(
                            legacy.expires_at_utc, table="scheduled_messages", record_id=schedule_id
                        )
                        gw["task_context"] = root_task_token(
                            ctx._require_identities().gateway,
                            team=ctx.team_id,
                            root_task_id=legacy.root_task_id or legacy.context_id,
                            request_id=legacy.context_id,
                            issued=ctx.now,
                            person_ref=person_ref,
                            expires_at=expires_at,
                        )
                        ctx.counts.converted_contexts += 1
                    else:
                        # Expired saved gateway context still proves vouching (owner_vouched=1),
                        # but we never sign an already-expired protocol 1 token.
                        gw["task_context"] = None

    if isinstance(gw, dict) and _is_legacy_schedule_run(gw.get("schedule_run")):
        gw["schedule_run"] = None

    return sched_doc, vouched, unverifiable, verified_person_id


def _convert_schedule_row_permit(
    raw_permit: Any,
    *,
    ctx: _ConversionContext,
    schedule_id: str,
    owner_id: str | None,
    schedule_label: str,
) -> tuple[dict[str, Any] | None, datetime | None, bool, bool, bool, str | None]:
    """Convert ``scheduled_messages.run_permit_json``.

    Returns ``(new_token, new_expires, vouched_by_permit, expired, unverifiable, verified_person_id)``.
    """

    if not _is_legacy_signed_permit(raw_permit):
        return None, None, False, False, False, None
    try:
        permit = ctx.keyring.verify_permit(raw_permit, team_id=ctx.team_id, expected_schedule_id=schedule_id)
    except _LegacyVerifyError as exc:
        ctx.add_issue("unverifiable", "scheduled_messages", schedule_id, str(exc), schedule_label)
        return None, None, False, False, True, None
    if permit.person_id is not None and owner_id and permit.person_id != owner_id:
        ctx.add_issue(
            "unverifiable",
            "scheduled_messages",
            schedule_id,
            f"stored schedule permit names person {permit.person_id!r}, expected owner {owner_id!r}",
            schedule_label,
        )
        return None, None, False, False, True, None
    person_ref, person_err = ctx.resolve_person_ref(permit.person_id)
    if person_err is not None:
        ctx.add_issue("unverifiable", "scheduled_messages", schedule_id, person_err, schedule_label)
        return None, None, False, False, True, None
    vouched = permit.person_id is not None
    if _utc(permit.expires_at_utc) <= ctx.now:
        return None, None, vouched, True, False, permit.person_id
    expires_at = ctx.bounded_expiry(permit.expires_at_utc, table="scheduled_messages", record_id=schedule_id)
    token = schedule_permit_token(
        ctx._require_identities().scheduler,
        team=ctx.team_id,
        schedule_id=schedule_id,
        issued=ctx.now,
        person_ref=person_ref,
        owner_permit=False,
        expires_at=expires_at,
    )
    ctx.counts.converted_permits += 1
    return token, expires_at, vouched, False, False, permit.person_id


# ---------------------------------------------------------------------------
# Reporting (logs + durable administrator Access Inbox notice) & marker
# ---------------------------------------------------------------------------


def _report_issues(
    manager_storage: StorageSettings | None,
    *,
    team_id: str,
    issues: Sequence[ConversionIssue],
    now: datetime,
) -> bool:
    if not issues:
        return False
    unverifiable = [item for item in issues if item.category == "unverifiable"]
    expired = [item for item in issues if item.category == "expired"]

    if unverifiable:
        LOGGER.warning(
            "Stored 0.9 contexts or permits could not be verified and were cleared (rows preserved). "
            "event=authority_token_conversion_unverifiable team_id=%s count=%d records=%s",
            team_id,
            len(unverifiable),
            "; ".join(f"{i.table}:{i.record_id} ({i.reason})" for i in unverifiable[:25]),
        )
    if expired:
        LOGGER.warning(
            "Stored 0.9 contexts or permits were already expired and were not converted (rows preserved). "
            "event=authority_token_conversion_expired team_id=%s count=%d records=%s",
            team_id,
            len(expired),
            "; ".join(f"{i.table}:{i.record_id} ({i.reason})" for i in expired[:25]),
        )

    if manager_storage is None:
        raise RuntimeError(
            "Could not store the authority-token conversion notice for administrators: "
            "manager storage is not configured."
        )

    summary_lines: list[str] = [
        "Some stored 0.9 task contexts or schedule permits could not be converted into protocol 1 authority tokens "
        "during the first start of 0.10. Affected rows were kept intact with their legacy credentials cleared so "
        "nothing acts for a person without verified authority."
    ]
    if unverifiable:
        summary_lines.append("")
        summary_lines.append(f"Unverifiable records ({len(unverifiable)}):")
        for item in unverifiable[:30]:
            extra = f" [{item.detail}]" if item.detail else ""
            summary_lines.append(f"- {item.table} {item.record_id}{extra}: {item.reason}")
        if len(unverifiable) > 30:
            summary_lines.append(f"- ... and {len(unverifiable) - 30} more (see startup logs).")
        if any(item.table.startswith("scheduled_messages") for item in unverifiable):
            summary_lines.append(
                "Next steps for unvouched schedules: system schedules re-vouch automatically when the gateway "
                "refreshes them; user schedules must be recreated or re-installed by their owner or an administrator."
            )
    if expired:
        summary_lines.append("")
        summary_lines.append(f"Expired, not converted ({len(expired)}):")
        for item in expired[:30]:
            extra = f" [{item.detail}]" if item.detail else ""
            summary_lines.append(f"- {item.table} {item.record_id}{extra}: {item.reason}")
        if len(expired) > 30:
            summary_lines.append(f"- ... and {len(expired) - 30} more (see startup logs).")

    fingerprint = hashlib.sha256(
        json.dumps([(i.category, i.table, i.record_id, i.reason) for i in issues], sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    title = (
        f"Authority token upgrade: {len(unverifiable)} unverifiable, {len(expired)} expired record(s)"
        if unverifiable and expired
        else (
            f"Authority token upgrade: {len(unverifiable)} unverifiable record(s) need attention"
            if unverifiable
            else f"Authority token upgrade: {len(expired)} expired record(s) were not converted"
        )
    )
    notice = AccessNoticeV1(
        notice_id=f"authority_token_upgrade:{team_id}:{fingerprint}"[:120],
        team_id=team_id,
        kind=AccessNoticeKind.AUTHORITY_TOKEN_UPGRADE,
        recipient_roles=(PERSON_ROLE_ADMIN,),
        title=title[:400],
        summary="\n".join(summary_lines)[:4000],
        created_at_utc=now,
    )
    try:
        AccessRepository(manager_storage).create_notice_if_absent(notice)
        return True
    except Exception as exc:
        LOGGER.exception(
            "Could not store the authority-token conversion notice for administrators; keeping the pending "
            "report for the next start. event=authority_token_conversion_notice_failed team_id=%s",
            team_id,
        )
        raise RuntimeError(
            f"Could not store the authority-token conversion notice for administrators ({exc}); "
            "the pending report was kept so the next start will retry."
        ) from exc


def _issue_key(issue: ConversionIssue) -> tuple[str, str, str, str, str]:
    return (issue.category, issue.table, issue.record_id, issue.reason, issue.detail)


def _merge_issues(
    existing: Sequence[ConversionIssue], new: Sequence[ConversionIssue]
) -> list[ConversionIssue]:
    seen: set[tuple[str, str, str, str, str]] = set()
    merged: list[ConversionIssue] = []
    for item in (*existing, *new):
        key = _issue_key(item)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def _load_pending_report(path: Path, team_id: str) -> list[ConversionIssue]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return []
    if not isinstance(body, dict) or body.get("team_id") != team_id:
        return []
    raw_issues = body.get("issues")
    if not isinstance(raw_issues, list):
        return []
    loaded: list[ConversionIssue] = []
    for item in raw_issues:
        if not isinstance(item, Mapping):
            continue
        category = item.get("category")
        if category not in ("unverifiable", "expired"):
            continue
        loaded.append(
            ConversionIssue(
                category=category,
                table=str(item.get("table") or ""),
                record_id=str(item.get("record_id") or ""),
                reason=str(item.get("reason") or ""),
                detail=str(item.get("detail") or ""),
            )
        )
    return loaded


def _write_pending_report(
    path: Path, *, team_id: str, issues: Sequence[ConversionIssue], now: datetime
) -> None:
    """Persist cleared-record issues atomically (fsync + replace) before committing any database write."""

    from id_generation import new_id_suffix

    body = {
        "team_id": team_id,
        "recorded_at": now.isoformat(),
        "issues": [
            {
                "category": item.category,
                "table": item.table,
                "record_id": item.record_id,
                "reason": item.reason,
                "detail": item.detail,
            }
            for item in issues
        ],
    }
    content = (json.dumps(body, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{new_id_suffix()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_pending_report(path: Path) -> None:
    path.unlink(missing_ok=True)


def has_pending_authority_token_report(config: "GatewayInstanceConfig") -> bool:
    """Whether an earlier conversion attempt saved cleared-record issues whose notice is still undelivered."""

    return bool(
        _load_pending_report(
            config.layout.authority_tokens_pending_report_path,
            str(config.manager.team_id),
        )
    )


def _marker_matches_team(path: Path, team_id: str) -> bool:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return False
    return isinstance(body, dict) and body.get("team_id") == team_id


def _write_marker(path: Path, *, team_id: str, counts: ConversionCounts, now: datetime) -> None:
    from custom_agents.governance.authorized_operations import atomic_write_bytes
    from release_version import release_version

    body = {
        "upgraded_at": now.isoformat(),
        "version": release_version(),
        "team_id": team_id,
        "counts": counts.as_dict(),
    }
    atomic_write_bytes(path, (json.dumps(body, indent=2) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# Live host certificate acquisition (installation-authority)
# ---------------------------------------------------------------------------


async def _acquire_signing_identities_async(config: "GatewayInstanceConfig") -> SigningIdentities:
    from assistant_gateway.message_host import GATEWAY_RUNTIME, gateway_profile
    from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
    from custom_transport.protocol1.host_runtime import MessageHost, TEAM_SERVICES_RUNTIME
    from custom_transport.subjects import NatsSubjectMapper
    from team_services.process import team_services_profile

    team_id = str(config.manager.team_id)
    transport = config.transport
    servers = list(transport.nats_servers or ([transport.nats_url] if transport.nats_url else []))

    async def _join_one(runtime_name: str, profile: Any) -> HostIdentity:
        cfg = NatsTransportConfig(
            servers=servers,
            name=f"{config.instance_name}-authority-upgrade-{runtime_name}",
            subject_prefix=transport.subject_prefix,
            jetstream_enabled=False,
            user=transport.nats_user,
            password=transport.nats_password,
            credentials_file=transport.credentials_file,
            reply_inbox_runtime=runtime_name,
        )
        adapter = NatsTransportAdapter(config=cfg, mapper=NatsSubjectMapper(team_id=team_id), logger=LOGGER)
        await adapter.connect()
        try:
            host = MessageHost(config.layout, profile, adapter.nats_connection, logger=LOGGER)
            live = await host.start(report=False)
            return live.identity
        finally:
            with contextlib.suppress(Exception):
                await adapter.close()

    gateway_identity = await _join_one(GATEWAY_RUNTIME, gateway_profile(team_id))
    scheduler_identity = await _join_one(TEAM_SERVICES_RUNTIME, team_services_profile(team_id))
    return SigningIdentities(gateway=gateway_identity, scheduler=scheduler_identity)


def acquire_signing_identities(config: "GatewayInstanceConfig") -> SigningIdentities:
    """Join ``installation-authority`` briefly as ``gateway`` and ``team-services`` to obtain their active certificates."""

    return asyncio.run(_acquire_signing_identities_async(config))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def authority_tokens_upgrade_done(config: "GatewayInstanceConfig") -> bool:
    """Whether ``InstanceLayout.authority_tokens_upgrade_path`` records completion for the current team."""

    return _marker_matches_team(config.layout.authority_tokens_upgrade_path, str(config.manager.team_id))


def upgrade_authority_tokens(
    config: "GatewayInstanceConfig",
    *,
    env: Mapping[str, str] | None = None,
    allow_unverifiable: bool = False,
    identities: SigningIdentities | None = None,
    acquire_identities: Callable[["GatewayInstanceConfig"], SigningIdentities] = acquire_signing_identities,
    now: datetime | None = None,
) -> AuthorityTokenUpgradeResult:
    """Run the one-time first-start conversion of stored 0.9 signed contexts and permits.

    Idempotent: once ``InstanceLayout.authority_tokens_upgrade_path`` records the current
    team id and no pending report remains, a second start returns ``already_done=True``
    without touching any store or NATS.
    """

    team_id = str(config.manager.team_id)
    marker_path = config.layout.authority_tokens_upgrade_path
    pending_path = config.layout.authority_tokens_pending_report_path
    moment = _utc(now) if now is not None else datetime.now(timezone.utc)
    source_env = os.environ if env is None else env

    pending_issues = _load_pending_report(pending_path, team_id)
    if _marker_matches_team(marker_path, team_id) and not pending_issues:
        return AuthorityTokenUpgradeResult(already_done=True)

    manager_storage, _ = _resolve_storages(config, env=source_env)
    had_legacy = has_legacy_authority_records(config, env=source_env)
    if not had_legacy:
        if pending_issues:
            # A previous conversion committed cleared rows before crashing or failing to store
            # the Access Inbox notice. Deliver that durable report before writing the marker.
            notice_stored = _report_issues(
                manager_storage, team_id=team_id, issues=pending_issues, now=moment
            )
            _remove_pending_report(pending_path)
            pending_counts = ConversionCounts(
                expired=sum(1 for item in pending_issues if item.category == "expired"),
                unverifiable=sum(1 for item in pending_issues if item.category == "unverifiable"),
            )
            _write_marker(marker_path, team_id=team_id, counts=pending_counts, now=moment)
            still_set = tuple(present_removed_signing_key_env_vars(source_env))
            if still_set:
                LOGGER.warning(
                    "Finished reporting converted 0.9 records. Remove these obsolete signing-key variables "
                    "from team.env or Docker secrets before the next restart (future starts will refuse "
                    "them): %s. event=authority_token_conversion_remove_old_keys variables=%s",
                    ", ".join(still_set),
                    ",".join(still_set),
                )
            LOGGER.info(
                "Delivered pending authority-token conversion report and recorded upgrade complete. "
                "event=authority_tokens_upgraded team_id=%s had_legacy=true expired=%d unverifiable=%d",
                team_id,
                pending_counts.expired,
                pending_counts.unverifiable,
            )
            return AuthorityTokenUpgradeResult(
                already_done=False,
                had_legacy_records=True,
                counts=pending_counts,
                issues=tuple(pending_issues),
                notice_stored=notice_stored,
                removed_keys_still_set=still_set,
            )
        # First start without the marker and no legacy rows to convert: write the marker,
        # warn if any obsolete 0.9 signing-key variable is still set (refused from the next
        # start on), and leave NATS untouched.
        _write_marker(marker_path, team_id=team_id, counts=ConversionCounts(), now=moment)
        still_set = tuple(present_removed_signing_key_env_vars(source_env))
        if still_set:
            LOGGER.warning(
                "No legacy 0.9 contexts or permits needed conversion. Remove these obsolete "
                "signing-key variables from team.env or Docker secrets before the next restart "
                "(future starts will refuse them): %s. "
                "event=authority_token_conversion_remove_old_keys variables=%s",
                ", ".join(still_set),
                ",".join(still_set),
            )
        LOGGER.info(
            "No legacy 0.9 signed contexts or permits found; recorded authority-token upgrade complete. "
            "event=authority_tokens_upgraded team_id=%s had_legacy=false",
            team_id,
        )
        return AuthorityTokenUpgradeResult(
            already_done=False,
            had_legacy_records=False,
            removed_keys_still_set=still_set,
        )

    keyring = _LegacyKeyring.from_env(source_env)
    if keyring.is_empty and not allow_unverifiable:
        raise MissingLegacyVerifyKeysError(
            "Stored 0.9 signed task contexts or schedule permits need one-time conversion to protocol 1 authority "
            "tokens, but none of ACCESS_CONTEXT_VERIFY_KEYS, ACCESS_GATEWAY_SIGNING_KEY, or "
            "ACCESS_SCHEDULER_SIGNING_KEY (or their _FILE variants) are set. Keep or restore the 0.9 keys for this "
            "first 0.10 start so stored records can be verified and re-signed (and remove them after the first start "
            "finishes). If the old keys were permanently lost, start once with --allow-unverifiable-authority-tokens "
            f"(or run `assistant_cli.py upgrade authority-tokens --config {config.config_path} --allow-unverifiable` "
            "while the instance is stopped) to clear unverifiable contexts and report them to administrators."
        )

    # Without any 0.9 verification key, no legacy record can verify or be re-signed, so no live
    # installation-authority join is needed (allowing offline CLI recovery as well as the launcher flag).
    if identities is not None:
        signing_identities: SigningIdentities | None = identities
    elif keyring.is_empty and allow_unverifiable:
        signing_identities = None
    else:
        signing_identities = acquire_identities(config)

    # Ensure every 0.9 access_people row has a persistent person_ref before converting.
    people: dict[str, TeamPersonV1] = {}
    if manager_storage is not None and _open_connection_if_exists(manager_storage) is not None:
        access_repo = AccessRepository(manager_storage)
        people = {person.person_id: person for person in access_repo.team_people(team_id)}

    system_work_ids: frozenset[str] = frozenset()
    if manager_storage is not None:
        connect_mgr = _open_connection_if_exists(manager_storage)
        if connect_mgr is not None:
            with connect_mgr() as conn:
                system_work_ids = _load_system_work_schedule_ids(conn, _dialect_for(manager_storage), team_id)

    ctx = _ConversionContext(
        team_id=team_id,
        now=moment,
        keyring=keyring,
        identities=signing_identities,
        people=people,
        system_work_schedule_ids=system_work_ids,
    )

    for role, storage in _distinct_storages(config, env=source_env):
        connect = _open_connection_if_exists(storage)
        if connect is None:
            continue
        dialect = _dialect_for(storage)
        with connect() as conn:
            if role == "manager":
                _convert_active_task_runtime(conn, dialect, ctx)
                _apply_ledger_backfills(conn, dialect, ctx)
                _convert_retired_task_runtime(conn, dialect, ctx)
                _convert_simple_json_table(
                    conn, dialect, ctx, table="agent_deferred_messages", pk_column="owner_agent_id",
                    json_column="messages_json", team_column="team_id",
                )
                _convert_simple_json_table(
                    conn, dialect, ctx, table="worker_task_handbacks", pk_column="handback_id",
                    json_column="payload_json", team_column="team_id",
                )
                _convert_scheduled_messages(conn, dialect, ctx)
                _convert_simple_json_table(
                    conn, dialect, ctx, table="schedule_worker_bindings", pk_column="schedule_id",
                    json_column="pending_schedule_json", team_column="team_id",
                )
                _convert_conversation_task_links(conn, dialect, ctx)
                _convert_person_held_rows(conn, dialect, ctx)
            # Gateway tables may live in the same DB (PostgreSQL / shared SQLite) or a separate gateway DB.
            _convert_simple_json_table(
                conn, dialect, ctx, table="gateway_deliveries", pk_column="delivery_id", json_column="payload_json",
            )
            _convert_simple_json_table(
                conn, dialect, ctx, table="gateway_held_guidance", pk_column="correlation_id", json_column="steering_json",
            )
            # Persist cleared-record issues durably BEFORE committing so a crash or notice-store
            # error between commit and notice delivery can never lose the administrator report.
            merged_issues = _merge_issues(pending_issues, ctx.issues)
            if merged_issues:
                _write_pending_report(pending_path, team_id=team_id, issues=merged_issues, now=moment)
            conn.commit()

    all_issues = _merge_issues(pending_issues, ctx.issues)
    notice_stored = _report_issues(manager_storage, team_id=team_id, issues=all_issues, now=moment)
    if all_issues:
        _remove_pending_report(pending_path)
    _write_marker(marker_path, team_id=team_id, counts=ctx.counts, now=moment)

    still_set = tuple(present_removed_signing_key_env_vars(source_env))
    if still_set:
        LOGGER.warning(
            "Converted stored 0.9 contexts and permits to protocol 1 authority tokens. Remove these obsolete "
            "signing-key variables from team.env or Docker secrets before the next restart (future starts will "
            "refuse them): %s. event=authority_token_conversion_remove_old_keys variables=%s",
            ", ".join(still_set),
            ",".join(still_set),
        )

    LOGGER.info(
        "Authority-token first-start conversion completed. event=authority_tokens_upgraded "
        "team_id=%s converted_contexts=%d converted_schedule_runs=%d converted_permits=%d "
        "vouched_schedules=%d fresh_permits=%d ledger_digests=%d expired=%d unverifiable=%d",
        team_id,
        ctx.counts.converted_contexts,
        ctx.counts.converted_schedule_runs,
        ctx.counts.converted_permits,
        ctx.counts.vouched_schedules,
        ctx.counts.fresh_permits_issued,
        ctx.counts.ledger_digests_backfilled,
        ctx.counts.expired,
        ctx.counts.unverifiable,
    )
    return AuthorityTokenUpgradeResult(
        already_done=False,
        had_legacy_records=True,
        counts=ctx.counts,
        issues=tuple(all_issues),
        notice_stored=notice_stored,
        removed_keys_still_set=still_set,
    )


__all__ = [
    "AuthorityTokenUpgradeResult",
    "ConversionCounts",
    "ConversionIssue",
    "MissingLegacyVerifyKeysError",
    "SigningIdentities",
    "acquire_signing_identities",
    "authority_tokens_upgrade_done",
    "has_legacy_authority_records",
    "has_pending_authority_token_report",
    "upgrade_authority_tokens",
]
