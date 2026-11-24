"""Deterministic unit tests for custom_transport/protocol1/blobs.py (A1, S2, FA-6, FA-7)."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pytest
from nats.js.api import Header, PubAck, RawStreamMsg
from nats.js.errors import APIError, NotFoundError

from custom_transport.protocol1 import blobs
from custom_transport.protocol1.blobs import (
    BUCKET_NAME,
    CHUNK_SUBJECT_PREFIX,
    HEADER_AGENT,
    HEADER_PERSON,
    HEADER_REQUEST,
    HEADER_SHA256,
    HEADER_TEAM,
    JS_ERR_MAX_BYTES_EXCEEDED,
    JS_ERR_NO_MESSAGE_FOUND,
    JS_ERR_STREAM_NOT_FOUND,
    JS_ERR_WRONG_LAST_SEQUENCE,
    META_SUBJECT_PREFIX,
    STREAM_NAME,
    BlobConcurrentWrite,
    BlobCorrupted,
    BlobDeleted,
    BlobError,
    BlobNotFound,
    BlobOwner,
    BlobStoreFull,
    BlobSubjectListShifted,
    BlobTooLarge,
    BlobWriteTimedOut,
    blob_chunk_subject,
    blob_metadata_subject,
    blob_object_name,
    check_chunks_present,
    decode_metadata_subject,
    delete_blob,
    get_blob,
    list_stream_subjects,
    parse_blob_metadata,
    purge_blob_chunks,
    put_blob,
    sha256_unpadded,
    stat_blob,
    sweep_orphan_chunks,
)

TEAM = "team_example"
REQUEST_OWNER = BlobOwner(request_id="req_knowledge_1", person_ref="person_7c1")
AGENT_OWNER = BlobOwner(agent_id="researcher-2")


@dataclass
class _StoredMessage:
    seq: int
    subject: str
    data: bytes
    time: datetime
    headers: dict[str, str]


class FakeJetStream:
    """In-memory fake of ``OBJ_UA1_BLOBS`` enforcing JetStream CAS, no-rollup, max_bytes, and S2 APIs."""

    def __init__(
        self,
        *,
        now: Optional[datetime] = None,
        max_bytes: Optional[int] = None,
        page_size: int = 100_000,
        stream_exists: bool = True,
    ) -> None:
        self._prefix = "$JS.API"
        self._timeout = 5.0
        self.now = now or datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
        self.max_bytes = max_bytes
        self.page_size = page_size
        self.stream_exists = stream_exists
        self.messages: list[_StoredMessage] = []
        self._next_seq = 1
        self.calls: list[tuple[str, Any]] = []
        self.on_publish: Optional[Callable[[str, bytes, dict[str, str]], None]] = None
        self.on_api_request: Optional[Callable[[str, dict[str, Any]], Optional[dict[str, Any]]]] = None

    def _require_stream(self, stream_name: str) -> None:
        if not self.stream_exists or stream_name != STREAM_NAME:
            raise NotFoundError(code=404, err_code=JS_ERR_STREAM_NOT_FOUND, description="stream not found")

    def _current_bytes(self) -> int:
        return sum(len(m.data) for m in self.messages)

    def _matches(self, subject: str, pattern: str) -> bool:
        if pattern == ">":
            return True
        if pattern.endswith(".>"):
            return subject.startswith(pattern[:-1])
        return subject == pattern

    def _last_seq_for_subject(self, subject: str) -> int:
        matching = [m.seq for m in self.messages if m.subject == subject]
        return matching[-1] if matching else 0

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> PubAck:
        self.calls.append(("publish", subject, dict(headers or {})))
        hdrs = dict(headers or {})
        if Header.ROLLUP in hdrs or "Nats-Rollup" in hdrs:
            raise APIError(code=400, err_code=10111, description="rollup not permitted")
        if self.on_publish is not None:
            self.on_publish(subject, payload, hdrs)
        if Header.EXPECTED_LAST_SUBJECT_SEQUENCE in hdrs:
            expected = int(hdrs[Header.EXPECTED_LAST_SUBJECT_SEQUENCE])
            actual = self._last_seq_for_subject(subject)
            if actual != expected:
                raise APIError(
                    code=400,
                    err_code=JS_ERR_WRONG_LAST_SEQUENCE,
                    description=f"wrong last sequence: {actual}",
                )
        if self.max_bytes is not None and self._current_bytes() + len(payload) > self.max_bytes:
            raise APIError(
                code=400,
                err_code=JS_ERR_MAX_BYTES_EXCEEDED,
                description="maximum bytes exceeded",
            )
        seq = self._next_seq
        self._next_seq += 1
        self.messages.append(_StoredMessage(seq=seq, subject=subject, data=bytes(payload), time=self.now, headers=hdrs))
        return PubAck(stream=STREAM_NAME, seq=seq)

    async def get_last_msg(self, stream_name: str, subject: str, direct: bool = False) -> RawStreamMsg:
        self.calls.append(("get_last_msg", stream_name, subject))
        self._require_stream(stream_name)
        for msg in reversed(self.messages):
            if msg.subject == subject:
                return RawStreamMsg(
                    subject=msg.subject,
                    seq=msg.seq,
                    data=msg.data if msg.data else None,
                    time=msg.time,
                    headers=msg.headers or None,
                )
        raise NotFoundError(code=404, err_code=JS_ERR_NO_MESSAGE_FOUND, description="no message found")

    async def get_msg(
        self,
        stream_name: str,
        seq: Optional[int] = None,
        subject: Optional[str] = None,
        direct: bool = False,
        next: bool = False,
    ) -> RawStreamMsg:
        self.calls.append(("get_msg", stream_name, seq, subject, next))
        self._require_stream(stream_name)
        if not next:
            return await self.get_last_msg(stream_name, subject or "")
        start = seq or 1
        for msg in self.messages:
            if msg.seq >= start and msg.subject == subject:
                return RawStreamMsg(
                    subject=msg.subject,
                    seq=msg.seq,
                    # Match nats-py: empty payloads decode to None.
                    data=msg.data if msg.data else None,
                    time=msg.time,
                    headers=msg.headers or None,
                )
        raise NotFoundError(code=404, err_code=JS_ERR_NO_MESSAGE_FOUND, description="no message found")

    async def purge_stream(
        self,
        name: str,
        seq: Optional[int] = None,
        subject: Optional[str] = None,
        keep: Optional[int] = None,
    ) -> bool:
        self.calls.append(("purge_stream", name, subject, keep))
        self._require_stream(name)
        matching = [m for m in self.messages if subject is None or self._matches(m.subject, subject)]
        to_keep = set()
        if keep:
            for m in matching[-keep:]:
                to_keep.add(m.seq)
        to_drop = {m.seq for m in matching if m.seq not in to_keep}
        self.messages = [m for m in self.messages if m.seq not in to_drop]
        return True

    async def _api_request(self, req_subject: str, payload: bytes, timeout: float = 5.0) -> dict[str, Any]:
        req = json.loads(payload.decode("utf-8")) if payload else {}
        self.calls.append(("api_request", req_subject, req))
        if self.on_api_request is not None:
            override = self.on_api_request(req_subject, req)
            if override is not None:
                return override
        if req_subject == f"{self._prefix}.STREAM.INFO.{STREAM_NAME}":
            self._require_stream(STREAM_NAME)
            filt = req.get("subjects_filter", ">")
            offset = int(req.get("offset", 0))
            counts: dict[str, int] = {}
            for msg in self.messages:
                if self._matches(msg.subject, filt):
                    counts[msg.subject] = counts.get(msg.subject, 0) + 1
            ordered = sorted(counts.items())
            page = dict(ordered[offset:offset + self.page_size])
            return {"state": {"subjects": page}, "total": len(ordered)}
        raise AssertionError(f"Unexpected JetStream API call: {req_subject}")

    async def subscribe(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("S2: blob client must never create consumers or call subscribe()")


def test_naming_and_owner_validation() -> None:
    digest = sha256_unpadded(b"hello")
    name = blob_object_name(TEAM, digest)
    assert name == f"{TEAM}~{digest}"

    meta_subj = blob_metadata_subject(name)
    assert meta_subj.startswith(META_SUBJECT_PREFIX)
    # Padded base64url matches ADR-20.
    assert meta_subj == META_SUBJECT_PREFIX + base64.urlsafe_b64encode(name.encode()).decode()
    assert decode_metadata_subject(meta_subj) == name
    assert decode_metadata_subject(f"{META_SUBJECT_PREFIX}not-valid-b64") is None
    assert decode_metadata_subject("wrong.prefix") is None

    assert blob_chunk_subject("Zq4GJ3mT8wKx1bN0aRdYfE") == f"{CHUNK_SUBJECT_PREFIX}Zq4GJ3mT8wKx1bN0aRdYfE"

    # Reject subject injection / wildcards in nuid and invalid team/sha256.
    for bad_nuid in ("", "a.b", "a*", ">", "a b", "x/y", "a" * 65):
        with pytest.raises(ValueError):
            blob_chunk_subject(bad_nuid)
    with pytest.raises(ValueError):
        blob_object_name("_bad_team", digest)
    with pytest.raises(ValueError):
        blob_object_name(TEAM, "not-a-sha256")
    # Non-canonical trailing bits in base64url are refused.
    non_canonical = digest[:-1] + ("B" if digest[-1] == "A" else "f")
    if non_canonical != digest:
        with pytest.raises(ValueError):
            blob_object_name(TEAM, non_canonical)

    # Owner validation matches contract and C08 (agent owner forbids person_ref).
    with pytest.raises(ValueError):
        BlobOwner()
    with pytest.raises(ValueError):
        BlobOwner(request_id="req_1", agent_id="researcher-2")
    with pytest.raises(ValueError):
        BlobOwner(agent_id="researcher-2", person_ref="person_7c1")

    req_hdrs = REQUEST_OWNER.to_headers(TEAM, digest)
    assert req_hdrs == {
        HEADER_SHA256: [digest],
        HEADER_TEAM: [TEAM],
        HEADER_REQUEST: ["req_knowledge_1"],
        HEADER_PERSON: ["person_7c1"],
    }
    agent_hdrs = AGENT_OWNER.to_headers(TEAM, digest)
    assert agent_hdrs == {
        HEADER_SHA256: [digest],
        HEADER_TEAM: [TEAM],
        HEADER_AGENT: ["researcher-2"],
    }


@pytest.mark.asyncio
async def test_put_and_verified_get_multichunk_and_exact_boundary() -> None:
    js = FakeJetStream()
    chunk_size = blobs.default_chunk_bytes()
    assert chunk_size == 131_072

    # Multi-chunk blob (2 full chunks + 17 trailing bytes = 3 chunks).
    data = bytes(range(256)) * ((chunk_size * 2) // 256) + b"trailing-17-bytes"
    put = await put_blob(js, TEAM, data, REQUEST_OWNER, nuid_factory=lambda: "nuid_multi_1")
    assert put.reused is False
    assert put.size == len(data)
    assert put.chunks == 3
    assert put.nuid == "nuid_multi_1"
    assert put.to_blob_ref(media_type="text/plain", name="page.md") == {
        "sha256": put.sha256,
        "size": len(data),
        "media_type": "text/plain",
        "name": "page.md",
    }

    # Verify ADR-20 metadata on wire has no extra fields, no rollup header, and CAS header=0.
    meta_msg = await js.get_last_msg(STREAM_NAME, blob_metadata_subject(blob_object_name(TEAM, put.sha256)))
    raw_doc = json.loads(meta_msg.data.decode("utf-8"))
    assert set(raw_doc.keys()) == {"name", "bucket", "nuid", "size", "chunks", "digest", "headers", "options"}
    assert raw_doc["bucket"] == BUCKET_NAME
    assert raw_doc["digest"] == "SHA-256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode()
    assert meta_msg.headers == {Header.EXPECTED_LAST_SUBJECT_SEQUENCE: "0"}
    assert not any(call[0] == "api_request" and "STREAM.NAMES" in call[1] for call in js.calls)

    # Verified get reads chunks directly via STREAM.MSG.GET (S2), never creating consumers.
    fetched = await get_blob(js, TEAM, put.sha256, expected_size=len(data), max_bytes=len(data))
    assert fetched == data

    # Exact chunk-size boundary (1 full chunk).
    exact = b"x" * chunk_size
    put_exact = await put_blob(js, TEAM, exact, AGENT_OWNER, nuid_factory=lambda: "nuid_exact_1")
    assert put_exact.chunks == 1
    assert await get_blob(js, TEAM, put_exact.sha256) == exact

    # Second put of identical bytes reuses existing live object without uploading new chunks.
    publishes_before = sum(1 for c in js.calls if c[0] == "publish")
    put_again = await put_blob(js, TEAM, data, AGENT_OWNER, nuid_factory=lambda: "nuid_unused")
    publishes_after = sum(1 for c in js.calls if c[0] == "publish")
    assert put_again.reused is True
    assert put_again.nuid == "nuid_multi_1"
    assert publishes_after == publishes_before


@pytest.mark.asyncio
async def test_put_and_get_empty_blob_zero_chunks() -> None:
    js = FakeJetStream()
    put = await put_blob(js, TEAM, b"", REQUEST_OWNER, nuid_factory=lambda: "nuid_empty_1")
    assert put.size == 0
    assert put.chunks == 0
    assert await check_chunks_present(js, put.metadata) is True
    assert await get_blob(js, TEAM, put.sha256, expected_size=0, max_bytes=0) == b""


@pytest.mark.asyncio
async def test_get_handles_zero_byte_chunk_payload_from_nats_py() -> None:
    """C07: nats-py returns RawStreamMsg.data=None for 0-byte chunk messages."""

    js = FakeJetStream()
    content = b"hello"
    sha = sha256_unpadded(content)
    name = blob_object_name(TEAM, sha)
    # Two chunks: 0-byte chunk followed by 5-byte chunk (valid per ADR-20 since readers assume no chunk size).
    await js.publish(blob_chunk_subject("nuid_zero_chunk"), b"")
    await js.publish(blob_chunk_subject("nuid_zero_chunk"), content)
    meta = {
        "name": name,
        "bucket": BUCKET_NAME,
        "nuid": "nuid_zero_chunk",
        "size": len(content),
        "chunks": 2,
        "digest": f"SHA-256={sha}=",
        "headers": REQUEST_OWNER.to_headers(TEAM, sha),
        "options": {"max_chunk_size": 131_072},
    }
    await js.publish(blob_metadata_subject(name), json.dumps(meta).encode())
    assert await get_blob(js, TEAM, sha) == content


@pytest.mark.asyncio
async def test_get_refuses_missing_deleted_too_large_and_corrupted_blobs() -> None:
    js = FakeJetStream()
    data = b"integrity-check-payload"
    sha = sha256_unpadded(data)

    with pytest.raises(BlobNotFound):
        await get_blob(js, TEAM, sha)

    put = await put_blob(js, TEAM, data, REQUEST_OWNER, nuid_factory=lambda: "nuid_corrupt_test")

    # Pre-download max_bytes check refuses without fetching chunks.
    js.calls.clear()
    with pytest.raises(BlobTooLarge):
        await get_blob(js, TEAM, sha, max_bytes=len(data) - 1)
    assert not any(c[0] == "get_msg" for c in js.calls)

    # Pre-download expected_size check refuses without fetching chunks.
    with pytest.raises(BlobCorrupted):
        await get_blob(js, TEAM, sha, expected_size=len(data) + 1)

    # Tampered chunk bytes fail SHA-256 verification.
    chunk_subj = blob_chunk_subject(put.nuid)
    for msg in js.messages:
        if msg.subject == chunk_subj:
            msg.data = b"X" + msg.data[1:]
    with pytest.raises(BlobCorrupted, match="SHA-256 mismatch"):
        await get_blob(js, TEAM, sha)

    # Restore chunk bytes, then add a trailing extra chunk -> refused.
    for msg in js.messages:
        if msg.subject == chunk_subj:
            msg.data = data
    await js.publish(chunk_subj, b"extra")
    with pytest.raises(BlobCorrupted, match="more than the declared"):
        await get_blob(js, TEAM, sha)

    # Purge chunks while metadata stays live -> missing chunk refused.
    await purge_blob_chunks(js, put.nuid)
    with pytest.raises(BlobCorrupted, match="missing chunk"):
        await get_blob(js, TEAM, sha)

    # Deleted blob refuses with BlobDeleted (subclass of BlobNotFound).
    await delete_blob(js, TEAM, sha)
    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, sha)


@pytest.mark.asyncio
async def test_get_enforces_max_bytes_during_chunk_download_against_lying_metadata() -> None:
    js = FakeJetStream()
    sha = sha256_unpadded(b"small")
    name = blob_object_name(TEAM, sha)
    # Metadata claims size=5, chunks=1, but stored chunk is 20 bytes.
    await js.publish(blob_chunk_subject("nuid_lying"), b"A" * 20)
    meta = {
        "name": name,
        "bucket": BUCKET_NAME,
        "nuid": "nuid_lying",
        "size": 5,
        "chunks": 1,
        "digest": f"SHA-256={sha}=",
        "headers": REQUEST_OWNER.to_headers(TEAM, sha),
        "options": {"max_chunk_size": 131_072},
    }
    await js.publish(blob_metadata_subject(name), json.dumps(meta).encode())

    with pytest.raises(BlobTooLarge):
        await get_blob(js, TEAM, sha, max_bytes=10)
    with pytest.raises(BlobCorrupted, match="exceeded declared size"):
        await get_blob(js, TEAM, sha, max_bytes=100)


@pytest.mark.asyncio
async def test_put_half_grace_abort_publishes_no_metadata_and_purges_nothing() -> None:
    js = FakeJetStream()
    times = iter([100.0, 100.0, 100.0 + 43_200.0])  # hits half-grace right before metadata publish
    data = b"a" * 200
    with pytest.raises(BlobWriteTimedOut):
        await put_blob(
            js,
            TEAM,
            data,
            REQUEST_OWNER,
            chunk_bytes=200,
            clock=lambda: next(times),
            half_grace_seconds=43_200.0,
            nuid_factory=lambda: "nuid_aborted",
        )
    # Chunk remains (writer never purges), metadata was never published.
    assert await stat_blob(js, TEAM, sha256_unpadded(data)) is None
    assert not any(c[0] == "purge_stream" for c in js.calls)
    assert any(m.subject == blob_chunk_subject("nuid_aborted") for m in js.messages)


@pytest.mark.asyncio
async def test_put_fa7_cas_conflict_reuses_winner_or_retries_with_same_nuid() -> None:
    # Case 1: Concurrent writer publishes valid live metadata before our metadata CAS -> we reuse winner.
    js = FakeJetStream()
    data = b"concurrent-content"
    sha = sha256_unpadded(data)
    name = blob_object_name(TEAM, sha)
    meta_subj = blob_metadata_subject(name)
    injected = False

    def race_live_winner(subject: str, payload: bytes, headers: dict[str, str]) -> None:
        nonlocal injected
        if subject == meta_subj and not injected:
            injected = True
            # Simulate winner having already published chunks + metadata at seq before our CAS.
            js.messages.append(_StoredMessage(900, blob_chunk_subject("nuid_winner"), data, js.now, {}))
            winner_meta = {
                "name": name,
                "bucket": BUCKET_NAME,
                "nuid": "nuid_winner",
                "size": len(data),
                "chunks": 1,
                "digest": f"SHA-256={sha}=",
                "headers": AGENT_OWNER.to_headers(TEAM, sha),
                "options": {"max_chunk_size": 131_072},
            }
            js.messages.append(_StoredMessage(901, meta_subj, json.dumps(winner_meta).encode(), js.now, {}))
            js._next_seq = 902

    js.on_publish = race_live_winner
    result = await put_blob(js, TEAM, data, REQUEST_OWNER, nuid_factory=lambda: "nuid_loser")
    assert result.reused is True
    assert result.nuid == "nuid_winner"

    # Case 2 (C07): Concurrent delete writes deleted:true before our metadata CAS -> retries with SAME uploaded nuid.
    js2 = FakeJetStream()
    injected2 = False
    nuid_calls = 0

    def next_nuid() -> str:
        nonlocal nuid_calls
        nuid_calls += 1
        return f"nuid_retry_{nuid_calls}"

    def race_deleted_marker(subject: str, payload: bytes, headers: dict[str, str]) -> None:
        nonlocal injected2
        if subject == meta_subj and not injected2:
            injected2 = True
            deleted_meta = {
                "name": name,
                "bucket": BUCKET_NAME,
                "nuid": "nuid_old_deleted",
                "size": 0,
                "chunks": 0,
                "digest": "",
                "deleted": True,
                "headers": AGENT_OWNER.to_headers(TEAM, sha),
                "options": {"max_chunk_size": 131_072},
            }
            js2.messages.append(_StoredMessage(500, meta_subj, json.dumps(deleted_meta).encode(), js2.now, {}))
            js2._next_seq = 501

    js2.on_publish = race_deleted_marker
    res2 = await put_blob(js2, TEAM, data, REQUEST_OWNER, nuid_factory=next_nuid)
    assert res2.reused is False
    assert res2.nuid == "nuid_retry_1"
    assert nuid_calls == 1  # Reused already-uploaded chunks on CAS retry!
    assert await get_blob(js2, TEAM, sha) == data


@pytest.mark.asyncio
async def test_put_fa7_overwrites_corrupt_or_incomplete_live_metadata() -> None:
    js = FakeJetStream()
    data = b"recoverable-bytes"
    sha = sha256_unpadded(data)
    name = blob_object_name(TEAM, sha)
    meta_subj = blob_metadata_subject(name)

    # Corrupt JSON sitting on metadata subject -> put_blob replaces it with CAS on its seq.
    await js.publish(meta_subj, b"{not json")
    res = await put_blob(js, TEAM, data, REQUEST_OWNER, nuid_factory=lambda: "nuid_fixed_1")
    assert res.reused is False
    assert await get_blob(js, TEAM, sha) == data

    # Live metadata whose chunks were purged (broken live object) -> put_blob replaces it via CAS.
    await purge_blob_chunks(js, "nuid_fixed_1")
    res2 = await put_blob(js, TEAM, data, AGENT_OWNER, nuid_factory=lambda: "nuid_fixed_2")
    assert res2.reused is False
    assert res2.nuid == "nuid_fixed_2"
    assert await get_blob(js, TEAM, sha) == data


@pytest.mark.asyncio
async def test_put_translates_10077_to_blob_store_full() -> None:
    js = FakeJetStream(max_bytes=50)
    with pytest.raises(BlobStoreFull):
        await put_blob(js, TEAM, b"x" * 100, REQUEST_OWNER, nuid_factory=lambda: "nuid_full")


@pytest.mark.asyncio
async def test_delete_blob_writes_marker_keeps_headers_purges_chunks_and_respects_cas() -> None:
    js = FakeJetStream()
    data = b"secret-to-delete"
    sha = sha256_unpadded(data)
    put = await put_blob(js, TEAM, data, REQUEST_OWNER, nuid_factory=lambda: "nuid_del_1")

    # Wrong expected_seq or expected_nuid raises BlobConcurrentWrite without mutating.
    with pytest.raises(BlobConcurrentWrite):
        await delete_blob(js, TEAM, sha, expected_seq=put.seq + 99)
    with pytest.raises(BlobConcurrentWrite):
        await delete_blob(js, TEAM, sha, expected_nuid="nuid_other")

    recorded_nuids: list[str] = []

    async def record_nuid(nuid: str) -> None:
        recorded_nuids.append(nuid)
        # Chunks are still present when on_marker_written fires (marker written before purge).
        assert any(m.subject == blob_chunk_subject(nuid) for m in js.messages)

    deleted = await delete_blob(
        js, TEAM, sha, expected_seq=put.seq, expected_nuid=put.nuid, on_marker_written=record_nuid
    )
    assert deleted.deleted is True
    assert deleted.already_deleted is False
    assert deleted.nuid == "nuid_del_1"
    assert recorded_nuids == ["nuid_del_1"]

    # Chunks are purged and only 1 metadata message (deleted: true keeping owner headers) remains.
    assert not any(m.subject == blob_chunk_subject("nuid_del_1") for m in js.messages)
    meta_msgs = [m for m in js.messages if m.subject == blob_metadata_subject(blob_object_name(TEAM, sha))]
    assert len(meta_msgs) == 1
    marker = parse_blob_metadata(meta_msgs[0].data, expected_team=TEAM, expected_sha256=sha, seq=meta_msgs[0].seq)
    assert marker.deleted is True
    assert marker.size == 0
    assert marker.chunks == 0
    assert marker.digest == ""
    assert marker.nuid == "nuid_del_1"
    assert marker.headers == REQUEST_OWNER.to_headers(TEAM, sha)

    # Retrying delete on an already-deleted blob is idempotent and purges any leftover chunks.
    await js.publish(blob_chunk_subject("nuid_del_1"), b"leftover")
    retry = await delete_blob(js, TEAM, sha)
    assert retry.deleted is True
    assert retry.already_deleted is True
    assert not any(m.subject == blob_chunk_subject("nuid_del_1") for m in js.messages)

    # Deleting a non-existent blob returns deleted=False.
    missing = await delete_blob(js, TEAM, sha256_unpadded(b"never-existed"))
    assert missing.deleted is False


@pytest.mark.asyncio
async def test_delete_blob_reclaims_space_when_store_is_at_max_bytes(caplog: pytest.LogCaptureFixture) -> None:
    """C09: delete_blob succeeds and frees bytes when OBJ_UA1_BLOBS is full (10077)."""

    js = FakeJetStream()
    data = b"p" * 2000
    sha = sha256_unpadded(data)
    await put_blob(js, TEAM, data, REQUEST_OWNER, nuid_factory=lambda: "nuid_full_del")

    # Fill the store budget so a ~350-byte deleted:true marker cannot fit before reclaiming chunks.
    bytes_before = js._current_bytes()
    js.max_bytes = bytes_before + 50
    recorded: list[str] = []

    async def on_marker(nuid: str) -> None:
        recorded.append(nuid)

    with caplog.at_level("WARNING"):
        result = await delete_blob(js, TEAM, sha, on_marker_written=on_marker)

    assert result.deleted is True
    assert result.already_deleted is False
    assert result.nuid == "nuid_full_del"
    assert recorded == ["nuid_full_del"]
    assert "event=blob_delete_store_full_reclaim" in caplog.text
    assert not any(m.subject == blob_chunk_subject("nuid_full_del") for m in js.messages)
    meta_msgs = [m for m in js.messages if m.subject == blob_metadata_subject(blob_object_name(TEAM, sha))]
    assert len(meta_msgs) == 1
    marker = parse_blob_metadata(meta_msgs[0].data, expected_team=TEAM, expected_sha256=sha, seq=meta_msgs[0].seq)
    assert marker.deleted is True
    assert marker.headers == REQUEST_OWNER.to_headers(TEAM, sha)
    assert js._current_bytes() < bytes_before - 1500
    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, sha)

    # Space is freed so a fresh blob fits again.
    fresh = await put_blob(js, TEAM, b"q" * 1000, AGENT_OWNER, nuid_factory=lambda: "nuid_after_free")
    assert await get_blob(js, TEAM, fresh.sha256) == b"q" * 1000

    # A 0-byte blob in a stream full of other live blobs cannot free enough room -> re-raises BlobStoreFull.
    js_zero = FakeJetStream()
    await put_blob(js_zero, TEAM, b"z" * 1000, AGENT_OWNER, nuid_factory=lambda: "nuid_other_live")
    empty_put = await put_blob(js_zero, TEAM, b"", REQUEST_OWNER, nuid_factory=lambda: "nuid_empty_full")
    js_zero.max_bytes = js_zero._current_bytes() + 10
    with pytest.raises(BlobStoreFull):
        await delete_blob(js_zero, TEAM, empty_put.sha256)

    # Concurrent metadata write after chunk reclaim still triggers CAS failure (BlobConcurrentWrite).
    js_race = FakeJetStream()
    race_put = await put_blob(js_race, TEAM, b"r" * 2000, REQUEST_OWNER, nuid_factory=lambda: "nuid_race_del")
    js_race.max_bytes = js_race._current_bytes() + 50
    meta_subj = blob_metadata_subject(blob_object_name(TEAM, race_put.sha256))
    attempts = 0

    def inject_after_reclaim(subject: str, payload: bytes, headers: dict[str, str]) -> None:
        nonlocal attempts
        if subject == meta_subj:
            attempts += 1
            if attempts == 2:
                # Simulate a concurrent writer publishing newer metadata right before our retry CAS.
                new_meta = {
                    "name": blob_object_name(TEAM, race_put.sha256),
                    "bucket": BUCKET_NAME,
                    "nuid": "nuid_race_winner",
                    "size": 2000,
                    "chunks": 1,
                    "digest": f"SHA-256={race_put.sha256}=",
                    "headers": AGENT_OWNER.to_headers(TEAM, race_put.sha256),
                    "options": {"max_chunk_size": 131_072},
                }
                js_race.messages.append(_StoredMessage(999, meta_subj, json.dumps(new_meta).encode(), js_race.now, {}))
                js_race._next_seq = 1000

    js_race.on_publish = inject_after_reclaim
    with pytest.raises(BlobConcurrentWrite):
        await delete_blob(js_race, TEAM, race_put.sha256)
    latest = await stat_blob(js_race, TEAM, race_put.sha256)
    assert latest is not None and latest.nuid == "nuid_race_winner" and latest.deleted is False


@pytest.mark.asyncio
async def test_sweep_orphan_chunks_fa6_deleted_marker_and_grace_boundary() -> None:
    t0 = datetime(2026, 11, 20, 0, 0, 0, tzinfo=timezone.utc)
    js = FakeJetStream(now=t0, page_size=2)  # force multi-page STREAM.INFO pagination

    # 1. Live blob written at t0 -> MUST NOT be swept even when old.
    live_data = b"live-blob"
    live_put = await put_blob(js, TEAM, live_data, REQUEST_OWNER, nuid_factory=lambda: "nuid_live")

    # 2. Interrupted delete (FA-6): deleted:true marker written at t0, chunks left behind -> MUST be swept when old.
    del_data = b"interrupted-delete-blob"
    del_sha = sha256_unpadded(del_data)
    del_put = await put_blob(js, TEAM, del_data, REQUEST_OWNER, nuid_factory=lambda: "nuid_deleted_marker")
    del_meta_subj = blob_metadata_subject(blob_object_name(TEAM, del_sha))
    await js.publish(
        del_meta_subj,
        json.dumps(del_put.metadata.as_deleted_payload()).encode(),
        headers={Header.EXPECTED_LAST_SUBJECT_SEQUENCE: str(del_put.seq)},
    )

    # 3. Unreferenced orphan chunk written at t0 -> MUST be swept when old.
    await js.publish(blob_chunk_subject("nuid_orphan_old"), b"abandoned")

    # 4. Recent orphan chunk written within grace+skew -> MUST NOT be swept.
    sweep_time = t0 + timedelta(seconds=86_400 + 8 + 1)
    js.now = sweep_time - timedelta(seconds=86_400 + 8)  # exactly at cutoff (not strictly older)
    await js.publish(blob_chunk_subject("nuid_orphan_recent"), b"in-flight")

    result = await sweep_orphan_chunks(js, now=sweep_time)
    assert result.checked_chunk_subjects == 4
    assert result.old_chunk_subjects == 3
    assert set(result.purged_chunk_subjects) == {
        blob_chunk_subject("nuid_deleted_marker"),
        blob_chunk_subject("nuid_orphan_old"),
    }
    assert result.kept_live_nuids == 1
    assert len(result.metadata_entries) == 2

    # Live blob still readable; recent orphan still present; deleted-marker & old orphan chunks purged.
    assert await get_blob(js, TEAM, live_put.sha256) == live_data
    assert any(m.subject == blob_chunk_subject("nuid_orphan_recent") for m in js.messages)
    assert not any(m.subject == blob_chunk_subject("nuid_deleted_marker") for m in js.messages)
    assert not any(m.subject == blob_chunk_subject("nuid_orphan_old") for m in js.messages)


@pytest.mark.asyncio
async def test_list_stream_subjects_handles_page_shift_and_missing_stream() -> None:
    js = FakeJetStream(page_size=2)
    for nuid in ("n1", "n2", "n3", "n4"):
        await js.publish(blob_chunk_subject(nuid), b"c")

    # Multi-page listing joins cleanly.
    subjects = await list_stream_subjects(js, f"{CHUNK_SUBJECT_PREFIX}>")
    assert set(subjects.keys()) == {blob_chunk_subject(n) for n in ("n1", "n2", "n3", "n4")}

    # Force every second page to omit the join key -> raises BlobSubjectListShifted after PAGE_ATTEMPTS.
    def shift_pages(req_subject: str, req: dict[str, Any]) -> Optional[dict[str, Any]]:
        if req.get("offset", 0) == 0:
            return {"state": {"subjects": {blob_chunk_subject("n1"): 1, blob_chunk_subject("n2"): 1}}, "total": 4}
        return {"state": {"subjects": {blob_chunk_subject("n3"): 1, blob_chunk_subject("n4"): 1}}, "total": 4}

    js.on_api_request = shift_pages
    with pytest.raises(BlobSubjectListShifted):
        await list_stream_subjects(js, f"{CHUNK_SUBJECT_PREFIX}>")

    # Missing stream surfaces as BlobError rather than silent empty/not-found.
    missing_js = FakeJetStream(stream_exists=False)
    with pytest.raises(BlobError, match="does not exist"):
        await stat_blob(missing_js, TEAM, sha256_unpadded(b"x"))
    with pytest.raises(BlobError, match="does not exist"):
        await list_stream_subjects(missing_js, f"{CHUNK_SUBJECT_PREFIX}>")


def test_parse_blob_metadata_rejects_malformed_fields_and_ignores_extra_nats_fields() -> None:
    data = b"sample"
    sha = sha256_unpadded(data)
    name = blob_object_name(TEAM, sha)
    valid = {
        "name": name,
        "bucket": BUCKET_NAME,
        "nuid": "nuid_ok",
        "size": len(data),
        "chunks": 1,
        "digest": f"SHA-256={sha}=",
        "headers": REQUEST_OWNER.to_headers(TEAM, sha),
        "options": {"max_chunk_size": 131_072},
        "mtime": "2026-11-24T12:00:00Z",
        "description": "ignored by reader",
    }
    parsed = parse_blob_metadata(json.dumps(valid).encode(), expected_team=TEAM, expected_sha256=sha, seq=7)
    assert parsed.seq == 7
    assert parsed.team == TEAM
    assert parsed.sha256 == sha
    assert parsed.person_ref == "person_7c1"
    assert parsed.owner_request_id == "req_knowledge_1"

    # Bad bucket, bad name, bad nuid, bool size, missing digest, agent+person header all refused.
    bad_cases = [
        {**valid, "bucket": "OTHER"},
        {**valid, "name": f"other_team~{sha}"},
        {**valid, "nuid": "bad.nuid"},
        {**valid, "size": True},
        {**valid, "chunks": 0},
        {**valid, "digest": "SHA-256=wrong="},
        {**valid, "deleted": True, "size": 1},
        {**valid, "headers": {HEADER_SHA256: [sha], HEADER_TEAM: [TEAM], HEADER_AGENT: ["a1"], HEADER_PERSON: ["p1"]}},
    ]
    for bad in bad_cases:
        with pytest.raises(BlobCorrupted):
            parse_blob_metadata(json.dumps(bad).encode(), expected_team=TEAM, expected_sha256=sha)
