"""Team-services protocol 1 dead-letter watch, CRIT-1 sweep, orphan cleanup, and unified departure deletion.

Implements step 4's team-services responsibilities (``contracts/messages/v1/README.md`` "Dead letters"):
- **Notice watch (A5, C02, C03, C04, C05, C10):** pulls signed ``dlq.notice`` frames from
  ``dlq~<team>`` on ``UA1_DLQ_<team>``, verifies each notice against the team's trust store,
  checks its raw copy's size and SHA-256, immediately deletes any newly fetched raw copy
  naming a departed person (never surfacing departed ``upgrade_old_format`` prompt text),
  summarizes permanently invalid notices, commits the administrators' ``AccessNoticeV1`` and
  per-``(team_id, issuer_runtime, copy_id)`` receipts atomically in ``AccessRepository``, sends
  one chat notice through the injectable ``ChatNoticeSender`` port (defaulting to a legacy
  ``UserNotification`` envelope until step 6 moves it to ``person.notify``), and holds a batch
  with ``+WPI`` whenever both Inbox and chat fail so outages never burn ``dlq~<team>`` deliveries.
- **Recovery scan (C03, C10):** scans ``UA1_DLQ_<team>`` up to ``dlq~<team>``'s ``cutoff_seq``
  using persisted per-stream scan cursors and per-copy receipts to recover notices stranded by
  crash loops (resetting automatically if a recreated stream's sequence range starts over).
- **CRIT-1 sweep (A4, C08, C15):** sweeps ``UA1_PART_<team>~0..N-1``, ``UA1_POOL_WORK_<team>``,
  and ``UA1_EDGE_WORK_<team>`` (never ``UA1_TEAM_WORK_<team>``, which belongs to the installation
  authority) using ``sweep_exhausted_streams``.
- **Shared raw-copy maintenance scan (C01, C04):** walks ``ua.v1.<team>.*.dlq.raw.*`` once per
  pass to delete both any copy naming a departed person and any orphaned raw copy older than
  the grace period (``max_deliveries * ack_wait_seconds`` plus margin) that has no matching notice.
- **Unified departure operation (A6, C04, C06, C11):** serves ``team.delete_person_data`` on
  ``svc.team`` (replacing unreleased ``blobs.delete_person``), running blob ownership removal +
  deletion and bounded-budget dead-letter raw-copy scanning with at most one background
  continuation per ``(team, person)`` that returns cumulative progress counts on ``retry_later``
  and joins on repeat calls without re-running blob deletion or starting a second scan.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import NotFoundError

from access_control.people import PERSON_ROLE_ADMIN, is_administrator
from access_control.people_directory import PeopleDirectory
from custom_agents.instance_delivery_context import default_notification_gateway_context
from custom_agents.stuck_work import ChatRoute
from custom_messages.contracts.access_v2 import AccessNoticeKind, AccessNoticeV1, PersonStanding
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import RequesterType
from custom_messages.wire.encoding import b64url
from custom_messages.wire.frame import Receiver, VerifiedFrame
from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT
from custom_messages.wire.refusals import FrameRefused
from custom_messages.transport_codec import encode_envelope
from custom_transport.protocol1.dead_letters import (
    PEER_REVOCATIONS_NAK_DELAY_SECONDS,
    DeadLetterSetter,
    DurableRefusalAction,
    OldEnvelopeSummary,
    StoredStreamMessage,
    SweepSummary,
    classify_durable_refusal,
    consumer_cutoff_seq,
    consumer_info,
    delete_stream_message,
    dlq_notice_consumer_name,
    dlq_notice_subject,
    dlq_notice_wildcard_subject,
    dlq_raw_subject,
    dlq_raw_wildcard_subject,
    dlq_stream_name,
    extract_old_envelope_summary,
    get_stream_message,
    hold_with_in_progress,
    message_stored_timestamp_ns,
    parse_dlq_notice_subject,
    parse_dlq_raw_subject,
    raw_copy_names_person,
    stream_info,
    sweep_exhausted_streams,
)
from custom_transport.protocol1.host_runtime import TEAM_SERVICES_RUNTIME
from custom_transport.protocol1.live import LiveHost, answer_headers
from custom_transport.send_barrier import before_send
from custom_transport.subjects import NatsSubjectMapper
from storage.access_repository import AccessRepository
from team_services.blobs import LEAD_HOST_ROLE, BlobService
from team_services.people import GATEWAY_ROLE, command_person
from team_services.service import OperationFamily

_LOGGER = logging.getLogger(__name__)

# Notice watch limits matching custom_agents/dead_letter_watch.py.
WATCH_BATCH = 200
REPORT_QUIET_MINUTES = 15
LISTED_IN_NOTICE = 10
DEAD_LETTER_NOTICE_REASON = "messages_set_aside"

# UA1_DLQ_<team> retention from contracts/messages/v1/transport.yaml (30 days + 60s).
DLQ_MAX_AGE_SECONDS = 2_592_060
DLQ_RETENTION_DAYS = 30
# Keep per-copy reporting receipts strictly longer than UA1_DLQ_<team>'s max_age_seconds (C10).
RECEIPT_RETENTION_SECONDS = DLQ_MAX_AGE_SECONDS + 3_600
# Orphan grace: max_deliveries (20) * ack_wait_seconds (60) + 5m margin (C01).
DEFAULT_ORPHAN_GRACE_SECONDS = 20 * 60 + 300
# Bounded raw-copy scan time budget inside team.delete_person_data's 30s expiry (C04).
DEFAULT_DELETE_BUDGET_SECONDS = 15.0
# Per-pass time budget for periodic raw-copy and recovery scans so upkeep stays responsive (C20).
DEFAULT_SCAN_BUDGET_SECONDS = 10.0
# Batch size for receipt lookups during UA1_DLQ recovery scans (C20).
_RECOVERY_LOOKUP_BATCH = 100
# Bounded in-memory cache of notice ids already sent to chat while awaiting Inbox store recovery (C22).
_MAX_CHAT_SENT_CACHE = 1024
# Default ack_wait_seconds for dlq~<team> from contracts/messages/v1/transport.yaml.
DEFAULT_NOTICE_ACK_WAIT_SECONDS = 60.0

TEAM_PERSON_DATA_KINDS = ("team.delete_person_data",)
TEAM_PERSON_DATA_SIDE_BY_SIDE_KINDS = frozenset(TEAM_PERSON_DATA_KINDS)

_DEPARTED_STANDINGS = frozenset({PersonStanding.DISABLED, PersonStanding.REMOVED})
_SHORT_TEXT_MAX = 500

_REASON_WORDS: Mapping[str, str] = {
    "malformed": "the message was malformed and could not be read",
    "too_large": "the message exceeded the protocol 1 frame size limit",
    "bad_header": "the message's NATS headers did not match its signed frame",
    "unknown_protocol": "the message used an unsupported protocol version",
    "unknown_kind": "this release does not know the message's kind",
    "unknown_key": "the sender's signing key or certificate was not trusted",
    "bad_signature": "the message's signature did not verify",
    "revoked": "the sender's certificate or key was revoked",
    "wrong_issuer": "the sender's issuer fields did not match its certificate",
    "wrong_sender": "the sender's role is not allowed to send this message kind",
    "wrong_audience": "the message was addressed to a different audience",
    "subject_mismatch": "the NATS subject did not match the signed frame",
    "expired": "the message expired before it could be handled",
    "not_yet_valid": "the message's validity window was in the future",
    "bad_authority": "the message's authority token failed verification",
    "invalid_body": "the message's body failed schema validation",
    "replay": "the message replayed an earlier message id with different bytes",
    "handler_failed": "handling it kept failing",
    "delivery_limit": "it ran out of allowed deliveries before a receiver could set it aside",
    "upgrade_drain": "it was waiting in a queue during a contract upgrade and could not be handled",
    "upgrade_old_format": "it was still queued in the pre-0.10 format at the upgrade",
}


class _ReportingUnavailableError(RuntimeError):
    """Internal signal that both Inbox and chat reporting failed for the current batch."""


class ChatNoticeSender(Protocol):
    """Port for sending a dead-letter administrator alert to chat (C05)."""

    async def send_notice(self, *, title: str, text: str, notice_id: str, reason: str) -> bool: ...


@dataclass(frozen=True)
class FormattedAdminNotice:
    """Plain-language title and summary sent to administrators' Inbox and chat."""

    title: str
    text: str


@dataclass(frozen=True)
class _VerifiedNoticeItem:
    issuer_runtime: str
    copy_id: str
    reason: str
    original_subject: str
    original_stream: str
    original_stream_sequence: int
    deliveries: int
    release: str
    issued_at: str
    claimed_kind: Optional[str]
    claimed_message_id: Optional[str]
    pointer: Optional[str]
    raw_stored: bool
    raw_size: int
    raw_status: str  # "ok", "not_stored", "missing", "mismatch", "departed_deleted"
    old_envelope: Optional[OldEnvelopeSummary] = None
    person_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class _InvalidNoticeItem:
    subject_runtime: str
    copy_id: str
    refusal_code: str
    subject: str
    stream_seq: int


@dataclass(frozen=True)
class RawCopyScanReport:
    """Outcome of one shared ``UA1_DLQ_<team>`` raw-copy maintenance pass (C01, C04, C20)."""

    copies_scanned: int
    departed_deleted: int
    orphans_deleted: int
    budget_reached: bool = False
    duration_ms: float = 0.0


@dataclass(frozen=True)
class DeadLetterMaintenanceReport:
    """Summary of one ``DeadLetterService.maintain`` run."""

    sweep: SweepSummary
    recovered_notices: int
    raw_scan: RawCopyScanReport
    receipts_pruned: int


@dataclass(frozen=True)
class PersonDataDeleteReply:
    """Structured outcome of ``TeamPersonDepartureService.delete_person_data``."""

    outcome: str  # "ok", "retry_later", "not_allowed", "runtime_error"
    owners_removed: int = 0
    blobs_deleted: int = 0
    blobs_kept: int = 0
    dlq_copies_deleted: int = 0
    dlq_copies_kept: int = 0
    message: Optional[str] = None

    def to_wire_body(self) -> dict[str, Any]:
        if self.outcome == "ok":
            return {
                "outcome": "ok",
                "owners_removed": self.owners_removed,
                "blobs_deleted": self.blobs_deleted,
                "blobs_kept": self.blobs_kept,
                "dlq_copies_deleted": self.dlq_copies_deleted,
                "dlq_copies_kept": self.dlq_copies_kept,
            }
        if self.outcome == "retry_later":
            body: dict[str, Any] = {
                "outcome": "retry_later",
                "owners_removed": self.owners_removed,
                "blobs_deleted": self.blobs_deleted,
                "blobs_kept": self.blobs_kept,
                "dlq_copies_deleted": self.dlq_copies_deleted,
                "dlq_copies_kept": self.dlq_copies_kept,
            }
            if self.message:
                body["message"] = _short_message(self.message)
            return body
        return {
            "outcome": self.outcome,
            "message": _short_message(self.message or "Departed person data deletion failed."),
        }


@dataclass
class _DepartureRun:
    """Single active departure deletion (initial phase + optional background continuation) per person (C04, C11, C19)."""

    person_ref: str
    person_id: str
    owners_removed: int = 0
    blobs_deleted: int = 0
    blobs_kept: int = 0
    dlq_copies_deleted: int = 0
    dlq_copies_kept: int = 0
    next_seq: int = 1
    initial_done: asyncio.Event = None  # type: ignore[assignment]
    initial_reply: Optional[PersonDataDeleteReply] = None
    initial_error: Optional[Exception] = None
    continuation_task: Optional[asyncio.Task[None]] = None

    def __post_init__(self) -> None:
        if self.initial_done is None:
            self.initial_done = asyncio.Event()


def _short_message(text: str) -> str:
    cleaned = text.strip() or "Dead-letter operation failed."
    if len(cleaned) <= _SHORT_TEXT_MAX:
        return cleaned
    return cleaned[: _SHORT_TEXT_MAX - 3] + "..."


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _ns_to_datetime(ts_ns: int) -> datetime:
    secs, rem_ns = divmod(ts_ns, 1_000_000_000)
    return datetime.fromtimestamp(secs, tz=timezone.utc).replace(microsecond=rem_ns // 1000)


def _notice_id_for_receipts(team_id: str, receipt_keys: Sequence[tuple[str, str]]) -> str:
    canonical = json.dumps([team_id, sorted(set(receipt_keys))], separators=(",", ":"))
    return f"p1_dlq:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"[:120]


def _verified_item_kind(item: _VerifiedNoticeItem) -> str:
    if item.reason == "upgrade_old_format":
        if item.old_envelope is not None and item.old_envelope.message_class:
            return item.old_envelope.message_class
        return "old-format message"
    return item.claimed_kind or "an unreadable message"


def _verified_line_extras(item: _VerifiedNoticeItem, dlq_stream: str) -> list[str]:
    status_notes = {
        "missing": f"raw copy is missing from {dlq_stream}",
        "mismatch": "raw copy did not match the notice's size or SHA-256",
        "not_stored": "original message exceeded the frame limit so its bytes were not kept",
        "departed_deleted": "raw copy belonged to a departed person (a person who has left) and was deleted immediately",
    }
    note = status_notes.get(item.raw_status)
    if note is not None:
        return [note]
    if item.reason != "upgrade_old_format" or item.old_envelope is None:
        return []
    extras: list[str] = []
    if item.person_labels:
        extras.append(f"for {', '.join(item.person_labels)}")
    if item.old_envelope.task_id:
        extras.append(f"task {item.old_envelope.task_id}")
    if item.old_envelope.request_id:
        extras.append(f"request {item.old_envelope.request_id}")
    return extras


def _format_verified_line(item: _VerifiedNoticeItem, dlq_stream: str) -> str:
    kind = _verified_item_kind(item)
    reason_words = _REASON_WORDS.get(item.reason, item.reason)
    msg_id = f" ({item.claimed_message_id})" if item.claimed_message_id else ""
    pointer_part = f" at {item.pointer}" if item.pointer else ""
    release_part = f", set aside by {item.issuer_runtime} (release {item.release})"
    base = (
        f"- {kind}{msg_id} from {item.original_stream}#{item.original_stream_sequence} "
        f"({item.original_subject}): {reason_words}{pointer_part}{release_part}, "
        f"at {item.issued_at}; copy {item.copy_id}"
    )
    extras = _verified_line_extras(item, dlq_stream)
    return f"{base} ({'; '.join(extras)})." if extras else f"{base}."


def _format_invalid_line(item: _InvalidNoticeItem) -> str:
    return (
        f"- invalid dead-letter notice from {item.subject_runtime} on {item.subject} "
        f"(stream sequence {item.stream_seq}): refused as {item.refusal_code}; copy {item.copy_id}."
    )


def _advice_for_batch(
    verified: Sequence[_VerifiedNoticeItem],
    invalid: Sequence[_InvalidNoticeItem],
) -> str:
    parts: list[str] = []
    if any(item.reason == "upgrade_old_format" for item in verified):
        parts.append(
            "Old-format messages from before the 0.10 upgrade could not run on the new protocol; "
            "ask the named person to send any needed request again."
        )
    if any(item.reason == "delivery_limit" for item in verified):
        parts.append(
            "Messages that ran out of deliveries were swept after no receiver finished handling them; "
            "check receiver logs for repeated crashes or errors."
        )
    if any(item.reason == "handler_failed" for item in verified):
        parts.append("Handling failed repeatedly; check receiver logs for the error.")
    if any(
        item.reason not in {"upgrade_old_format", "delivery_limit", "handler_failed", "upgrade_drain"}
        for item in verified
    ):
        parts.append(
            "Messages that failed verification usually come from an out-of-date or misconfigured host; "
            "run the same release and trust records everywhere."
        )
    if invalid:
        parts.append(
            "One or more notices in the dead-letter stream failed verification and were summarized by "
            "their subject and refusal code."
        )
    return " ".join(parts) or "Inspect the logs and the dead-letter stream to see why the messages were set aside."


def format_dlq_admin_notice(
    verified: Sequence[_VerifiedNoticeItem],
    invalid: Sequence[_InvalidNoticeItem],
    *,
    dlq_stream: str,
    waiting_after: int = 0,
) -> FormattedAdminNotice:
    """Build plain-language title and body for administrators matching ``custom_agents/dead_letter_watch.py``."""

    total = len(verified) + len(invalid)
    lines: list[str] = []
    for item in verified:
        if len(lines) >= LISTED_IN_NOTICE:
            break
        lines.append(_format_verified_line(item, dlq_stream))
    for inv in invalid:
        if len(lines) >= LISTED_IN_NOTICE:
            break
        lines.append(_format_invalid_line(inv))
    if total > LISTED_IN_NOTICE:
        lines.append(f"- and {total - LISTED_IN_NOTICE} more.")
    if waiting_after > 0:
        lines.append(f"- {waiting_after} more were set aside after these; the next notice lists them.")
    advice = _advice_for_batch(verified, invalid)
    text = (
        "\n".join(lines)
        + f"\n\n{advice} The messages are kept for up to {DLQ_RETENTION_DAYS} days in the NATS stream "
        f"{dlq_stream} for an operator to look at; nothing was sent to anyone in their place."
    )
    return FormattedAdminNotice(
        title=f"{total} message(s) could not be handled and were set aside",
        text=text,
    )


def owner_chat_route_from_config(config: Any) -> Optional[ChatRoute]:
    """Build the owner's proactive ``ChatRoute`` directly from a loaded ``GatewayInstanceConfig``."""

    route = str(config.manager.response_queue_name or "").strip()
    if not route:
        return None
    context = default_notification_gateway_context(config)
    if context.channel_name not in {"cli", "local"} and not context.chat_id:
        return None
    return ChatRoute(routing_key=route, gateway_context=context)


class LegacyNatsChatNoticeSender:
    """Send administrator dead-letter chat notices as legacy ``UserNotification`` envelopes (C05).

    Replaced by ``person.notify`` once step 6 moves gateway notifications onto protocol 1.
    """

    def __init__(
        self,
        *,
        nc_fn: Callable[[], Any],
        mapper: NatsSubjectMapper,
        people: PeopleDirectory,
        owner_person_id: str,
        route_fn: Callable[[], Optional[ChatRoute]],
        sender_id: str = TEAM_SERVICES_RUNTIME,
        logger: logging.Logger = _LOGGER,
    ) -> None:
        self._nc_fn = nc_fn
        self._mapper = mapper
        self._people = people
        self._owner_person_id = owner_person_id
        self._route_fn = route_fn
        self._sender_id = sender_id
        self._logger = logger

    def _active_admin_route(self) -> Optional[ChatRoute]:
        person = self._people.person(self._owner_person_id)
        if not person.active or not is_administrator(person.roles):
            return None
        return self._route_fn()

    def build_envelope(
        self,
        route: ChatRoute,
        *,
        title: str,
        text: str,
        notice_id: str,
        reason: str,
    ) -> MessageEnvelope:
        context = route.gateway_context.model_copy(
            update={
                "scheduled_job_id": None,
                "schedule_owner_agent_id": None,
                "heartbeat_source_id": None,
                "delivery_mode": "autonomous",
            }
        )
        return MessageEnvelope(
            msg_content=UserNotification(
                text=f"{title}\n\n{text}",
                urgency="normal",
                reason=reason,
                source_task_id=None,
                source_agent_id=self._sender_id,
                source_agent_name="Team Services",
            ),
            msg_meta=MessageMeta(
                from_agent_name="Team Services",
                msg_correlation_id=notice_id,
                requester_type=RequesterType.AGENT_REQUESTER,
                is_response_requested=False,
                gateway_context=context,
            ),
            msg_header=MessageHeader(
                from_agent_id=self._sender_id,
                to_routing_path=route.routing_key,
                response_routing_key=self._sender_id,
                idempotency_key=notice_id,
            ),
        )

    async def send_notice(self, *, title: str, text: str, notice_id: str, reason: str) -> bool:
        try:
            route = await asyncio.to_thread(self._active_admin_route)
            if route is None:
                return False
            nc = self._nc_fn()
            if nc is None:
                return False
            envelope = self.build_envelope(route, title=title, text=text, notice_id=notice_id, reason=reason)
            subject = self._mapper.route_subject(route.routing_key, envelope=envelope)
            payload = encode_envelope(envelope)
            headers = {
                "Nats-Msg-Id": notice_id,
                "UA-Message-Class": "UserNotification",
                "UA-Correlation-Id": notice_id,
                "UA-From-Agent": self._sender_id,
            }
            await before_send()
            js = nc.jetstream()
            await js.publish(subject, payload, headers=headers)
            return True
        except Exception:
            self._logger.error(
                "Could not send the dead-letter chat notice to administrators. "
                "event=protocol1_dlq_chat_failed notice_id=%s",
                notice_id,
                exc_info=True,
            )
            return False


class DeadLetterService:
    """Team-services dead-letter notice watch, recovery scan, CRIT-1 sweep, and raw-copy upkeep."""

    def __init__(
        self,
        *,
        team_id: str,
        live_fn: Callable[[], Optional[LiveHost]],
        access_repository: AccessRepository,
        people: PeopleDirectory,
        chat_sender: Optional[ChatNoticeSender] = None,
        refresh_trust_fn: Optional[Callable[[], Any]] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        logger: logging.Logger = _LOGGER,
        report_quiet_seconds: float = REPORT_QUIET_MINUTES * 60.0,
        fetch_timeout_seconds: float = 2.0,
        ack_wait_seconds: float = DEFAULT_NOTICE_ACK_WAIT_SECONDS,
        initial_retry_seconds: float = 1.0,
        max_retry_seconds: float = 30.0,
        orphan_grace_seconds: int = DEFAULT_ORPHAN_GRACE_SECONDS,
        receipt_retention_seconds: int = RECEIPT_RETENTION_SECONDS,
        scan_budget_seconds: float = DEFAULT_SCAN_BUDGET_SECONDS,
        api_timeout_seconds: float = 5.0,
    ) -> None:
        self._team_id = team_id
        self._dlq_stream = dlq_stream_name(team_id)
        self._consumer_name = dlq_notice_consumer_name(team_id)
        self._live_fn = live_fn
        self._access = access_repository
        self._people = people
        self._chat_sender = chat_sender
        self._refresh_trust_fn = refresh_trust_fn
        self._clock = clock
        self._logger = logger
        self._report_quiet_seconds = max(0.0, float(report_quiet_seconds))
        self._fetch_timeout_seconds = max(0.05, float(fetch_timeout_seconds))
        self._ack_wait_seconds = max(0.05, float(ack_wait_seconds))
        self._initial_retry_seconds = max(0.01, float(initial_retry_seconds))
        self._max_retry_seconds = max(self._initial_retry_seconds, float(max_retry_seconds))
        self._orphan_grace_seconds = max(0, int(orphan_grace_seconds))
        self._receipt_retention_seconds = max(DLQ_MAX_AGE_SECONDS, int(receipt_retention_seconds))
        self._scan_budget_seconds = max(0.0, float(scan_budget_seconds))
        self._api_timeout_seconds = max(0.2, float(api_timeout_seconds))

        self._last_reported_at: Optional[datetime] = None
        self._logged_stranded: set[tuple[str, int]] = set()
        self._missing_consumer_logged = False
        self._pull_sub: Any = None
        self._pause_until: float = 0.0
        # C20 & C23: bounded incremental raw-copy scan state (skip confirmed-paired copies unless departed set changes;
        # track in-progress departed re-walk targets so budget-paused passes resume instead of restarting at first_seq).
        self._raw_scan_stream_created: str = ""
        self._orphan_confirmed_seq: int = 0
        self._raw_scan_resume_seq: Optional[int] = None
        self._raw_scan_saw_young: bool = False
        self._last_departed_targets: Optional[tuple[frozenset[str], frozenset[str]]] = None
        self._departed_rewalk_targets: Optional[tuple[frozenset[str], frozenset[str]]] = None
        # C22: bounded in-memory set of notice ids already sent to chat while awaiting Inbox store recovery.
        self._chat_sent_notice_ids: dict[str, None] = {}

    @property
    def team_id(self) -> str:
        return self._team_id

    @property
    def dlq_stream(self) -> str:
        return self._dlq_stream

    def _require_live(self) -> LiveHost:
        live = self._live_fn()
        if live is None:
            raise RuntimeError("Team services LiveHost is not started yet.")
        return live

    def _dlq_receiver(self, live: LiveHost) -> Receiver:
        return Receiver(
            installation=live.identity.installation,
            team=self._team_id,
            target="team_services",
            id=self._team_id,
            runtime=live.identity.runtime,
        )

    def _departed_person_targets(self) -> tuple[frozenset[str], frozenset[str]]:
        records = self._people.store.team_people(self._team_id)
        refs = frozenset(
            rec.person_ref
            for rec in records
            if rec.standing in _DEPARTED_STANDINGS and rec.person_ref
        )
        ids = frozenset(
            rec.person_id
            for rec in records
            if rec.standing in _DEPARTED_STANDINGS and rec.person_id
        )
        return refs, ids

    def sweep_stream_names(self, live: LiveHost) -> tuple[str, ...]:
        """Team work streams swept by ``team_services`` (never ``UA1_TEAM_WORK_<team>``)."""

        partition_count = int(getattr(live.trust.store, "partition_count", DEFAULT_PARTITION_COUNT))
        parts = tuple(f"UA1_PART_{self._team_id}~{idx}" for idx in range(partition_count))
        return (*parts, f"UA1_POOL_WORK_{self._team_id}", f"UA1_EDGE_WORK_{self._team_id}")

    # -- lifecycle hooks -----------------------------------------------------------------------

    async def maintain(self) -> DeadLetterMaintenanceReport:
        """Periodic upkeep: CRIT-1 sweep, DLQ notice recovery scan, shared raw-copy scan, and receipt pruning."""

        sweep = await self.sweep_work_streams()
        recovered = await self.recover_exhausted_notices()
        raw_scan = await self.scan_raw_copies()
        cutoff = _utc(self._clock()) - timedelta(seconds=self._receipt_retention_seconds)
        pruned = await asyncio.to_thread(
            self._access.prune_dlq_reported_notices, self._team_id, before_utc=cutoff
        )
        return DeadLetterMaintenanceReport(
            sweep=sweep,
            recovered_notices=recovered,
            raw_scan=raw_scan,
            receipts_pruned=pruned,
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Run the ``dlq~<team>`` notice watch continuously until ``stop`` fires."""

        while not stop.is_set():
            wait_seconds = self._next_poll_delay_seconds()
            if wait_seconds > 0:
                if await self._wait_stop(stop, wait_seconds):
                    return
                continue
            try:
                await self.poll_notices_once(stop=stop)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception(
                    "Dead-letter notice watch pass failed; retrying shortly. "
                    "event=protocol1_dlq_watch_failed team=%s",
                    self._team_id,
                )
                self._pull_sub = None
                if await self._wait_stop(stop, min(5.0, self._max_retry_seconds)):
                    return

    def _next_poll_delay_seconds(self) -> float:
        now_mono = time.monotonic()
        if self._pause_until > now_mono:
            return self._pause_until - now_mono
        if self._last_reported_at is not None and self._report_quiet_seconds > 0:
            elapsed = (_utc(self._clock()) - self._last_reported_at).total_seconds()
            remaining = self._report_quiet_seconds - elapsed
            if remaining > 0:
                return min(remaining, 5.0)
        return 0.0

    @staticmethod
    async def _wait_stop(stop: asyncio.Event, timeout: float) -> bool:
        if stop.is_set():
            return True
        try:
            await asyncio.wait_for(stop.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return stop.is_set()

    # -- notice watch (A5, C02, C03, C04, C05) -------------------------------------------------

    async def _ensure_pull_subscription(self, live: LiveHost) -> Any:
        if self._pull_sub is not None:
            return self._pull_sub
        js = live.nc.jetstream()
        try:
            self._pull_sub = await js.pull_subscribe_bind(self._consumer_name, stream=self._dlq_stream)
            self._missing_consumer_logged = False
            return self._pull_sub
        except NotFoundError:
            if not self._missing_consumer_logged:
                self._logger.info(
                    "Dead-letter notice consumer is not created yet; waiting for topology setup. "
                    "event=protocol1_dlq_consumer_missing stream=%s consumer=%s",
                    self._dlq_stream,
                    self._consumer_name,
                )
                self._missing_consumer_logged = True
            return None

    async def poll_notices_once(self, *, stop: Optional[asyncio.Event] = None) -> int:
        """Pull up to ``WATCH_BATCH`` notices from ``dlq~<team>``, verify, report, and ack."""

        live = self._require_live()
        sub = await self._ensure_pull_subscription(live)
        if sub is None:
            self._pause_until = time.monotonic() + min(5.0, self._max_retry_seconds)
            return 0
        try:
            messages = await sub.fetch(WATCH_BATCH, timeout=self._fetch_timeout_seconds)
        except (NatsTimeoutError, TimeoutError, asyncio.TimeoutError):
            return 0
        if not messages:
            return 0
        return await self.process_delivered_notices(messages, stop=stop)

    async def _pause_for_stale_revocations(self) -> None:
        if self._refresh_trust_fn is not None:
            with contextlib.suppress(Exception):
                await self._refresh_trust_fn()
        self._pause_until = time.monotonic() + min(5.0, self._max_retry_seconds)

    async def process_delivered_notices(
        self,
        messages: Sequence[Any],
        *,
        stop: Optional[asyncio.Event] = None,
    ) -> int:
        """Process one batch of delivered ``dlq~<team>`` messages (also callable directly in tests)."""

        if not messages:
            return 0
        live = self._require_live()
        now = _utc(self._clock())
        receiver = self._dlq_receiver(live)
        departed_refs, departed_ids = await asyncio.to_thread(self._departed_person_targets)

        verified_items: list[_VerifiedNoticeItem] = []
        invalid_items: list[_InvalidNoticeItem] = []
        ackable_messages: list[Any] = []

        for msg in messages:
            if stop is not None and stop.is_set():
                break
            outcome = await self._inspect_delivered_notice(
                live, msg, receiver=receiver, now=now, departed_refs=departed_refs, departed_ids=departed_ids
            )
            if outcome == "pause":
                await self._pause_for_stale_revocations()
                break
            if outcome == "nak_peer":
                continue
            ackable_messages.append(msg)
            self._append_notice_outcome(outcome, verified_items, invalid_items)

        if not ackable_messages:
            return 0
        waiting_after = int(getattr(getattr(ackable_messages[-1], "metadata", None), "num_pending", 0) or 0)
        return await self._report_and_ack_batch(
            verified_items, invalid_items, ackable_messages=ackable_messages, waiting_after=waiting_after, stop=stop
        )

    @staticmethod
    def _append_notice_outcome(
        outcome: _VerifiedNoticeItem | _InvalidNoticeItem | str,
        verified_items: list[_VerifiedNoticeItem],
        invalid_items: list[_InvalidNoticeItem],
    ) -> None:
        if isinstance(outcome, _VerifiedNoticeItem):
            verified_items.append(outcome)
        elif isinstance(outcome, _InvalidNoticeItem):
            invalid_items.append(outcome)

    async def _inspect_delivered_notice(
        self,
        live: LiveHost,
        msg: Any,
        *,
        receiver: Receiver,
        now: datetime,
        departed_refs: frozenset[str],
        departed_ids: frozenset[str],
    ) -> _VerifiedNoticeItem | _InvalidNoticeItem | str:
        raw_bytes = bytes(msg.data)
        subject = str(msg.subject)
        headers = answer_headers(msg)
        stream_seq = int(getattr(getattr(getattr(msg, "metadata", None), "sequence", None), "stream", 0) or 0)
        stored_at = _ns_to_datetime(message_stored_timestamp_ns(msg))
        try:
            verified = await live.verify(
                raw_bytes,
                receiver=receiver,
                arrived_on=subject,
                now=now,
                replay=lambda _key: None,
                headers=headers,
                stored_at=stored_at,
            )
        except FrameRefused as refused:
            action = classify_durable_refusal(
                refused, own_installation=live.identity.installation, raw_bytes=raw_bytes
            )
            if action is DurableRefusalAction.PAUSE_PULL:
                self._logger.warning(
                    "Pausing dead-letter notice watch because own-installation revocations are not current. "
                    "event=protocol1_dlq_watch_revocations_stale stream=%s seq=%s",
                    self._dlq_stream,
                    stream_seq,
                )
                return "pause"
            if action is DurableRefusalAction.NAK_DELAY:
                await msg.nak(delay=PEER_REVOCATIONS_NAK_DELAY_SECONDS)
                return "nak_peer"
            return self._invalid_item_from_refusal(subject, raw_bytes, stream_seq, refused.code)

        if verified.frame.kind != "dlq.notice":
            return self._invalid_item_from_refusal(subject, raw_bytes, stream_seq, "unknown_kind")

        return await self._inspect_verified_notice(
            live,
            verified,
            departed_refs=departed_refs,
            departed_ids=departed_ids,
        )

    def _invalid_item_from_refusal(
        self, subject: str, raw_bytes: bytes, stream_seq: int, refusal_code: str
    ) -> _InvalidNoticeItem:
        parsed = parse_dlq_notice_subject(subject, self._team_id)
        if parsed is not None:
            runtime, copy_id = parsed
        else:
            runtime = "_invalid_subject"
            copy_id = hashlib.sha256(f"{subject}\x00".encode("utf-8") + raw_bytes).hexdigest()
        self._logger.warning(
            "Dead-letter notice failed verification; summarizing for administrators and recording receipt. "
            "event=protocol1_dlq_invalid_notice stream=%s seq=%s subject=%s runtime=%s copy_id=%s refusal=%s",
            self._dlq_stream,
            stream_seq,
            subject,
            runtime,
            copy_id,
            refusal_code,
        )
        return _InvalidNoticeItem(
            subject_runtime=runtime,
            copy_id=copy_id,
            refusal_code=refusal_code,
            subject=subject,
            stream_seq=stream_seq,
        )

    async def _inspect_verified_notice(
        self,
        live: LiveHost,
        verified: VerifiedFrame,
        *,
        departed_refs: frozenset[str],
        departed_ids: frozenset[str],
    ) -> _VerifiedNoticeItem:
        body = verified.raw["body"]
        issuer_runtime = str(verified.frame.issuer.runtime)
        copy_id = str(body["copy_id"])
        reason = str(body["reason"])
        original = body["original"]
        raw_info = body["raw"]
        claimed = body.get("claimed") if isinstance(body.get("claimed"), Mapping) else {}

        raw_stored = bool(raw_info["stored"])
        raw_size = int(raw_info["size"])
        raw_sha256 = str(raw_info["sha256"])
        raw_status = "not_stored"
        old_envelope: Optional[OldEnvelopeSummary] = None

        if raw_stored:
            raw_status, old_envelope = await self._verify_raw_copy(
                live.nc,
                issuer_runtime=issuer_runtime,
                copy_id=copy_id,
                reason=reason,
                expected_size=raw_size,
                expected_sha256=raw_sha256,
                departed_refs=departed_refs,
                departed_ids=departed_ids,
            )

        person_labels: tuple[str, ...] = ()
        if reason == "upgrade_old_format" and old_envelope is not None and old_envelope.person_ids:
            labels: list[str] = []
            for pid in sorted(old_envelope.person_ids):
                label = "a person who has left" if pid in departed_ids else pid
                if label not in labels:
                    labels.append(label)
            person_labels = tuple(labels)

        return _VerifiedNoticeItem(
            issuer_runtime=issuer_runtime,
            copy_id=copy_id,
            reason=reason,
            original_subject=str(original["subject"]),
            original_stream=str(original["stream"]),
            original_stream_sequence=int(original["stream_sequence"]),
            deliveries=int(original["deliveries"]),
            release=str(body["release"]),
            issued_at=str(verified.frame.issued_at),
            claimed_kind=str(claimed["kind"]) if "kind" in claimed else None,
            claimed_message_id=str(claimed["message_id"]) if "message_id" in claimed else None,
            pointer=str(body["pointer"]) if "pointer" in body else None,
            raw_stored=raw_stored,
            raw_size=raw_size,
            raw_status=raw_status,
            old_envelope=old_envelope,
            person_labels=person_labels,
        )

    async def _verify_raw_copy(
        self,
        nc: Any,
        *,
        issuer_runtime: str,
        copy_id: str,
        reason: str,
        expected_size: int,
        expected_sha256: str,
        departed_refs: frozenset[str],
        departed_ids: frozenset[str],
    ) -> tuple[str, Optional[OldEnvelopeSummary]]:
        raw_subj = dlq_raw_subject(self._team_id, issuer_runtime, copy_id)
        stored = await get_stream_message(
            nc, self._dlq_stream, last_by_subj=raw_subj, timeout=self._api_timeout_seconds
        )
        if stored is None:
            return "missing", None

        # C04: Check departed standing immediately when fetching any raw copy; delete straight away
        # and never surface departed upgrade_old_format prompt text in administrator notices.
        if (departed_refs or departed_ids) and raw_copy_names_person(
            stored.data, person_refs=departed_refs, person_ids=departed_ids
        ):
            await delete_stream_message(nc, self._dlq_stream, stored.seq, timeout=self._api_timeout_seconds)
            self._logger.info(
                "Deleted raw dead-letter copy naming a departed person during notice watch. "
                "event=protocol1_dlq_departed_copy_deleted_on_watch stream=%s seq=%s runtime=%s copy_id=%s",
                self._dlq_stream,
                stored.seq,
                issuer_runtime,
                copy_id,
            )
            return "departed_deleted", None

        actual_sha = b64url(hashlib.sha256(stored.data).digest())
        if len(stored.data) != expected_size or actual_sha != expected_sha256:
            return "mismatch", None

        if reason == "upgrade_old_format":
            return "ok", extract_old_envelope_summary(stored.data)
        return "ok", None

    async def _report_and_ack_batch(
        self,
        verified_items: Sequence[_VerifiedNoticeItem],
        invalid_items: Sequence[_InvalidNoticeItem],
        *,
        ackable_messages: Sequence[Any],
        waiting_after: int,
        stop: Optional[asyncio.Event],
    ) -> int:
        # C02: Filter out any copy already recorded in protocol1_dlq_reported_notices before building
        # the notice and chat message (handles crash-before-ack, changed batches, and duplicates).
        candidate_keys = [
            *((item.issuer_runtime, item.copy_id) for item in verified_items),
            *((item.subject_runtime, item.copy_id) for item in invalid_items),
        ]
        already_reported = await asyncio.to_thread(
            self._access.dlq_reported_copy_keys, self._team_id, candidate_keys
        )
        fresh_verified = [
            item for item in verified_items if (item.issuer_runtime, item.copy_id) not in already_reported
        ]
        fresh_invalid = [
            item for item in invalid_items if (item.subject_runtime, item.copy_id) not in already_reported
        ]
        if not fresh_verified and not fresh_invalid:
            for msg in ackable_messages:
                await msg.ack()
            return 0

        receipt_keys = [
            *((item.issuer_runtime, item.copy_id) for item in fresh_verified),
            *((item.subject_runtime, item.copy_id) for item in fresh_invalid),
        ]
        formatted = format_dlq_admin_notice(
            fresh_verified, fresh_invalid, dlq_stream=self._dlq_stream, waiting_after=waiting_after
        )
        notice_id = _notice_id_for_receipts(self._team_id, receipt_keys)

        delivered = await self._deliver_holding_on_outage(
            ackable_messages=ackable_messages,
            notice_id=notice_id,
            formatted=formatted,
            receipt_keys=receipt_keys,
            stop=stop,
        )
        if not delivered:
            return 0

        for msg in ackable_messages:
            await msg.ack()
        self._last_reported_at = _utc(self._clock())
        self._logger.warning(
            "Told administrators about protocol 1 set-aside messages. "
            "event=protocol1_dlq_notices_reported team=%s notice_id=%s count=%s invalid=%s",
            self._team_id,
            notice_id,
            len(fresh_verified) + len(fresh_invalid),
            len(fresh_invalid),
        )
        return len(fresh_verified) + len(fresh_invalid)

    def _remember_chat_sent(self, notice_id: str) -> None:
        self._chat_sent_notice_ids[notice_id] = None
        if len(self._chat_sent_notice_ids) > _MAX_CHAT_SENT_CACHE:
            oldest = next(iter(self._chat_sent_notice_ids))
            self._chat_sent_notice_ids.pop(oldest, None)

    def _forget_chat_sent(self, notice_id: str) -> None:
        self._chat_sent_notice_ids.pop(notice_id, None)

    async def _deliver_holding_on_outage(
        self,
        *,
        ackable_messages: Sequence[Any],
        notice_id: str,
        formatted: FormattedAdminNotice,
        receipt_keys: Sequence[tuple[str, str]],
        stop: Optional[asyncio.Event],
    ) -> bool:
        async def attempt_deliver() -> bool:
            now = _utc(self._clock())
            inbox_ok, inbox_created = await self._try_store_inbox_with_receipts(
                notice_id, formatted, receipt_keys, now
            )
            chat_sender = self._chat_sender
            if (
                chat_sender is not None
                and notice_id not in self._chat_sent_notice_ids
                and (not inbox_ok or inbox_created)
            ):
                chat_ok = await chat_sender.send_notice(
                    title=formatted.title,
                    text=formatted.text,
                    notice_id=notice_id,
                    reason=DEAD_LETTER_NOTICE_REASON,
                )
                if chat_ok:
                    self._remember_chat_sent(notice_id)
            if not inbox_ok:
                # C22: Keep holding until Inbox + receipts commit so recovery scan cannot duplicate chat alerts.
                raise _ReportingUnavailableError(
                    f"Inbox notice and receipt store failed for {notice_id}"
                )
            self._forget_chat_sent(notice_id)
            return True

        def log_holding(exc: Exception) -> None:
            self._logger.error(
                "Could not tell administrators about set-aside messages; holding batch with +WPI until reporting recovers. "
                "event=protocol1_dlq_watch_reporting_holding team=%s notice_id=%s count=%s error=%s",
                self._team_id,
                notice_id,
                len(receipt_keys),
                exc,
            )

        if not ackable_messages:
            # Recovery scan path has no JetStream delivery handle to send +WPI on; try once and let next pass retry.
            try:
                return await attempt_deliver()
            except _ReportingUnavailableError as exc:
                log_holding(exc)
                return False

        outcome = await hold_with_in_progress(
            lambda: ackable_messages,
            attempt_deliver,
            ack_wait_seconds=self._ack_wait_seconds,
            initial_retry_seconds=self._initial_retry_seconds,
            max_retry_seconds=self._max_retry_seconds,
            retryable=lambda exc: isinstance(exc, _ReportingUnavailableError),
            on_first_failure=log_holding,
            stop=stop,
            logger=self._logger,
        )
        return bool(outcome)

    async def _try_store_inbox_with_receipts(
        self,
        notice_id: str,
        formatted: FormattedAdminNotice,
        receipt_keys: Sequence[tuple[str, str]],
        now: datetime,
    ) -> tuple[bool, bool]:
        notice = AccessNoticeV1(
            notice_id=notice_id[:120],
            team_id=self._team_id,
            kind=AccessNoticeKind.MESSAGES_SET_ASIDE,
            recipient_roles=(PERSON_ROLE_ADMIN,),
            title=formatted.title[:400],
            summary=formatted.text[:4000],
            created_at_utc=now,
        )
        try:
            notice_created, receipts_inserted = await asyncio.to_thread(
                self._access.record_dlq_notice_report,
                self._team_id,
                notice,
                receipt_keys,
                reported_at_utc=now,
            )
            return True, bool(notice_created or receipts_inserted > 0)
        except Exception:
            self._logger.exception(
                "Could not store dead-letter Inbox notice and receipts. "
                "event=protocol1_dlq_inbox_store_failed team=%s notice_id=%s",
                self._team_id,
                notice_id,
            )
            return False, False

    # -- recovery scan over UA1_DLQ_<team> (C03, C10, C20) -------------------------------------

    async def _fetch_stored_notice_batch(
        self,
        nc: Any,
        *,
        start_seq: int,
        cutoff: int,
        wildcard: str,
        deadline: float,
        stop: Optional[asyncio.Event],
    ) -> tuple[list[StoredStreamMessage], int, bool]:
        """Read up to ``_RECOVERY_LOOKUP_BATCH`` notices in ``[start_seq, cutoff]`` within ``deadline``."""

        batch: list[StoredStreamMessage] = []
        next_seq = start_seq
        reached_end = False
        while next_seq <= cutoff and len(batch) < _RECOVERY_LOOKUP_BATCH:
            if stop is not None and stop.is_set():
                return batch, next_seq, False
            stored = await get_stream_message(
                nc, self._dlq_stream, seq=next_seq, next_by_subj=wildcard, timeout=self._api_timeout_seconds
            )
            if stored is None or stored.seq > cutoff:
                reached_end = True
                break
            batch.append(stored)
            next_seq = stored.seq + 1
            if time.monotonic() >= deadline and next_seq <= cutoff:
                return batch, next_seq, False
        if next_seq > cutoff:
            reached_end = True
        return batch, next_seq, reached_end

    async def _filter_unreported_stored_notices(
        self, batch: Sequence[StoredStreamMessage]
    ) -> list[StoredStreamMessage]:
        """Filter out notices already recorded in ``protocol1_dlq_reported_notices`` in one batched DB call (C20)."""

        parsed_keys = [
            key
            for stored in batch
            if (key := parse_dlq_notice_subject(stored.subject, self._team_id)) is not None
        ]
        already = (
            await asyncio.to_thread(self._access.dlq_reported_copy_keys, self._team_id, parsed_keys)
            if parsed_keys
            else set()
        )
        return [
            stored
            for stored in batch
            if parse_dlq_notice_subject(stored.subject, self._team_id) not in already
        ]

    async def _inspect_unreported_batch(
        self,
        live: LiveHost,
        batch: Sequence[StoredStreamMessage],
        *,
        receiver: Receiver,
        now: datetime,
        departed_refs: frozenset[str],
        departed_ids: frozenset[str],
        verified_items: list[_VerifiedNoticeItem],
        invalid_items: list[_InvalidNoticeItem],
    ) -> bool:
        """Inspect all unreported notices in ``batch``; returns False if temporarily stalled."""

        unreported = await self._filter_unreported_stored_notices(batch)
        for stored in unreported:
            outcome = await self._inspect_stored_notice(
                live, stored, receiver=receiver, now=now, departed_refs=departed_refs, departed_ids=departed_ids
            )
            if outcome == "temporary":
                return False
            self._append_notice_outcome(outcome, verified_items, invalid_items)
        return True

    async def _collect_unreported_notices_in_range(
        self,
        live: LiveHost,
        *,
        start_seq: int,
        cutoff: int,
        stop: Optional[asyncio.Event],
        deadline: float,
    ) -> Optional[tuple[list[_VerifiedNoticeItem], list[_InvalidNoticeItem], int, int]]:
        """Scan ``[start_seq, cutoff]`` in batched lookups within ``deadline``.

        Returns ``(verified, invalid, scanned_up_to_seq, messages_scanned)`` or ``None`` if interrupted/stalled.
        """

        now = _utc(self._clock())
        receiver = self._dlq_receiver(live)
        departed_refs, departed_ids = await asyncio.to_thread(self._departed_person_targets)
        verified_items: list[_VerifiedNoticeItem] = []
        invalid_items: list[_InvalidNoticeItem] = []
        next_seq = start_seq
        scanned_up_to = start_seq - 1
        messages_scanned = 0
        wildcard = dlq_notice_wildcard_subject(self._team_id)

        while next_seq <= cutoff:
            if stop is not None and stop.is_set():
                return None
            batch, next_seq, reached_end = await self._fetch_stored_notice_batch(
                live.nc, start_seq=next_seq, cutoff=cutoff, wildcard=wildcard, deadline=deadline, stop=stop
            )
            messages_scanned += len(batch)
            if batch:
                ok = await self._inspect_unreported_batch(
                    live,
                    batch,
                    receiver=receiver,
                    now=now,
                    departed_refs=departed_refs,
                    departed_ids=departed_ids,
                    verified_items=verified_items,
                    invalid_items=invalid_items,
                )
                if not ok:
                    return None
                scanned_up_to = batch[-1].seq
            if reached_end:
                scanned_up_to = cutoff
                break
            if time.monotonic() >= deadline:
                break
        return verified_items, invalid_items, scanned_up_to, messages_scanned

    async def _recovery_scan_bounds(
        self, live: LiveHost
    ) -> Optional[tuple[int, int, int, str]]:
        """Return ``(start_seq, cutoff, last_scanned, stream_created)`` or ``None`` if nothing to scan."""

        info = await stream_info(live.nc, self._dlq_stream, timeout=self._api_timeout_seconds)
        if info is None or info.messages == 0:
            return None
        consumer = await consumer_info(
            live.nc, self._dlq_stream, self._consumer_name, timeout=self._api_timeout_seconds
        )
        if consumer is None:
            return None
        cutoff = consumer_cutoff_seq(consumer)
        if cutoff <= 0 or cutoff < info.first_seq:
            return None

        last_scanned, saved_created = await asyncio.to_thread(
            self._access.get_dlq_scan_cursor, self._team_id, self._dlq_stream
        )
        # C10: Reset persisted cursor if UA1_DLQ_<team> was recreated with a fresh sequence range.
        if (saved_created and info.created and saved_created != info.created) or info.last_seq < last_scanned:
            last_scanned = 0
        if cutoff <= last_scanned:
            return None
        return max(info.first_seq, last_scanned + 1), cutoff, last_scanned, info.created

    async def recover_exhausted_notices(self, *, stop: Optional[asyncio.Event] = None) -> int:
        """Scan ``(last_scanned_seq, cutoff_seq]`` on ``UA1_DLQ_<team>`` in bounded passes to recover stranded notices."""

        started = time.monotonic()
        live = self._require_live()
        bounds = await self._recovery_scan_bounds(live)
        if bounds is None:
            return 0
        start_seq, cutoff, last_scanned, stream_created = bounds

        deadline = started + self._scan_budget_seconds
        collected = await self._collect_unreported_notices_in_range(
            live, start_seq=start_seq, cutoff=cutoff, stop=stop, deadline=deadline
        )
        if collected is None:
            return 0
        verified_items, invalid_items, scanned_up_to, messages_scanned = collected

        reported = 0
        if verified_items or invalid_items:
            reported = await self._report_recovered_batch(verified_items, invalid_items, stop=stop)
            if reported == 0:
                return 0

        if scanned_up_to > last_scanned:
            await asyncio.to_thread(
                self._access.set_dlq_scan_cursor,
                self._team_id,
                self._dlq_stream,
                scanned_up_to,
                stream_created=stream_created,
                updated_at_utc=_utc(self._clock()),
            )
        duration_ms = (time.monotonic() - started) * 1000.0
        log_fn = self._logger.info if (reported > 0 or scanned_up_to < cutoff) else self._logger.debug
        log_fn(
            "Completed dead-letter notice recovery scan pass. event=protocol1_dlq_recovery_scan_pass "
            "team=%s scanned=%s recovered=%s cursor=%s cutoff=%s duration_ms=%.1f",
            self._team_id,
            messages_scanned,
            reported,
            scanned_up_to,
            cutoff,
            duration_ms,
        )
        return reported

    async def _inspect_stored_notice(
        self,
        live: LiveHost,
        stored: StoredStreamMessage,
        *,
        receiver: Receiver,
        now: datetime,
        departed_refs: frozenset[str],
        departed_ids: frozenset[str],
    ) -> _VerifiedNoticeItem | _InvalidNoticeItem | str:
        try:
            verified = await live.verify(
                stored.data,
                receiver=receiver,
                arrived_on=stored.subject,
                now=now,
                replay=lambda _key: None,
                headers=dict(stored.headers),
                stored_at=_ns_to_datetime(stored.stored_at_ns),
            )
        except FrameRefused as refused:
            action = classify_durable_refusal(
                refused, own_installation=live.identity.installation, raw_bytes=stored.data
            )
            if action is not DurableRefusalAction.SET_ASIDE:
                return "temporary"
            return self._invalid_item_from_refusal(stored.subject, stored.data, stored.seq, refused.code)

        if verified.frame.kind != "dlq.notice":
            return self._invalid_item_from_refusal(stored.subject, stored.data, stored.seq, "unknown_kind")

        return await self._inspect_verified_notice(
            live,
            verified,
            departed_refs=departed_refs,
            departed_ids=departed_ids,
        )

    async def _report_recovered_batch(
        self,
        verified_items: Sequence[_VerifiedNoticeItem],
        invalid_items: Sequence[_InvalidNoticeItem],
        *,
        stop: Optional[asyncio.Event],
    ) -> int:
        candidate_keys = [
            *((item.issuer_runtime, item.copy_id) for item in verified_items),
            *((item.subject_runtime, item.copy_id) for item in invalid_items),
        ]
        already = await asyncio.to_thread(
            self._access.dlq_reported_copy_keys, self._team_id, candidate_keys
        )
        fresh_verified = [
            item for item in verified_items if (item.issuer_runtime, item.copy_id) not in already
        ]
        fresh_invalid = [
            item for item in invalid_items if (item.subject_runtime, item.copy_id) not in already
        ]
        if not fresh_verified and not fresh_invalid:
            return 0
        receipt_keys = [
            *((item.issuer_runtime, item.copy_id) for item in fresh_verified),
            *((item.subject_runtime, item.copy_id) for item in fresh_invalid),
        ]
        formatted = format_dlq_admin_notice(fresh_verified, fresh_invalid, dlq_stream=self._dlq_stream)
        notice_id = _notice_id_for_receipts(self._team_id, receipt_keys)
        delivered = await self._deliver_holding_on_outage(
            ackable_messages=(),
            notice_id=notice_id,
            formatted=formatted,
            receipt_keys=receipt_keys,
            stop=stop,
        )
        if not delivered:
            return 0
        self._logger.warning(
            "Recovered stranded dead-letter notices from %s. "
            "event=protocol1_dlq_notices_recovered team=%s notice_id=%s count=%s invalid=%s",
            self._dlq_stream,
            self._team_id,
            notice_id,
            len(fresh_verified) + len(fresh_invalid),
            len(fresh_invalid),
        )
        return len(fresh_verified) + len(fresh_invalid)

    # -- CRIT-1 sweep of team work streams (A4, C08, C15) --------------------------------------

    async def sweep_work_streams(self, *, stop: Optional[asyncio.Event] = None) -> SweepSummary:
        """Sweep partition, pool, and edge streams of this team for exhausted deliveries."""

        live = self._require_live()
        setter = DeadLetterSetter(live, logger=self._logger)
        streams = self.sweep_stream_names(live)
        summary = await sweep_exhausted_streams(
            live.nc,
            streams,
            setter,
            team=self._team_id,
            stop=stop,
            logged_stranded=self._logged_stranded,
            timeout=self._api_timeout_seconds,
            logger=self._logger,
        )
        log_fn = (
            self._logger.warning
            if (summary.messages_swept > 0 or summary.stopped_full or summary.streams_failed > 0)
            else self._logger.debug
        )
        log_fn(
            "Completed team dead-letter sweep pass. event=protocol1_dlq_team_sweep_pass "
            "team=%s streams_checked=%s streams_failed=%s consumers_checked=%s "
            "messages_swept=%s stopped_full=%s duration_ms=%.1f",
            self._team_id,
            summary.streams_checked,
            summary.streams_failed,
            summary.consumers_checked,
            summary.messages_swept,
            summary.stopped_full,
            summary.duration_ms,
        )
        return summary

    # -- shared raw-copy maintenance scan (C01 orphan cleanup + C04 departed safety net + C20) -

    def _reset_raw_scan_if_stream_changed(self, stream_created: str, last_seq: int) -> None:
        if (
            self._raw_scan_stream_created
            and stream_created
            and self._raw_scan_stream_created != stream_created
        ) or last_seq < self._orphan_confirmed_seq:
            self._orphan_confirmed_seq = 0
            self._raw_scan_resume_seq = None
            self._raw_scan_saw_young = False
            self._last_departed_targets = None
            self._departed_rewalk_targets = None
        if stream_created:
            self._raw_scan_stream_created = stream_created

    def _raw_scan_start_seq(
        self,
        *,
        first_seq: int,
        departed_targets: tuple[frozenset[str], frozenset[str]],
    ) -> int:
        departed_refs, departed_ids = departed_targets
        has_departed = bool(departed_refs or departed_ids)
        if has_departed and self._last_departed_targets != departed_targets:
            if self._departed_rewalk_targets == departed_targets and self._raw_scan_resume_seq is not None:
                return max(first_seq, self._raw_scan_resume_seq)
            self._departed_rewalk_targets = departed_targets
            self._raw_scan_resume_seq = None
            self._raw_scan_saw_young = False
            return first_seq
        if self._departed_rewalk_targets is not None:
            self._departed_rewalk_targets = None
            self._raw_scan_resume_seq = None
            self._raw_scan_saw_young = False
        if self._raw_scan_resume_seq is not None:
            return max(first_seq, self._raw_scan_resume_seq)
        self._raw_scan_saw_young = False
        return max(first_seq, self._orphan_confirmed_seq + 1)

    def _advance_orphan_confirmed_seq(self, seq: int, *, saw_young_copy: bool) -> None:
        if not saw_young_copy and seq > self._orphan_confirmed_seq:
            self._orphan_confirmed_seq = seq

    def _pause_raw_copy_scan(self, next_seq: int, *, saw_young_copy: bool) -> None:
        self._raw_scan_resume_seq = next_seq
        self._raw_scan_saw_young = saw_young_copy

    async def _run_raw_copy_scan_loop(
        self,
        nc: Any,
        *,
        start_seq: int,
        last_seq: int,
        departed_refs: frozenset[str],
        departed_ids: frozenset[str],
        orphan_cutoff_ns: int,
        deadline: float,
        stop: Optional[asyncio.Event],
    ) -> tuple[int, int, int, bool]:
        copies_scanned = 0
        departed_deleted = 0
        orphans_deleted = 0
        saw_young_copy = self._raw_scan_saw_young
        next_seq = start_seq
        wildcard = dlq_raw_wildcard_subject(self._team_id)

        while next_seq <= last_seq:
            if stop is not None and stop.is_set():
                self._pause_raw_copy_scan(next_seq, saw_young_copy=saw_young_copy)
                return copies_scanned, departed_deleted, orphans_deleted, True
            stored = await get_stream_message(
                nc, self._dlq_stream, seq=next_seq, next_by_subj=wildcard, timeout=self._api_timeout_seconds
            )
            if stored is None:
                self._advance_orphan_confirmed_seq(last_seq, saw_young_copy=saw_young_copy)
                break
            copies_scanned += 1
            next_seq = stored.seq + 1
            outcome = await self._classify_and_clean_raw_copy(
                nc,
                stored,
                departed_refs=departed_refs,
                departed_ids=departed_ids,
                orphan_cutoff_ns=orphan_cutoff_ns,
                skip_orphan_check=stored.seq <= self._orphan_confirmed_seq,
            )
            departed_deleted += int(outcome == "departed")
            orphans_deleted += int(outcome == "orphan")
            if outcome == "young":
                saw_young_copy = True
            else:
                self._advance_orphan_confirmed_seq(stored.seq, saw_young_copy=saw_young_copy)
            if time.monotonic() >= deadline and next_seq <= last_seq:
                self._pause_raw_copy_scan(next_seq, saw_young_copy=saw_young_copy)
                return copies_scanned, departed_deleted, orphans_deleted, True
        return copies_scanned, departed_deleted, orphans_deleted, False

    async def scan_raw_copies(self, *, stop: Optional[asyncio.Event] = None) -> RawCopyScanReport:
        """Scan ``ua.v1.<team>.*.dlq.raw.*`` with a time budget, skipping already-confirmed paired copies (C20, C23)."""

        started = time.monotonic()
        live = self._require_live()
        info = await stream_info(live.nc, self._dlq_stream, timeout=self._api_timeout_seconds)
        if info is None or info.messages == 0:
            self._raw_scan_resume_seq = None
            self._raw_scan_saw_young = False
            self._departed_rewalk_targets = None
            return RawCopyScanReport(0, 0, 0, False, (time.monotonic() - started) * 1000.0)

        self._reset_raw_scan_if_stream_changed(info.created, info.last_seq)
        departed_targets = await asyncio.to_thread(self._departed_person_targets)
        departed_refs, departed_ids = departed_targets
        orphan_cutoff = _utc(self._clock()) - timedelta(seconds=self._orphan_grace_seconds)
        orphan_cutoff_ns = int(orphan_cutoff.timestamp() * 1_000_000_000)

        start_seq = self._raw_scan_start_seq(first_seq=info.first_seq, departed_targets=departed_targets)
        copies_scanned, departed_deleted, orphans_deleted, budget_reached = await self._run_raw_copy_scan_loop(
            live.nc,
            start_seq=start_seq,
            last_seq=info.last_seq,
            departed_refs=departed_refs,
            departed_ids=departed_ids,
            orphan_cutoff_ns=orphan_cutoff_ns,
            deadline=started + self._scan_budget_seconds,
            stop=stop,
        )
        if not budget_reached:
            self._raw_scan_resume_seq = None
            self._raw_scan_saw_young = False
            self._departed_rewalk_targets = None
            self._last_departed_targets = departed_targets

        duration_ms = (time.monotonic() - started) * 1000.0
        log_fn = (
            self._logger.info
            if (departed_deleted > 0 or orphans_deleted > 0 or budget_reached)
            else self._logger.debug
        )
        log_fn(
            "Completed dead-letter raw-copy maintenance pass. event=protocol1_dlq_raw_scan_pass "
            "team=%s copies_scanned=%s departed_deleted=%s orphans_deleted=%s budget_reached=%s "
            "confirmed_seq=%s duration_ms=%.1f",
            self._team_id,
            copies_scanned,
            departed_deleted,
            orphans_deleted,
            budget_reached,
            self._orphan_confirmed_seq,
            duration_ms,
        )
        return RawCopyScanReport(copies_scanned, departed_deleted, orphans_deleted, budget_reached, duration_ms)

    async def _classify_and_clean_raw_copy(
        self,
        nc: Any,
        stored: StoredStreamMessage,
        *,
        departed_refs: frozenset[str],
        departed_ids: frozenset[str],
        orphan_cutoff_ns: int,
        skip_orphan_check: bool = False,
    ) -> str:
        if (departed_refs or departed_ids) and raw_copy_names_person(
            stored.data, person_refs=departed_refs, person_ids=departed_ids
        ):
            if await delete_stream_message(nc, self._dlq_stream, stored.seq, timeout=self._api_timeout_seconds):
                self._logger.info(
                    "Deleted raw dead-letter copy naming a departed person during upkeep scan. "
                    "event=protocol1_dlq_departed_copy_deleted stream=%s seq=%s",
                    self._dlq_stream,
                    stored.seq,
                )
                return "departed"
            return "kept"
        if skip_orphan_check:
            return "confirmed"
        if stored.stored_at_ns > orphan_cutoff_ns:
            return "young"
        if await self._delete_if_orphan(nc, stored):
            return "orphan"
        return "confirmed"

    async def _delete_if_orphan(self, nc: Any, stored: StoredStreamMessage) -> bool:
        parsed = parse_dlq_raw_subject(stored.subject, self._team_id)
        if parsed is None:
            return False
        runtime, copy_id = parsed
        notice = await get_stream_message(
            nc,
            self._dlq_stream,
            last_by_subj=dlq_notice_subject(self._team_id, runtime, copy_id),
            timeout=self._api_timeout_seconds,
        )
        if notice is not None:
            return False
        deleted = await delete_stream_message(
            nc, self._dlq_stream, stored.seq, timeout=self._api_timeout_seconds
        )
        if deleted:
            self._logger.warning(
                "Deleted orphaned raw dead-letter copy with no matching notice after grace period. "
                "event=protocol1_dlq_orphan_raw_deleted stream=%s seq=%s runtime=%s copy_id=%s",
                self._dlq_stream,
                stored.seq,
                runtime,
                copy_id,
            )
        return deleted


class TeamPersonDepartureService:
    """Unified departed-person deletion for blobs and ``UA1_DLQ_<team>`` raw copies (A6, C04, C06, C11, C19)."""

    def __init__(
        self,
        *,
        team_id: str,
        live_fn: Callable[[], Optional[LiveHost]],
        people: PeopleDirectory,
        blobs: Optional[BlobService] = None,
        delete_budget_seconds: float = DEFAULT_DELETE_BUDGET_SECONDS,
        api_timeout_seconds: float = 5.0,
        logger: logging.Logger = _LOGGER,
    ) -> None:
        self._team_id = team_id
        self._dlq_stream = dlq_stream_name(team_id)
        self._live_fn = live_fn
        self._people = people
        self._blobs = blobs
        self._delete_budget_seconds = max(0.0, float(delete_budget_seconds))
        self._api_timeout_seconds = max(0.2, float(api_timeout_seconds))
        self._logger = logger
        self._active_runs: dict[str, _DepartureRun] = {}
        self._stopping = False

    @property
    def team_id(self) -> str:
        return self._team_id

    def active_continuation_count(self) -> int:
        return sum(
            1
            for run in self._active_runs.values()
            if run.continuation_task is not None and not run.continuation_task.done()
        )

    def cancel_continuations(self) -> None:
        """Cancel all active background departure scan continuations (on ``LockLost`` or shutdown)."""

        self._stopping = True
        for run in list(self._active_runs.values()):
            if run.continuation_task is not None and not run.continuation_task.done():
                run.continuation_task.cancel()
            if not run.initial_done.is_set():
                run.initial_reply = PersonDataDeleteReply(
                    outcome="runtime_error",
                    message="Team services stopped before departed-person deletion completed.",
                )
                run.initial_done.set()
        self._active_runs.clear()

    async def delete_person_data(self, person_ref: str) -> PersonDataDeleteReply:
        """Delete a departed person's blobs and ``UA1_DLQ_<team>`` raw copies (coalesced per person, C19)."""

        person = await asyncio.to_thread(self._people.person_by_ref, person_ref)
        if person is None:
            return PersonDataDeleteReply(
                outcome="not_allowed",
                message="The person reference is not known in this team.",
            )
        if person.standing not in _DEPARTED_STANDINGS:
            self._logger.warning(
                "Refused team.delete_person_data for a person who has not departed. "
                "event=team_delete_person_data_active_refused team=%s person_id=%s standing=%s",
                self._team_id,
                person.person_id,
                person.describe(),
            )
            return PersonDataDeleteReply(
                outcome="not_allowed",
                message=f"Cannot delete team data for a person whose standing is {person.describe()}.",
            )

        # C11 & C19: Check and register before any await so concurrent gateway + manager calls
        # coalesce onto one blob deletion and one DLQ scan with identical cumulative counts.
        existing = self._active_runs.get(person_ref)
        if existing is not None:
            return await self._join_active_run(existing)

        run = _DepartureRun(person_ref=person_ref, person_id=person.person_id)
        self._active_runs[person_ref] = run
        return await self._execute_initial_run(run)

    async def _join_active_run(self, run: _DepartureRun) -> PersonDataDeleteReply:
        if not run.initial_done.is_set():
            await run.initial_done.wait()
            if run.initial_error is not None:
                raise run.initial_error
            if run.initial_reply is not None:
                return run.initial_reply
        return PersonDataDeleteReply(
            outcome="retry_later",
            owners_removed=run.owners_removed,
            blobs_deleted=run.blobs_deleted,
            blobs_kept=run.blobs_kept,
            dlq_copies_deleted=run.dlq_copies_deleted,
            dlq_copies_kept=run.dlq_copies_kept,
            message="Dead-letter raw-copy scan is still continuing in the background.",
        )

    async def _execute_initial_run(self, run: _DepartureRun) -> PersonDataDeleteReply:
        keep_registered = False
        try:
            if self._blobs is not None:
                blob_res = await self._blobs.delete_person(run.person_ref)
                if blob_res.outcome != "ok":
                    reply = PersonDataDeleteReply(outcome=blob_res.outcome, message=blob_res.message)
                    run.initial_reply = reply
                    return reply
                run.owners_removed = blob_res.owners_removed
                run.blobs_deleted = blob_res.blobs_deleted
                run.blobs_kept = blob_res.blobs_kept

            finished = await self._scan_dlq_segment(run, budget_seconds=self._delete_budget_seconds)
            if finished:
                reply = PersonDataDeleteReply(
                    outcome="ok",
                    owners_removed=run.owners_removed,
                    blobs_deleted=run.blobs_deleted,
                    blobs_kept=run.blobs_kept,
                    dlq_copies_deleted=run.dlq_copies_deleted,
                    dlq_copies_kept=run.dlq_copies_kept,
                )
                run.initial_reply = reply
                self._logger.info(
                    "Deleted departed person's team data. event=team_delete_person_data_ok "
                    "team=%s person_id=%s owners_removed=%s blobs_deleted=%s blobs_kept=%s "
                    "dlq_copies_deleted=%s dlq_copies_kept=%s",
                    self._team_id,
                    run.person_id,
                    run.owners_removed,
                    run.blobs_deleted,
                    run.blobs_kept,
                    run.dlq_copies_deleted,
                    run.dlq_copies_kept,
                )
                return reply

            keep_registered = True
            run.continuation_task = asyncio.create_task(self._run_continuation(run))
            reply = PersonDataDeleteReply(
                outcome="retry_later",
                owners_removed=run.owners_removed,
                blobs_deleted=run.blobs_deleted,
                blobs_kept=run.blobs_kept,
                dlq_copies_deleted=run.dlq_copies_deleted,
                dlq_copies_kept=run.dlq_copies_kept,
                message="Dead-letter raw-copy scan reached its time budget and continues in the background.",
            )
            run.initial_reply = reply
            self._logger.warning(
                "Departed-person dead-letter scan reached its time budget; continuing in background. "
                "event=team_delete_person_data_retry_later team=%s person_id=%s "
                "dlq_copies_deleted=%s dlq_copies_kept=%s next_seq=%s",
                self._team_id,
                run.person_id,
                run.dlq_copies_deleted,
                run.dlq_copies_kept,
                run.next_seq,
            )
            return reply
        except Exception as exc:
            run.initial_error = exc
            raise
        finally:
            if not keep_registered and self._active_runs.get(run.person_ref) is run:
                self._active_runs.pop(run.person_ref, None)
            run.initial_done.set()

    async def _handle_dlq_copy_for_departure(
        self, nc: Any, stored: StoredStreamMessage, run: _DepartureRun
    ) -> None:
        if raw_copy_names_person(stored.data, person_ref=run.person_ref, person_id=run.person_id):
            if await delete_stream_message(nc, self._dlq_stream, stored.seq, timeout=self._api_timeout_seconds):
                run.dlq_copies_deleted += 1
        else:
            run.dlq_copies_kept += 1

    async def _scan_dlq_segment(
        self,
        run: _DepartureRun,
        *,
        budget_seconds: Optional[float],
    ) -> bool:
        """Scan raw copies from ``run.next_seq``; returns True when the stream end is reached."""

        live = self._live_fn()
        if live is None:
            raise RuntimeError("Team services LiveHost is not started yet.")
        info = await stream_info(live.nc, self._dlq_stream, timeout=self._api_timeout_seconds)
        if info is None or info.messages == 0:
            return True

        deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
        wildcard = dlq_raw_wildcard_subject(self._team_id)
        run.next_seq = max(run.next_seq, info.first_seq)

        while run.next_seq <= info.last_seq:
            if self._stopping:
                return False
            stored = await get_stream_message(
                live.nc, self._dlq_stream, seq=run.next_seq, next_by_subj=wildcard, timeout=self._api_timeout_seconds
            )
            if stored is None:
                break
            run.next_seq = stored.seq + 1
            await self._handle_dlq_copy_for_departure(live.nc, stored, run)
            if deadline is not None and time.monotonic() >= deadline and run.next_seq <= info.last_seq:
                return False

        return True

    async def _run_continuation(self, run: _DepartureRun) -> None:
        try:
            await self._scan_dlq_segment(run, budget_seconds=None)
            self._logger.info(
                "Finished background departed-person dead-letter scan. "
                "event=team_delete_person_data_continuation_ok team=%s person_id=%s "
                "dlq_copies_deleted=%s dlq_copies_kept=%s",
                self._team_id,
                run.person_id,
                run.dlq_copies_deleted,
                run.dlq_copies_kept,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                "Background departed-person dead-letter scan failed; upkeep will finish cleanup. "
                "event=team_delete_person_data_continuation_failed team=%s person_id=%s",
                self._team_id,
                run.person_id,
            )
        finally:
            # C19: Remove on completion or failure so any later departure starts fresh.
            if self._active_runs.get(run.person_ref) is run:
                self._active_runs.pop(run.person_ref, None)


class TeamPersonDataOperations:
    """Protocol 1 ``svc.team`` operation family for ``team.delete_person_data`` plus DLQ watch/upkeep."""

    def __init__(
        self,
        *,
        dead_letters: DeadLetterService,
        departure: TeamPersonDepartureService,
        people: PeopleDirectory,
        logger: logging.Logger = _LOGGER,
    ) -> None:
        self._dead_letters = dead_letters
        self._departure = departure
        self._people = people
        self._logger = logger

    @property
    def dead_letters(self) -> DeadLetterService:
        return self._dead_letters

    @property
    def departure(self) -> TeamPersonDepartureService:
        return self._departure

    def handlers(self) -> dict[str, Callable[[VerifiedFrame], Any]]:
        return {"team.delete_person_data": self.handle_delete_person_data}

    async def _run(self, stop: asyncio.Event) -> None:
        try:
            await self._dead_letters.run(stop)
        finally:
            self._departure.cancel_continuations()

    async def _maintain(self) -> None:
        await self._dead_letters.maintain()

    def _close(self) -> None:
        self._departure.cancel_continuations()

    def family(self) -> OperationFamily:
        # C20: Do not block svc.team startup on dead-letter stream scans (prepare=None);
        # maintain() and run() handle bounded recovery and upkeep once serving starts.
        return OperationFamily(
            self.handlers(),
            side_by_side=TEAM_PERSON_DATA_SIDE_BY_SIDE_KINDS,
            upkeep=self._maintain,
            close=self._close,
            run=self._run,
        )

    async def handle_delete_person_data(self, verified: VerifiedFrame) -> dict[str, Any]:
        certificate = verified.certificate
        if certificate is None:
            return {"outcome": "not_allowed", "message": "Sender certificate is missing."}

        roles = set(certificate.roles)
        if GATEWAY_ROLE in roles:
            check = await asyncio.to_thread(command_person, verified, self._people, self._logger)
            if check.person is None:
                return {"outcome": "not_allowed", "message": _short_message(check.reason)}
            if not is_administrator(check.person.roles):
                self._logger.warning(
                    "Refused team.delete_person_data from a non-administrator. "
                    "event=team_delete_person_data_admin_required team=%s person_id=%s",
                    self._departure.team_id,
                    check.person.person_id,
                )
                return {
                    "outcome": "not_allowed",
                    "message": "Only an administrator may delete a departed person's team data.",
                }
        elif LEAD_HOST_ROLE not in roles:
            return {
                "outcome": "not_allowed",
                "message": "Only the gateway or the team's lead host may delete a departed person's team data.",
            }

        body = verified.raw["body"]
        try:
            result = await self._departure.delete_person_data(str(body["person"]))
        except Exception as exc:
            self._logger.exception(
                "Departed-person team data deletion failed unexpectedly. "
                "event=team_delete_person_data_failed team=%s",
                self._departure.team_id,
            )
            return {
                "outcome": "runtime_error",
                "message": _short_message(f"Departed person data deletion failed: {exc}"),
            }
        return result.to_wire_body()


__all__ = [
    "DEAD_LETTER_NOTICE_REASON",
    "DEFAULT_DELETE_BUDGET_SECONDS",
    "DEFAULT_NOTICE_ACK_WAIT_SECONDS",
    "DEFAULT_ORPHAN_GRACE_SECONDS",
    "DEFAULT_SCAN_BUDGET_SECONDS",
    "DLQ_MAX_AGE_SECONDS",
    "DLQ_RETENTION_DAYS",
    "LISTED_IN_NOTICE",
    "RECEIPT_RETENTION_SECONDS",
    "REPORT_QUIET_MINUTES",
    "TEAM_PERSON_DATA_KINDS",
    "TEAM_PERSON_DATA_SIDE_BY_SIDE_KINDS",
    "WATCH_BATCH",
    "ChatNoticeSender",
    "DeadLetterMaintenanceReport",
    "DeadLetterService",
    "FormattedAdminNotice",
    "LegacyNatsChatNoticeSender",
    "PersonDataDeleteReply",
    "RawCopyScanReport",
    "TeamPersonDataOperations",
    "TeamPersonDepartureService",
    "format_dlq_admin_notice",
    "owner_chat_route_from_config",
]
