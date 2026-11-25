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
    agent_id: Any = "KnowledgeCuratorAgent",
    runtime: Optional[str] = None,
    session: Any = None,
    person_ref: Optional[str] = None,
    task_id: str = "task_merge_1",
    request_id: str = "req_merge_1",
    schedule_id: Optional[str] = None,
) -> VerifiedFrame:
    host_runtime = runtime or f"{role}-1"
    cert = SimpleNamespace(installation="install_test", runtime=host_runtime, teams=(TEAM,), roles=(role,))
    token = SimpleNamespace(
        person=person_ref,
        schedule_id=schedule_id,
        bound_to=SimpleNamespace(type="root_task", id=task_id, request_id=request_id),
    )
    authority = VerifiedAuthority(token=token, certificate=cert)  # type: ignore[arg-type]
    frame = SimpleNamespace(
        kind=kind,
        issuer=SimpleNamespace(installation="install_test", team=TEAM, runtime=host_runtime, agent=agent_id),
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
        session=session,
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

    # Hard ceiling (50,001 chars on add_note) is refused by decode_request_body before KnowledgeService runs;
    # configured limits (max_note_chars=34,000) are also enforced pre-download so oversized blobs are never fetched.
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
    assert ceiling_reply["outcome"] == "invalid_request" and "34,000" in ceiling_reply["message"]

    # F7: A 200 KB blob exceeds 35,000 * 4 bytes and is refused pre-download with ZERO chunk fetches!
    huge_ref = {"sha256": sha256_unpadded(b"never-fetched"), "size": 200_000, "media_type": TEXT_PLAIN_MEDIA_TYPE}
    env.js.calls.clear()
    pre_refused_frame = stand_in_frame(
        TEAM,
        "knowledge.write",
        {
            "request_id": "req_pre_download_refused",
            "space": "team",
            "title": "Way Too Big",
            "content": {"type": "blob", "ref": huge_ref},
            "confirm_review": False,
        },
        person_ref=ALICE_REF,
        request_id="req_pre_download_refused",
    )
    pre_refused = checked_reply("knowledge.write", await env.ops.handle(pre_refused_frame))
    assert pre_refused["outcome"] == "invalid_request" and "35,000" in pre_refused["message"]
    assert not any(c[0] == "get_msg" for c in env.js.calls)


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

    curator_session = SimpleNamespace(agent="curator-inst-1", agent_type="KnowledgeCuratorAgent")

    # Person-bound agent CANNOT merge team pages (personal_space_unavailable).
    person_team_merge = _root_task_frame(
        "knowledge.merge", {**merge_body, "request_id": "req_merge_team_denied"},
        session=curator_session, person_ref=ALICE_REF, request_id="task_req_alice_1",
    )
    denied = checked_reply("knowledge.merge", await env.ops.handle(person_team_merge))
    assert denied["outcome"] == "personal_space_unavailable"

    # System curation run merges team pages and returns a contract-valid blob-backed page reply.
    sys_merge = _root_task_frame(
        "knowledge.merge", merge_body, session=curator_session, person_ref=None,
        schedule_id="sched_curator_team", request_id="task_req_sys_1",
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
        session=curator_session,
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
        session=curator_session,
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


@pytest.mark.asyncio
async def test_request_blob_nats_timeout_returns_runtime_error_without_changing_data(tmp_path: Path) -> None:
    import nats.errors

    env = _setup(tmp_path)

    class _TimeoutGetStore:
        async def put(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return await env.blob_store.put(*args, **kwargs)

        async def get(self, *_args: Any, **_kwargs: Any) -> bytes:
            raise nats.errors.TimeoutError()

    env.ops._blobs = _TimeoutGetStore()
    ref = {"sha256": sha256_unpadded(b"long body"), "size": 9, "media_type": TEXT_PLAIN_MEDIA_TYPE}
    frame = stand_in_frame(
        TEAM,
        "knowledge.write",
        {
            "request_id": "req_write_nats_timeout",
            "space": "team",
            "title": "Should Not Save",
            "content": {"type": "blob", "ref": ref},
            "confirm_review": False,
        },
        person_ref=ALICE_REF,
        request_id="req_write_nats_timeout",
    )
    reply = checked_reply("knowledge.write", await env.ops.handle(frame))
    assert reply["outcome"] == "runtime_error"
    assert "Nothing was changed" in reply["message"]


@pytest.mark.asyncio
async def test_knowledge_merge_uses_session_agent_type_and_refuses_missing_or_oversized_agent_id(
    tmp_path: Path,
) -> None:
    env = _setup(tmp_path)
    for title in ("MergeSrc", "MergeDst"):
        frame = stand_in_frame(
            TEAM,
            "knowledge.write",
            {
                "request_id": f"seed_{title.lower()}",
                "space": "team",
                "title": title,
                "content": {"type": "inline", "text": f"Body {title}"},
                "confirm_review": False,
            },
            person_ref=ALICE_REF,
            request_id=f"seed_{title.lower()}",
        )
        assert (await env.ops.handle(frame))["outcome"] == "ok"

    # Missing issuer.agent with a >32-char runtime id returns not_allowed instead of failing BlobOwner validation.
    no_agent_frame = _root_task_frame(
        "knowledge.merge",
        {
            "request_id": "req_merge_no_agent",
            "space": "team",
            "page": "mergesrc",
            "seen_version": "1",
            "into": "mergedst",
            "target_seen_version": "1",
            "content": {"type": "inline", "text": "Merged."},
            "confirm_review": False,
        },
        agent_id=None,
        runtime="worker-host-with-a-very-long-runtime-identifier-exceeding-32-chars",
        session=None,
        person_ref=None,
        schedule_id="sched_curator_team",
    )
    refused = checked_reply("knowledge.merge", await env.ops.handle(no_agent_frame))
    assert refused["outcome"] == "not_allowed"

    # When verified.session is present, updated_by records session.agent_type (not issuer.agent instance id).
    session = SimpleNamespace(agent="curator-inst-0001", agent_type="KnowledgeCuratorAgent")
    session_frame = _root_task_frame(
        "knowledge.merge",
        {
            "request_id": "req_merge_with_session",
            "space": "team",
            "page": "mergesrc",
            "seen_version": "1",
            "into": "mergedst",
            "target_seen_version": "1",
            "content": {"type": "inline", "text": "Merged via session."},
            "confirm_review": False,
        },
        agent_id="curator-inst-0001",
        session=session,
        person_ref=None,
        schedule_id="sched_curator_team",
    )
    ok_reply = checked_reply("knowledge.merge", await env.ops.handle(session_frame))
    assert ok_reply["outcome"] == "ok"
    assert ok_reply["page"]["updated_by"] == {"kind": "agent", "agent_type": "KnowledgeCuratorAgent"}


@pytest.mark.asyncio
async def test_f2_knowledge_merge_enforces_real_cedar_authorization_and_session(tmp_path: Path) -> None:
    """F2 (SEC-2): knowledge.merge requires verified.session and real Cedar AccessService authorization."""

    from access_catalog.catalog import CatalogSource
    from access_control.cedar import CedarAccessClient
    from access_control.decisions import AccessService
    from access_control.entities import agent_role_resolver
    from custom_messages.contracts.access_v2 import (
        AccessLevel,
        GrantKind,
        GrantLifetime,
        GrantV1,
        SubjectKind,
        SubjectRefV1,
        TargetKind,
        TargetRefV1,
    )

    env = _setup(tmp_path)
    access_service = AccessService(
        store=env.access,
        catalog=CatalogSource(None).current(),
        cedar=CedarAccessClient(),
        team_id=TEAM,
        agent_role_of=agent_role_resolver("TeamArchitect"),
        lead_agent_type="TeamArchitect",
    )
    env.ops._access_service = access_service

    # Seed two team pages and two personal pages for Alice.
    for space, title in (("team", "SrcT"), ("team", "DstT"), ("personal", "SrcP"), ("personal", "DstP")):
        frame = stand_in_frame(
            TEAM,
            "knowledge.write",
            {
                "request_id": f"seed_{space}_{title.lower()}",
                "space": space,
                "title": title,
                "content": {"type": "inline", "text": f"Body {title}"},
                "confirm_review": False,
            },
            person_ref=ALICE_REF,
            request_id=f"seed_{space}_{title.lower()}",
        )
        assert (await env.ops.handle(frame))["outcome"] == "ok"

    def _merge_body(req_id: str, space: str, src: str, dst: str, *, content: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "request_id": req_id,
            "space": space,
            "page": src,
            "seen_version": "1",
            "into": dst,
            "target_seen_version": "1",
            "content": content or {"type": "inline", "text": "Merged text."},
            "confirm_review": False,
        }

    # 1. Missing verified.session -> not_allowed (even with curator agent_id on issuer).
    missing_sess = _root_task_frame(
        "knowledge.merge",
        _merge_body("req_no_sess", "team", "srct", "dstt"),
        agent_id="KnowledgeCuratorAgent",
        session=None,
        person_ref=None,
    )
    assert checked_reply("knowledge.merge", await env.ops.handle(missing_sess))["outcome"] == "not_allowed"

    # 2. Non-curator worker or TeamArchitect -> denied by real Cedar BEFORE fetching request blobs!
    unfetched_blob = {"type": "blob", "ref": {"sha256": sha256_unpadded(b"never-fetch"), "size": 11, "media_type": TEXT_PLAIN_MEDIA_TYPE}}
    for req_id, role, agent_type, pref in (
        ("req_worker_team", "worker_host", "ResearchWorker", None),
        ("req_worker_personal", "worker_host", "ResearchWorker", ALICE_REF),
        ("req_lead_personal", "lead_host", "TeamArchitect", ALICE_REF),
    ):
        env.js.calls.clear()
        frame = _root_task_frame(
            "knowledge.merge",
            _merge_body(req_id, "personal" if pref else "team", "srcp" if pref else "srct", "dstp" if pref else "dstt", content=unfetched_blob),
            role=role,
            agent_id="agent-1",
            session=SimpleNamespace(agent="agent-1", agent_type=agent_type),
            person_ref=pref,
            request_id=f"work_{req_id}",
        )
        reply = checked_reply("knowledge.merge", await env.ops.handle(frame))
        assert reply["outcome"] == "not_allowed", (agent_type, reply)
        assert not any(c[0] == "get_msg" for c in env.js.calls)

    # 3. KnowledgeCuratorAgent IS authorized by real Cedar for system team merge.
    curator_team = _root_task_frame(
        "knowledge.merge",
        _merge_body("req_curator_team", "team", "srct", "dstt"),
        agent_id="curator-1",
        session=SimpleNamespace(agent="curator-1", agent_type="KnowledgeCuratorAgent"),
        person_ref=None,
        schedule_id="sched_team_curation",
        request_id="work_curator_team",
    )
    assert checked_reply("knowledge.merge", await env.ops.handle(curator_team))["outcome"] == "ok"

    # 4. A block on fn:knowledge_curation denies even KnowledgeCuratorAgent via real Cedar.
    now = datetime.now(timezone.utc)
    env.access.create(GrantV1(
        grant_id="block-curation-alice",
        team_id=TEAM,
        kind=GrantKind.BLOCK,
        subject=SubjectRefV1(kind=SubjectKind.USER, id="alice"),
        target=TargetRefV1(kind=TargetKind.TOOL, id="fn:knowledge_curation"),
        level=AccessLevel.WRITE,
        lifetime=GrantLifetime.UNTIL_REVOKED,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id="alice"),
        created_at_utc=now,
        reason="Alice disabled curation merges",
    ))
    curator_blocked = _root_task_frame(
        "knowledge.merge",
        _merge_body("req_curator_blocked", "personal", "srcp", "dstp"),
        agent_id="curator-1",
        session=SimpleNamespace(agent="curator-1", agent_type="KnowledgeCuratorAgent"),
        person_ref=ALICE_REF,
        schedule_id="sched_alice_curation",
        request_id="work_curator_blocked",
    )
    assert checked_reply("knowledge.merge", await env.ops.handle(curator_blocked))["outcome"] == "not_allowed"

    # 5. team_operations wires AccessService when authorization.enabled=True and None when False,
    #    and forwards config.agent.request_conflict_checks (F10).
    cfg = GatewayInstanceConfig.create(
        instance_name="wire_auth",
        instance_root=str(tmp_path / "inst_auth"),
        team_id=TEAM,
        timezone="UTC",
        display_name="Wire Auth",
        manager_agent_id="manager_1",
        manager_storage_sqlite_path=str(tmp_path / "mgr_auth.db"),
        state_storage_sqlite_path=str(tmp_path / "gw_auth.db"),
        named_agent_type="TeamArchitect",
    )
    cfg.authorization.enabled = True
    cfg.agent.request_conflict_checks = False
    fams_on = team_operations(cfg, lambda: datetime.now(timezone.utc), js=FakeJetStream())
    k_ops_on = next(f for f in fams_on if "knowledge.merge" in f.handlers).handlers["knowledge.merge"].__self__  # type: ignore[attr-defined]
    assert k_ops_on._access_service is not None
    assert k_ops_on._request_conflict_checks is False

    cfg.authorization.enabled = False
    fams_off = team_operations(cfg, lambda: datetime.now(timezone.utc), js=FakeJetStream())
    k_ops_off = next(f for f in fams_off if "knowledge.merge" in f.handlers).handlers["knowledge.merge"].__self__  # type: ignore[attr-defined]
    assert k_ops_off._access_service is None


@pytest.mark.asyncio
async def test_f9_f10_knowledge_merge_change_ledger_refusals_and_conflict_switch(tmp_path: Path) -> None:
    """F9 & F10: knowledge.merge emits schema-valid change_conflict/needs_person_decision/request_closed/record_busy
    and honors request_conflict_checks."""

    from knowledge_base.records import KnowledgeError
    from storage.change_ledger import RequestState, close_request, open_request

    env = _setup(tmp_path)
    curator_session = SimpleNamespace(agent="curator-1", agent_type="KnowledgeCuratorAgent")
    now = datetime.now(timezone.utc)

    # Seed two personal pages for Alice.
    for title in ("Note One", "Note Two"):
        frame = stand_in_frame(
            TEAM,
            "knowledge.write",
            {
                "request_id": f"seed_f9_{title.lower().replace(' ', '_')}",
                "space": "personal",
                "title": title,
                "content": {"type": "inline", "text": f"Initial {title}."},
                "confirm_review": False,
            },
            person_ref=ALICE_REF,
            request_id=f"seed_f9_{title.lower().replace(' ', '_')}",
        )
        assert (await env.ops.handle(frame))["outcome"] == "ok"

    # Admit non-scheduled request req_merge_run at the current ledger position, then let Alice edit Note Two
    # while req_merge_run is running so rule (b) triggers change_conflict!
    with env.repo._write() as conn:
        open_request(
            conn, env.repo._dialect, team_id=TEAM, request_id="req_merge_run",
            person_id="alice", now=now,
        )

    edit_frame = stand_in_frame(
        TEAM,
        "knowledge.write",
        {
            "request_id": "alice_edit_note_two",
            "space": "personal",
            "page": "note-two",
            "seen_version": "1",
            "title": "Note Two",
            "content": {"type": "inline", "text": "Alice updated Note Two concurrently."},
            "confirm_review": False,
        },
        person_ref=ALICE_REF,
        request_id="alice_edit_note_two",
    )
    assert (await env.ops.handle(edit_frame))["outcome"] == "ok"

    # 1. First non-scheduled merge sees change_conflict with required case_id and text/plain blob_ref brief.
    conflict_frame = _root_task_frame(
        "knowledge.merge",
        {
            "request_id": "req_merge_conflict_1",
            "space": "personal",
            "page": "note-one",
            "seen_version": "1",
            "into": "note-two",
            "target_seen_version": "2",
            "content": {"type": "inline", "text": "Curator merged text."},
            "confirm_review": False,
        },
        session=curator_session,
        person_ref=ALICE_REF,
        request_id="req_merge_run",
        schedule_id=None,
    )
    conflict_reply = checked_reply("knowledge.merge", await env.ops.handle(conflict_frame))
    assert conflict_reply["outcome"] == "change_conflict"
    assert conflict_reply["case_id"]
    assert conflict_reply["brief"]["media_type"] == TEXT_PLAIN_MEDIA_TYPE
    brief_bytes = await env.blob_service.get(conflict_reply["brief"])
    assert conflict_reply["case_id"] in brief_bytes.decode("utf-8")

    # Idempotent retry of the same change_conflict command also returns a schema-valid reply with brief blob_ref!
    conflict_retry = checked_reply("knowledge.merge", await env.ops.handle(conflict_frame))
    assert conflict_retry["outcome"] == "change_conflict"
    assert conflict_retry["case_id"] == conflict_reply["case_id"]
    assert conflict_retry["brief"]["sha256"] == conflict_reply["brief"]["sha256"]

    # 2. Second attempt under the same request overlaps body -> needs_person_decision with case_id.
    overlap_frame = _root_task_frame(
        "knowledge.merge",
        {
            "request_id": "req_merge_overlap_2",
            "space": "personal",
            "page": "note-one",
            "seen_version": "1",
            "into": "note-two",
            "target_seen_version": "2",
            "content": {"type": "inline", "text": "Curator merged text again."},
            "confirm_review": False,
        },
        session=curator_session,
        person_ref=ALICE_REF,
        request_id="req_merge_run",
        schedule_id=None,
    )
    needs_reply = checked_reply("knowledge.merge", await env.ops.handle(overlap_frame))
    assert needs_reply["outcome"] == "needs_person_decision"
    assert needs_reply["case_id"] == conflict_reply["case_id"]

    # 3. Closed request -> request_closed with request_state.
    with env.repo._write() as conn:
        close_request(
            conn, env.repo._dialect, team_id=TEAM, request_id="req_merge_closed",
            person_id="alice", state=RequestState.STOPPED, now=now,
        )
    closed_frame = _root_task_frame(
        "knowledge.merge",
        {
            "request_id": "req_merge_closed_1",
            "space": "personal",
            "page": "note-one",
            "seen_version": "1",
            "into": "note-two",
            "target_seen_version": "2",
            "content": {"type": "inline", "text": "Should be refused closed."},
            "confirm_review": False,
        },
        session=curator_session,
        person_ref=ALICE_REF,
        request_id="req_merge_closed",
        schedule_id=None,
    )
    closed_reply = checked_reply("knowledge.merge", await env.ops.handle(closed_frame))
    assert closed_reply["outcome"] == "request_closed"
    assert closed_reply["request_state"] == "stopped"

    # 4. record_busy populates retry_at and optional claim and validates against the schema.
    real_merge = env.ops._service.merge
    claim = {
        "claim_id": "claim_1",
        "resource": "record:knowledge_page:personal:note-two",
        "ends_at": "2026-11-24T19:00:00.000Z",
        "whose": "same_person",
        "notes_total": 1,
    }

    def _raise_busy(*_a: Any, **_kw: Any) -> Any:
        raise KnowledgeError("record_busy", "Held by another claim.", {"retry_at": "2026-11-24T19:00:00.000Z", "claim": claim})

    env.ops._service.merge = _raise_busy  # type: ignore[method-assign]
    busy_reply = checked_reply(
        "knowledge.merge",
        await env.ops.handle(_root_task_frame(
            "knowledge.merge",
            {
                "request_id": "req_merge_busy_1",
                "space": "personal",
                "page": "note-one",
                "seen_version": "1",
                "into": "note-two",
                "target_seen_version": "2",
                "content": {"type": "inline", "text": "Busy test."},
                "confirm_review": False,
            },
            session=curator_session,
            person_ref=ALICE_REF,
            request_id="req_merge_run",
        )),
    )
    assert busy_reply["outcome"] == "record_busy"
    assert busy_reply["retry_at"] == "2026-11-24T19:00:00.000Z"
    assert busy_reply["claim"] == claim
    env.ops._service.merge = real_merge  # type: ignore[method-assign]

    # 5. F10: With request_conflict_checks=False, the same non-scheduled personal merge succeeds!
    env.ops._request_conflict_checks = False
    ok_unchecked = checked_reply(
        "knowledge.merge",
        await env.ops.handle(_root_task_frame(
            "knowledge.merge",
            {
                "request_id": "req_merge_unchecked_1",
                "space": "personal",
                "page": "note-one",
                "seen_version": "1",
                "into": "note-two",
                "target_seen_version": "2",
                "content": {"type": "inline", "text": "Merged with conflict checks disabled."},
                "confirm_review": False,
            },
            session=curator_session,
            person_ref=ALICE_REF,
            request_id="req_merge_run",
            schedule_id=None,
        )),
    )
    assert ok_unchecked["outcome"] == "ok"
