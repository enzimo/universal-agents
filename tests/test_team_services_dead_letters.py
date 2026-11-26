"""Unit and integration tests for team_services.dead_letters (Checkpoint 3).

Covers:
- A5 / C02: notice watch verifies dlq.notice, checks raw copy size/sha256, commits Inbox notice
  and per-(team, issuer_runtime, copy_id) receipts atomically, suppresses duplicate alerts on
  crash-before-ack, changed batches, and duplicate notices, and reports fresh copies on recreated streams.
- A5: missing raw copy, mismatched raw copy, oversized raw.stored=false, and upgrade_old_format
  summaries (naming task, request preview, and what to do).
- C03 / C10: permanently invalid notices summarized with receipts; reporting outages hold batch
  with +WPI past ack_wait periods and recover; UA1_DLQ recovery scan recovers stranded notices
  past max_deliveries using persisted scan cursors (resetting on recreated streams) and prunes old receipts.
- A4 / C08: team_services sweeps UA1_PART_<team>~*, UA1_POOL_WORK_<team>, UA1_EDGE_WORK_<team>,
  never UA1_TEAM_WORK_<team>.
- C01 / C04: shared raw-copy scan deletes both departed-person copies and orphaned raw copies
  older than grace without a matching notice; watch deletes newly fetched departed copies immediately
  and never surfaces departed upgrade_old_format prompt text.
- A6 / C04 / C06 / C11: unified team.delete_person_data authorization, blob + DLQ deletion,
  bounded time budget returning retry_later with cumulative progress counts and single background
  continuation joined by repeat calls without re-running blob deletion.
- C05: LegacyNatsChatNoticeSender produces real UserNotification envelopes accepted by
  GatewayManagerClient as unsolicited autonomous notifications.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from access_control.people import PERSON_ROLE_ADMIN, PERSON_ROLE_MEMBER, PERSON_ROLE_OWNER
from access_control.people_directory import PeopleDirectory
from assistant_gateway.application.manager_client import (
    GatewayManagerClient,
    GatewayManagerClientSettings,
)
from custom_agents.stuck_work import ChatRoute
from custom_messages.contracts.access_v2 import AccessNoticeKind, PersonStanding, TeamPersonV1
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext
from custom_messages.wire.encoding import b64url
from custom_messages.wire.frame import VerifiedAuthority, VerifiedFrame
from custom_messages.wire.kinds import MAX_FRAME_BYTES
from custom_messages.transport_codec import decode_envelope
from custom_transport.protocol1.dead_letters import (
    JS_ERR_NO_MESSAGE_FOUND,
    JS_ERR_STREAM_NOT_FOUND,
    DeadLetterSetter,
    dlq_notice_consumer_name,
    dlq_notice_subject,
    dlq_raw_subject,
    dlq_stream_name,
)
from custom_transport.protocol1.live import LiveHost
from custom_transport.protocol1.trust_state import PersistentTrustStore
from custom_transport.subjects import NatsSubjectMapper
from storage.access_repository import AccessRepository
from storage.config import StorageSettings, resolve_storage_settings
from team_services.blobs import PersonDeleteResult
from team_services.dead_letters import (
    DEAD_LETTER_NOTICE_REASON,
    DeadLetterService,
    LegacyNatsChatNoticeSender,
    TeamPersonDataOperations,
    TeamPersonDepartureService,
    _VerifiedNoticeItem,
    format_dlq_admin_notice,
)
from tests import message_contract_support as fixtures
from tests.test_protocol1_dead_letters import FakeDelivery, FakeJetStream, FakeNatsConnection
from tests.test_protocol1_trust_kernel import example_identity

TEAM = "team_example"
INSTALLATION = "install_example"
ADMIN_ID = "person-admin"
ADMIN_REF = "p-admin00000000000000001"
ALICE_ID = "person-alice"
ALICE_REF = "p-alice00000000000000001"
BOB_ID = "person-bob"
BOB_REF = "p-bob0000000000000000001"
NOW = fixtures.NOW


def _storage(tmp_path: Path, name: str = "dlq.db") -> StorageSettings:
    return resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / name)}})


def _person(
    person_id: str,
    person_ref: str,
    standing: PersonStanding = PersonStanding.ACTIVE,
    roles: tuple[str, ...] = (PERSON_ROLE_MEMBER,),
    display_name: str | None = None,
) -> TeamPersonV1:
    effective_roles = roles if standing is PersonStanding.ACTIVE else ()
    return TeamPersonV1(
        team_id=TEAM,
        person_id=person_id,
        person_ref=person_ref,
        standing=standing,
        roles=effective_roles,
        display_name=display_name,
        changed_at_utc=NOW,
    )


class DlqFakeNatsConnection(FakeNatsConnection):
    """Extends ``FakeNatsConnection`` with ``last_by_subj``, ``CONSUMER.INFO``, and headers support."""

    def __init__(self, js: FakeJetStream | None = None) -> None:
        super().__init__(js)
        self.consumer_info_map: dict[tuple[str, str], dict[str, Any]] = {}

    async def request(self, subject: str, payload: bytes = b"", *, timeout: float = 5.0) -> SimpleNamespace:
        parsed = json.loads(payload.decode("utf-8")) if payload else None
        if subject.startswith("$JS.API.CONSUMER.INFO."):
            self.requests.append((subject, parsed))
            rest = subject.removeprefix("$JS.API.CONSUMER.INFO.")
            stream, _, consumer = rest.partition(".")
            entry = self.consumer_info_map.get((stream, consumer))
            if entry is None:
                return self._json_reply({"error": {"code": 404, "err_code": 10014, "description": "consumer not found"}})
            return self._json_reply(entry)
        return await super().request(subject, payload, timeout=timeout)

    def _handle_msg_get(self, stream: str, req: dict[str, Any]) -> dict[str, Any]:
        if stream not in self.streams:
            return {"error": {"code": 404, "err_code": JS_ERR_STREAM_NOT_FOUND, "description": "stream not found"}}
        msgs = self.messages.get(stream, {})
        last_by_subj = req.get("last_by_subj")
        if last_by_subj is not None:
            for seq in sorted(msgs, reverse=True):
                entry = msgs[seq]
                if entry["subject"] == last_by_subj:
                    return self._msg_payload(seq, entry)
            return {"error": {"code": 404, "err_code": JS_ERR_NO_MESSAGE_FOUND, "description": "no message found"}}
        start_seq = int(req.get("seq", 1))
        subj_filter = req.get("next_by_subj")
        for seq in sorted(msgs):
            if seq < start_seq:
                continue
            entry = msgs[seq]
            if subj_filter and not _subject_matches(subj_filter, entry["subject"]):
                continue
            return self._msg_payload(seq, entry)
        return {"error": {"code": 404, "err_code": JS_ERR_NO_MESSAGE_FOUND, "description": "no message found"}}

    @staticmethod
    def _msg_payload(seq: int, entry: dict[str, Any]) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "subject": entry["subject"],
            "seq": seq,
            "data": base64.b64encode(entry["data"]).decode("ascii"),
            "time": entry["time"],
        }
        headers = entry.get("headers") or {}
        if headers:
            raw_hdr = "NATS/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
            msg["hdrs"] = base64.b64encode(raw_hdr.encode("utf-8")).decode("ascii")
        return {"message": msg}


def _subject_matches(pattern: str, subject: str) -> bool:
    pat_tokens = pattern.split(".")
    sub_tokens = subject.split(".")
    for idx, pat in enumerate(pat_tokens):
        if pat == ">":
            return idx < len(sub_tokens)
        if idx >= len(sub_tokens):
            return False
        if pat != "*" and pat != sub_tokens[idx]:
            return False
    return len(pat_tokens) == len(sub_tokens)


class RecordingChatSender:
    """In-memory ``ChatNoticeSender`` recording alerts and optionally failing a given number of calls."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.fail_count = 0

    async def send_notice(self, *, title: str, text: str, notice_id: str, reason: str) -> bool:
        if self.fail_count > 0:
            self.fail_count -= 1
            return False
        self.sent.append({"title": title, "text": text, "notice_id": notice_id, "reason": reason})
        return True


def _make_hosts(nc: DlqFakeNatsConnection) -> tuple[LiveHost, LiveHost]:
    """Return ``(team_services_live, worker_sender_live)`` sharing ``nc`` and the fixture trust store."""

    trust = fixtures.trust_store()
    ts_live = LiveHost(
        nc,
        example_identity("team-services-1", "test-services-key-1", 2),
        PersistentTrustStore(trust, Path("/nonexistent")),
        clock=lambda: NOW,
    )
    worker_live = LiveHost(
        nc,
        example_identity("worker-host-1", "test-worker-key-1", 3),
        PersistentTrustStore(trust, Path("/nonexistent")),
        clock=lambda: NOW,
    )
    return ts_live, worker_live


def _store_in_stream(
    nc: DlqFakeNatsConnection,
    stream: str,
    subject: str,
    data: bytes,
    *,
    headers: Optional[dict[str, str]] = None,
    stored_time: str = "2026-10-22T04:30:00.000000000Z",
) -> int:
    nc.streams.setdefault(stream, {"created": "2026-10-22T04:00:00Z"})
    msgs = nc.messages.setdefault(stream, {})
    seq = max(msgs, default=0) + 1
    msgs[seq] = {"subject": subject, "data": data, "time": stored_time, "headers": dict(headers or {})}
    return seq


async def _emit_set_aside_into_dlq(
    nc: DlqFakeNatsConnection,
    worker_live: LiveHost,
    *,
    raw_bytes: bytes,
    reason: str = "bad_signature",
    original_subject: str = "ua.v1.team_example.lead-host-1.work.agent.researcher-2.8",
    original_stream: str = "UA1_PART_team_example~8",
    original_seq: int = 1,
    stored_at_ns: int = 1_792_643_400_100_000_000,
    store_raw: bool = True,
    corrupt_raw: bool = False,
) -> tuple[Any, FakeDelivery]:
    """Use ``DeadLetterSetter`` to produce a real signed ``dlq.notice`` and store copy + notice in ``UA1_DLQ``."""

    setter = DeadLetterSetter(worker_live, release="0.10.0")
    before_len = len(nc.jetstream().published)
    res = await setter.set_aside(
        raw_bytes=raw_bytes,
        reason=reason,
        original_subject=original_subject,
        original_stream=original_stream,
        original_stream_sequence=original_seq,
        deliveries=1,
        stored_at=stored_at_ns,
        team=TEAM,
    )
    dlq_stream = dlq_stream_name(TEAM)
    new_publishes = nc.jetstream().published[before_len:]
    notice_bytes = b""
    notice_headers: dict[str, str] = {}
    for subj, payload, hdrs in new_publishes:
        if subj == res.raw_subject and store_raw:
            stored_payload = (payload + b"tampered") if corrupt_raw else payload
            _store_in_stream(nc, dlq_stream, subj, stored_payload, headers=hdrs)
        elif subj == res.notice_subject:
            notice_bytes, notice_headers = payload, hdrs
            notice_seq = _store_in_stream(nc, dlq_stream, subj, payload, headers=hdrs)
    delivery = FakeDelivery(
        data=notice_bytes,
        subject=res.notice_subject,
        stream=dlq_stream,
        stream_seq=notice_seq,
        timestamp_ns=int(NOW.timestamp() * 1_000_000_000),
    )
    delivery.headers = dict(notice_headers)
    return res, delivery


def _verified_command(
    kind: str,
    body: dict[str, Any],
    *,
    roles: tuple[str, ...],
    actor_person_ref: Optional[str] = None,
) -> VerifiedFrame:
    cert = SimpleNamespace(installation=INSTALLATION, roles=roles, teams=(TEAM,), runtime="caller-1")
    authority = (
        VerifiedAuthority(
            token=SimpleNamespace(
                person=actor_person_ref,
                bound_to=SimpleNamespace(type="command", id=body["request_id"]),
            ),  # type: ignore[arg-type]
            certificate=cert,  # type: ignore[arg-type]
        )
        if actor_person_ref is not None
        else None
    )
    frame = SimpleNamespace(kind=kind, issuer=SimpleNamespace(runtime="caller-1"))
    return VerifiedFrame(
        frame=frame,  # type: ignore[arg-type]
        body=SimpleNamespace(**body),
        certificate=cert,  # type: ignore[arg-type]
        session=None,
        authority=authority,
        replay_key=SimpleNamespace(),  # type: ignore[arg-type]
        frame_digest="d",
        raw={"kind": kind, "body": body},
    )


# -- Notice watch: A5, C02, C03, C04 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_notice_watch_verifies_copies_handles_anomalies_and_deduplicates_across_crashes(
    tmp_path: Path,
) -> None:
    """A5 & C02: atomic Inbox + receipts, missing/mismatch/oversized/upgrade_old_format, crash-before-ack & changed batch."""

    nc = DlqFakeNatsConnection()
    ts_live, worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.ACTIVE))
    people = PeopleDirectory(access, TEAM)
    chat = RecordingChatSender()
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        chat_sender=chat,
        clock=lambda: NOW,
        report_quiet_seconds=0.0,
    )

    # 1. Valid raw copy + missing raw copy + mismatched raw copy + oversized raw.stored=false + upgrade_old_format.
    _, d_ok = await _emit_set_aside_into_dlq(nc, worker_live, raw_bytes=b'{"protocol":"1.0-pre","kind":"task.request"}', original_seq=1)
    _, d_missing = await _emit_set_aside_into_dlq(nc, worker_live, raw_bytes=b"gone", original_seq=2, store_raw=False)
    _, d_mismatch = await _emit_set_aside_into_dlq(nc, worker_live, raw_bytes=b"original", original_seq=3, corrupt_raw=True)
    _, d_oversized = await _emit_set_aside_into_dlq(nc, worker_live, raw_bytes=b"z" * (MAX_FRAME_BYTES + 5), reason="too_large", original_seq=4)
    old_env = json.dumps({
        "msg_meta": {
            "msg_correlation_id": "req-old-9",
            "gateway_context": {"canonical_user_id": ALICE_ID},
        },
        "msg_content": {"message_class": "Task", "task_id": "task-old-9", "task_description": "Check quarterly budget"},
    }).encode("utf-8")
    _, d_old = await _emit_set_aside_into_dlq(nc, worker_live, raw_bytes=old_env, reason="upgrade_old_format", original_seq=5)

    reported = await service.process_delivered_notices([d_ok, d_missing, d_mismatch, d_oversized, d_old])
    assert reported == 5
    assert all(d.ack_calls == 1 for d in (d_ok, d_missing, d_mismatch, d_oversized, d_old))

    notices = access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))
    assert len(notices) == 1
    notice = notices[0][0]
    assert notice.kind is AccessNoticeKind.MESSAGES_SET_ASIDE
    assert "raw copy is missing" in notice.summary
    assert "did not match the notice's size or SHA-256" in notice.summary
    assert "exceeded the frame limit" in notice.summary
    # C21: Names the person, task id, and request id without copying personal prompt text into Inbox/chat.
    assert f"for {ALICE_ID}" in notice.summary
    assert "task-old-9" in notice.summary
    assert "req-old-9" in notice.summary
    assert "Check quarterly budget" not in notice.summary
    assert "ask the named person to send any needed request again" in notice.summary
    assert len(chat.sent) == 1 and chat.sent[0]["reason"] == DEAD_LETTER_NOTICE_REASON
    assert "Check quarterly budget" not in chat.sent[0]["text"]

    # 2. Crash-before-ack with CHANGED batch [d_ok, d_missing, d_new]: only d_new is reported!
    _, d_new = await _emit_set_aside_into_dlq(nc, worker_live, raw_bytes=b"brand-new", original_seq=6)
    d_ok_redeliv = FakeDelivery(data=d_ok.data, subject=d_ok.subject, stream=d_ok.metadata.stream, stream_seq=d_ok.metadata.sequence.stream)
    d_ok_redeliv.headers = dict(d_ok.headers)
    reported2 = await service.process_delivered_notices([d_ok_redeliv, d_new])
    assert reported2 == 1
    assert d_ok_redeliv.ack_calls == 1 and d_new.ack_calls == 1
    assert len(access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))) == 2
    assert len(chat.sent) == 2

    # 3. Duplicate twice-published notice is dropped and acked without alerting.
    d_dup = FakeDelivery(data=d_new.data, subject=d_new.subject, stream=d_new.metadata.stream, stream_seq=99)
    d_dup.headers = dict(d_new.headers)
    assert await service.process_delivered_notices([d_dup]) == 0
    assert d_dup.ack_calls == 1
    assert len(chat.sent) == 2

    # 4. Recreated stream reusing seq=1 with a later stored_at_ns gets a fresh copy_id and IS reported (C09).
    _, d_recreated = await _emit_set_aside_into_dlq(
        nc, worker_live, raw_bytes=b"fresh-stream", original_seq=1, stored_at_ns=1_792_700_000_000_000_000
    )
    assert await service.process_delivered_notices([d_recreated]) == 1
    assert len(chat.sent) == 3


@pytest.mark.asyncio
async def test_c04_watch_deletes_departed_copy_immediately_and_hides_prompt_text(tmp_path: Path) -> None:
    """C04: A newly fetched raw copy naming a departed person is deleted immediately and its prompt is never shown."""

    nc = DlqFakeNatsConnection()
    ts_live, worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.REMOVED))
    people = PeopleDirectory(access, TEAM)
    chat = RecordingChatSender()
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        chat_sender=chat,
        clock=lambda: NOW,
    )

    secret_prompt = "Alice confidential HR review text"
    old_env = json.dumps({
        "msg_meta": {"gateway_context": {"canonical_user_id": ALICE_ID}},
        "msg_content": {"message_class": "Task", "task_id": "task-alice-1", "task_description": secret_prompt},
    }).encode("utf-8")
    res, delivery = await _emit_set_aside_into_dlq(
        nc, worker_live, raw_bytes=old_env, reason="upgrade_old_format", original_seq=10
    )

    assert await service.process_delivered_notices([delivery]) == 1
    # Raw copy was deleted from UA1_DLQ_<team> straight away.
    dlq_stream = dlq_stream_name(TEAM)
    assert not any(entry["subject"] == res.raw_subject for entry in nc.messages[dlq_stream].values())
    notice = access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))[0][0]
    assert secret_prompt not in notice.summary
    assert "departed person" in notice.summary


@pytest.mark.asyncio
async def test_c03_invalid_notice_summarized_and_outage_holds_with_wpi_then_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C03: Permanently invalid notice is summarized with a receipt; reporting outage holds with +WPI until recovery."""

    nc = DlqFakeNatsConnection()
    ts_live, _worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    people = PeopleDirectory(access, TEAM)
    chat = RecordingChatSender()
    chat.fail_count = 3  # fail chat on first 3 attempts
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        chat_sender=chat,
        clock=lambda: NOW,
        ack_wait_seconds=0.06,
        initial_retry_seconds=0.04,
        max_retry_seconds=0.06,
    )

    # Also fail DB writes on the first 3 attempts so BOTH channels fail and +WPI hold runs!
    real_record = access.record_dlq_notice_report
    db_failures = {"remaining": 3}

    def flaky_record(*args: Any, **kwargs: Any) -> tuple[bool, int]:
        if db_failures["remaining"] > 0:
            db_failures["remaining"] -= 1
            raise RuntimeError("db temporarily down")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(access, "record_dlq_notice_report", flaky_record)

    bad_copy_id = "f" * 64
    bad_subj = dlq_notice_subject(TEAM, "rogue-host-1", bad_copy_id)
    bad_delivery = FakeDelivery(
        data=b"not-valid-json-frame",
        subject=bad_subj,
        stream=dlq_stream_name(TEAM),
        stream_seq=7,
        timestamp_ns=int(NOW.timestamp() * 1_000_000_000),
    )

    reported = await service.process_delivered_notices([bad_delivery])
    assert reported == 1
    assert bad_delivery.in_progress_calls >= 3
    assert bad_delivery.ack_calls == 1

    notice = access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))[0][0]
    assert "rogue-host-1" in notice.summary
    assert bad_copy_id in notice.summary
    assert "malformed" in notice.summary
    assert ("rogue-host-1", bad_copy_id) in access.dlq_reported_copy_keys(TEAM, [("rogue-host-1", bad_copy_id)])


@pytest.mark.asyncio
async def test_c03_c10_recovery_scan_recovers_stranded_notices_resets_on_recreated_stream_and_prunes_receipts(
    tmp_path: Path,
) -> None:
    """C03 & C10: UA1_DLQ recovery scan recovers crash-loop stranded notices, respects cursor, resets on recreated stream."""

    nc = DlqFakeNatsConnection()
    ts_live, worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    people = PeopleDirectory(access, TEAM)
    chat = RecordingChatSender()
    current_time = [NOW]
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        chat_sender=chat,
        clock=lambda: current_time[0],
        receipt_retention_seconds=2_592_060,
    )

    dlq_stream = dlq_stream_name(TEAM)
    consumer_name = dlq_notice_consumer_name(TEAM)
    res, _ = await _emit_set_aside_into_dlq(nc, worker_live, raw_bytes=b"stranded-notice", original_seq=1)
    notice_seq = max(nc.messages[dlq_stream])
    nc.consumer_info_map[(dlq_stream, consumer_name)] = {
        "name": consumer_name,
        "config": {"filter_subject": f"ua.v1.{TEAM}.*.dlq.notice.*", "max_deliver": 20},
        "delivered": {"stream_seq": notice_seq},
        "ack_floor": {"stream_seq": 0},
        "num_ack_pending": 0,
    }

    # First recovery pass finds the stranded notice, reports it, and advances the cursor.
    assert await service.recover_exhausted_notices() == 1
    assert access.get_dlq_scan_cursor(TEAM, dlq_stream)[0] == notice_seq
    # Second pass does nothing because cursor is at cutoff.
    assert await service.recover_exhausted_notices() == 0
    assert len(chat.sent) == 1

    # Simulate UA1_DLQ_<team> recreated: new created timestamp, sequences restart at 1, fresh timestamped copy_id.
    nc.streams[dlq_stream] = {"created": "2026-10-25T00:00:00Z"}
    nc.messages[dlq_stream] = {}
    res2, _ = await _emit_set_aside_into_dlq(
        nc, worker_live, raw_bytes=b"after-recreate", original_seq=1, stored_at_ns=1_792_900_000_000_000_000
    )
    assert res2.copy_id != res.copy_id
    new_seq = max(nc.messages[dlq_stream])
    nc.consumer_info_map[(dlq_stream, consumer_name)]["delivered"]["stream_seq"] = new_seq
    assert await service.recover_exhausted_notices() == 1
    assert len(chat.sent) == 2

    # Pruning via maintain() keeps receipts within max_age_seconds (29d) and prunes older ones (31d).
    current_time[0] = NOW + timedelta(days=29)
    report_29d = await service.maintain()
    assert report_29d.receipts_pruned == 0
    current_time[0] = NOW + timedelta(days=31)
    report_31d = await service.maintain()
    assert report_31d.receipts_pruned == 2


# -- CRIT-1 team sweep & shared raw-copy maintenance scan (A4, C01, C04, C08) --------------------


@pytest.mark.asyncio
async def test_team_sweep_covers_partitions_pool_edge_and_spares_team_work(tmp_path: Path) -> None:
    """A4 & C08: team_services sweeps UA1_PART~*, UA1_POOL_WORK, UA1_EDGE_WORK, never UA1_TEAM_WORK."""

    nc = DlqFakeNatsConnection()
    ts_live, _worker_live = _make_hosts(nc)
    # Use a small partition count via trust store for fast sweep verification.
    ts_live.trust.store.partition_count = 2
    access = AccessRepository(_storage(tmp_path))
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=PeopleDirectory(access, TEAM),
        clock=lambda: NOW,
    )

    streams = service.sweep_stream_names(ts_live)
    assert streams == (
        f"UA1_PART_{TEAM}~0",
        f"UA1_PART_{TEAM}~1",
        f"UA1_POOL_WORK_{TEAM}",
        f"UA1_EDGE_WORK_{TEAM}",
    )
    assert f"UA1_TEAM_WORK_{TEAM}" not in streams

    pool_stream = f"UA1_POOL_WORK_{TEAM}"
    nc.streams[pool_stream] = {"created": "2026-10-22T04:00:00Z"}
    nc.messages[pool_stream] = {
        1: {"subject": f"ua.v1.{TEAM}.lead-host-1.work.pool.worker", "data": b"stuck", "time": "2026-10-22T04:30:00Z"},
    }
    nc.consumers[pool_stream] = [
        {
            "name": f"pool~{TEAM}~worker",
            "config": {"filter_subject": f"ua.v1.{TEAM}.*.work.pool.worker", "max_deliver": 20},
            "delivered": {"stream_seq": 1},
            "ack_floor": {"stream_seq": 0},
            "num_ack_pending": 0,
        }
    ]

    summary = await service.sweep_work_streams()
    assert summary.messages_swept == 1
    assert summary.streams_failed == 0
    assert nc.messages[pool_stream] == {}


@pytest.mark.asyncio
async def test_c01_c04_shared_raw_copy_scan_deletes_orphans_and_departed_copies(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C01 & C04: scan_raw_copies deletes departed copies and old orphans while keeping young/paired copies."""

    nc = DlqFakeNatsConnection()
    ts_live, _worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    access.record_person(_person(BOB_ID, BOB_REF, PersonStanding.ACTIVE))
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=PeopleDirectory(access, TEAM),
        clock=lambda: NOW,
        orphan_grace_seconds=600,
    )

    dlq_stream = dlq_stream_name(TEAM)
    old_ts = (NOW - timedelta(seconds=1200)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    young_ts = (NOW - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")

    # 1. Departed Alice's copy (even if young) -> deleted immediately.
    alice_copy = json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode()
    seq_alice = _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "1" * 64), alice_copy, stored_time=young_ts)
    # 2. Old orphan raw copy (no matching notice) -> deleted by C01.
    bob_copy = json.dumps({"protocol": "1.0-pre", "authority": {"person": BOB_REF}, "body": {}}).encode()
    seq_orphan = _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "2" * 64), bob_copy, stored_time=old_ts)
    # 3. Young orphan raw copy (within grace) -> kept.
    seq_young = _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "3" * 64), bob_copy, stored_time=young_ts)
    # 4. Old raw copy WITH matching notice -> kept.
    seq_paired = _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "4" * 64), bob_copy, stored_time=old_ts)
    _store_in_stream(nc, dlq_stream, dlq_notice_subject(TEAM, "w1", "4" * 64), b"{}", stored_time=old_ts)

    with caplog.at_level(logging.WARNING):
        report = await service.scan_raw_copies()

    assert report.copies_scanned == 4
    assert report.departed_deleted == 1
    assert report.orphans_deleted == 1
    assert seq_alice not in nc.messages[dlq_stream]
    assert seq_orphan not in nc.messages[dlq_stream]
    assert seq_young in nc.messages[dlq_stream]
    assert seq_paired in nc.messages[dlq_stream]
    assert any("event=protocol1_dlq_orphan_raw_deleted" in r.getMessage() for r in caplog.records)


# -- Unified team.delete_person_data & bounded continuation (A6, C04, C06, C11) ------------------


@pytest.mark.asyncio
async def test_c04_c06_c11_unified_delete_person_data_auth_counts_and_single_continuation(
    tmp_path: Path,
) -> None:
    """A6, C04, C06, C11: authorization, blob+DLQ deletion, retry_later budget, single continuation joined on retry."""

    nc = DlqFakeNatsConnection()
    ts_live, _worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.ACTIVE))
    access.record_person(_person(BOB_ID, BOB_REF, PersonStanding.ACTIVE))
    people = PeopleDirectory(access, TEAM)

    blob_calls: list[str] = []

    class _FakeBlobs:
        async def delete_person(self, person_ref: str) -> PersonDeleteResult:
            blob_calls.append(person_ref)
            return PersonDeleteResult(outcome="ok", owners_removed=2, blobs_deleted=1, blobs_kept=1)

    dlq_service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        clock=lambda: NOW,
    )
    departure = TeamPersonDepartureService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        people=people,
        blobs=_FakeBlobs(),  # type: ignore[arg-type]
        delete_budget_seconds=10.0,
    )
    ops = TeamPersonDataOperations(dead_letters=dlq_service, departure=departure, people=people)

    # 1. Refused while Alice is still active.
    res_active = await ops.handle_delete_person_data(
        _verified_command("team.delete_person_data", {"request_id": "r1", "person": ALICE_REF}, roles=("gateway",), actor_person_ref=ADMIN_REF)
    )
    assert res_active["outcome"] == "not_allowed"

    # Disable Alice.
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))

    # 2. Refused for non-admin member via gateway and for worker_host.
    res_member = await ops.handle_delete_person_data(
        _verified_command("team.delete_person_data", {"request_id": "r2", "person": ALICE_REF}, roles=("gateway",), actor_person_ref=BOB_REF)
    )
    assert res_member["outcome"] == "not_allowed"
    res_worker = await ops.handle_delete_person_data(
        _verified_command("team.delete_person_data", {"request_id": "r3", "person": ALICE_REF}, roles=("worker_host",))
    )
    assert res_worker["outcome"] == "not_allowed"

    # 3. Populate UA1_DLQ_<team> with Alice's protocol 1 copy, Alice's legacy copy, and Bob's copy.
    dlq_stream = dlq_stream_name(TEAM)
    _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "a" * 64), json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode())
    _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "b" * 64), json.dumps({"msg_meta": {"gateway_context": {"canonical_user_id": ALICE_ID}}}).encode())
    seq_bob = _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "c" * 64), json.dumps({"protocol": "1.0-pre", "authority": {"person": BOB_REF}, "body": {}}).encode())

    res_ok = await ops.handle_delete_person_data(
        _verified_command("team.delete_person_data", {"request_id": "r4", "person": ALICE_REF}, roles=("gateway",), actor_person_ref=ADMIN_REF)
    )
    assert res_ok == {
        "outcome": "ok",
        "owners_removed": 2,
        "blobs_deleted": 1,
        "blobs_kept": 1,
        "dlq_copies_deleted": 2,
        "dlq_copies_kept": 1,
    }
    assert list(nc.messages[dlq_stream]) == [seq_bob]

    # 4. Bounded time budget (0.0s) triggers retry_later + single continuation; repeat call joins without re-running blobs!
    blob_calls.clear()
    nc.messages[dlq_stream] = {}
    for idx in range(4):
        copy_hex = f"{idx:064x}"
        payload = (
            json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode()
            if idx % 2 == 0
            else json.dumps({"protocol": "1.0-pre", "authority": {"person": BOB_REF}, "body": {}}).encode()
        )
        _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", copy_hex), payload)

    gate = asyncio.Event()
    orig_request = nc.request

    async def gated_request(subject: str, payload: bytes = b"", *, timeout: float = 5.0) -> Any:
        # Pause background continuation on seq >= 2 until test checks repeat-call join behavior.
        if subject == f"$JS.API.STREAM.MSG.GET.{dlq_stream}":
            req = json.loads(payload.decode("utf-8")) if payload else {}
            if int(req.get("seq", 1)) >= 2:
                await gate.wait()
        return await orig_request(subject, payload, timeout=timeout)

    nc.request = gated_request  # type: ignore[method-assign]
    bounded = TeamPersonDepartureService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        people=people,
        blobs=_FakeBlobs(),  # type: ignore[arg-type]
        delete_budget_seconds=0.0,
    )
    bounded_ops = TeamPersonDataOperations(dead_letters=dlq_service, departure=bounded, people=people)

    first = await bounded_ops.handle_delete_person_data(
        _verified_command("team.delete_person_data", {"request_id": "r5", "person": ALICE_REF}, roles=("lead_host",))
    )
    assert first["outcome"] == "retry_later"
    assert first["owners_removed"] == 2
    assert first["dlq_copies_deleted"] == 1
    assert first["dlq_copies_kept"] == 0
    assert bounded.active_continuation_count() == 1
    assert len(blob_calls) == 1

    # Repeat call while continuation is running joins it: does NOT re-run blob deletion or start a second scan!
    repeat = await bounded_ops.handle_delete_person_data(
        _verified_command("team.delete_person_data", {"request_id": "r6", "person": ALICE_REF}, roles=("lead_host",))
    )
    assert repeat["outcome"] == "retry_later"
    assert len(blob_calls) == 1
    assert bounded.active_continuation_count() == 1

    # Release continuation gate and wait for completion; entry is removed so a later departure scans fresh (C19).
    gate.set()
    for _ in range(100):
        if bounded.active_continuation_count() == 0:
            break
        await asyncio.sleep(0.01)
    assert bounded.active_continuation_count() == 0
    # Continuation already deleted both of Alice's raw copies and left both of Bob's.
    assert len(nc.messages[dlq_stream]) == 2

    # A new departure call after the continuation finishes starts fresh (re-runs blob deletion and DLQ scan).
    fresh_after_finish = await bounded_ops.handle_delete_person_data(
        _verified_command("team.delete_person_data", {"request_id": "r7", "person": ALICE_REF}, roles=("lead_host",))
    )
    assert len(blob_calls) == 2
    assert fresh_after_finish["outcome"] in {"ok", "retry_later"}
    for _ in range(100):
        if bounded.active_continuation_count() == 0:
            break
        await asyncio.sleep(0.01)


# -- C05: LegacyNatsChatNoticeSender integration with real GatewayManagerClient ------------------


@pytest.mark.asyncio
async def test_c05_legacy_chat_sender_envelope_reaches_gateway_manager_client_unsolicited(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C05: Real UserNotification envelope from LegacyNatsChatNoticeSender is accepted by GatewayManagerClient."""

    nc = DlqFakeNatsConnection()
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    people = PeopleDirectory(access, TEAM)
    response_route = f"{TEAM}_gateway_response"
    mapper = NatsSubjectMapper(team_id=TEAM, subject_prefix="ua", user_queue_name=response_route)
    route = ChatRoute(
        routing_key=response_route,
        gateway_context=GatewayContext(
            gateway_instance_name="inst-1",
            session_key="default",
            channel_name="telegram",
            user_id=ADMIN_ID,
            canonical_user_id=ADMIN_ID,
            chat_id="chat-admin-1",
            delivery_mode="agent_notification",
        ),
    )
    sender = LegacyNatsChatNoticeSender(
        nc_fn=lambda: nc,
        mapper=mapper,
        people=people,
        owner_person_id=ADMIN_ID,
        route_fn=lambda: route,
    )

    ok = await sender.send_notice(
        title="1 message(s) could not be handled and were set aside",
        text="- details",
        notice_id="p1_dlq:test123",
        reason=DEAD_LETTER_NOTICE_REASON,
    )
    assert ok is True
    assert len(nc.jetstream().published) == 1
    subject, payload, headers = nc.jetstream().published[0]
    assert subject == mapper.gateway_response_subject(response_route)
    assert headers["Nats-Msg-Id"] == "p1_dlq:test123"

    # Feed the real decoded envelope into GatewayManagerClient's response pipeline -> arrives at on_unsolicited!
    envelope = decode_envelope(payload)
    received: list[MessageEnvelope] = []

    async def on_unsolicited(env: MessageEnvelope) -> None:
        received.append(env)

    client = GatewayManagerClient(
        settings=GatewayManagerClientSettings(
            instance_name="inst-1",
            user_display_name="Admin",
            team_id=TEAM,
            manager_named_agent_type="TeamArchitect",
            response_route=response_route,
            initial_manager_id="lead-1",
        ),
        id_generator=SimpleNamespace(new_id=lambda: "id-1"),
        logger=logging.getLogger("test"),
        on_unsolicited=on_unsolicited,
    )
    await client.handle_response(envelope)
    assert len(received) == 1
    delivered = received[0]
    assert isinstance(delivered.msg_content, UserNotification)
    assert delivered.msg_content.reason == DEAD_LETTER_NOTICE_REASON
    assert delivered.msg_meta is not None
    assert delivered.msg_meta.gateway_context is not None
    assert delivered.msg_meta.gateway_context.delivery_mode == "autonomous"
    assert delivered.msg_meta.gateway_context.chat_id == "chat-admin-1"

    # Publish failure logs event=protocol1_dlq_chat_failed and returns False.
    nc.jetstream().fail_on.append((subject, RuntimeError("nats publish refused")))
    with caplog.at_level(logging.ERROR):
        failed = await sender.send_notice(
            title="t", text="x", notice_id="p1_dlq:fail456", reason=DEAD_LETTER_NOTICE_REASON
        )
    assert failed is False
    assert any(
        "event=protocol1_dlq_chat_failed" in r.getMessage() and "p1_dlq:fail456" in r.getMessage()
        for r in caplog.records
    )

    # Demoted / inactive owner receives no chat alert.
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_MEMBER,)))
    assert await sender.send_notice(title="t", text="x", notice_id="p1_dlq:nonadmin", reason=DEAD_LETTER_NOTICE_REASON) is False


# -- C19, C20, C22 fixes -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c19_concurrent_departure_calls_coalesce_and_finished_continuation_resets(tmp_path: Path) -> None:
    """C19: Concurrent gateway + manager calls coalesce onto one scan; finished/failed continuations do not stick."""

    nc = DlqFakeNatsConnection()
    ts_live, _worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    access.record_person(_person(BOB_ID, BOB_REF, PersonStanding.ACTIVE))
    people = PeopleDirectory(access, TEAM)

    dlq_stream = dlq_stream_name(TEAM)
    _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "1" * 64), json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode())
    _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "2" * 64), json.dumps({"protocol": "1.0-pre", "authority": {"person": BOB_REF}, "body": {}}).encode())

    blob_started = asyncio.Event()
    blob_release = asyncio.Event()
    blob_calls: list[str] = []

    class _SlowBlobs:
        async def delete_person(self, person_ref: str) -> PersonDeleteResult:
            blob_calls.append(person_ref)
            blob_started.set()
            await blob_release.wait()
            return PersonDeleteResult(outcome="ok", owners_removed=1, blobs_deleted=1, blobs_kept=0)

    departure = TeamPersonDepartureService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        people=people,
        blobs=_SlowBlobs(),  # type: ignore[arg-type]
        delete_budget_seconds=10.0,
    )

    # 1. Two concurrent calls for the same person coalesce into ONE blob delete and ONE DLQ scan!
    task1 = asyncio.create_task(departure.delete_person_data(ALICE_REF))
    await blob_started.wait()
    task2 = asyncio.create_task(departure.delete_person_data(ALICE_REF))
    await asyncio.sleep(0.01)
    blob_release.set()
    r1, r2 = await asyncio.gather(task1, task2)
    assert len(blob_calls) == 1
    assert r1 == r2
    assert r1.outcome == "ok"
    assert r1.dlq_copies_deleted == 1 and r1.dlq_copies_kept == 1

    # 2. A later departure (e.g. Alice restored & disabled again with a new copy) runs fresh!
    _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "3" * 64), json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode())
    r3 = await departure.delete_person_data(ALICE_REF)
    assert len(blob_calls) == 2
    assert r3.outcome == "ok"
    assert r3.dlq_copies_deleted == 1 and r3.dlq_copies_kept == 1

    # 3. Failed background continuation removes its entry so the next call retries cleanly instead of sticking on error.
    _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "4" * 64), json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode())
    _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "5" * 64), json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode())
    fail_second_get = {"fail": True}
    orig_req = nc.request

    async def flaky_req(subject: str, payload: bytes = b"", *, timeout: float = 5.0) -> Any:
        if subject == f"$JS.API.STREAM.MSG.GET.{dlq_stream}" and fail_second_get["fail"]:
            req = json.loads(payload.decode("utf-8")) if payload else {}
            if int(req.get("seq", 1)) > 2:
                fail_second_get["fail"] = False
                raise RuntimeError("transient nats disconnect during continuation")
        return await orig_req(subject, payload, timeout=timeout)

    nc.request = flaky_req  # type: ignore[method-assign]
    bounded = TeamPersonDepartureService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        people=people,
        blobs=_SlowBlobs(),  # type: ignore[arg-type]
        delete_budget_seconds=0.0,
    )
    r_retry = await bounded.delete_person_data(ALICE_REF)
    assert r_retry.outcome == "retry_later"
    for _ in range(100):
        if bounded.active_continuation_count() == 0:
            break
        await asyncio.sleep(0.01)
    assert bounded.active_continuation_count() == 0
    # Next call runs fresh (now with normal budget) and deletes remaining Alice copies!
    bounded._delete_budget_seconds = 10.0
    r_after_fail = await bounded.delete_person_data(ALICE_REF)
    assert r_after_fail.outcome == "ok"
    assert r_after_fail.dlq_copies_deleted == 2


@pytest.mark.asyncio
async def test_c20_incremental_raw_scan_skips_confirmed_pairs_and_batches_recovery_lookups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C20: scan_raw_copies skips confirmed paired copies on later passes, re-walks on departed changes without
    re-checking notices, respects scan budget, and recover_exhausted_notices batches receipt lookups."""

    nc = DlqFakeNatsConnection()
    ts_live, worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    access.record_person(_person(BOB_ID, BOB_REF, PersonStanding.ACTIVE))
    people = PeopleDirectory(access, TEAM)
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        chat_sender=RecordingChatSender(),
        clock=lambda: NOW,
        orphan_grace_seconds=600,
        scan_budget_seconds=10.0,
    )

    dlq_stream = dlq_stream_name(TEAM)
    old_ts = (NOW - timedelta(seconds=1200)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    bob_copy = json.dumps({"protocol": "1.0-pre", "authority": {"person": BOB_REF}, "body": {}}).encode()
    _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "a" * 64), bob_copy, stored_time=old_ts)
    _store_in_stream(nc, dlq_stream, dlq_notice_subject(TEAM, "w1", "a" * 64), b"{}", stored_time=old_ts)
    _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "b" * 64), bob_copy, stored_time=old_ts)
    _store_in_stream(nc, dlq_stream, dlq_notice_subject(TEAM, "w1", "b" * 64), b"{}", stored_time=old_ts)

    # Pass 1 confirms both old paired copies.
    rep1 = await service.scan_raw_copies()
    assert rep1.copies_scanned == 2
    assert rep1.orphans_deleted == 0

    # Pass 2 with unchanged departed set does 0 raw copy reads and 0 notice lookups!
    nc.requests.clear()
    rep2 = await service.scan_raw_copies()
    assert rep2.copies_scanned == 0
    notice_gets = [
        req for subj, req in nc.requests
        if subj == f"$JS.API.STREAM.MSG.GET.{dlq_stream}" and isinstance(req, dict) and "last_by_subj" in req
    ]
    assert notice_gets == []

    # When Bob departs, scan_raw_copies re-walks older copies to delete Bob's copies without re-checking notices.
    access.record_person(_person(BOB_ID, BOB_REF, PersonStanding.DISABLED))
    nc.requests.clear()
    rep3 = await service.scan_raw_copies()
    assert rep3.copies_scanned == 2
    assert rep3.departed_deleted == 2
    notice_gets_after_depart = [
        req for subj, req in nc.requests
        if subj == f"$JS.API.STREAM.MSG.GET.{dlq_stream}" and isinstance(req, dict) and "last_by_subj" in req
    ]
    assert notice_gets_after_depart == []

    # Recovery scan batches dlq_reported_copy_keys into ONE DB lookup for a batch of 5 notices!
    nc.messages[dlq_stream] = {}
    for seq_idx in range(1, 6):
        await _emit_set_aside_into_dlq(nc, worker_live, raw_bytes=f"msg-{seq_idx}".encode(), original_seq=seq_idx)
    last_seq = max(nc.messages[dlq_stream])
    consumer_name = dlq_notice_consumer_name(TEAM)
    nc.consumer_info_map[(dlq_stream, consumer_name)] = {
        "name": consumer_name,
        "config": {"filter_subject": f"ua.v1.{TEAM}.*.dlq.notice.*", "max_deliver": 20},
        "delivered": {"stream_seq": last_seq},
        "ack_floor": {"stream_seq": 0},
        "num_ack_pending": 0,
    }
    lookup_batches: list[int] = []
    real_lookup = access.dlq_reported_copy_keys

    def counting_lookup(team_id: str, keys: Any) -> set[tuple[str, str]]:
        key_list = list(keys)
        lookup_batches.append(len(key_list))
        return real_lookup(team_id, key_list)

    monkeypatch.setattr(access, "dlq_reported_copy_keys", counting_lookup)
    assert await service.recover_exhausted_notices() == 5
    # One batched lookup of 5 keys during collection + one batched lookup of 5 keys in _report_recovered_batch.
    assert lookup_batches == [5, 5]


@pytest.mark.asyncio
async def test_c22_inbox_outage_with_chat_working_holds_and_avoids_duplicate_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C22: When Inbox store fails while chat works, batch holds with +WPI and does not resend chat on recovery."""

    nc = DlqFakeNatsConnection()
    ts_live, worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    people = PeopleDirectory(access, TEAM)
    chat = RecordingChatSender()
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        chat_sender=chat,
        clock=lambda: NOW,
        ack_wait_seconds=0.06,
        initial_retry_seconds=0.02,
        max_retry_seconds=0.04,
    )

    real_record = access.record_dlq_notice_report
    db_failures = {"remaining": 2}

    def flaky_record(*args: Any, **kwargs: Any) -> tuple[bool, int]:
        if db_failures["remaining"] > 0:
            db_failures["remaining"] -= 1
            raise RuntimeError("inbox db temporarily down")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(access, "record_dlq_notice_report", flaky_record)

    res, delivery = await _emit_set_aside_into_dlq(nc, worker_live, raw_bytes=b"payload-c22", original_seq=1)
    reported = await service.process_delivered_notices([delivery])

    assert reported == 1
    assert delivery.in_progress_calls >= 2
    assert delivery.ack_calls == 1
    assert len(chat.sent) == 1  # Chat sent ONCE on attempt 1, NOT repeated when Inbox recovered on attempt 3!
    assert len(access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))) == 1
    assert (worker_live.identity.runtime, res.copy_id) in access.dlq_reported_copy_keys(
        TEAM, [(worker_live.identity.runtime, res.copy_id)]
    )

    # Subsequent recovery scan finds the receipt and sends no duplicate Inbox or chat notice.
    dlq_stream = dlq_stream_name(TEAM)
    consumer_name = dlq_notice_consumer_name(TEAM)
    nc.consumer_info_map[(dlq_stream, consumer_name)] = {
        "name": consumer_name,
        "config": {"filter_subject": f"ua.v1.{TEAM}.*.dlq.notice.*", "max_deliver": 20},
        "delivered": {"stream_seq": max(nc.messages[dlq_stream])},
        "ack_floor": {"stream_seq": 0},
        "num_ack_pending": 0,
    }
    assert await service.recover_exhausted_notices() == 0
    assert len(chat.sent) == 1


@pytest.mark.asyncio
async def test_c23_budgeted_raw_scan_resumes_across_passes_with_departed_set(tmp_path: Path) -> None:
    """C23: A budget-paused raw-copy scan with a non-empty departed set resumes across passes to the end,
    deletes the departed person's last copy and advances _orphan_confirmed_seq, does not re-walk once finished,
    and restarts from first_seq only when the departed set changes again."""

    nc = DlqFakeNatsConnection()
    ts_live, _worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.ACTIVE))
    access.record_person(_person(BOB_ID, BOB_REF, PersonStanding.DISABLED))
    people = PeopleDirectory(access, TEAM)
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        chat_sender=RecordingChatSender(),
        clock=lambda: NOW,
        orphan_grace_seconds=600,
        scan_budget_seconds=0.0,  # Ends each pass after 1 raw copy.
    )

    dlq_stream = dlq_stream_name(TEAM)
    old_ts = (NOW - timedelta(seconds=1200)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    alice_copy = json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode()
    bob_copy = json.dumps({"protocol": "1.0-pre", "authority": {"person": BOB_REF}, "body": {}}).encode()
    seq_alice_1 = _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "a" * 64), alice_copy, stored_time=old_ts)
    _store_in_stream(nc, dlq_stream, dlq_notice_subject(TEAM, "w1", "a" * 64), b"{}", stored_time=old_ts)
    seq_orphan = _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "b" * 64), alice_copy, stored_time=old_ts)
    seq_bob = _store_in_stream(nc, dlq_stream, dlq_raw_subject(TEAM, "w1", "c" * 64), bob_copy, stored_time=old_ts)
    _store_in_stream(nc, dlq_stream, dlq_notice_subject(TEAM, "w1", "c" * 64), b"{}", stored_time=old_ts)

    # Pass 1 scans copy 1 (Alice paired) and pauses at resume_seq > seq_alice_1 with Bob's departed set in progress.
    rep1 = await service.scan_raw_copies()
    assert rep1.copies_scanned == 1 and rep1.budget_reached is True
    assert rep1.departed_deleted == 0 and rep1.orphans_deleted == 0
    assert service._orphan_confirmed_seq == seq_alice_1
    assert service._raw_scan_resume_seq == seq_alice_1 + 1

    # Pass 2 resumes at copy 2 (orphan) and deletes it.
    rep2 = await service.scan_raw_copies()
    assert rep2.copies_scanned == 1 and rep2.budget_reached is True
    assert rep2.orphans_deleted == 1 and rep2.departed_deleted == 0
    assert service._orphan_confirmed_seq == seq_orphan
    assert seq_orphan not in nc.messages[dlq_stream]

    # Pass 3 resumes at copy 3 (Bob's copy) and deletes it; trailing notice still sits at seq_bob + 1 so pass 3 hits budget.
    rep3 = await service.scan_raw_copies()
    assert rep3.copies_scanned == 1 and rep3.departed_deleted == 1
    assert seq_bob not in nc.messages[dlq_stream]
    assert service._orphan_confirmed_seq == seq_bob

    # Pass 4 reaches the end of the stream and records the completed departed set.
    rep4 = await service.scan_raw_copies()
    assert rep4.copies_scanned == 0 and rep4.budget_reached is False
    assert service._raw_scan_resume_seq is None
    assert service._departed_rewalk_targets is None
    assert service._last_departed_targets == (frozenset({BOB_REF}), frozenset({BOB_ID}))

    # Pass 5 with the unchanged departed set does NOT restart a full re-walk!
    nc.requests.clear()
    rep5 = await service.scan_raw_copies()
    assert rep5.copies_scanned == 0 and rep5.budget_reached is False
    assert not any(subj == f"$JS.API.STREAM.MSG.GET.{dlq_stream}" for subj, _ in nc.requests)

    # When Alice departs too (departed set changes), a new full re-walk starts from first_seq and deletes Alice's copy!
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    rep6 = await service.scan_raw_copies()
    assert rep6.copies_scanned == 1 and rep6.departed_deleted == 1
    assert seq_alice_1 not in nc.messages[dlq_stream]


@pytest.mark.asyncio
async def test_f1_team_services_advisory_watch_and_near_expiry_inbox_warning(tmp_path: Path) -> None:
    """F1: DeadLetterService handles MAX_DELIVERIES advisories on busy consumers and posts idempotent
    administrators' Inbox warnings when a work stream's oldest message nears max_age."""

    from custom_transport.protocol1.dead_letters import (
        MAX_DELIVERIES_ADVISORY_PREFIX,
        MAX_DELIVERIES_ADVISORY_TYPE,
        dlq_advisory_stream_name,
        dlq_advisory_team_consumer_name,
    )
    from tests.test_protocol1_dead_letters import FakeDelivery

    nc = DlqFakeNatsConnection()
    ts_live, _worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    people = PeopleDirectory(access, TEAM)
    now = datetime(2026, 10, 29, 0, 0, 0, tzinfo=timezone.utc)
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        chat_sender=RecordingChatSender(),
        clock=lambda: now,
        near_expiry_margin_seconds=86_400.0,
    )

    part_stream = f"UA1_PART_{TEAM}~0"
    max_age_ns = 604_860 * 1_000_000_000
    nc.streams[part_stream] = {"created": "2026-10-20T00:00:00Z"}
    nc.messages[part_stream] = {
        1: {
            "subject": f"ua.v1.{TEAM}.lead-host-1.work.agent.r1.0",
            "data": b"poison-busy",
            "time": "2026-10-22T12:00:00Z",
        },
        2: {
            "subject": f"ua.v1.{TEAM}.lead-host-1.work.agent.r1.0",
            "data": b"still-in-flight",
            "time": "2026-10-22T12:05:00Z",
        },
    }
    nc.consumers[part_stream] = [
        {
            "name": "agent~r1",
            "config": {"filter_subject": f"ua.v1.{TEAM}.*.work.agent.r1.0", "max_deliver": 20},
            "delivered": {"stream_seq": 2},
            "ack_floor": {"stream_seq": 0},
            "num_ack_pending": 1,  # busy consumer: cutoff is 0
        }
    ]

    orig_req = nc.request

    async def req_with_first_ts(subject: str, payload: bytes = b"", *, timeout: float = 5.0) -> Any:
        resp = await orig_req(subject, payload, timeout=timeout)
        if subject == f"$JS.API.STREAM.INFO.{part_stream}":
            doc = json.loads(resp.data.decode("utf-8"))
            msgs = nc.messages.get(part_stream, {})
            if msgs:
                doc["state"]["first_ts"] = msgs[min(msgs)]["time"]
            doc["config"] = {"max_age": max_age_ns}
            resp = SimpleNamespace(data=json.dumps(doc).encode("utf-8"))
        return resp

    nc.request = req_with_first_ts  # type: ignore[method-assign]

    # 1. Idle cutoff sweep leaves busy consumer untouched, but emits ONE Inbox near-expiry notice naming stream & seq 1.
    summary1 = await service.sweep_work_streams()
    assert summary1.messages_swept == 0
    assert len(summary1.near_expiry) == 1
    assert summary1.near_expiry[0].stream == part_stream and summary1.near_expiry[0].first_seq == 1
    notices = access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))
    assert len(notices) == 1
    warn_notice, _ = notices[0]
    assert warn_notice.kind is AccessNoticeKind.QUEUED_MESSAGE_EXPIRING
    assert part_stream in warn_notice.title and "1" in warn_notice.title
    assert part_stream in warn_notice.summary and "1" in warn_notice.summary

    # Repeat sweep does NOT duplicate the Inbox notice for the same (stream, first_seq, first_ts).
    await service.sweep_work_streams()
    assert len(access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))) == 1

    # 2. Advisory watch receives MAX_DELIVERIES for seq 1, sets it aside and deletes seq 1 while keeping seq 2!
    adv_payload = json.dumps(
        {
            "type": MAX_DELIVERIES_ADVISORY_TYPE,
            "id": "adv-busy-1",
            "timestamp": "2026-10-29T00:00:00Z",
            "stream": part_stream,
            "consumer": "agent~r1",
            "stream_seq": 1,
            "deliveries": 20,
        }
    ).encode("utf-8")
    adv_msg = FakeDelivery(
        data=adv_payload,
        subject=f"{MAX_DELIVERIES_ADVISORY_PREFIX}.{part_stream}.agent~r1",
        stream=dlq_advisory_stream_name(TEAM),
        stream_seq=1,
    )
    adv_sub = SimpleNamespace(fetch=AsyncMock(return_value=[adv_msg]))

    async def _bind_adv(consumer: str, *, stream: str) -> Any:
        assert (stream, consumer) == (dlq_advisory_stream_name(TEAM), dlq_advisory_team_consumer_name(TEAM))
        return adv_sub

    nc.jetstream().pull_subscribe_bind = _bind_adv  # type: ignore[attr-defined]

    assert await service.poll_advisories_once() == 1
    assert adv_msg.ack_calls == 1
    assert set(nc.messages[part_stream]) == {2}
    # Duplicate advisory for now-deleted seq 1 acks cleanly without publishing a second DLQ copy.
    adv_msg_dup = FakeDelivery(
        data=adv_payload,
        subject=f"{MAX_DELIVERIES_ADVISORY_PREFIX}.{part_stream}.agent~r1",
        stream=dlq_advisory_stream_name(TEAM),
        stream_seq=2,
    )
    adv_sub.fetch = AsyncMock(return_value=[adv_msg_dup])
    published_before = len(nc.jetstream().published)
    assert await service.poll_advisories_once() == 0
    assert adv_msg_dup.ack_calls == 1
    assert len(nc.jetstream().published) == published_before


@pytest.mark.asyncio
async def test_c26_team_services_posts_near_expiry_inbox_warning_for_team_work_without_sweeping_it(
    tmp_path: Path,
) -> None:
    """C26: DeadLetterService checks STREAM.INFO on UA1_TEAM_WORK_<team> for near-expiry Inbox warnings
    without ever sweeping or deleting messages from UA1_TEAM_WORK_<team>."""

    nc = DlqFakeNatsConnection()
    ts_live, _worker_live = _make_hosts(nc)
    ts_live.trust.store.partition_count = 1
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    people = PeopleDirectory(access, TEAM)
    now = datetime(2026, 10, 29, 0, 0, 0, tzinfo=timezone.utc)
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        clock=lambda: now,
        near_expiry_margin_seconds=86_400.0,
    )

    team_work_stream = f"UA1_TEAM_WORK_{TEAM}"
    max_age_ns = 604_860 * 1_000_000_000
    nc.streams[team_work_stream] = {"created": "2026-10-20T00:00:00Z"}
    nc.messages[team_work_stream] = {
        1: {"subject": f"ua.v1.{TEAM}.lead-host-1.work.team", "data": b"stuck-team-work", "time": "2026-10-22T12:00:00Z"},
    }
    orig_req = nc.request

    async def req_with_age(subject: str, payload: bytes = b"", *, timeout: float = 5.0) -> Any:
        # Must never attempt CONSUMER.LIST, STREAM.MSG.GET, or STREAM.MSG.DELETE on UA1_TEAM_WORK.
        assert not subject.startswith(f"$JS.API.CONSUMER.LIST.{team_work_stream}")
        assert not subject.startswith(f"$JS.API.STREAM.MSG.GET.{team_work_stream}")
        assert not subject.startswith(f"$JS.API.STREAM.MSG.DELETE.{team_work_stream}")
        resp = await orig_req(subject, payload, timeout=timeout)
        if subject == f"$JS.API.STREAM.INFO.{team_work_stream}":
            doc = json.loads(resp.data.decode("utf-8"))
            doc["state"]["first_ts"] = "2026-10-22T12:00:00Z"
            doc["config"] = {"max_age": max_age_ns}
            resp = SimpleNamespace(data=json.dumps(doc).encode("utf-8"))
        return resp

    nc.request = req_with_age  # type: ignore[method-assign]

    summary = await service.sweep_work_streams()
    assert summary.messages_swept == 0
    assert any(item.stream == team_work_stream and item.first_seq == 1 for item in summary.near_expiry)
    assert 1 in nc.messages[team_work_stream]  # untouched by team_services
    notices = access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))
    assert len(notices) == 1
    assert notices[0][0].kind is AccessNoticeKind.QUEUED_MESSAGE_EXPIRING
    assert team_work_stream in notices[0][0].title


@pytest.mark.asyncio
async def test_f2_cross_runtime_duplicate_notices_deduplicated_in_batch_and_across_batches(
    tmp_path: Path,
) -> None:
    """F2 (CON-1 / FA-4 / DI-3): Two runtimes setting the same message aside produce ONE alert in-batch and across batches."""

    nc = DlqFakeNatsConnection()
    ts_live, worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,)))
    people = PeopleDirectory(access, TEAM)
    chat = RecordingChatSender()
    current_time = [NOW]
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        chat_sender=chat,
        clock=lambda: current_time[0],
        report_quiet_seconds=0.0,
    )

    # 1. Cross-runtime duplicate across two separate batches (worker-host-1 then team-services sweep).
    payload_a = b'{"protocol":"1.0-pre","kind":"task.request","message_id":"019a0000-0000-7000-8000-000000000001"}'
    res_w1, deliv_w1 = await _emit_set_aside_into_dlq(
        nc, worker_live, raw_bytes=payload_a, reason="handler_failed", original_seq=11
    )
    res_ts1, deliv_ts1 = await _emit_set_aside_into_dlq(
        nc, ts_live, raw_bytes=payload_a, reason="delivery_limit", original_seq=11
    )
    assert res_w1.copy_id != res_ts1.copy_id  # different runtime keys -> different copy_ids

    assert await service.process_delivered_notices([deliv_w1]) == 1
    assert len(access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))) == 1
    assert len(chat.sent) == 1

    # Second batch carries the sweep's notice for the exact same (stream, seq, raw.sha256): suppressed + acked + alias stored!
    assert await service.process_delivered_notices([deliv_ts1]) == 0
    assert deliv_ts1.ack_calls == 1
    assert len(access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))) == 1
    assert len(chat.sent) == 1
    assert (ts_live.identity.runtime, res_ts1.copy_id) in access.dlq_reported_copy_keys(
        TEAM, [(ts_live.identity.runtime, res_ts1.copy_id)]
    )

    # 2. Cross-runtime duplicate WITHIN ONE FETCHED BATCH: deduplicated to 1 line and 1 notice!
    payload_b = b'{"protocol":"1.0-pre","kind":"task.request","message_id":"019a0000-0000-7000-8000-000000000002"}'
    res_w2, deliv_w2 = await _emit_set_aside_into_dlq(
        nc, worker_live, raw_bytes=payload_b, reason="handler_failed", original_seq=22
    )
    res_ts2, deliv_ts2 = await _emit_set_aside_into_dlq(
        nc, ts_live, raw_bytes=payload_b, reason="delivery_limit", original_seq=22
    )
    assert res_w2.copy_id != res_ts2.copy_id
    assert await service.process_delivered_notices([deliv_w2, deliv_ts2]) == 1
    assert deliv_w2.ack_calls == 1 and deliv_ts2.ack_calls == 1
    notices_after = access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))
    assert len(notices_after) == 2
    assert len(chat.sent) == 2
    assert "1 message(s) could not be handled" in chat.sent[-1]["title"]
    assert (ts_live.identity.runtime, res_ts2.copy_id) in access.dlq_reported_copy_keys(
        TEAM, [(ts_live.identity.runtime, res_ts2.copy_id)]
    )

    # 3. Different bytes at the same sequence (recreated stream) get a different raw.sha256 and ARE reported!
    payload_c = b'{"protocol":"1.0-pre","kind":"task.request","message_id":"019a0000-0000-7000-8000-000000000003"}'
    _, deliv_recreated = await _emit_set_aside_into_dlq(
        nc, worker_live, raw_bytes=payload_c, reason="handler_failed", original_seq=11,
        stored_at_ns=1_792_800_000_000_000_000,
    )
    assert await service.process_delivered_notices([deliv_recreated]) == 1
    assert len(chat.sent) == 3

    # 4. Pruning removes old entries from both protocol1_dlq_reported_notices and protocol1_dlq_reported_originals.
    orig_a = (f"UA1_PART_{TEAM}~8", 11, b64url(hashlib.sha256(payload_a).digest()))
    assert orig_a in access.dlq_reported_original_keys(TEAM, [orig_a])
    access.prune_dlq_reported_notices(TEAM, before_utc=NOW + timedelta(days=31))
    assert access.dlq_reported_original_keys(TEAM, [orig_a]) == {}


@pytest.mark.asyncio
async def test_f3_untrusted_text_blocked_in_invalid_subject_original_subject_old_envelope_and_chat_shortened(
    tmp_path: Path,
) -> None:
    """F3 (CRIT-2, SEC-2, SEC-3, GR-5): blocks sender-chosen text in invalid subjects, off-team original.subject,
    old-envelope identifiers/unknown people, uses PeopleDirectory display names, and shortens chat text."""

    nc = DlqFakeNatsConnection()
    ts_live, worker_live = _make_hosts(nc)
    access = AccessRepository(_storage(tmp_path))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,), display_name="Admin User"))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.ACTIVE, display_name="Alice Example"))
    people = PeopleDirectory(access, TEAM)
    chat = RecordingChatSender()
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        chat_sender=chat,
        clock=lambda: NOW,
        report_quiet_seconds=0.0,
    )

    # 1. SEC-2: Invalid notice on unparsable subject never prints publisher-chosen subject tokens.
    evil_token = "URGENT_admins_must_run_curl_evil_sh_now"
    bad_delivery = FakeDelivery(
        data=b"not-a-frame",
        subject=f"ua.v1.{TEAM}.worker-1.dlq.notice.{evil_token}",
        stream=dlq_stream_name(TEAM),
        stream_seq=41,
        timestamp_ns=int(NOW.timestamp() * 1_000_000_000),
    )
    bad_delivery_garbage = FakeDelivery(
        data=b"not-a-frame-2",
        subject=f"completely.weird.{evil_token}",
        stream=dlq_stream_name(TEAM),
        stream_seq=42,
        timestamp_ns=int(NOW.timestamp() * 1_000_000_000),
    )

    # 2. CRIT-2: Validly signed notice whose protocol 1 original.subject lacks ua.v1.<team>. prefix shows "an unexpected subject".
    _, off_team_delivery = await _emit_set_aside_into_dlq(
        nc,
        worker_live,
        raw_bytes=b'{"protocol":"1.0-pre","kind":"task.request"}',
        reason="malformed",
        original_subject="other.prefix.attacker-payload",
        original_seq=43,
    )

    # 3. SEC-3 & GR-5: upgrade_old_format drops hostile message_class/task_id, uses display_name for Alice,
    #    and labels unknown person ids as "a person not in the team's records".
    old_known = json.dumps({
        "msg_meta": {"msg_correlation_id": "req-ok-7", "gateway_context": {"canonical_user_id": ALICE_ID}},
        "msg_content": {
            "message_class": "TaskRequest: Alice password is secret",
            "task_id": "z" * 250,
            "root_task_id": "task-clean-7",
        },
    }).encode("utf-8")
    _, old_known_deliv = await _emit_set_aside_into_dlq(
        nc, worker_live, raw_bytes=old_known, reason="upgrade_old_format", original_seq=44
    )
    old_unknown = json.dumps({
        "msg_meta": {"gateway_context": {"canonical_user_id": "attacker-fake-person-text"}},
        "msg_content": {"message_class": "TaskRequest", "task_id": "task-clean-8"},
    }).encode("utf-8")
    _, old_unknown_deliv = await _emit_set_aside_into_dlq(
        nc, worker_live, raw_bytes=old_unknown, reason="upgrade_old_format", original_seq=45
    )

    reported = await service.process_delivered_notices(
        [bad_delivery, bad_delivery_garbage, off_team_delivery, old_known_deliv, old_unknown_deliv]
    )
    assert reported == 5
    notice = access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))[0][0]
    for channel_text in (notice.summary, chat.sent[0]["text"]):
        assert evil_token not in channel_text
        assert "worker-1 on an unexpected subject (stream sequence 41)" in channel_text
        assert "invalid dead-letter notice on an unexpected subject (stream sequence 42)" in channel_text
        assert "other.prefix.attacker-payload" not in channel_text
        assert "(an unexpected subject)" in channel_text
        assert "password is secret" not in channel_text
        assert ("z" * 250) not in channel_text
        assert "for Alice Example" in channel_text
        assert "task-clean-7" in channel_text
        assert "attacker-fake-person-text" not in channel_text
        assert "for a person not in the team's records" in channel_text

    # 4. SEC-3: Chat text is shortened identically to Inbox summary (<= 4000 chars).
    long_items = [
        _VerifiedNoticeItem(
            issuer_runtime="worker-host-1",
            copy_id=f"{i:064x}",
            reason="malformed",
            original_subject=f"ua.v1.{TEAM}.lead-1." + ("a" * 100) + "." + ("b" * 100) + "." + ("c" * 100) + "." + ("d" * 100),
            original_stream="UA1_PART_" + ("s" * 200),
            original_stream_sequence=i + 1,
            deliveries=1,
            release="0.10.0",
            issued_at="2026-10-22T00:00:00Z",
            claimed_kind="task.request",
            claimed_message_id="019a0000-0000-7000-8000-000000000001",
            pointer="/body/prompt",
            raw_stored=True,
            raw_size=10,
            raw_status="ok",
            raw_sha256="A" * 43,
        )
        for i in range(10)
    ]
    formatted_long = format_dlq_admin_notice(long_items, (), dlq_stream=dlq_stream_name(TEAM))
    assert len(formatted_long.text) <= 4000
    assert formatted_long.text.endswith("...")


def test_c29_dlq_reported_original_keys_uses_full_primary_key_index(tmp_path: Path) -> None:
    """C29: dlq_reported_original_keys queries by the full 4-column primary key via VALUES so SQLite never scans the team."""

    import sqlite3

    access = AccessRepository(_storage(tmp_path, "c29_pk.db"))
    # Verify the exact SQL pattern executes with full 4-column PK index search in SQLite.
    with sqlite3.connect(str(tmp_path / "c29_pk.db")) as con:
        plan = con.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT original_stream, original_stream_sequence, raw_sha256, notice_id "
            "FROM protocol1_dlq_reported_originals "
            "WHERE (team_id, original_stream, original_stream_sequence, raw_sha256) IN (VALUES (?,?,?,?),(?,?,?,?))",
            (TEAM, "S1", 1, "h1", TEAM, "S1", 2, "h2"),
        ).fetchall()
    plan_text = " ".join(str(row) for row in plan)
    assert "team_id=? AND original_stream=? AND original_stream_sequence=? AND raw_sha256=?" in plan_text

    # Verify >200 keys chunk cleanly across 100-row batches and find exact matches only.
    from custom_messages.contracts.access_v2 import AccessNoticeV1

    notice = AccessNoticeV1(
        notice_id="p1_dlq:c29_batch",
        team_id=TEAM,
        kind=AccessNoticeKind.MESSAGES_SET_ASIDE,
        recipient_roles=(PERSON_ROLE_ADMIN,),
        title="t",
        summary="s",
        created_at_utc=NOW,
    )
    keys = [(f"UA1_PART_{TEAM}~0", i, f"{i:043d}") for i in range(1, 251)]
    receipts = [("worker-1", f"{i:064x}") for i in range(1, 251)]
    orig_map = dict(zip(receipts, keys))
    created, inserted = access.record_dlq_notice_report(
        TEAM, notice, receipts, original_keys=orig_map, reported_at_utc=NOW
    )
    assert created is True and inserted == 250
    found = access.dlq_reported_original_keys(TEAM, [*keys, ("UA1_PART_team_example~0", 999, "missing")])
    assert len(found) == 250
    assert ("UA1_PART_team_example~0", 999, "missing") not in found


@pytest.mark.asyncio
async def test_c30_shared_authority_constant_people_snapshot_and_alias_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """C30: AUTHORITY_RUNTIME is one shared constant; snapshot reuses PeopleDirectory.people(); alias failure logs warning."""

    from custom_transport.protocol1.joining import AUTHORITY_RUNTIME as JOINING_AUTHORITY_RUNTIME
    from installation_authority.service import AUTHORITY_RUNTIME as SERVICE_AUTHORITY_RUNTIME

    assert SERVICE_AUTHORITY_RUNTIME is JOINING_AUTHORITY_RUNTIME

    access = AccessRepository(_storage(tmp_path, "c30.db"))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,), display_name="Admin"))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED, display_name="Alice Gone"))
    people = PeopleDirectory(access, TEAM)
    nc = DlqFakeNatsConnection()
    ts_live, _ = _make_hosts(nc)

    test_logger = logging.getLogger("test.c30_alias_warning")
    service = DeadLetterService(
        team_id=TEAM, live_fn=lambda: ts_live, access_repository=access, people=people, logger=test_logger
    )
    refs, ids, by_id = service._people_directory_snapshot()
    assert refs == frozenset({ALICE_REF})
    assert ids == frozenset({ALICE_ID})
    assert by_id[ADMIN_ID].display_name == "Admin"
    assert by_id[ALICE_ID].person_ref == ALICE_REF

    def _fail_aliases(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("db temporarily locked")

    access.record_dlq_copy_aliases = _fail_aliases  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="test.c30_alias_warning"):
        await service._record_db_duplicate_aliases([("worker-2", "b" * 64, "notice-1")])
    warnings = [r for r in caplog.records if "event=protocol1_dlq_duplicate_alias_store_failed" in r.getMessage()]
    assert len(warnings) == 1
    assert f"team={TEAM}" in warnings[0].getMessage()
    assert "count=1" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_f4_fa2_blob_delete_failure_still_scans_dlq_and_keeps_blob_retry(tmp_path: Path) -> None:
    """F4 (FA-2): A failed blob delete still runs the DLQ raw-copy scan, combines all counts, and preserves blob retry."""

    access = AccessRepository(_storage(tmp_path, "f4_fa2.db"))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    access.record_person(_person(BOB_ID, BOB_REF, PersonStanding.ACTIVE))
    people = PeopleDirectory(access, TEAM)
    nc = DlqFakeNatsConnection()
    ts_live, _ = _make_hosts(nc)

    dlq_stream = dlq_stream_name(TEAM)
    _store_in_stream(
        nc,
        dlq_stream,
        dlq_raw_subject(TEAM, "worker-host-1", "1" * 64),
        json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode(),
    )
    _store_in_stream(
        nc,
        dlq_stream,
        dlq_raw_subject(TEAM, "worker-host-1", "2" * 64),
        json.dumps({"protocol": "1.0-pre", "authority": {"person": BOB_REF}, "body": {}}).encode(),
    )

    class _FailingBlobService:
        def __init__(self) -> None:
            self.calls = 0

        async def delete_person(self, person_ref: str) -> PersonDeleteResult:
            self.calls += 1
            return PersonDeleteResult(
                outcome="runtime_error",
                owners_removed=3,
                blobs_deleted=1,
                blobs_kept=2,
                message="1 blob(s) could not be deleted from OBJ_UA1_BLOBS and remain queued for retry.",
            )

    blobs = _FailingBlobService()
    departure = TeamPersonDepartureService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        people=people,
        blobs=blobs,  # type: ignore[arg-type]
    )
    reply = await departure.delete_person_data(ALICE_REF)
    assert blobs.calls == 1
    assert reply.outcome == "runtime_error"
    assert reply.owners_removed == 3
    assert reply.blobs_deleted == 1
    assert reply.blobs_kept == 2
    # DLQ scan STILL ran and deleted Alice's raw copy while keeping Bob's!
    assert reply.dlq_copies_deleted == 1
    assert reply.dlq_copies_kept == 1
    assert 1 not in nc.messages[dlq_stream]
    assert 2 in nc.messages[dlq_stream]


@pytest.mark.asyncio
async def test_f4_fa3_maintain_isolates_step_failures_and_runs_raw_scan_first(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """F4 (FA-3): An error in recovery or sweep inside maintain() never skips departed raw-copy deletion or receipt pruning."""

    access = AccessRepository(_storage(tmp_path, "f4_fa3.db"))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    people = PeopleDirectory(access, TEAM)
    nc = DlqFakeNatsConnection()
    ts_live, _ = _make_hosts(nc)

    dlq_stream = dlq_stream_name(TEAM)
    _store_in_stream(
        nc,
        dlq_stream,
        dlq_raw_subject(TEAM, "worker-host-1", "a" * 64),
        json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode(),
    )
    # Seed an expired receipt that pruning should remove even if sweep and recovery raise.
    from custom_messages.contracts.access_v2 import AccessNoticeV1

    old_time = NOW - timedelta(days=40)
    old_notice = AccessNoticeV1(
        notice_id="p1_dlq:old_prune_test",
        team_id=TEAM,
        kind=AccessNoticeKind.MESSAGES_SET_ASIDE,
        recipient_roles=(PERSON_ROLE_ADMIN,),
        title="old",
        summary="old",
        created_at_utc=old_time,
    )
    access.record_dlq_notice_report(TEAM, old_notice, [("worker-1", "b" * 64)], reported_at_utc=old_time)

    test_logger = logging.getLogger("test.f4_fa3_maintain")
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        clock=lambda: NOW,
        logger=test_logger,
    )

    async def _boom_sweep(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("sweep boom")

    async def _boom_recover(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("recovery boom")

    service.sweep_work_streams = _boom_sweep  # type: ignore[method-assign]
    service.recover_exhausted_notices = _boom_recover  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="test.f4_fa3_maintain"):
        report = await service.maintain()

    assert report.raw_scan.departed_deleted == 1
    assert 1 not in nc.messages[dlq_stream]
    assert report.receipts_pruned == 1
    assert report.sweep.streams_failed == 1
    assert report.recovered_notices == 0
    failed_events = [r.getMessage() for r in caplog.records if "event=protocol1_dlq_maintain_step_failed" in r.getMessage()]
    assert any("step=sweep_work_streams" in msg for msg in failed_events)
    assert any("step=recover_exhausted_notices" in msg for msg in failed_events)


@pytest.mark.asyncio
async def test_f6_departure_stop_never_starts_continuation_or_logs_finished(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """F6: Stopping during initial departure scan starts no continuation; stopping during continuation logs stopped, not ok."""

    access = AccessRepository(_storage(tmp_path, "f6.db"))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    people = PeopleDirectory(access, TEAM)
    nc = DlqFakeNatsConnection()
    ts_live, _ = _make_hosts(nc)

    dlq_stream = dlq_stream_name(TEAM)
    for idx in range(1, 5):
        _store_in_stream(
            nc,
            dlq_stream,
            dlq_raw_subject(TEAM, "worker-host-1", f"{idx:064x}"),
            json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode(),
        )

    test_logger = logging.getLogger("test.f6_stop")
    departure = TeamPersonDepartureService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        people=people,
        delete_budget_seconds=10.0,
        logger=test_logger,
    )
    # 1. Already stopping during initial scan -> does NOT start a continuation task.
    departure._stopping = True
    with caplog.at_level(logging.INFO, logger="test.f6_stop"):
        reply = await departure.delete_person_data(ALICE_REF)
    assert reply.outcome == "runtime_error"
    assert departure.active_continuation_count() == 0
    assert any("event=team_delete_person_data_stopped" in r.getMessage() for r in caplog.records)
    assert not any("event=team_delete_person_data_retry_later" in r.getMessage() for r in caplog.records)

    # 2. Stopping during _run_continuation logs continuation_stopped with next_seq, never continuation_ok.
    caplog.clear()
    from team_services.dead_letters import _DepartureRun

    run = _DepartureRun(person_ref=ALICE_REF, person_id=ALICE_ID, next_seq=2)
    with caplog.at_level(logging.INFO, logger="test.f6_stop"):
        await departure._run_continuation(run)
    messages = [r.getMessage() for r in caplog.records]
    assert any("event=team_delete_person_data_continuation_stopped" in m and "next_seq=2" in m for m in messages)
    assert not any("event=team_delete_person_data_continuation_ok" in m for m in messages)


@pytest.mark.asyncio
async def test_f8_con3_held_notice_rebuilds_departed_standing_before_delivery(tmp_path: Path) -> None:
    """F8 (CON-3): If a person departs while a notice batch is held on an Inbox outage, delivery rebuilds text without naming them."""

    access = AccessRepository(_storage(tmp_path, "f8_con3.db"))
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, (PERSON_ROLE_OWNER,), display_name="Admin"))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.ACTIVE, display_name="Alice Active"))
    people = PeopleDirectory(access, TEAM)
    nc = DlqFakeNatsConnection()
    ts_live, worker_live = _make_hosts(nc)
    chat = RecordingChatSender()
    chat.fail_count = 1

    old_alice = json.dumps(
        {
            "msg_meta": {"gateway_context": {"canonical_user_id": ALICE_ID}, "msg_correlation_id": "req-hold-1"},
            "msg_content": {"message_class": "Task", "task_id": "task-hold-1", "task_description": "secret"},
        }
    ).encode("utf-8")
    _, delivery = await _emit_set_aside_into_dlq(
        nc,
        worker_live,
        raw_bytes=old_alice,
        reason="upgrade_old_format",
        original_subject="ua.legacy.work.team_lead",
        original_stream="UA_WORK",
        original_seq=1,
    )

    real_record = access.record_dlq_notice_report
    attempts = {"n": 0}

    def _fail_first_then_depart(*args: Any, **kwargs: Any) -> tuple[bool, int]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            # Alice departs while the batch is held!
            access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED, display_name="Alice Active"))
            raise RuntimeError("transient inbox outage")
        return real_record(*args, **kwargs)

    access.record_dlq_notice_report = _fail_first_then_depart  # type: ignore[method-assign]
    service = DeadLetterService(
        team_id=TEAM,
        live_fn=lambda: ts_live,
        access_repository=access,
        people=people,
        chat_sender=chat,
        clock=lambda: NOW,
        report_quiet_seconds=0.0,
        ack_wait_seconds=0.3,
        initial_retry_seconds=0.02,
        max_retry_seconds=0.05,
    )
    reported = await service.process_delivered_notices([delivery])
    assert reported == 1
    assert attempts["n"] == 2
    notice = access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))[0][0]
    for channel_text in (notice.summary, chat.sent[0]["text"]):
        assert "Alice Active" not in channel_text
        assert "task-hold-1" not in channel_text
        assert "departed person" in channel_text
    assert 1 not in nc.messages[dlq_stream_name(TEAM)]


@pytest.mark.asyncio
async def test_f8_crit3_departure_scan_breaks_when_stored_seq_exceeds_last_seq(tmp_path: Path) -> None:
    """F8 (DI-1/CRIT-3): _scan_dlq_segment stops when next_by_subj returns a message appended past info.last_seq."""

    access = AccessRepository(_storage(tmp_path, "f8_crit3.db"))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    people = PeopleDirectory(access, TEAM)
    nc = DlqFakeNatsConnection()
    ts_live, _ = _make_hosts(nc)

    dlq_stream = dlq_stream_name(TEAM)
    # Seq 1 is a notice (does not match raw wildcard); seq 2 will be appended AFTER stream_info reports last_seq=1.
    _store_in_stream(nc, dlq_stream, dlq_notice_subject(TEAM, "worker-host-1", "1" * 64), b"{}")

    orig_request = nc.request

    async def _inject_after_info(subject: str, payload: bytes = b"", *, timeout: float = 5.0) -> Any:
        resp = await orig_request(subject, payload, timeout=timeout)
        if subject == f"$JS.API.STREAM.INFO.{dlq_stream}" and 2 not in nc.messages[dlq_stream]:
            _store_in_stream(
                nc,
                dlq_stream,
                dlq_raw_subject(TEAM, "worker-host-1", "2" * 64),
                json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode(),
            )
        return resp

    nc.request = _inject_after_info  # type: ignore[method-assign]
    departure = TeamPersonDepartureService(team_id=TEAM, live_fn=lambda: ts_live, people=people)
    reply = await departure.delete_person_data(ALICE_REF)
    assert reply.outcome == "ok"
    assert reply.dlq_copies_deleted == 0
    assert reply.dlq_copies_kept == 0
    # Seq 2 was past the snapshot bound info.last_seq=1, so the scan broke without reading past its bound.
    assert 2 in nc.messages[dlq_stream]
