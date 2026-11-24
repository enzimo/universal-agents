"""Deterministic tests for team_services.knowledge blob transport, post-fetch limits, merge, and wiring (A5)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from access_control.people_directory import PeopleDirectory
from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.wire.frame import VerifiedAuthority, VerifiedFrame
from custom_messages.wire.sizes import INLINE_TEXT_MAX_CHARS, REPLY_BODY_MAX_BYTES, encoded_size
from custom_transport.protocol1.blobs import BlobOwner, BlobStoreFull, sha256_unpadded
from custom_transport.protocol1.knowledge_blobs import (
    TEXT_PLAIN_MEDIA_TYPE,
    TeamServicesBlobStore,
    resolve_reply_body,
)
from knowledge_base.service import knowledge_service_for
from runtime_settings.knowledge import knowledge_settings
from storage.access_repository import AccessRepository
from storage.blob_index_repository import BlobIndexRepository
from storage.config import resolve_storage_settings
from storage.knowledge_repository import create_knowledge_repository
from team_services.blobs import BlobService
from team_services.knowledge import KNOWLEDGE_KINDS, KnowledgeOperations
from team_services.outcomes import AnswerUnknown
from team_services.process import team_operations
from tests.team_services_support import checked_reply, stand_in_frame
from tests.test_protocol1_blobs import FakeJetStream

TEAM = "team_example"
ALICE_REF = "p-AliceRef00000000000001"
BOB_REF = "p-BobRef0000000000000002"


def _root_task_frame(
    kind: str,
    body: dict[str, Any],
    *,
    role: str = "worker_host",
    agent_id: str = "KnowledgeCuratorAgent",
    person_ref: Optional[str] = None,
    task_id: str = "task_merge_1",
    request_id: str = "req_merge_1",
    schedule_id: Optional[str] = None,
) -> VerifiedFrame:
    cert = SimpleNamespace(installation="install_test", runtime=f"{role}-1", teams=(TEAM,), roles=(role,))
    token = SimpleNamespace(
        person=person_ref,
        schedule_id=schedule_id,
        bound_to=SimpleNamespace(type="root_task", id=task_id, request_id=request_id),
    )
    authority = VerifiedAuthority(token=token, certificate=cert)  # type: ignore[arg-type]
    frame = SimpleNamespace(
        kind=kind,
        issuer=SimpleNamespace(installation="install_test", team=TEAM, runtime=f"{role}-1", agent=agent_id),
    )
    raw = {
        "kind": kind,
        "idempotency_key": f"{kind}:{body['request_id']}",
        "body_digest": sha256_unpadded(str(sorted(body.items())).encode("utf-8")),
        "body": body,
    }
    return SimpleNamespace(
        frame=frame,
        body=body,
        raw=raw,
        certificate=cert,
        authority=authority,
        session=None,
        replay_key=None,
        frame_digest="",
    )  # type: ignore[return-value]


def _setup(tmp_path: Path, *, max_body_chars: int = 500_000, max_note_chars: int = 50_000) -> SimpleNamespace:
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "team.db")}})
    access = AccessRepository(storage)
    now = datetime.now(timezone.utc)
    for pid, pref in (("alice", ALICE_REF), ("bob", BOB_REF)):
        access.record_person(TeamPersonV1(
            team_id=TEAM, person_id=pid, person_ref=pref, standing=PersonStanding.ACTIVE,
            roles=("member",), changed_at_utc=now,
        ))
    people = PeopleDirectory(access, TEAM)
    js = FakeJetStream()
    blob_service = BlobService(
        repository=BlobIndexRepository(storage),
        people=people,
        js=js,
        team_id=TEAM,
        clock=lambda: datetime.now(timezone.utc),
    )
    repo = create_knowledge_repository(storage)
    settings = knowledge_settings({
        "enabled": True,
        "limits": {
            "max_body_chars": max_body_chars,
            "max_note_chars": max_note_chars,
            "max_title_chars": 1000,
            "max_summary_chars": 4000,
            "max_tags": 100,
            "writes_per_agent_per_hour": 1000,
            "page_edits_per_agent": 100,
            "new_pages_per_work": 100,
        },
    })
    ops = KnowledgeOperations(
        repo, people, settings, lambda: datetime.now(timezone.utc), logging.getLogger("test.ts_knowledge"),
        blobs=blob_service,
    )
    service = knowledge_service_for(repo, settings=settings)
    return SimpleNamespace(
        access=access, people=people, js=js, blob_service=blob_service,
        blob_store=TeamServicesBlobStore(blob_service), repo=repo, ops=ops, service=service,
    )


@pytest.mark.asyncio
async def test_team_services_enforces_configured_and_ceiling_limits_after_blob_fetch(tmp_path: Path) -> None:
    env = _setup(tmp_path, max_body_chars=35_000, max_note_chars=34_000)
    owner = BlobOwner(request_id="req_limits", person_ref=ALICE_REF)

    # 36,000-char body blob is within 500k ceiling, fetched, and refused by configured max_body_chars=35,000.
    over_config_body = "b" * 36_000
    body_ref = await env.blob_service.put_and_register(
        over_config_body.encode("utf-8"), owner, media_type=TEXT_PLAIN_MEDIA_TYPE
    )
    write_frame = stand_in_frame(
        TEAM,
        "knowledge.write",
        {
            "request_id": "req_over_config_body",
            "space": "team",
            "title": "Too Big",
            "content": {"type": "blob", "ref": body_ref},
            "confirm_review": False,
        },
        person_ref=ALICE_REF,
        request_id="req_over_config_body",
    )
    reply = checked_reply("knowledge.write", await env.ops.handle(write_frame))
    assert reply["outcome"] == "invalid_request"
    assert "35,000" in reply["message"] or "35000" in reply["message"]

    # Create a valid 34,500-char page via blob, then test note limit enforcement (34,500 > 34,000).
    ok_body = "k" * 34_500
    ok_ref = await env.blob_service.put_and_register(ok_body.encode("utf-8"), owner, media_type=TEXT_PLAIN_MEDIA_TYPE)
    ok_frame = stand_in_frame(
        TEAM,
        "knowledge.write",
        {
            "request_id": "req_ok_body",
            "space": "team",
            "title": "Fits Config",
            "content": {"type": "blob", "ref": ok_ref},
            "confirm_review": False,
        },
        person_ref=ALICE_REF,
        request_id="req_ok_body",
    )
    ok_reply = checked_reply("knowledge.write", await env.ops.handle(ok_frame))
    assert ok_reply["outcome"] == "ok" and ok_reply["page"]["content"]["type"] == "blob"
    resolved = await resolve_reply_body(ok_reply, env.blob_store)
    assert resolved["page"]["content"]["text"] == ok_body

    over_note_ref = await env.blob_service.put_and_register(
        ("n" * 34_500).encode("utf-8"), owner, media_type=TEXT_PLAIN_MEDIA_TYPE
    )
    note_frame = stand_in_frame(
        TEAM,
        "knowledge.add_note",
        {
            "request_id": "req_over_note",
            "space": "team",
            "page": "fits-config",
            "note": {"type": "blob", "ref": over_note_ref},
            "confirm_review": False,
        },
        person_ref=ALICE_REF,
        request_id="req_over_note",
    )
    note_reply = checked_reply("knowledge.add_note", await env.ops.handle(note_frame))
    assert note_reply["outcome"] == "invalid_request"

    # Hard ceiling (50,001 chars on add_note) is refused by decode_request_body before KnowledgeService runs.
    ceiling_note_ref = await env.blob_service.put_and_register(
        ("c" * 50_001).encode("utf-8"), owner, media_type=TEXT_PLAIN_MEDIA_TYPE
    )
    ceiling_frame = stand_in_frame(
        TEAM,
        "knowledge.add_note",
        {
            "request_id": "req_ceiling_note",
            "space": "team",
            "page": "fits-config",
            "note": {"type": "blob", "ref": ceiling_note_ref},
            "confirm_review": False,
        },
        person_ref=ALICE_REF,
        request_id="req_ceiling_note",
    )
    ceiling_reply = checked_reply("knowledge.add_note", await env.ops.handle(ceiling_frame))
    assert ceiling_reply["outcome"] == "invalid_request" and "50,000" in ceiling_reply["message"]


@pytest.mark.asyncio
async def test_knowledge_merge_handles_long_blob_text_for_system_and_personal_spaces(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    assert "knowledge.merge" in KNOWLEDGE_KINDS

    # 1. Seed two team pages and merge them with a 60,000-character merged body via system root_task token.
    owner_sys = BlobOwner(agent_id="KnowledgeCuratorAgent")
    for title, text in (("Source Guide", "Old notes."), ("Target Guide", "Current notes.")):
        frame = stand_in_frame(
            TEAM,
            "knowledge.write",
            {
                "request_id": f"seed_{title.lower().replace(' ', '_')}",
                "space": "team",
                "title": title,
                "content": {"type": "inline", "text": text},
                "confirm_review": False,
            },
            person_ref=ALICE_REF,
            request_id=f"seed_{title.lower().replace(' ', '_')}",
        )
        assert (await env.ops.handle(frame))["outcome"] == "ok"

    merged_body = ("Merged long team guide line.\n" * 3_000)[:60_000]
    merged_ref = await env.blob_service.put_and_register(
        merged_body.encode("utf-8"), owner_sys, media_type=TEXT_PLAIN_MEDIA_TYPE
    )
    merge_body = {
        "request_id": "req_merge_team_1",
        "space": "team",
        "page": "source-guide",
        "seen_version": "1",
        "into": "target-guide",
        "target_seen_version": "1",
        "content": {"type": "blob", "ref": merged_ref},
        "title": "Combined Guide",
        "confirm_review": False,
    }

    # Person-bound agent CANNOT merge team pages (personal_space_unavailable).
    person_team_merge = _root_task_frame(
        "knowledge.merge", {**merge_body, "request_id": "req_merge_team_denied"},
        person_ref=ALICE_REF, request_id="task_req_alice_1",
    )
    denied = checked_reply("knowledge.merge", await env.ops.handle(person_team_merge))
    assert denied["outcome"] == "personal_space_unavailable"

    # System curation run merges team pages and returns a contract-valid blob-backed page reply.
    sys_merge = _root_task_frame(
        "knowledge.merge", merge_body, person_ref=None, schedule_id="sched_curator_team", request_id="task_req_sys_1"
    )
    merged_reply = checked_reply("knowledge.merge", await env.ops.handle(sys_merge))
    assert merged_reply["outcome"] == "ok"
    assert encoded_size(merged_reply) <= REPLY_BODY_MAX_BYTES
    assert merged_reply["page"]["content"]["type"] == "blob"
    resolved = await resolve_reply_body(merged_reply, env.blob_store)
    assert resolved["page"]["title"] == "Combined Guide"
    assert resolved["page"]["content"]["text"] == merged_body

    # Idempotent retry returns the same merged page without failing on stale revision.
    retry_reply = checked_reply("knowledge.merge", await env.ops.handle(sys_merge))
    assert retry_reply["outcome"] == "ok"
    assert (await resolve_reply_body(retry_reply, env.blob_store))["page"]["content"]["text"] == merged_body

    # 2. Person-bound agent merges two personal pages for Alice; departed person is refused.
    for title in ("Draft A", "Draft B"):
        frame = stand_in_frame(
            TEAM,
            "knowledge.write",
            {
                "request_id": f"seed_p_{title.lower().replace(' ', '_')}",
                "space": "personal",
                "title": title,
                "content": {"type": "inline", "text": f"Body of {title}."},
                "confirm_review": False,
            },
            person_ref=ALICE_REF,
            request_id=f"seed_p_{title.lower().replace(' ', '_')}",
        )
        assert (await env.ops.handle(frame))["outcome"] == "ok"

    personal_merged = ("Personal merged text.\n" * 2_000).strip()
    personal_ref = await env.blob_service.put_and_register(
        personal_merged.encode("utf-8"),
        BlobOwner(request_id="task_req_alice_2", person_ref=ALICE_REF),
        media_type=TEXT_PLAIN_MEDIA_TYPE,
    )
    personal_frame = _root_task_frame(
        "knowledge.merge",
        {
            "request_id": "req_merge_personal_1",
            "space": "personal",
            "page": "draft-a",
            "seen_version": "1",
            "into": "draft-b",
            "target_seen_version": "1",
            "content": {"type": "blob", "ref": personal_ref},
            "confirm_review": False,
        },
        person_ref=ALICE_REF,
        request_id="task_req_alice_2",
        schedule_id="sched_curator_alice",
    )
    personal_reply = checked_reply("knowledge.merge", await env.ops.handle(personal_frame))
    assert personal_reply["outcome"] == "ok"
    assert (await resolve_reply_body(personal_reply, env.blob_store))["page"]["content"]["text"] == personal_merged

    # Departed person refused before fetching request blobs.
    env.access.record_person(TeamPersonV1(
        team_id=TEAM, person_id="bob", person_ref=BOB_REF, standing=PersonStanding.DISABLED,
        roles=(), changed_at_utc=datetime.now(timezone.utc),
    ))
    departed_frame = _root_task_frame(
        "knowledge.merge",
        {
            "request_id": "req_merge_departed",
            "space": "personal",
            "page": "draft-a",
            "seen_version": "1",
            "into": "draft-b",
            "target_seen_version": "1",
            "content": {"type": "blob", "ref": {"sha256": sha256_unpadded(b"missing"), "size": 7, "media_type": TEXT_PLAIN_MEDIA_TYPE}},
            "confirm_review": False,
        },
        person_ref=BOB_REF,
        request_id="task_req_bob_1",
    )
    departed_reply = checked_reply("knowledge.merge", await env.ops.handle(departed_frame))
    assert departed_reply["outcome"] == "not_allowed"


@pytest.mark.asyncio
async def test_reply_blob_store_full_returns_runtime_error_on_read_and_answer_unknown_after_commit(
    tmp_path: Path,
) -> None:
    env = _setup(tmp_path)
    long_body = "x" * (INLINE_TEXT_MAX_CHARS + 5_000)
    owner = BlobOwner(request_id="req_seed", person_ref=ALICE_REF)
    ref = await env.blob_service.put_and_register(long_body.encode("utf-8"), owner, media_type=TEXT_PLAIN_MEDIA_TYPE)

    # Save a long page cleanly first.
    write_ok = await env.ops.handle(stand_in_frame(
        TEAM,
        "knowledge.write",
        {
            "request_id": "req_write_initial",
            "space": "team",
            "title": "Long Page",
            "content": {"type": "blob", "ref": ref},
            "confirm_review": False,
        },
        person_ref=ALICE_REF,
        request_id="req_write_initial",
    ))
    assert write_ok["outcome"] == "ok"

    # Simulate BlobStoreFull on outgoing reply blob writes while keeping reads working.
    class _FullPutStore:
        async def put(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise BlobStoreFull("OBJ_UA1_BLOBS is full")

        async def get(self, blob_ref: dict[str, Any], *, max_bytes: Optional[int] = None) -> bytes:
            return await env.blob_store.get(blob_ref, max_bytes=max_bytes)

    env.ops._blobs = _FullPutStore()

    # On READ: nothing changed, returns plain runtime_error mentioning max_bytes.blobs.
    read_reply = checked_reply("knowledge.read", await env.ops.handle(stand_in_frame(
        TEAM,
        "knowledge.read",
        {"request_id": "req_read_full", "space": "team", "page": "long-page"},
        person_ref=ALICE_REF,
        request_id="req_read_full",
    )))
    assert read_reply["outcome"] == "runtime_error"
    assert "transport.queues.max_bytes.blobs" in read_reply["message"]

    # On CHANGE (add_note): change commits in DB, then reply blob write fails -> AnswerUnknown!
    with pytest.raises(AnswerUnknown):
        await env.ops.handle(stand_in_frame(
            TEAM,
            "knowledge.add_note",
            {
                "request_id": "req_note_full",
                "space": "team",
                "page": "long-page",
                "note": {"type": "inline", "text": "Important note."},
                "confirm_review": False,
            },
            person_ref=ALICE_REF,
            request_id="req_note_full",
        ))


def test_team_operations_wires_blob_service_into_knowledge_operations(tmp_path: Path) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="wire_check",
        instance_root=str(tmp_path / "instance"),
        team_id=TEAM,
        timezone="UTC",
        display_name="Owner",
        manager_agent_id="mgr_1",
        manager_storage_sqlite_path=str(tmp_path / "manager.db"),
        state_storage_sqlite_path=str(tmp_path / "gateway.db"),
        named_agent_type="TeamArchitect",
    )
    js = FakeJetStream()
    families = team_operations(config, lambda: datetime.now(timezone.utc), js=js)
    knowledge_family = next(f for f in families if "knowledge.read" in f.handlers)
    assert "knowledge.merge" in knowledge_family.handlers
    knowledge_ops = knowledge_family.handlers["knowledge.read"].__self__  # type: ignore[attr-defined]
    assert isinstance(knowledge_ops, KnowledgeOperations)
    assert knowledge_ops._blobs is not None
