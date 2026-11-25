"""Protocol 1 dead-letter set-aside path, +WPI hold helper, CRIT-1 sweep, and departure matching.

``contracts/messages/v1/README.md`` ("Dead letters"):
- A receiver derives each copy id deterministically from a receiver-local secret
  (domain-separated SHA-256 of its Ed25519 application private key) and the set-aside
  message's ``(stream, stream_sequence, stored_timestamp_ns)`` via HMAC-SHA256 (FA-8,
  C09). Redeliveries of the same message reuse the copy id so ``max_msgs_per_subject: 1``
  replaces a half-finished copy instead of leaving an orphan, while a recreated stream
  whose sequences restart at 1 gets a new copy id.
- Raw copies are published to ``ua.v1.<team>.<runtime>.dlq.raw.<copy>`` only up to
  ``MAX_FRAME_BYTES``; larger messages set ``raw.stored = false``. The signed
  ``dlq.notice`` carries identifiers only (no free text) and is published to
  ``ua.v1.<team>.<runtime>.dlq.notice.<copy>`` with ``UA-Kind``, ``UA-Id``, and
  ``Nats-Msg-Id``.
- When ``UA1_DLQ_<team>`` is full (JetStream error ``10077``), the receiver holds the
  refused message with ``+WPI`` (``msg.in_progress()``) within each ``ack_wait_seconds``,
  retrying with growing pauses, logging its ids once, and acknowledging the original only
  after both raw copy and notice are stored (A3, FA-2). ``DeadLetterHoldTracker`` keys
  holds by ``(stream, stream_sequence)`` so a redelivery updates the held message handle
  instead of starting a second hold (C07).
- ``sweep_exhausted_stream`` / ``sweep_exhausted_streams`` recover messages whose consumer
  exhausted ``max_deliveries`` without a set-aside (A4, CRIT-1) using the exact cutoff
  ``delivered.stream_seq`` when ``num_ack_pending == 0`` else ``ack_floor.stream_seq``,
  skipping empty streams via ``STREAM.INFO`` and consumers below ``first_seq`` (C08).
- ``raw_copy_names_person`` matches protocol 1 frames (``authority.person`` and any
  ``person_ref`` in ``body``) and the exact closed set of legacy ``MessageEnvelope``
  person fields (A6, C13).
"""

from __future__ import annotations

import asyncio
import base64
import calendar
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Collection, Final, Mapping, Optional, Sequence, TypeVar

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from nats.aio.msg import Msg
from nats.errors import Error as NatsError
from nats.js.errors import APIError
from pydantic import TypeAdapter, ValidationError

from custom_messages.wire.encoding import b64url
from custom_messages.wire.frame import HEADER_MSG_ID
from custom_messages.wire.kinds import (
    INSTALLATION_SCOPE,
    KINDS,
    MAX_FRAME_BYTES,
    TEMPORARY_REFUSALS,
    VERIFICATION_REFUSALS,
)
from custom_messages.wire.models import Kind, MessageId, ProtocolLabel, RuntimeId, ScopeId, TeamId
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.live import LiveHost
from custom_transport.send_barrier import before_send
from release_version import release_version

_LOGGER = logging.getLogger(__name__)

# Domain prefix for deriving each receiver's copy-id HMAC secret from its application private key.
_COPY_ID_SECRET_DOMAIN: Final = b"universal-agents/dlq-copy-id/v1\x00"

# JetStream API error codes handled by dead-letter storage and sweep calls.
JS_ERR_NO_MESSAGE_FOUND: Final = 10037
JS_ERR_STREAM_NOT_FOUND: Final = 10059
JS_ERR_MAX_BYTES_EXCEEDED: Final = 10077

# Contract-derived reasons allowed on dlq.notice (every permanent refusal plus the four runtime/upgrade reasons).
DLQ_EXTRA_REASONS: Final[frozenset[str]] = frozenset(
    {"handler_failed", "delivery_limit", "upgrade_drain", "upgrade_old_format"}
)
DLQ_REASONS: Final[frozenset[str]] = (
    (frozenset(VERIFICATION_REFUSALS) - frozenset(TEMPORARY_REFUSALS)) | DLQ_EXTRA_REASONS
)

# Minimum negative-ack delay for peer-installation revocations_not_current (README "Who sent it").
PEER_REVOCATIONS_NAK_DELAY_SECONDS: Final[float] = 60.0

# Safe limits for best-effort inspection of untrusted raw message bytes.
_MAX_UNTRUSTED_PARSE_BYTES: Final = MAX_FRAME_BYTES * 2
# Any JSON document up to _MAX_UNTRUSTED_PARSE_BYTES has at most half that many values; cap >= bytes covers all valid inputs (C17).
_MAX_WALK_NODES: Final = _MAX_UNTRUSTED_PARSE_BYTES
_MAX_REQUEST_PREVIEW_CHARS: Final = 240

_COPY_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_POINTER_PATTERN: Final = re.compile(r"^(/[a-z0-9_]{1,64}){0,16}$")
_RFC3339_PATTERN: Final = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$"
)

_TEAM_ADAPTER: Final = TypeAdapter(TeamId)
_RUNTIME_ADAPTER: Final = TypeAdapter(RuntimeId)
_KIND_ADAPTER: Final = TypeAdapter(Kind)
_MESSAGE_ID_ADAPTER: Final = TypeAdapter(MessageId)
_SCOPE_ADAPTER: Final = TypeAdapter(ScopeId)
_PROTOCOL_ADAPTER: Final = TypeAdapter(ProtocolLabel)

_T = TypeVar("_T")


class DeadLetterError(RuntimeError):
    """Base error for protocol 1 dead-letter operations."""


class DeadLetterStreamFull(DeadLetterError):
    """``UA1_DLQ_<team>`` refused a publish because its byte limit was reached (JetStream 10077)."""


class DurableRefusalAction(str, Enum):
    """How a protocol 1 durable receiver responds to a ``FrameRefused`` verification failure."""

    SET_ASIDE = "set_aside"
    PAUSE_PULL = "pause_pull"
    NAK_DELAY = "nak_delay"


@dataclass(frozen=True)
class SetAsideResult:
    """Identifiers produced by one completed set-aside write into ``UA1_DLQ_<team>``."""

    copy_id: str
    team: str
    raw_subject: str
    notice_subject: str
    raw_stored: bool
    notice_message_id: str


@dataclass(frozen=True)
class StreamRange:
    """Message count and sequence bounds reported by ``$JS.API.STREAM.INFO.<stream>``."""

    name: str
    messages: int
    first_seq: int
    last_seq: int
    created: str = ""


@dataclass(frozen=True)
class ConsumerCutoff:
    """Delivery/ack positions for one JetStream consumer returned by ``CONSUMER.LIST`` or ``CONSUMER.INFO``."""

    name: str
    filter_subject: str
    delivered_stream_seq: int
    ack_floor_stream_seq: int
    num_ack_pending: int
    max_deliver: int


@dataclass(frozen=True)
class StoredStreamMessage:
    """One stored message returned by ``$JS.API.STREAM.MSG.GET.<stream>``."""

    stream: str
    seq: int
    subject: str
    data: bytes
    stored_at_ns: int
    stored_at: str
    headers: Mapping[str, str]


@dataclass(frozen=True)
class StreamSweepReport:
    """Outcome of sweeping one stream for exhausted deliveries."""

    stream: str
    consumers_checked: int
    messages_swept: int
    stopped_full: bool
    duration_ms: float


@dataclass(frozen=True)
class SweepSummary:
    """Aggregated outcome of sweeping a set of streams (``streams_checked`` counts completed streams; ``streams_failed`` counts per-stream failures)."""

    streams_checked: int
    consumers_checked: int
    messages_swept: int
    stopped_full: bool
    duration_ms: float
    streams_failed: int = 0


@dataclass(frozen=True)
class OldEnvelopeSummary:
    """Identifiers and bounded request preview read from a legacy ``MessageEnvelope`` raw copy."""

    message_class: Optional[str]
    task_id: Optional[str]
    request_preview: Optional[str]
    person_ids: frozenset[str]


# -- Subject, stream, and copy-id helpers --------------------------------------------------------


def dlq_stream_name(team: str) -> str:
    """The dead-letter stream name ``UA1_DLQ_<team>`` after validating ``team``."""

    _validate_id(_TEAM_ADAPTER, team, "team id")
    return f"UA1_DLQ_{team}"


def dlq_raw_subject(team: str, runtime: str, copy_id: str) -> str:
    """The subject for a raw copy: ``ua.v1.<team>.<runtime>.dlq.raw.<copy_id>``."""

    _validate_id(_TEAM_ADAPTER, team, "team id")
    _validate_id(_RUNTIME_ADAPTER, runtime, "runtime id")
    _validate_copy_id(copy_id)
    return f"ua.v1.{team}.{runtime}.dlq.raw.{copy_id}"


def dlq_notice_subject(team: str, runtime: str, copy_id: str) -> str:
    """The subject for a dead-letter notice: ``ua.v1.<team>.<runtime>.dlq.notice.<copy_id>``."""

    _validate_id(_TEAM_ADAPTER, team, "team id")
    _validate_id(_RUNTIME_ADAPTER, runtime, "runtime id")
    _validate_copy_id(copy_id)
    return f"ua.v1.{team}.{runtime}.dlq.notice.{copy_id}"


def dlq_raw_wildcard_subject(team: str) -> str:
    """Wildcard subject matching every raw copy in ``team``'s dead-letter stream."""

    _validate_id(_TEAM_ADAPTER, team, "team id")
    return f"ua.v1.{team}.*.dlq.raw.*"


def dlq_notice_wildcard_subject(team: str) -> str:
    """Wildcard subject matching every notice in ``team``'s dead-letter stream."""

    _validate_id(_TEAM_ADAPTER, team, "team id")
    return f"ua.v1.{team}.*.dlq.notice.*"


def dlq_notice_consumer_name(team: str) -> str:
    """The team services' durable notice consumer name ``dlq~<team>``."""

    _validate_id(_TEAM_ADAPTER, team, "team id")
    return f"dlq~{team}"


def parse_dlq_raw_subject(subject: str, team: str) -> Optional[tuple[str, str]]:
    """Return ``(runtime, copy_id)`` when ``subject`` is a raw copy in ``team``'s DLQ."""

    return _parse_dlq_subject(subject, team, "raw")


def parse_dlq_notice_subject(subject: str, team: str) -> Optional[tuple[str, str]]:
    """Return ``(runtime, copy_id)`` when ``subject`` is a notice in ``team``'s DLQ."""

    return _parse_dlq_subject(subject, team, "notice")


def _parse_dlq_subject(subject: str, team: str, kind_token: str) -> Optional[tuple[str, str]]:
    parts = subject.split(".")
    if len(parts) != 7:
        return None
    if parts[0] != "ua" or parts[1] != "v1" or parts[2] != team or parts[4] != "dlq" or parts[5] != kind_token:
        return None
    runtime, copy_id = parts[3], parts[6]
    if not _COPY_ID_PATTERN.fullmatch(copy_id):
        return None
    try:
        _RUNTIME_ADAPTER.validate_python(runtime)
    except ValidationError:
        return None
    return runtime, copy_id


def copy_id_secret_from_key(private_key: Ed25519PrivateKey) -> bytes:
    """Derive a 32-byte receiver-local secret for copy-id HMACs from ``private_key``."""

    seed = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return hashlib.sha256(_COPY_ID_SECRET_DOMAIN + seed).digest()


def stored_timestamp_ns(stored_at: datetime | str | int) -> int:
    """Convert a JetStream stored timestamp (nanosecond int, RFC 3339 string, or UTC datetime) to exact UTC nanoseconds."""

    if isinstance(stored_at, int) and not isinstance(stored_at, bool):
        if stored_at < 0:
            raise ValueError(f"stored_at_ns must be non-negative, got {stored_at}")
        return stored_at
    if isinstance(stored_at, datetime):
        if stored_at.tzinfo is None:
            raise ValueError("stored_at datetime must include a timezone")
        utc = stored_at.astimezone(timezone.utc)
        whole_seconds = calendar.timegm(utc.utctimetuple())
        return whole_seconds * 1_000_000_000 + utc.microsecond * 1_000
    if isinstance(stored_at, str):
        return _parse_rfc3339_ns(stored_at)
    raise TypeError(f"Unsupported stored_at type: {type(stored_at).__name__}")


def _parse_rfc3339_ns(text: str) -> int:
    match = _RFC3339_PATTERN.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"Invalid RFC 3339 timestamp: {text!r}")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    frac = match.group(7) or ""
    nanos = int(frac.ljust(9, "0"))
    tz_text = match.group(8)
    offset_seconds = 0
    if tz_text != "Z":
        sign = 1 if tz_text[0] == "+" else -1
        tz_hours, tz_mins = int(tz_text[1:3]), int(tz_text[4:6])
        offset_seconds = sign * (tz_hours * 3600 + tz_mins * 60)
    # Validate calendar date/time via datetime constructor before computing epoch nanoseconds.
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    whole_seconds = calendar.timegm(dt.utctimetuple()) - offset_seconds
    return whole_seconds * 1_000_000_000 + nanos


def message_stored_timestamp_ns(message: Any) -> int:
    """Extract the exact JetStream stored timestamp (UTC nanoseconds) from a delivered ``Msg``."""

    reply = getattr(message, "reply", None)
    parse_error: Optional[Exception] = None
    if isinstance(reply, str) and reply.startswith("$JS.ACK."):
        try:
            tokens = Msg.Metadata._get_metadata_fields(reply)
            # JetStream v1 ack replies have 9 tokens (timestamp at index 7); v2 replies have 11+ (Msg.Ack.Timestamp == 9).
            ts_index = 7 if len(tokens) == 9 else Msg.Ack.Timestamp
            return int(tokens[ts_index])
        except Exception as exc:
            parse_error = exc
    metadata = getattr(message, "metadata", None)
    timestamp = getattr(metadata, "timestamp", None)
    if timestamp is not None:
        _LOGGER.warning(
            "Falling back to microsecond metadata.timestamp for copy-id derivation because the $JS.ACK "
            "reply timestamp could not be read. event=protocol1_dlq_ack_timestamp_fallback stream=%s seq=%s error=%s",
            getattr(metadata, "stream", "-"),
            getattr(getattr(metadata, "sequence", None), "stream", "-"),
            parse_error or "missing_or_non_js_ack_reply",
        )
        return stored_timestamp_ns(timestamp)
    raise ValueError("JetStream message carries neither a $JS.ACK reply timestamp nor metadata.timestamp")


def derive_copy_id(
    secret: bytes,
    *,
    stream: str,
    stream_sequence: int,
    stored_at: datetime | str | int,
) -> str:
    """Compute the deterministic, unguessable 64-hex-char copy id for one stored message (FA-8, C09)."""

    if not secret:
        raise ValueError("copy-id secret must be non-empty")
    if not stream or not isinstance(stream, str):
        raise ValueError("stream name must be a non-empty string")
    if isinstance(stream_sequence, bool) or not isinstance(stream_sequence, int) or stream_sequence < 0:
        raise ValueError(f"stream_sequence must be a non-negative integer, got {stream_sequence!r}")
    ts_ns = stored_timestamp_ns(stored_at)
    payload = f"{stream}\x00{stream_sequence}\x00{ts_ns}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


# -- Unverified claims and dlq.notice body construction ------------------------------------------


def _safe_parse_json_object(raw_bytes: bytes, *, max_bytes: int = _MAX_UNTRUSTED_PARSE_BYTES) -> Optional[dict[str, Any]]:
    if not raw_bytes or len(raw_bytes) > max_bytes:
        return None
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _validated_optional(adapter: TypeAdapter[Any], value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        return str(adapter.validate_python(value))
    except ValidationError:
        return None


def extract_unverified_claims(raw_bytes: bytes) -> dict[str, str]:
    """Extract schema-valid unverified identifier claims from a refused frame's bytes (never raises)."""

    doc = _safe_parse_json_object(raw_bytes)
    if doc is None:
        return {}
    raw_issuer = doc.get("issuer")
    issuer: Mapping[str, Any] = raw_issuer if isinstance(raw_issuer, Mapping) else {}
    candidates = (
        ("kind", _KIND_ADAPTER, doc.get("kind")),
        ("message_id", _MESSAGE_ID_ADAPTER, doc.get("message_id")),
        ("issuer_team", _SCOPE_ADAPTER, issuer.get("team")),
        ("issuer_runtime", _RUNTIME_ADAPTER, issuer.get("runtime")),
        ("protocol", _PROTOCOL_ADAPTER, doc.get("protocol")),
    )
    claims: dict[str, str] = {}
    for field_name, adapter, raw_value in candidates:
        valid = _validated_optional(adapter, raw_value)
        if valid is not None:
            claims[field_name] = valid
    return claims


def extract_claimed_issuer_installation(raw_bytes: bytes) -> Optional[str]:
    """Read unverified ``issuer.installation`` from raw frame bytes (used to classify ``revocations_not_current``)."""

    doc = _safe_parse_json_object(raw_bytes)
    if doc is None:
        return None
    issuer = doc.get("issuer")
    if not isinstance(issuer, dict):
        return None
    installation = issuer.get("installation")
    return installation if isinstance(installation, str) and installation else None


def classify_durable_refusal(
    refused: FrameRefused,
    *,
    own_installation: str,
    raw_bytes: bytes = b"",
    claimed_issuer_installation: Optional[str] = None,
) -> DurableRefusalAction:
    """Decide whether a durable receiver sets aside, pauses pulls, or nak-delays a verification refusal (C07)."""

    if refused.code not in TEMPORARY_REFUSALS:
        return DurableRefusalAction.SET_ASIDE
    issuer_installation = claimed_issuer_installation
    if issuer_installation is None and raw_bytes:
        issuer_installation = extract_claimed_issuer_installation(raw_bytes)
    if issuer_installation is None or issuer_installation == own_installation:
        return DurableRefusalAction.PAUSE_PULL
    return DurableRefusalAction.NAK_DELAY


def build_dlq_notice_body(
    *,
    copy_id: str,
    reason: str,
    raw_bytes: bytes,
    original_subject: str,
    original_stream: str,
    original_stream_sequence: int,
    deliveries: int,
    pointer: Optional[str] = None,
    release: Optional[str] = None,
) -> dict[str, Any]:
    """Build and schema-validate a ``dlq.notice`` body containing identifiers only (A1)."""

    _validate_copy_id(copy_id)
    if reason in TEMPORARY_REFUSALS:
        raise ValueError(f"Temporary refusal {reason!r} must never be set aside as a dead letter")
    if reason not in DLQ_REASONS:
        raise ValueError(f"Unknown dlq.notice reason: {reason!r}")

    body: dict[str, Any] = {
        "copy_id": copy_id,
        "reason": reason,
        "original": {
            "subject": original_subject,
            "stream": original_stream,
            "stream_sequence": original_stream_sequence,
            "deliveries": deliveries,
        },
        "raw": {
            "stored": len(raw_bytes) <= MAX_FRAME_BYTES,
            "size": len(raw_bytes),
            "sha256": b64url(hashlib.sha256(raw_bytes).digest()),
        },
        "release": (release or release_version())[:64],
    }

    if reason != "upgrade_old_format":
        if pointer and _POINTER_PATTERN.fullmatch(pointer):
            body["pointer"] = pointer
        claims = extract_unverified_claims(raw_bytes)
        if claims:
            body["claimed"] = claims

    model = KINDS["dlq.notice"].body_model
    assert model is not None
    model.validate_python(body)
    return body


# -- Shared +WPI hold helper (A3, C03, C07) ------------------------------------------------------


def _is_stream_full_error(exc: BaseException) -> bool:
    if isinstance(exc, DeadLetterStreamFull):
        return True
    if isinstance(exc, APIError):
        return getattr(exc, "err_code", None) == JS_ERR_MAX_BYTES_EXCEEDED
    return False


async def _send_in_progress(messages: Sequence[Any], logger: logging.Logger) -> None:
    for msg in messages:
        try:
            await msg.in_progress()
        except Exception:
            logger.debug("Failed sending +WPI on held message.", exc_info=True)


async def _sleep_or_stop(step: float, stop: Optional[asyncio.Event]) -> bool:
    if stop is None:
        await asyncio.sleep(step)
        return False
    if stop.is_set():
        return True
    try:
        await asyncio.wait_for(stop.wait(), timeout=step)
        return True
    except asyncio.TimeoutError:
        return stop.is_set()


async def _wait_with_wpi(
    messages_fn: Callable[[], Sequence[Any]],
    *,
    delay_seconds: float,
    wpi_interval: float,
    stop: Optional[asyncio.Event],
    logger: logging.Logger,
) -> bool:
    """Wait ``delay_seconds`` while periodically sending ``+WPI``; returns True if ``stop`` fired."""

    deadline = time.monotonic() + max(0.0, delay_seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return bool(stop is not None and stop.is_set())
        step = min(remaining, wpi_interval)
        if await _sleep_or_stop(step, stop):
            return True
        if time.monotonic() < deadline or step >= wpi_interval * 0.9:
            await _send_in_progress(messages_fn(), logger)


async def hold_with_in_progress(
    messages_fn: Callable[[], Sequence[Any]],
    action: Callable[[], Awaitable[_T]],
    *,
    ack_wait_seconds: float,
    initial_retry_seconds: float = 1.0,
    max_retry_seconds: float = 30.0,
    retryable: Callable[[Exception], bool],
    on_first_failure: Optional[Callable[[Exception], None]] = None,
    stop: Optional[asyncio.Event] = None,
    logger: logging.Logger = _LOGGER,
) -> Optional[_T]:
    """Run ``action()`` until it succeeds or ``stop`` fires, holding messages with ``+WPI`` on retryable errors.

    Shared by both receiver set-aside holding when ``UA1_DLQ_<team>`` is full (A3, FA-2) and the
    team services' notice watch when reporting channels are unavailable (C03). Returns ``None``
    without acknowledging when ``stop`` is set before ``action()`` succeeds.
    """

    wpi_interval = max(0.02, min(ack_wait_seconds / 3.0, ack_wait_seconds * 0.8))
    attempt = 0
    first_failure_handler = on_first_failure
    while stop is None or not stop.is_set():
        try:
            return await action()
        except Exception as exc:
            if not retryable(exc):
                raise
            if first_failure_handler is not None:
                first_failure_handler(exc)
                first_failure_handler = None
            await _send_in_progress(messages_fn(), logger)
            delay = min(max_retry_seconds, initial_retry_seconds * (2 ** min(attempt, 10)))
            attempt += 1
            if await _wait_with_wpi(messages_fn, delay_seconds=delay, wpi_interval=wpi_interval, stop=stop, logger=logger):
                return None
    return None


# -- DeadLetterSetter & DeadLetterHoldTracker ----------------------------------------------------


class _SetAsideTransientExhausted(Exception):
    """Internal signal that non-full NATS publish retries ran out inside the current ack_wait."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


_TRANSIENT_PUBLISH_ERRORS: Final = (NatsError, APIError, TimeoutError, asyncio.TimeoutError)


class DeadLetterSetter:
    """Write raw copies and signed ``dlq.notice`` frames to ``UA1_DLQ_<team>`` (A1, A2, A3)."""

    def __init__(
        self,
        host: LiveHost,
        *,
        release: Optional[str] = None,
        ack_wait_seconds: float = 30.0,
        initial_retry_seconds: float = 1.0,
        max_retry_seconds: float = 30.0,
        publish_timeout_seconds: float = 5.0,
        transient_retries: int = 2,
        logger: logging.Logger = _LOGGER,
    ) -> None:
        self.host = host
        self.release = release or release_version()
        self.ack_wait_seconds = ack_wait_seconds
        self.initial_retry_seconds = initial_retry_seconds
        self.max_retry_seconds = max_retry_seconds
        self.publish_timeout_seconds = publish_timeout_seconds
        self.transient_retries = max(0, int(transient_retries))
        self.logger = logger

    def resolve_notice_team(self, team: Optional[str]) -> str:
        """Choose the destination team for ``UA1_DLQ_<team>`` (installation traffic routes to the first served team)."""

        served = tuple(sorted(self.host.identity.certificate.teams))
        if not served:
            raise DeadLetterError("Host certificate serves no team; cannot choose a dead-letter stream")
        if team is None or team == INSTALLATION_SCOPE:
            return served[0]
        if team not in self.host.identity.certificate.teams:
            raise DeadLetterError(f"Host certificate does not serve team {team!r}")
        return team

    def copy_id_for(
        self,
        *,
        stream: str,
        stream_sequence: int,
        stored_at: datetime | str | int,
    ) -> str:
        """Derive this receiver's deterministic copy id for ``(stream, stream_sequence, stored_at)``."""

        secret = copy_id_secret_from_key(self.host.identity.keys.application_key)
        return derive_copy_id(secret, stream=stream, stream_sequence=stream_sequence, stored_at=stored_at)

    async def _js_publish(self, subject: str, payload: bytes, *, headers: Optional[dict[str, str]] = None) -> None:
        js = self.host.nc.jetstream()
        await before_send()
        try:
            await js.publish(subject, payload, timeout=self.publish_timeout_seconds, headers=headers)
        except APIError as exc:
            if _is_stream_full_error(exc):
                raise DeadLetterStreamFull(f"Dead-letter stream refused publish to {subject}: {exc}") from exc
            raise

    async def set_aside(
        self,
        *,
        raw_bytes: bytes,
        reason: str,
        original_subject: str,
        original_stream: str,
        original_stream_sequence: int,
        deliveries: int,
        stored_at: datetime | str | int,
        team: Optional[str] = None,
        pointer: Optional[str] = None,
        skip_raw_publish: bool = False,
        on_raw_stored: Optional[Callable[[], None]] = None,
    ) -> SetAsideResult:
        """Store raw copy (when within ``MAX_FRAME_BYTES``) then signed ``dlq.notice`` in ``UA1_DLQ_<team>``."""

        notice_team = self.resolve_notice_team(team)
        runtime = self.host.identity.runtime
        copy_id = self.copy_id_for(
            stream=original_stream,
            stream_sequence=original_stream_sequence,
            stored_at=stored_at,
        )
        raw_subject = dlq_raw_subject(notice_team, runtime, copy_id)
        notice_subject = dlq_notice_subject(notice_team, runtime, copy_id)
        notice_body = build_dlq_notice_body(
            copy_id=copy_id,
            reason=reason,
            raw_bytes=raw_bytes,
            original_subject=original_subject,
            original_stream=original_stream,
            original_stream_sequence=original_stream_sequence,
            deliveries=deliveries,
            pointer=pointer,
            release=self.release,
        )
        audience = {
            "installation": self.host.identity.installation,
            "team": notice_team,
            "target": "team_services",
            "id": notice_team,
        }
        frame = self.host.sign("dlq.notice", notice_body, audience, team=notice_team, subject=notice_subject)
        notice_bytes, notice_headers = self.host.encode(frame)
        notice_headers[HEADER_MSG_ID] = str(frame["idempotency_key"])

        raw_stored = bool(notice_body["raw"]["stored"])
        if raw_stored and not skip_raw_publish:
            await self._js_publish(raw_subject, raw_bytes)
            if on_raw_stored is not None:
                on_raw_stored()

        await self._js_publish(notice_subject, notice_bytes, headers=notice_headers)
        return SetAsideResult(
            copy_id=copy_id,
            team=notice_team,
            raw_subject=raw_subject,
            notice_subject=notice_subject,
            raw_stored=raw_stored,
            notice_message_id=str(frame["message_id"]),
        )

    async def _attempt_set_aside_with_transient_retries(
        self,
        *,
        holder: list[Any],
        raw_bytes: bytes,
        reason: str,
        original_subject: str,
        original_stream: str,
        original_seq: int,
        stored_ns: int,
        team: Optional[str],
        pointer: Optional[str],
        raw_stored_in_hold: bool,
        on_raw_stored: Callable[[], None],
        effective_ack_wait: float,
        stop: Optional[asyncio.Event],
    ) -> SetAsideResult:
        deliveries = max(1, int(holder[0].metadata.num_delivered))
        retry_deadline = time.monotonic() + max(0.0, effective_ack_wait * 0.5)
        for retry_index in range(self.transient_retries + 1):
            try:
                return await self.set_aside(
                    raw_bytes=raw_bytes,
                    reason=reason,
                    original_subject=original_subject,
                    original_stream=original_stream,
                    original_stream_sequence=original_seq,
                    deliveries=deliveries,
                    stored_at=stored_ns,
                    team=team,
                    pointer=pointer,
                    skip_raw_publish=raw_stored_in_hold,
                    on_raw_stored=on_raw_stored,
                )
            except DeadLetterStreamFull:
                raise
            except _TRANSIENT_PUBLISH_ERRORS as exc:
                remaining = retry_deadline - time.monotonic()
                if retry_index >= self.transient_retries or remaining <= 0:
                    raise _SetAsideTransientExhausted(exc) from exc
                pause = min(0.25, remaining / 2.0, self.initial_retry_seconds * (2 ** retry_index))
                if await _sleep_or_stop(max(0.0, pause), stop):
                    raise _SetAsideTransientExhausted(exc) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    async def set_aside_and_ack(
        self,
        message: Any,
        *,
        reason: str,
        team: Optional[str] = None,
        pointer: Optional[str] = None,
        stop: Optional[asyncio.Event] = None,
        ack_wait_seconds: Optional[float] = None,
        message_holder: Optional[list[Any]] = None,
    ) -> Optional[SetAsideResult]:
        """Set a JetStream ``message`` aside, holding with ``+WPI`` while ``UA1_DLQ_<team>`` is full, then ack."""

        holder = message_holder if message_holder is not None else [message]
        metadata = message.metadata
        original_stream = str(metadata.stream)
        original_seq = int(metadata.sequence.stream)
        stored_ns = message_stored_timestamp_ns(message)
        raw_bytes = bytes(message.data)
        original_subject = str(message.subject)
        raw_stored_in_hold = False
        effective_ack_wait = ack_wait_seconds if ack_wait_seconds is not None else self.ack_wait_seconds

        def mark_raw_stored() -> None:
            nonlocal raw_stored_in_hold
            raw_stored_in_hold = True

        async def attempt_store() -> SetAsideResult:
            return await self._attempt_set_aside_with_transient_retries(
                holder=holder,
                raw_bytes=raw_bytes,
                reason=reason,
                original_subject=original_subject,
                original_stream=original_stream,
                original_seq=original_seq,
                stored_ns=stored_ns,
                team=team,
                pointer=pointer,
                raw_stored_in_hold=raw_stored_in_hold,
                on_raw_stored=mark_raw_stored,
                effective_ack_wait=effective_ack_wait,
                stop=stop,
            )

        def log_holding(exc: Exception) -> None:
            claims = extract_unverified_claims(raw_bytes)
            self.logger.warning(
                "Dead-letter stream is full; holding message with +WPI until room returns. "
                "event=protocol1_dlq_stream_full_holding stream=%s seq=%s subject=%s reason=%s "
                "claimed_kind=%s claimed_message_id=%s error=%s",
                original_stream,
                original_seq,
                original_subject,
                reason,
                claims.get("kind", "-"),
                claims.get("message_id", "-"),
                exc,
            )

        try:
            result = await hold_with_in_progress(
                lambda: (holder[0],),
                attempt_store,
                ack_wait_seconds=effective_ack_wait,
                initial_retry_seconds=self.initial_retry_seconds,
                max_retry_seconds=self.max_retry_seconds,
                retryable=lambda exc: isinstance(exc, DeadLetterStreamFull),
                on_first_failure=log_holding,
                stop=stop,
                logger=self.logger,
            )
        except _SetAsideTransientExhausted as exhausted:
            # Leave the message unacknowledged (no +WPI) so deliveries count and the CRIT-1 sweep can recover it (C14).
            self.logger.warning(
                "Dead-letter set-aside failed after transient retries; leaving message unacknowledged for redelivery/sweep. "
                "event=protocol1_dlq_set_aside_failed stream=%s seq=%s subject=%s reason=%s error_type=%s error=%s",
                original_stream,
                original_seq,
                original_subject,
                reason,
                type(exhausted.cause).__name__,
                exhausted.cause,
            )
            return None
        if result is None:
            return None
        await holder[0].ack()
        return result


@dataclass
class _HeldEntry:
    holder: list[Any]
    task: asyncio.Task[Optional[SetAsideResult]]


class DeadLetterHoldTracker:
    """Track in-flight background set-aside holds keyed by ``(stream, stream_sequence)`` (C07).

    A held message takes no execution slot. If NATS redelivers the same ``(stream, seq)`` while
    it is already held, the tracker swaps the message handle in place instead of starting a
    second hold loop.
    """

    def __init__(self, setter: DeadLetterSetter) -> None:
        self._setter = setter
        self._held: dict[tuple[str, int], _HeldEntry] = {}

    @property
    def active_count(self) -> int:
        return len(self._held)

    def is_holding(self, stream: str, stream_sequence: int) -> bool:
        return (stream, stream_sequence) in self._held

    def hold(
        self,
        message: Any,
        *,
        reason: str,
        team: Optional[str] = None,
        pointer: Optional[str] = None,
        stop: Optional[asyncio.Event] = None,
        ack_wait_seconds: Optional[float] = None,
    ) -> asyncio.Task[Optional[SetAsideResult]]:
        """Start holding ``message`` in the background, or replace the handle if already held."""

        key = (str(message.metadata.stream), int(message.metadata.sequence.stream))
        existing = self._held.get(key)
        if existing is not None and not existing.task.done():
            existing.holder[0] = message
            return existing.task

        holder = [message]

        async def _run() -> Optional[SetAsideResult]:
            try:
                return await self._setter.set_aside_and_ack(
                    message,
                    reason=reason,
                    team=team,
                    pointer=pointer,
                    stop=stop,
                    ack_wait_seconds=ack_wait_seconds,
                    message_holder=holder,
                )
            except Exception as exc:
                self._setter.logger.error(
                    "Background dead-letter set-aside failed unexpectedly; leaving message unacknowledged. "
                    "event=protocol1_dlq_set_aside_failed stream=%s seq=%s reason=%s error_type=%s error=%s",
                    key[0],
                    key[1],
                    reason,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                return None
            finally:
                current = self._held.get(key)
                if current is not None and current.holder is holder:
                    self._held.pop(key, None)

        task = asyncio.create_task(_run())
        self._held[key] = _HeldEntry(holder=holder, task=task)
        return task

    async def drain(self) -> None:
        """Wait for all active background holds to complete (typically after setting ``stop``)."""

        while self._held:
            tasks = [entry.task for entry in list(self._held.values())]
            await asyncio.gather(*tasks, return_exceptions=True)


# -- JetStream API helpers and CRIT-1 sweep (A4, C08) --------------------------------------------


async def _js_api_json(nc: Any, subject: str, payload: Mapping[str, Any] | None = None, *, timeout: float = 5.0) -> dict[str, Any]:
    body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8") if payload else b""
    await before_send()
    response = await nc.request(subject, body, timeout=timeout)
    data = json.loads(response.data.decode("utf-8"))
    if not isinstance(data, dict):
        raise DeadLetterError(f"Unexpected JetStream API response from {subject}")
    return data


def _api_error_code(data: Mapping[str, Any]) -> Optional[int]:
    error = data.get("error")
    if isinstance(error, Mapping):
        code = error.get("err_code")
        if isinstance(code, int):
            return code
    return None


def _raise_if_api_error(subject: str, data: Mapping[str, Any]) -> None:
    error = data.get("error")
    if isinstance(error, Mapping):
        code = error.get("err_code")
        description = error.get("description", "JetStream API error")
        raise APIError(code=error.get("code"), err_code=code, description=str(description))


async def stream_info(nc: Any, stream: str, *, timeout: float = 5.0) -> Optional[StreamRange]:
    """Read stream message count and sequence range via ``$JS.API.STREAM.INFO.<stream>``; ``None`` if absent."""

    subject = f"$JS.API.STREAM.INFO.{stream}"
    data = await _js_api_json(nc, subject, timeout=timeout)
    if _api_error_code(data) == JS_ERR_STREAM_NOT_FOUND:
        return None
    _raise_if_api_error(subject, data)
    state = data.get("state") or {}
    return StreamRange(
        name=stream,
        messages=int(state.get("messages", 0)),
        first_seq=int(state.get("first_seq", 0)),
        last_seq=int(state.get("last_seq", 0)),
        created=str(data.get("created", "")),
    )


def _consumer_cutoff_from_dict(entry: Mapping[str, Any]) -> ConsumerCutoff:
    config = entry.get("config") or {}
    delivered = entry.get("delivered") or {}
    ack_floor = entry.get("ack_floor") or {}
    return ConsumerCutoff(
        name=str(entry.get("name") or config.get("durable_name") or ""),
        filter_subject=str(config.get("filter_subject") or ""),
        delivered_stream_seq=int(delivered.get("stream_seq", 0)),
        ack_floor_stream_seq=int(ack_floor.get("stream_seq", 0)),
        num_ack_pending=int(entry.get("num_ack_pending", 0)),
        max_deliver=int(config.get("max_deliver", 1)),
    )


async def list_consumers(nc: Any, stream: str, *, timeout: float = 5.0) -> list[ConsumerCutoff]:
    """List every consumer of ``stream`` via paginated ``$JS.API.CONSUMER.LIST.<stream>``."""

    subject = f"$JS.API.CONSUMER.LIST.{stream}"
    consumers: list[ConsumerCutoff] = []
    offset = 0
    while True:
        payload = {"offset": offset} if offset > 0 else None
        data = await _js_api_json(nc, subject, payload, timeout=timeout)
        if _api_error_code(data) == JS_ERR_STREAM_NOT_FOUND:
            return []
        _raise_if_api_error(subject, data)
        page = data.get("consumers") or []
        for entry in page:
            if isinstance(entry, Mapping):
                consumers.append(_consumer_cutoff_from_dict(entry))
        total = int(data.get("total", len(consumers)))
        if not page or len(consumers) >= total:
            return consumers
        offset = len(consumers)


async def consumer_info(nc: Any, stream: str, consumer: str, *, timeout: float = 5.0) -> Optional[ConsumerCutoff]:
    """Read one consumer's cutoff info via ``$JS.API.CONSUMER.INFO.<stream>.<consumer>``."""

    subject = f"$JS.API.CONSUMER.INFO.{stream}.{consumer}"
    data = await _js_api_json(nc, subject, timeout=timeout)
    err_code = _api_error_code(data)
    if err_code in (JS_ERR_STREAM_NOT_FOUND, 10014):  # stream or consumer not found
        return None
    _raise_if_api_error(subject, data)
    return _consumer_cutoff_from_dict(data)


def consumer_cutoff_seq(consumer: ConsumerCutoff) -> int:
    """Exact race-free sequence cutoff for exhausted deliveries (CRIT-1, verified on nats-server 2.15.1).

    Messages awaiting ack, negatively acknowledged with a delay, or past ``ack_wait`` waiting for
    redelivery all stay in ``num_ack_pending`` until ``max_deliver`` is exhausted. Every message
    still stored on a workqueue consumer's filter subject with ``1 <= seq <= cutoff_seq`` has
    exhausted its deliveries.
    """

    if consumer.num_ack_pending == 0:
        return consumer.delivered_stream_seq
    return consumer.ack_floor_stream_seq


def _parse_nats_headers(encoded_hdrs: Optional[str]) -> dict[str, str]:
    if not encoded_hdrs:
        return {}
    try:
        raw = base64.b64decode(encoded_hdrs).decode("utf-8", errors="replace")
    except Exception:
        return {}
    headers: dict[str, str] = {}
    lines = raw.split("\r\n")
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip()] = value.strip()
    return headers


async def get_stream_message(
    nc: Any,
    stream: str,
    *,
    seq: Optional[int] = None,
    next_by_subj: Optional[str] = None,
    last_by_subj: Optional[str] = None,
    timeout: float = 5.0,
) -> Optional[StoredStreamMessage]:
    """Read one message from ``stream`` via ``$JS.API.STREAM.MSG.GET.<stream>``; ``None`` if absent."""

    request: dict[str, Any] = {}
    if last_by_subj is not None:
        request["last_by_subj"] = last_by_subj
    else:
        if seq is not None:
            request["seq"] = seq
        if next_by_subj is not None:
            request["next_by_subj"] = next_by_subj
    subject = f"$JS.API.STREAM.MSG.GET.{stream}"
    data = await _js_api_json(nc, subject, request, timeout=timeout)
    if _api_error_code(data) in (JS_ERR_NO_MESSAGE_FOUND, JS_ERR_STREAM_NOT_FOUND):
        return None
    _raise_if_api_error(subject, data)
    message = data.get("message") or {}
    encoded_data = message.get("data") or ""
    raw_bytes = base64.b64decode(encoded_data) if encoded_data else b""
    stored_at = str(message.get("time") or "")
    return StoredStreamMessage(
        stream=stream,
        seq=int(message["seq"]),
        subject=str(message["subject"]),
        data=raw_bytes,
        stored_at_ns=stored_timestamp_ns(stored_at),
        stored_at=stored_at,
        headers=_parse_nats_headers(message.get("hdrs")),
    )


async def delete_stream_message(nc: Any, stream: str, seq: int, *, timeout: float = 5.0) -> bool:
    """Erase message ``seq`` from ``stream`` via ``$JS.API.STREAM.MSG.DELETE.<stream>``."""

    subject = f"$JS.API.STREAM.MSG.DELETE.{stream}"
    data = await _js_api_json(nc, subject, {"seq": seq}, timeout=timeout)
    if _api_error_code(data) in (JS_ERR_NO_MESSAGE_FOUND, JS_ERR_STREAM_NOT_FOUND):
        return False
    _raise_if_api_error(subject, data)
    return bool(data.get("success", False))


async def sweep_exhausted_stream(
    nc: Any,
    stream: str,
    setter: DeadLetterSetter,
    *,
    team: str,
    stop: Optional[asyncio.Event] = None,
    logged_stranded: Optional[set[tuple[str, int]]] = None,
    timeout: float = 5.0,
    logger: logging.Logger = _LOGGER,
) -> StreamSweepReport:
    """Sweep one protocol 1 workqueue stream for messages that exhausted ``max_deliveries`` (A4, CRIT-1, C08)."""

    started = time.monotonic()
    info = await stream_info(nc, stream, timeout=timeout)
    if info is None or info.messages == 0:
        return StreamSweepReport(stream, 0, 0, False, (time.monotonic() - started) * 1000.0)

    consumers = await list_consumers(nc, stream, timeout=timeout)
    consumers_checked = 0
    messages_swept = 0
    stopped_full = False

    for consumer in consumers:
        if stop is not None and stop.is_set():
            break
        cutoff = consumer_cutoff_seq(consumer)
        if cutoff <= 0 or cutoff < info.first_seq:
            continue
        consumers_checked += 1
        swept, full = await _sweep_consumer_range(
            nc,
            stream,
            consumer,
            setter,
            team=team,
            first_seq=info.first_seq,
            cutoff_seq=cutoff,
            stop=stop,
            logged_stranded=logged_stranded,
            timeout=timeout,
            logger=logger,
        )
        messages_swept += swept
        if full:
            stopped_full = True
            break

    duration_ms = (time.monotonic() - started) * 1000.0
    if messages_swept > 0 or stopped_full:
        logger.warning(
            "Swept stream for exhausted deliveries. event=protocol1_dlq_sweep_pass stream=%s "
            "consumers_checked=%s messages_swept=%s stopped_full=%s duration_ms=%.1f",
            stream,
            consumers_checked,
            messages_swept,
            stopped_full,
            duration_ms,
        )
    else:
        logger.debug(
            "Swept stream for exhausted deliveries. event=protocol1_dlq_sweep_pass stream=%s "
            "consumers_checked=%s messages_swept=0 stopped_full=False duration_ms=%.1f",
            stream,
            consumers_checked,
            duration_ms,
        )
    return StreamSweepReport(stream, consumers_checked, messages_swept, stopped_full, duration_ms)


def _record_full_sweep(
    stream: str,
    seq: int,
    consumer_name: str,
    logged_stranded: Optional[set[tuple[str, int]]],
    logger: logging.Logger,
) -> None:
    marker = (stream, seq)
    if logged_stranded is not None and marker in logged_stranded:
        return
    if logged_stranded is not None:
        logged_stranded.add(marker)
    logger.warning(
        "Dead-letter stream is full; stranded exhausted message remains queued for the next sweep. "
        "event=protocol1_dlq_sweep_stream_full stream=%s seq=%s consumer=%s",
        stream,
        seq,
        consumer_name,
    )


async def _sweep_consumer_range(
    nc: Any,
    stream: str,
    consumer: ConsumerCutoff,
    setter: DeadLetterSetter,
    *,
    team: str,
    first_seq: int,
    cutoff_seq: int,
    stop: Optional[asyncio.Event],
    logged_stranded: Optional[set[tuple[str, int]]],
    timeout: float,
    logger: logging.Logger,
) -> tuple[int, bool]:
    swept = 0
    next_seq = first_seq
    filter_subject = consumer.filter_subject or None
    while next_seq <= cutoff_seq and (stop is None or not stop.is_set()):
        stored = await get_stream_message(
            nc, stream, seq=next_seq, next_by_subj=filter_subject, timeout=timeout
        )
        if stored is None or stored.seq > cutoff_seq:
            return swept, False
        try:
            await setter.set_aside(
                raw_bytes=stored.data,
                reason="delivery_limit",
                original_subject=stored.subject,
                original_stream=stream,
                original_stream_sequence=stored.seq,
                deliveries=max(1, consumer.max_deliver),
                stored_at=stored.stored_at_ns,
                team=team,
            )
        except DeadLetterStreamFull:
            _record_full_sweep(stream, stored.seq, consumer.name, logged_stranded, logger)
            return swept, True
        if logged_stranded is not None:
            logged_stranded.discard((stream, stored.seq))
        await delete_stream_message(nc, stream, stored.seq, timeout=timeout)
        swept += 1
        next_seq = stored.seq + 1
    return swept, False


async def sweep_exhausted_streams(
    nc: Any,
    streams: Sequence[str],
    setter: DeadLetterSetter,
    *,
    team: str,
    stop: Optional[asyncio.Event] = None,
    logged_stranded: Optional[set[tuple[str, int]]] = None,
    timeout: float = 5.0,
    logger: logging.Logger = _LOGGER,
) -> SweepSummary:
    """Sweep ``streams`` in order for exhausted deliveries, stopping early if ``UA1_DLQ_<team>`` is full."""

    started = time.monotonic()
    streams_checked = 0
    streams_failed = 0
    consumers_checked = 0
    messages_swept = 0
    stopped_full = False
    for stream in streams:
        if stop is not None and stop.is_set():
            break
        try:
            report = await sweep_exhausted_stream(
                nc,
                stream,
                setter,
                team=team,
                stop=stop,
                logged_stranded=logged_stranded,
                timeout=timeout,
                logger=logger,
            )
        except DeadLetterStreamFull:
            stopped_full = True
            break
        except Exception as exc:
            streams_failed += 1
            logger.warning(
                "Could not sweep stream for exhausted deliveries; continuing with remaining streams. "
                "event=protocol1_dlq_sweep_stream_failed stream=%s error_type=%s error=%s",
                stream,
                type(exc).__name__,
                exc,
            )
            continue
        streams_checked += 1
        consumers_checked += report.consumers_checked
        messages_swept += report.messages_swept
        if report.stopped_full:
            stopped_full = True
            break
    return SweepSummary(
        streams_checked=streams_checked,
        consumers_checked=consumers_checked,
        messages_swept=messages_swept,
        stopped_full=stopped_full,
        duration_ms=(time.monotonic() - started) * 1000.0,
        streams_failed=streams_failed,
    )


# -- Departure matching (A6, C13) and legacy upgrade_old_format summary (A5) ---------------------


# Closed set of legacy MessageEnvelope paths holding a GatewayContext or direct person id (C13).
_OLD_ENVELOPE_GATEWAY_CONTEXT_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("msg_meta", "gateway_context"),
    ("msg_content", "original_envelope_snapshot", "msg_meta", "gateway_context"),
    ("msg_content", "payload_meta", "gateway_context"),
    ("msg_content", "handed_back", "meta", "gateway_context"),
)
_OLD_ENVELOPE_DIRECT_PERSON_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("msg_content", "canonical_user_id"),
    ("msg_content", "person_id"),
    ("msg_content", "task_context", "context", "person_id"),
)


def _nested_value(mapping: Any, *path: str) -> Any:
    current = mapping
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _nested_str(mapping: Any, *path: str) -> Optional[str]:
    value = _nested_value(mapping, *path)
    return value if isinstance(value, str) and value else None


def _gateway_context_person_ids(gateway_context: Any) -> set[str]:
    """Extract canonical person ids from the three exact ``GatewayContext`` person fields (C13)."""

    if not isinstance(gateway_context, Mapping):
        return set()
    return {
        value
        for value in (
            _nested_str(gateway_context, "canonical_user_id"),
            _nested_str(gateway_context, "task_context", "context", "person_id"),
            _nested_str(gateway_context, "schedule_run", "permit", "permit", "person_id"),
        )
        if value is not None
    }


def _old_envelope_person_ids(doc: Mapping[str, Any]) -> frozenset[str]:
    """Return every canonical person id from the closed C13 set of legacy ``MessageEnvelope`` fields."""

    found: set[str] = set()
    for path in _OLD_ENVELOPE_GATEWAY_CONTEXT_PATHS:
        found.update(_gateway_context_person_ids(_nested_value(doc, *path)))
    for path in _OLD_ENVELOPE_DIRECT_PERSON_PATHS:
        value = _nested_str(doc, *path)
        if value is not None:
            found.add(value)
    return frozenset(found)


def _child_values(value: Any) -> Sequence[Any]:
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, list):
        return value
    return ()


def _contains_any_string(root: Any, targets: frozenset[str]) -> bool:
    if not targets:
        return False
    stack: list[Any] = [root]
    visited = 0
    while stack:
        if visited >= _MAX_WALK_NODES:
            _LOGGER.warning(
                "Departure JSON walk reached node cap (%s); treating copy as naming the departing person "
                "so personal content is not retained. event=protocol1_dlq_departure_walk_cap_reached nodes=%s",
                _MAX_WALK_NODES,
                visited,
            )
            return True
        value = stack.pop()
        visited += 1
        if isinstance(value, str):
            if value in targets:
                return True
            continue
        stack.extend(_child_values(value))
    return False


def _merge_targets(single: Optional[str], many: Collection[str]) -> frozenset[str]:
    items = [item for item in many if item]
    if single:
        items.append(single)
    return frozenset(items)


def _protocol1_frame_names_person(doc: Mapping[str, Any], target_refs: frozenset[str]) -> bool:
    if not target_refs:
        return False
    if _nested_str(doc, "authority", "person") in target_refs:
        return True
    if not isinstance(doc.get("protocol"), str):
        return False
    body = doc.get("body")
    return isinstance(body, Mapping) and _contains_any_string(body, target_refs)


def _legacy_envelope_names_person(doc: Mapping[str, Any], target_ids: frozenset[str]) -> bool:
    if not target_ids:
        return False
    if not isinstance(doc.get("msg_meta"), Mapping) and not isinstance(doc.get("msg_content"), Mapping):
        return False
    return bool(_old_envelope_person_ids(doc) & target_ids)


def raw_copy_names_person(
    data: bytes,
    *,
    person_ref: Optional[str] = None,
    person_id: Optional[str] = None,
    person_refs: Collection[str] = (),
    person_ids: Collection[str] = (),
) -> bool:
    """Whether raw copy ``data`` names any departing person (protocol 1 frame or legacy ``MessageEnvelope``).

    Matches:
    - Protocol 1 frames (identified by a ``protocol`` string plus a ``body`` or ``authority`` object):
      ``authority.person`` and any ``person_ref`` value inside ``body``.
    - Legacy ``MessageEnvelope`` objects (``msg_meta`` / ``msg_content``): only the closed set of
      person-id fields documented in ``contracts/messages/v1/README.md`` ("People's content", C13).
    Unreadable bytes return ``False``.
    """

    target_refs = _merge_targets(person_ref, person_refs)
    target_ids = _merge_targets(person_id, person_ids)
    if not target_refs and not target_ids:
        return False
    doc = _safe_parse_json_object(data)
    if doc is None:
        return False
    return _protocol1_frame_names_person(doc, target_refs) or _legacy_envelope_names_person(doc, target_ids)


def _clean_preview(text: str) -> Optional[str]:
    compact = " ".join(text.split())
    if not compact:
        return None
    if len(compact) <= _MAX_REQUEST_PREVIEW_CHARS:
        return compact
    return compact[: _MAX_REQUEST_PREVIEW_CHARS - 1].rstrip() + "…"


def extract_old_envelope_summary(data: bytes) -> Optional[OldEnvelopeSummary]:
    """Read task id, message class, bounded request preview, and person ids from an ``upgrade_old_format`` copy."""

    doc = _safe_parse_json_object(data)
    if doc is None:
        return None
    msg_content = doc.get("msg_content")
    msg_meta = doc.get("msg_meta")
    if not isinstance(msg_content, Mapping) and not isinstance(msg_meta, Mapping):
        return None

    content = msg_content if isinstance(msg_content, Mapping) else {}
    meta = msg_meta if isinstance(msg_meta, Mapping) else {}
    message_class = _nested_str(content, "message_class")
    task_id = (
        _nested_str(content, "task_id")
        or _nested_str(content, "root_task_id")
        or _nested_str(meta, "root_task_id")
        or _nested_str(content, "original_envelope_snapshot", "msg_content", "task_id")
    )
    raw_preview = (
        _nested_str(content, "task_description")
        or _nested_str(content, "prompt")
        or _nested_str(content, "instruction")
        or _nested_str(content, "message")
        or _nested_str(content, "user_message")
        or _nested_str(content, "content")
    )
    return OldEnvelopeSummary(
        message_class=message_class,
        task_id=task_id,
        request_preview=_clean_preview(raw_preview) if raw_preview else None,
        person_ids=_old_envelope_person_ids(doc),
    )


def _validate_id(adapter: TypeAdapter[str], value: str, what: str) -> None:
    try:
        adapter.validate_python(value)
    except ValidationError:
        raise ValueError(f"{value!r} is not a valid protocol 1 {what}") from None


def _validate_copy_id(copy_id: str) -> None:
    if not isinstance(copy_id, str) or not _COPY_ID_PATTERN.fullmatch(copy_id):
        raise ValueError(f"{copy_id!r} is not a 64-character lowercase hex copy id")
