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
import json
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

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
) -> TeamPersonV1:
    effective_roles = roles if standing is PersonStanding.ACTIVE else ()
    return TeamPersonV1(
        team_id=TEAM,
        person_id=person_id,
        person_ref=person_ref,
        standing=standing,
        roles=effective_roles,
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
        "msg_meta": {"gateway_context": {"canonical_user_id": ALICE_ID}},
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
    assert "task-old-9" in notice.summary
    assert "Check quarterly budget" in notice.summary
    assert "send any needed request again" in notice.summary
    assert len(chat.sent) == 1 and chat.sent[0]["reason"] == DEAD_LETTER_NOTICE_REASON

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

    # Release continuation gate and wait for completion; next call reports final cumulative ok counts.
    gate.set()
    for _ in range(100):
        if bounded.active_continuation_count() == 0:
            break
        await asyncio.sleep(0.01)
    assert bounded.active_continuation_count() == 0

    final = await bounded_ops.handle_delete_person_data(
        _verified_command("team.delete_person_data", {"request_id": "r7", "person": ALICE_REF}, roles=("lead_host",))
    )
    assert final == {
        "outcome": "ok",
        "owners_removed": 2,
        "blobs_deleted": 1,
        "blobs_kept": 1,
        "dlq_copies_deleted": 2,
        "dlq_copies_kept": 2,
    }
    assert len(blob_calls) == 1  # Blob deletion ran once across the entire continuation lifecycle.


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
        id_generator=SimpleNamespace(new_id=lambda: "id-1"),  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
        on_unsolicited=on_unsolicited,
    )
    await client.handle_response(envelope)
    assert len(received) == 1
    delivered = received[0]
    assert isinstance(delivered.msg_content, UserNotification)
    assert delivered.msg_content.reason == DEAD_LETTER_NOTICE_REASON
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
