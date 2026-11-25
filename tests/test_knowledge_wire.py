"""Deterministic tests for Knowledge wire encoding, UA1_BLOBS codec, ClientBlobStore, and GatewayKnowledgePages (A5)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from access_control.people_directory import PeopleDirectory
from assistant_gateway.application.knowledge_pages import (
    BLOB_CHANGE_REPLY_READ_FAILED_TEXT,
    BLOB_READ_FAILED_TEXT,
    BLOB_STORE_FULL_TEXT,
    BLOB_UNAVAILABLE_TEXT,
    BLOB_WRITE_FAILED_TEXT,
    GatewayKnowledgePages,
    GatewayKnowledgeRefused,
)
from assistant_gateway.application.knowledge_wire import (
    KnowledgeRequestNotSent,
    knowledge_request,
    knowledge_view,
)
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.team_commands import (
    GatewayTeamCommandService,
    TeamCommandRefused,
    TeamServicesNoAnswer,
    TeamServicesUnavailable,
)
from assistant_gateway.message_host import GatewayHostUnavailable
from assistant_gateway.team_services_client import gateway_blob_store
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.control_v1 import KnowledgeCommandAction, KnowledgeCommandPayload
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.sizes import INLINE_TEXT_MAX_CHARS, REPLY_BODY_MAX_BYTES, encoded_size
from custom_transport.protocol1.live import NO_ANSWER_IN_TIME, RequestRefused, ServiceUnavailable
from custom_transport.protocol1.blobs import (
    BlobCorrupted,
    BlobOwner,
    BlobStoreFull,
    BlobTooLarge,
    sha256_unpadded,
)
from custom_transport.protocol1.knowledge_blobs import (
    APPLICATION_JSON_MEDIA_TYPE,
    MAX_BODY_CHARS_CEILING,
    MAX_NOTE_CHARS_CEILING,
    TEXT_PLAIN_MEDIA_TYPE,
    ClientBlobStore,
    KnowledgeBlobUnavailable,
    decode_request_body,
    decode_text_content,
    fit_reply_body,
    fit_request_body,
    resolve_reply_body,
)
from storage.access_repository import AccessRepository
from storage.blob_index_repository import BlobIndexRepository
from storage.config import resolve_storage_settings
from team_services.blobs import BlobOperations, BlobService
from tests.team_services_support import checked_reply, stand_in_frame
from tests.test_protocol1_blobs import FakeJetStream

TEAM = "team_example"
ALICE_REF = "p-AliceRef00000000000001"
OWNER = BlobOwner(request_id="req_knowledge_wire_1", person_ref=ALICE_REF)


class _MemBlobStore:
    def __init__(self, *, fail_put: Optional[Exception] = None, fail_get: Optional[Exception] = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls: int = 0
        self.get_calls: int = 0
        self.fail_put = fail_put
        self.fail_get = fail_get

    async def put(
        self,
        data: bytes,
        *,
        media_type: str,
        owner: BlobOwner,
        name: Optional[str] = None,
    ) -> dict[str, Any]:
        if self.fail_put is not None:
            raise self.fail_put
        self.put_calls += 1
        payload = bytes(data)
        digest = sha256_unpadded(payload)
        self.objects[digest] = payload
        ref: dict[str, Any] = {"sha256": digest, "size": len(payload), "media_type": media_type}
        if name is not None:
            ref["name"] = name
        return ref

    async def get(self, ref: dict[str, Any], *, max_bytes: Optional[int] = None) -> bytes:
        if self.fail_get is not None:
            raise self.fail_get
        self.get_calls += 1
        raw = self.objects[str(ref["sha256"])]
        if max_bytes is not None and len(raw) > max_bytes:
            raise BlobTooLarge(f"{len(raw)} > {max_bytes}")
        return raw


def _inline_page(
    *,
    body: str = "Short body.",
    notes: Optional[list[dict[str, Any]]] = None,
    backlinks: Optional[list[dict[str, Any]]] = None,
    summary: str = "",
) -> dict[str, Any]:
    return {
        "type": "inline",
        "space": "team",
        "page": "guide",
        "title": "Guide",
        "page_type": "how-to",
        "summary": summary,
        "tags": ["ops"],
        "status": "active",
        "seen_version": "3",
        "updated_at": "2026-11-24T12:00:00.000Z",
        "content": {"type": "inline", "text": body},
        "created_at": "2026-11-20T12:00:00.000Z",
        "updated_by": {"kind": "person", "person": ALICE_REF},
        "backlinks": backlinks or [],
        "open_notes": notes or [],
        "pending_contributions": [],
        "links": [],
    }


def test_knowledge_wire_allows_long_text_and_still_refuses_empty_note() -> None:
    long_text = "a" * (INLINE_TEXT_MAX_CHARS + 5_000)
    kind, body = knowledge_request(
        KnowledgeCommandPayload(action=KnowledgeCommandAction.WRITE, title="Big", body=long_text)
    )
    assert kind == "knowledge.write"
    assert body["content"] == {"type": "inline", "text": long_text}

    with pytest.raises(KnowledgeRequestNotSent):
        knowledge_request(KnowledgeCommandPayload(action=KnowledgeCommandAction.ADD_NOTE, page="big", note="   "))


@pytest.mark.asyncio
async def test_fit_and_decode_request_body_spills_over_inline_limit_and_deduplicates_publish() -> None:
    blobs = _MemBlobStore()

    # Exact inline limit stays inline without touching blob store.
    short_body = {"content": {"type": "inline", "text": "x" * INLINE_TEXT_MAX_CHARS}}
    fitted_short = await fit_request_body(short_body, blobs, OWNER)
    assert fitted_short["content"]["type"] == "inline"
    assert blobs.put_calls == 0

    # Edited publish_contribution carries the same >32,768 text in both content and note;
    # fit_request_body caches identical bytes so only one put runs, and decode_request_body fetches once.
    long_text = ("Ünïcode — ✓ 🚀 line\n" * 4_000)[:60_000]
    pub_body = {
        "contribution_id": "contrib_1",
        "edited": True,
        "content": {"type": "inline", "text": long_text},
        "note": {"type": "inline", "text": long_text},
    }
    fitted_pub = await fit_request_body(pub_body, blobs, OWNER)
    assert blobs.put_calls == 1
    assert fitted_pub["content"]["type"] == "blob"
    assert fitted_pub["note"] == fitted_pub["content"]

    decoded_pub = await decode_request_body("publish_contribution", fitted_pub, blobs)
    assert blobs.get_calls == 1
    assert decoded_pub["content"] == {"type": "inline", "text": long_text}
    assert decoded_pub["note"] == {"type": "inline", "text": long_text}

    # Combined inline text under 32,768 chars each but exceeding tight max_bytes spills largest field.
    tight = {
        "content": {"type": "inline", "text": "界" * 10_000},
        "note": {"type": "inline", "text": "short note"},
    }
    fitted_tight = await fit_request_body(tight, blobs, OWNER, max_bytes=10_000)
    assert fitted_tight["content"]["type"] == "blob"
    assert fitted_tight["note"]["type"] == "inline"


@pytest.mark.asyncio
async def test_fit_and_resolve_reply_body_spills_text_and_oversized_page_json_with_schema_check() -> None:
    blobs = _MemBlobStore()

    # 1. Text > 32,768 chars in page, open_notes, and note spills to text/plain blobs.
    body_500k = ("Page body line — ✓.\n" * 30_000)[:MAX_BODY_CHARS_CEILING]
    note_50k = ("Note line — ✓.\n" * 4_000)[:MAX_NOTE_CHARS_CEILING]
    note_obj = {
        "note_id": "note_1",
        "content": {"type": "inline", "text": note_50k},
        "created_at": "2026-11-24T12:00:00.000Z",
        "created_by": {"kind": "person", "person": ALICE_REF},
    }
    reply = {"outcome": "ok", "page": _inline_page(body=body_500k, notes=[note_obj]), "note": dict(note_obj)}

    fitted = await fit_reply_body(reply, blobs, OWNER)
    assert encoded_size(fitted) <= REPLY_BODY_MAX_BYTES
    assert fitted["page"]["type"] == "inline"
    assert fitted["page"]["content"]["type"] == "blob"
    assert fitted["page"]["open_notes"][0]["content"]["type"] == "blob"
    assert fitted["note"]["content"] == fitted["page"]["open_notes"][0]["content"]
    # Dedup within one reply: body_500k + note_50k = only 2 puts even though note appears twice.
    assert blobs.put_calls == 2

    resolved = await resolve_reply_body(fitted, blobs)
    assert resolved["page"]["content"]["text"] == body_500k
    assert resolved["page"]["open_notes"][0]["content"]["text"] == note_50k
    assert resolved["note"]["content"]["text"] == note_50k

    view = knowledge_view(KnowledgeCommandAction.ADD_NOTE, resolved, label=lambda _e: "You")
    assert view.page is not None and view.page.body == body_500k
    assert view.page.open_notes[0].body == note_50k

    # 2. Oversized page metadata (many wide backlinks + tags) spills the complete page to PageBlob (application/json).
    wide_backlinks = [
        {
            "space": "team",
            "page": f"backlink-{idx}",
            "title": "界" * 990,
            "page_type": "reference",
            "summary": "界" * 3990,
            "tags": [f"tag-{idx}-{t}-" + "x" * 60 for t in range(100)],
            "status": "active",
            "seen_version": "1",
            "updated_at": "2026-11-24T12:00:00.000Z",
        }
        for idx in range(15)
    ]
    huge_meta_reply = {"outcome": "ok", "page": _inline_page(body=body_500k, backlinks=wide_backlinks)}
    fitted_meta = await fit_reply_body(huge_meta_reply, blobs, OWNER)
    assert encoded_size(fitted_meta) <= REPLY_BODY_MAX_BYTES
    assert fitted_meta["page"]["type"] == "blob"
    assert fitted_meta["page"]["ref"]["media_type"] == APPLICATION_JSON_MEDIA_TYPE

    resolved_meta = await resolve_reply_body(fitted_meta, blobs)
    assert resolved_meta["page"]["type"] == "inline"
    assert resolved_meta["page"]["content"]["text"] == body_500k
    assert len(resolved_meta["page"]["backlinks"]) == 15

    # 3. Oversized history page spills page to PageBlob so revisions still paginate cleanly.
    revisions = [
        {
            "revision": rev,
            "updated_at": "2026-11-24T12:00:00.000Z",
            "updated_by": {"kind": "person", "person": ALICE_REF},
            "title": f"Rev {rev}",
            "status": "active",
            "change_kind": "edit",
        }
        for rev in range(5, 0, -1)
    ]
    history_reply = {
        "outcome": "ok",
        "page": _inline_page(body=body_500k, backlinks=wide_backlinks),
        "revisions": revisions,
        "total": 5,
    }
    fitted_hist = await fit_reply_body(history_reply, blobs, OWNER, list_key="revisions", offset=0)
    assert encoded_size(fitted_hist) <= REPLY_BODY_MAX_BYTES
    assert fitted_hist["page"]["type"] == "blob"
    assert len(fitted_hist["revisions"]) == 5


@pytest.mark.asyncio
async def test_decode_rejects_bad_media_type_invalid_utf8_oversized_and_invalid_page_schema() -> None:
    blobs = _MemBlobStore()

    # Wrong media type for text_content.
    ref_json = await blobs.put(b"hello", media_type=APPLICATION_JSON_MEDIA_TYPE, owner=OWNER)
    with pytest.raises(BlobCorrupted):
        await decode_text_content({"type": "blob", "ref": ref_json}, blobs, max_chars=100)

    # Invalid UTF-8 bytes.
    ref_bad_utf8 = await blobs.put(b"\xff\xfe\xfd", media_type=TEXT_PLAIN_MEDIA_TYPE, owner=OWNER)
    with pytest.raises(BlobCorrupted):
        await decode_text_content({"type": "blob", "ref": ref_bad_utf8}, blobs, max_chars=100)

    # Size > max_chars * 4 refused BEFORE calling blobs.get.
    calls_before = blobs.get_calls
    with pytest.raises(BlobTooLarge):
        await decode_text_content(
            {"type": "blob", "ref": {"sha256": ref_bad_utf8["sha256"], "size": 500, "media_type": TEXT_PLAIN_MEDIA_TYPE}},
            blobs,
            max_chars=100,
        )
    assert blobs.get_calls == calls_before

    # Byte size fits max_chars * 4, but character count exceeds max_chars after UTF-8 decode.
    ref_ascii_over = await blobs.put(b"a" * 101, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=OWNER)
    with pytest.raises(BlobTooLarge):
        await decode_text_content({"type": "blob", "ref": ref_ascii_over}, blobs, max_chars=100)

    # PageBlob with wrong media_type or invalid PageInline schema is refused.
    bad_page_ref = await blobs.put(json.dumps({"type": "inline", "extra": "nope"}).encode("utf-8"),
                                   media_type=APPLICATION_JSON_MEDIA_TYPE, owner=OWNER)
    with pytest.raises(BlobCorrupted):
        await resolve_reply_body({"outcome": "ok", "page": {"type": "blob", "ref": bad_page_ref}}, blobs)

    wrong_mt_page_ref = await blobs.put(b"{}", media_type=TEXT_PLAIN_MEDIA_TYPE, owner=OWNER)
    with pytest.raises(BlobCorrupted):
        await resolve_reply_body({"outcome": "ok", "page": {"type": "blob", "ref": wrong_mt_page_ref}}, blobs)

    # Missing blob store raises KnowledgeBlobUnavailable when a blob is needed.
    with pytest.raises(KnowledgeBlobUnavailable):
        await fit_request_body({"content": {"type": "inline", "text": "x" * (INLINE_TEXT_MAX_CHARS + 1)}}, None, OWNER)


@pytest.mark.asyncio
async def test_client_blob_store_register_first_put_dedup_and_departed_refusal(tmp_path: Path) -> None:
    js = FakeJetStream()
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "blobs.db")}})
    access = AccessRepository(storage)
    now = datetime.now(timezone.utc)
    access.record_person(TeamPersonV1(
        team_id=TEAM, person_id="alice", person_ref=ALICE_REF, standing=PersonStanding.ACTIVE,
        roles=("member",), changed_at_utc=now,
    ))
    people = PeopleDirectory(access, TEAM)
    service = BlobService(repository=BlobIndexRepository(storage), people=people, js=js, team_id=TEAM)
    ops = BlobOperations(service, people, logging.getLogger("test.client_blobs"))

    register_calls: list[dict[str, Any]] = []

    async def _register(body: dict[str, Any]) -> dict[str, Any]:
        register_calls.append(dict(body))
        frame = stand_in_frame(TEAM, "blobs.register", body, person_ref=None, request_id=body["request_id"])
        return checked_reply("blobs.register", await ops.handle_register(frame))

    counter = {"n": 0}

    def _next_id() -> str:
        counter["n"] += 1
        return f"reg_{counter['n']}"

    client = ClientBlobStore(get_js=lambda: _async_val(js), team=TEAM, register=_register, request_ids=_next_id)
    payload = b"hello protocol 1 blob world"

    # First put: register returns present=False -> put_blob -> register returns present=True.
    ref1 = await client.put(payload, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=OWNER)
    assert len(register_calls) == 2
    assert ref1["sha256"] == sha256_unpadded(payload) and ref1["size"] == len(payload)
    assert await client.get(ref1) == payload

    # Second put of identical bytes: dedup hit in 1 register call without re-uploading.
    ref2 = await client.put(
        payload, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=BlobOwner(request_id="req_second", person_ref=ALICE_REF)
    )
    assert len(register_calls) == 3
    assert ref2 == ref1

    # Departed person is refused before uploading.
    access.record_person(TeamPersonV1(
        team_id=TEAM, person_id="alice", person_ref=ALICE_REF, standing=PersonStanding.DISABLED,
        roles=(), changed_at_utc=now,
    ))
    with pytest.raises(PermissionError):
        await client.put(b"blocked bytes", media_type=TEXT_PLAIN_MEDIA_TYPE, owner=OWNER)


async def _async_val(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_gateway_knowledge_pages_maps_blob_errors_to_plain_refusals() -> None:
    class _Refs:
        def person_ref(self, _pid: str) -> str:
            return ALICE_REF

        def person_id(self, _ref: str) -> str:
            return "alice"

    class _StubTeam:
        def __init__(self, reply: dict[str, Any]) -> None:
            self.reply = reply

        async def command(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return self.reply

    ctx = GatewayRouteContextV1(session_key="website:alice", canonical_user_id="alice")
    long_write = KnowledgeCommandPayload(
        action=KnowledgeCommandAction.WRITE, title="Big", body="x" * (INLINE_TEXT_MAX_CHARS + 10)
    )

    # BlobStoreFull on write.
    pages_full = GatewayKnowledgePages(
        team_commands=GatewayTeamCommandService(team_services=_StubTeam({}), references=_Refs(), request_ids=lambda: "c1"),
        references=_Refs(), display_name=lambda pid: pid, enabled=True,
        blobs=_MemBlobStore(fail_put=BlobStoreFull("full")),
    )
    with pytest.raises(GatewayKnowledgeRefused) as exc_full:
        await pages_full.run(ctx, long_write)
    assert exc_full.value.code == "runtime_error" and exc_full.value.message == BLOB_STORE_FULL_TEXT

    # No blob store configured on write.
    pages_none = GatewayKnowledgePages(
        team_commands=GatewayTeamCommandService(team_services=_StubTeam({}), references=_Refs(), request_ids=lambda: "c2"),
        references=_Refs(), display_name=lambda pid: pid, enabled=True, blobs=None,
    )
    with pytest.raises(GatewayKnowledgeRefused) as exc_none:
        await pages_none.run(ctx, long_write)
    assert exc_none.value.code == "runtime_error" and exc_none.value.message == BLOB_UNAVAILABLE_TEXT

    # Corrupted blob on read reply.
    bad_reply = {
        "outcome": "ok",
        "page": {
            "type": "blob",
            "ref": {"sha256": sha256_unpadded(b"x"), "size": 1, "media_type": APPLICATION_JSON_MEDIA_TYPE},
        },
    }
    pages_bad = GatewayKnowledgePages(
        team_commands=GatewayTeamCommandService(
            team_services=_StubTeam(bad_reply), references=_Refs(), request_ids=lambda: "c3"
        ),
        references=_Refs(), display_name=lambda pid: pid, enabled=True,
        blobs=_MemBlobStore(fail_get=BlobCorrupted("tampered digest")),
    )
    with pytest.raises(GatewayKnowledgeRefused) as exc_bad:
        await pages_bad.run(ctx, KnowledgeCommandPayload(action=KnowledgeCommandAction.READ, page="guide"))
    assert exc_bad.value.code == "runtime_error" and exc_bad.value.message == BLOB_READ_FAILED_TEXT


@pytest.mark.asyncio
async def test_gateway_blob_store_translates_host_errors_and_change_reply_uses_refresh_text() -> None:
    import nats.errors

    class _FailingHost:
        team = TEAM

        def __init__(self, handle_err: Optional[Exception] = None, req_err: Optional[Exception] = None) -> None:
            self.handle_err = handle_err
            self.req_err = req_err

        async def blob_store_handle(self, *, timeout: float) -> Any:
            if self.handle_err is not None:
                raise self.handle_err
            return FakeJetStream()

        async def request_team(self, kind: str, body: dict[str, Any], *, team: str, timeout: float) -> Any:
            if self.req_err is not None:
                raise self.req_err
            raise AssertionError("unexpected call")

    # gateway_blob_store translates GatewayHostUnavailable, ServiceUnavailable, RequestRefused, and FrameRefused.
    store_joining = gateway_blob_store(_FailingHost(handle_err=GatewayHostUnavailable("joining")), timeout_seconds=5.0)
    assert store_joining is not None
    with pytest.raises(TeamServicesUnavailable, match="still connecting"):
        await store_joining.get({"sha256": sha256_unpadded(b"a"), "size": 1})

    store_down = gateway_blob_store(
        _FailingHost(req_err=ServiceUnavailable("blobs.register", "ua.v1.team_example.gw.svc.team", "no_responders")),
        timeout_seconds=5.0,
    )
    assert store_down is not None
    with pytest.raises(TeamServicesUnavailable, match="not running"):
        await store_down.put(b"hello", media_type=TEXT_PLAIN_MEDIA_TYPE, owner=OWNER)

    store_timeout = gateway_blob_store(
        _FailingHost(req_err=ServiceUnavailable("blobs.register", "ua.v1.team_example.gw.svc.team", NO_ANSWER_IN_TIME)),
        timeout_seconds=5.0,
    )
    assert store_timeout is not None
    with pytest.raises(TeamServicesNoAnswer):
        await store_timeout.put(b"hello", media_type=TEXT_PLAIN_MEDIA_TYPE, owner=OWNER)

    store_refused = gateway_blob_store(
        _FailingHost(req_err=RequestRefused("blobs.register", "handler_failed", None)), timeout_seconds=5.0
    )
    assert store_refused is not None
    with pytest.raises(TeamCommandRefused):
        await store_refused.put(b"hello", media_type=TEXT_PLAIN_MEDIA_TYPE, owner=OWNER)

    store_frame = gateway_blob_store(
        _FailingHost(req_err=FrameRefused("bad_signature")), timeout_seconds=5.0
    )
    assert store_frame is not None
    with pytest.raises(TeamServicesUnavailable, match="could not be checked"):
        await store_frame.put(b"hello", media_type=TEXT_PLAIN_MEDIA_TYPE, owner=OWNER)

    # GatewayKnowledgePages catches transport/NATS errors cleanly on both encode and decode,
    # and uses a post-save refresh message when reply blob decoding fails after a change action.
    class _Refs:
        def person_ref(self, _pid: str) -> str:
            return ALICE_REF

        def person_id(self, _ref: str) -> str:
            return "alice"

    class _StubTeam:
        def __init__(self, reply: dict[str, Any]) -> None:
            self.reply = reply

        async def command(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return self.reply

    ctx = GatewayRouteContextV1(session_key="website:alice", canonical_user_id="alice")
    long_write = KnowledgeCommandPayload(
        action=KnowledgeCommandAction.WRITE, title="Big", body="x" * (INLINE_TEXT_MAX_CHARS + 10)
    )
    for err, expected_text in (
        (nats.errors.TimeoutError(), BLOB_WRITE_FAILED_TEXT),
        (TeamServicesNoAnswer("timeout"), BLOB_WRITE_FAILED_TEXT),
        (TeamCommandRefused("blobs.register", "handler_failed"), BLOB_WRITE_FAILED_TEXT),
        (TeamServicesUnavailable("The team services are not running right now."), "The team services are not running right now."),
    ):
        pages = GatewayKnowledgePages(
            team_commands=GatewayTeamCommandService(team_services=_StubTeam({}), references=_Refs(), request_ids=lambda: "c"),
            references=_Refs(), display_name=lambda pid: pid, enabled=True,
            blobs=_MemBlobStore(fail_put=err),
        )
        with pytest.raises(GatewayKnowledgeRefused) as exc_info:
            await pages.run(ctx, long_write)
        assert exc_info.value.code == "runtime_error"
        assert exc_info.value.message == expected_text

    blob_reply = {
        "outcome": "ok",
        "page": {
            **_inline_page(body="short"),
            "content": {
                "type": "blob",
                "ref": {"sha256": sha256_unpadded(b"x"), "size": 1, "media_type": TEXT_PLAIN_MEDIA_TYPE},
            },
        },
    }
    pages_change_fail = GatewayKnowledgePages(
        team_commands=GatewayTeamCommandService(
            team_services=_StubTeam(blob_reply), references=_Refs(), request_ids=lambda: "c4"
        ),
        references=_Refs(), display_name=lambda pid: pid, enabled=True,
        blobs=_MemBlobStore(fail_get=nats.errors.TimeoutError()),
    )
    with pytest.raises(GatewayKnowledgeRefused) as exc_change:
        await pages_change_fail.run(
            ctx, KnowledgeCommandPayload(action=KnowledgeCommandAction.WRITE, title="Short", body="short")
        )
    assert exc_change.value.code == "runtime_error"
    assert exc_change.value.message == BLOB_CHANGE_REPLY_READ_FAILED_TEXT
    assert "Try again" not in exc_change.value.message


@pytest.mark.asyncio
async def test_fit_reply_body_paginates_list_and_history_without_spilling_sublimit_inline_text() -> None:
    blobs = _MemBlobStore()
    wide_rows = [
        {
            "space": "team",
            "page": f"page-{idx:02d}",
            "title": f"Wide Page {idx}",
            "page_type": "reference",
            "summary": "s" * 2000,
            "tags": [f"tag-{t}" for t in range(30)],
            "status": "active",
            "seen_version": "1",
            "updated_at": "2026-11-24T12:00:00.000Z",
            "snippet": {"type": "inline", "text": f"Match snippet {idx}: " + ("w" * 500)},
        }
        for idx in range(20)
    ]
    list_reply = {"outcome": "ok", "pages": wide_rows, "total": 20}
    # Paginates without touching UA1_BLOBS at all (even when blobs=None).
    fitted_list = await fit_reply_body(list_reply, None, OWNER, max_bytes=12_000, list_key="pages", offset=0)
    assert 0 < len(fitted_list["pages"]) < 20
    assert fitted_list["next_offset"] == len(fitted_list["pages"])
    assert all(row["snippet"]["type"] == "inline" for row in fitted_list["pages"])

    # If the very first row alone exceeds max_bytes because of a sub-limit snippet, only row 0's snippet spills.
    huge_first_row = [
        {**wide_rows[0], "snippet": {"type": "inline", "text": "x" * 10_000}},
        wide_rows[1],
    ]
    fitted_first = await fit_reply_body(
        {"outcome": "ok", "pages": huge_first_row, "total": 2}, blobs, OWNER, max_bytes=6_000, list_key="pages", offset=0
    )
    assert len(fitted_first["pages"]) == 2
    assert fitted_first["pages"][0]["snippet"]["type"] == "blob"
    assert fitted_first["pages"][1]["snippet"]["type"] == "inline"
    assert blobs.put_calls == 1

    # History with many revisions and a sub-limit inline page that fits revs[:1] paginates with 0 blob puts.
    revs = [
        {
            "revision": rev,
            "updated_at": "2026-11-24T12:00:00.000Z",
            "updated_by": {"kind": "person", "person": ALICE_REF},
            "title": f"Revision title {rev} " + ("r" * 400),
            "status": "active",
            "change_kind": "edit",
        }
        for rev in range(25, 0, -1)
    ]
    hist_reply = {"outcome": "ok", "page": _inline_page(body="b" * 1_500), "revisions": revs, "total": 25}
    fitted_hist = await fit_reply_body(hist_reply, None, OWNER, max_bytes=6_000, list_key="revisions", offset=0)
    assert 0 < len(fitted_hist["revisions"]) < 25
    assert fitted_hist["page"]["type"] == "inline"
    assert fitted_hist["page"]["content"]["type"] == "inline"
