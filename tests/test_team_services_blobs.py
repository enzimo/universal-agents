"""Deterministic tests for team-services blob index, retention, CR-3/C01/C04/C06/FA-6, and departure wiring (A3, A4)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from access_control.people_directory import PeopleDirectory
from assistant_gateway.application.identity_service import PersonChange, ResolvedGatewayUser
from assistant_gateway.application.person_lifecycle import (
    ForgottenRecords,
    GatewayPersonLifecycle,
    GatewayPersonLifecycleSettings,
    HeldPersonData,
)
from assistant_gateway.application.team_commands import GatewayTeamCommandService, TeamServicesUnavailable
from assistant_gateway.person_departure import GatewayTeamData
from custom_agents.agent_host import AgentProcessHost
from custom_agents.agent_requests import AgentUnreachable, configure_process_host
from custom_agents.person_departure_runtime import settle_person
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.wire.frame import COMMAND_BINDING
from custom_transport.protocol1.blobs import (
    BlobDeleted,
    BlobOwner,
    blob_chunk_subject,
    blob_metadata_subject,
    blob_object_name,
    get_blob,
    put_blob,
    sha256_unpadded,
    stat_blob,
)
from storage.access_repository import AccessRepository
from storage.blob_index_repository import BlobIndexRepository, BlobOwnerConflict, BlobSizeMismatch
from storage.config import resolve_storage_settings
from team_services.blobs import BlobOperations, BlobService
from team_services.dead_letters import DeadLetterService, TeamPersonDataOperations, TeamPersonDepartureService
from tests.test_protocol1_blobs import FakeJetStream

TEAM = "team_example"
START = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
ALICE_ID = "alice"
ALICE_REF = "p-AliceRef00000000000001"
BOB_ID = "bob"
BOB_REF = "p-BobRef0000000000000002"
ADMIN_ID = "admin"
ADMIN_REF = "p-AdminRef00000000000003"


class _Clock:
    def __init__(self, initial: datetime = START) -> None:
        self.now = initial

    def __call__(self) -> datetime:
        return self.now

    def now_utc(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now


def _storage(tmp_path: Path, name: str = "blobs.db") -> Any:
    return resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / name)}})


def _seed_people(storage: Any, people: list[TeamPersonV1]) -> PeopleDirectory:
    access = AccessRepository(storage)
    for person in people:
        access.record_person(person)
    return PeopleDirectory(access, TEAM)


def _person(
    person_id: str,
    person_ref: str,
    standing: PersonStanding = PersonStanding.ACTIVE,
    roles: tuple[str, ...] = ("member",),
) -> TeamPersonV1:
    return TeamPersonV1(
        team_id=TEAM,
        person_id=person_id,
        person_ref=person_ref,
        standing=standing,
        roles=roles if standing is PersonStanding.ACTIVE else (),
        changed_at_utc=START,
    )


def _verified_frame(
    kind: str,
    body: dict[str, Any],
    *,
    roles: tuple[str, ...] = ("gateway",),
    runtime: str = "gateway",
    actor_person_ref: Optional[str] = None,
) -> Any:
    cert = SimpleNamespace(
        installation="install_test",
        runtime=runtime,
        teams=(TEAM,),
        roles=roles,
    )
    frame = SimpleNamespace(
        kind=kind,
        issuer=SimpleNamespace(installation="install_test", team=TEAM, runtime=runtime),
    )
    authority = None
    if actor_person_ref is not None:
        token = SimpleNamespace(
            bound_to=SimpleNamespace(type=COMMAND_BINDING, id=str(body["request_id"])),
            person=actor_person_ref,
        )
        authority = SimpleNamespace(token=token, certificate=cert)
    return SimpleNamespace(
        frame=frame,
        raw={"body": dict(body)},
        certificate=cert,
        authority=authority,
    )


# -- BlobIndexRepository tests ---------------------------------------------------------------


def test_blob_index_repository_multi_owner_expiry_and_person_removal(tmp_path: Path) -> None:
    repo = BlobIndexRepository(_storage(tmp_path))
    sha_shared = sha256_unpadded(b"shared-content")
    sha_alice = sha256_unpadded(b"alice-only")
    sha_agent = sha256_unpadded(b"system-agent")

    # Register Alice and Bob on sha_shared; extending Alice's expiry keeps the later timestamp.
    r1 = repo.register_owner(
        team_id=TEAM,
        sha256=sha_shared,
        owner_kind="request",
        owner_id="req_alice_1",
        person_ref=ALICE_REF,
        size_bytes=14,
        registered_at_utc=START,
        expires_at_utc=START + timedelta(days=7),
    )
    assert r1.expires_at_utc == START + timedelta(days=7)

    r1_shorter = repo.register_owner(
        team_id=TEAM,
        sha256=sha_shared,
        owner_kind="request",
        owner_id="req_alice_1",
        person_ref=ALICE_REF,
        size_bytes=14,
        registered_at_utc=START + timedelta(hours=1),
        expires_at_utc=START + timedelta(days=3),
    )
    assert r1_shorter.expires_at_utc == START + timedelta(days=7)

    r1_longer = repo.register_owner(
        team_id=TEAM,
        sha256=sha_shared,
        owner_kind="request",
        owner_id="req_alice_1",
        person_ref=ALICE_REF,
        size_bytes=14,
        registered_at_utc=START + timedelta(days=2),
        expires_at_utc=START + timedelta(days=9),
    )
    assert r1_longer.expires_at_utc == START + timedelta(days=9)

    with pytest.raises(BlobSizeMismatch):
        repo.register_owner(
            team_id=TEAM,
            sha256=sha_shared,
            owner_kind="request",
            owner_id="req_bad_size",
            person_ref=BOB_REF,
            size_bytes=999,
            registered_at_utc=START,
            expires_at_utc=START + timedelta(days=7),
        )

    repo.register_owner(
        team_id=TEAM,
        sha256=sha_shared,
        owner_kind="request",
        owner_id="req_bob_1",
        person_ref=BOB_REF,
        size_bytes=14,
        registered_at_utc=START,
        expires_at_utc=START + timedelta(days=10),
    )
    repo.register_owner(
        team_id=TEAM,
        sha256=sha_alice,
        owner_kind="request",
        owner_id="req_alice_2",
        person_ref=ALICE_REF,
        size_bytes=10,
        registered_at_utc=START,
        expires_at_utc=START + timedelta(days=7),
    )
    repo.register_owner(
        team_id=TEAM,
        sha256=sha_agent,
        owner_kind="agent",
        owner_id="worker-1",
        person_ref=None,
        size_bytes=12,
        registered_at_utc=START,
        expires_at_utc=START + timedelta(days=2),
    )

    assert repo.list_indexed_person_refs(TEAM) == (ALICE_REF, BOB_REF)

    # Expire at day 3: only system agent owner expires and becomes pending delete.
    removed_count, unowned = repo.remove_expired_owners(TEAM, START + timedelta(days=3))
    assert removed_count == 1
    assert unowned == (sha_agent,)
    assert not repo.has_owners(TEAM, sha_agent)
    pending_agent = repo.get_pending_delete(TEAM, sha_agent)
    assert pending_agent is not None
    assert pending_agent.reason == "expired"
    assert pending_agent.target_nuid is None
    assert repo.set_pending_delete_nuid(TEAM, sha_agent, "nuid_agent_1") is True
    assert repo.complete_pending_delete(TEAM, sha_agent, expected_nuid="wrong_nuid") is False
    assert repo.complete_pending_delete(TEAM, sha_agent, expected_nuid="nuid_agent_1") is True
    assert repo.get_pending_delete(TEAM, sha_agent) is None

    # Remove Alice: sha_alice becomes unowned (queued pending delete), sha_shared stays for Bob.
    removal = repo.remove_person_owners(TEAM, ALICE_REF, START + timedelta(days=4))
    assert removal.owners_removed == 2
    assert removal.unowned_sha256s == (sha_alice,)
    assert removal.kept_sha256s == (sha_shared,)
    assert repo.has_owners(TEAM, sha_shared)
    assert not repo.has_owners(TEAM, sha_alice)
    pending_alice = repo.get_pending_delete(TEAM, sha_alice)
    assert pending_alice is not None
    assert pending_alice.reason == "person_departed"
    assert pending_alice.person_ref == ALICE_REF


# -- BlobService & BlobOperations tests ------------------------------------------------------


@pytest.mark.asyncio
async def test_register_multi_owner_retention_and_expiry(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    people = _seed_people(storage, [_person(ALICE_ID, ALICE_REF), _person(BOB_ID, BOB_REF)])
    clock = _Clock()
    js = FakeJetStream(now=clock.now)
    repo = BlobIndexRepository(storage)
    service = BlobService(repository=repo, people=people, js=js, team_id=TEAM, retention_days=7, clock=clock)
    ops = BlobOperations(service, people)

    payload = b"hello protocol 1 blob store" * 20
    sha = sha256_unpadded(payload)

    # Register before upload -> present=False, no owner indexed.
    reply_absent = await ops.handle_register(
        _verified_frame(
            "blobs.register",
            {
                "request_id": "reg_1",
                "sha256": sha,
                "size": len(payload),
                "owner": {"type": "request", "request_id": "req_alice_1"},
                "person": ALICE_REF,
            },
        )
    )
    assert reply_absent == {"outcome": "ok", "present": False}
    assert not repo.has_owners(TEAM, sha)

    # Upload chunks + metadata, then register Alice -> present=True with expires_at.
    alice_owner = BlobOwner(request_id="req_alice_1", person_ref=ALICE_REF)
    await put_blob(js, TEAM, payload, alice_owner)
    reply_alice = await ops.handle_register(
        _verified_frame(
            "blobs.register",
            {
                "request_id": "reg_2",
                "sha256": sha,
                "size": len(payload),
                "owner": {"type": "request", "request_id": "req_alice_1"},
                "person": ALICE_REF,
            },
        )
    )
    assert reply_alice["outcome"] == "ok"
    assert reply_alice["present"] is True
    assert reply_alice["expires_at"] == "2026-12-01T12:00:00.000Z"

    # 3 days later, Bob registers the same blob -> dedup hit, extends team retention to day 10.
    clock.advance(days=3)
    js.now = clock.now
    reply_bob = await ops.handle_register(
        _verified_frame(
            "blobs.register",
            {
                "request_id": "reg_3",
                "sha256": sha,
                "size": len(payload),
                "owner": {"type": "request", "request_id": "req_bob_1"},
                "person": BOB_REF,
            },
        )
    )
    assert reply_bob["outcome"] == "ok"
    assert reply_bob["present"] is True
    assert len(repo.list_owners(TEAM, sha)) == 2

    # At day 8: Alice's owner expires, Bob's remains -> blob MUST stay readable!
    clock.advance(days=5)
    js.now = clock.now
    expired_owners, expired_deleted = await service.expire_blobs()
    assert expired_owners == 1
    assert expired_deleted == 0
    assert await get_blob(js, TEAM, sha) == payload

    # At day 11: Bob's owner expires -> 0 owners remain -> deleted:true marker + chunk purge!
    clock.advance(days=3)
    js.now = clock.now
    expired_owners, expired_deleted = await service.expire_blobs()
    assert expired_owners == 1
    assert expired_deleted == 1
    meta = await stat_blob(js, TEAM, sha)
    assert meta is not None and meta.deleted is True
    assert meta.person_ref == ALICE_REF  # owner headers preserved on deleted:true marker
    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, sha)
    assert not any(m.subject == blob_chunk_subject(meta.nuid) for m in js.messages)


@pytest.mark.asyncio
async def test_c04_departed_person_register_refused_and_raced_upload_deleted(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    access = AccessRepository(storage)
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    people = PeopleDirectory(access, TEAM)
    clock = _Clock()
    js = FakeJetStream(now=clock.now)
    service = BlobService(repository=BlobIndexRepository(storage), people=people, js=js, team_id=TEAM, clock=clock)

    payload = b"departed-person-raced-upload"
    sha = sha256_unpadded(payload)
    # Simulate a writer that uploaded bytes before noticing Alice was disabled.
    put_res = await put_blob(js, TEAM, payload, BlobOwner(request_id="req_raced", person_ref=ALICE_REF))
    assert await get_blob(js, TEAM, sha) == payload

    # Registration is refused AND the unindexed live blob carrying Alice's UA-Blob-Person is deleted immediately!
    reg = await service.register(
        sha256=sha,
        size=len(payload),
        owner=BlobOwner(request_id="req_raced", person_ref=ALICE_REF),
    )
    assert reg.outcome == "not_allowed"
    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, sha)
    assert not any(m.subject == blob_chunk_subject(put_res.nuid) for m in js.messages)


@pytest.mark.asyncio
async def test_c01_pending_delete_target_nuid_purges_old_chunks_without_touching_new_upload(tmp_path: Path) -> None:
    """C01: After a crash between deleted:true marker for nuid1 and chunk purge, a fresh upload under nuid2
    still purges departed nuid1's chunks and keeps live nuid2 intact."""

    storage = _storage(tmp_path)
    people = _seed_people(
        storage,
        [_person(ALICE_ID, ALICE_REF, PersonStanding.REMOVED), _person(BOB_ID, BOB_REF, PersonStanding.ACTIVE)],
    )
    clock = _Clock()
    js = FakeJetStream(now=clock.now)
    repo = BlobIndexRepository(storage)
    service = BlobService(repository=repo, people=people, js=js, team_id=TEAM, clock=clock)

    payload = b"same-content-re-uploaded"
    sha = sha256_unpadded(payload)

    # Upload nuid1 for Alice, simulate crash after deleted marker and target_nuid recorded, before chunk purge.
    first = await put_blob(
        js, TEAM, payload, BlobOwner(request_id="req_a", person_ref=ALICE_REF), nuid_factory=lambda: "nuid_old_1"
    )
    meta_subject = blob_metadata_subject(blob_object_name(TEAM, sha))
    await js.publish(
        meta_subject,
        json.dumps(first.metadata.as_deleted_payload(), separators=(",", ":")).encode("utf-8"),
    )
    repo.queue_pending_delete(
        TEAM, sha, requested_at_utc=clock.now, reason="person_departed", target_nuid="nuid_old_1", person_ref=ALICE_REF
    )
    assert any(m.subject == blob_chunk_subject("nuid_old_1") for m in js.messages)

    # Suppose Bob uploads fresh nuid2 before retry_pending_deletes runs, then calls blobs.register:
    second = await put_blob(
        js, TEAM, payload, BlobOwner(request_id="req_b", person_ref=BOB_REF), nuid_factory=lambda: "nuid_new_2"
    )
    assert second.nuid == "nuid_new_2"

    # Calling retry_pending_deletes MUST purge nuid_old_1 WITHOUT touching live nuid_new_2!
    completed = await service.retry_pending_deletes()
    assert completed == 1
    assert not any(m.subject == blob_chunk_subject("nuid_old_1") for m in js.messages)
    assert any(m.subject == blob_chunk_subject("nuid_new_2") for m in js.messages)
    assert await get_blob(js, TEAM, sha) == payload
    assert repo.get_pending_delete(TEAM, sha) is None

    # Now test the other path: blobs.register itself encounters pending_delete(target_nuid=nuid_old_3)
    # when live nuid_new_4 already exists -> purges nuid_old_3 and registers Bob on nuid_new_4!
    payload2 = b"another-shared-content"
    sha2 = sha256_unpadded(payload2)
    third = await put_blob(
        js, TEAM, payload2, BlobOwner(request_id="req_a2", person_ref=ALICE_REF), nuid_factory=lambda: "nuid_old_3"
    )
    await js.publish(
        blob_metadata_subject(blob_object_name(TEAM, sha2)),
        json.dumps(third.metadata.as_deleted_payload(), separators=(",", ":")).encode("utf-8"),
    )
    repo.queue_pending_delete(
        TEAM, sha2, requested_at_utc=clock.now, reason="person_departed", target_nuid="nuid_old_3", person_ref=ALICE_REF
    )
    await put_blob(
        js, TEAM, payload2, BlobOwner(request_id="req_b2", person_ref=BOB_REF), nuid_factory=lambda: "nuid_new_4"
    )
    reg = await service.register(
        sha256=sha2, size=len(payload2), owner=BlobOwner(request_id="req_b2", person_ref=BOB_REF)
    )
    assert reg.outcome == "ok" and reg.present is True
    assert not any(m.subject == blob_chunk_subject("nuid_old_3") for m in js.messages)
    assert await get_blob(js, TEAM, sha2) == payload2
    assert repo.get_pending_delete(TEAM, sha2) is None
    assert repo.has_owners(TEAM, sha2)


@pytest.mark.asyncio
async def test_cr3_serialized_register_vs_delete_and_put_and_register(tmp_path: Path) -> None:
    """CR-3: put_and_register retries cleanly when a concurrent delete removes the blob between upload and register."""

    storage = _storage(tmp_path)
    people = _seed_people(storage, [_person(ALICE_ID, ALICE_REF), _person(BOB_ID, BOB_REF)])
    clock = _Clock()
    js = FakeJetStream(now=clock.now)
    repo = BlobIndexRepository(storage)
    service = BlobService(repository=repo, people=people, js=js, team_id=TEAM, clock=clock)

    payload = b"cr3-race-content"
    sha = sha256_unpadded(payload)
    bob_owner = BlobOwner(request_id="req_bob_cr3", person_ref=BOB_REF)

    # First register returns present=False; simulate a delete landing right after put_blob
    # on attempt 1 so register returns present=False again, then attempt 2 succeeds.
    original_register = service.register
    calls = 0

    async def raced_register(*, sha256: str, size: int, owner: BlobOwner) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            # Simulate concurrent expiry/delete removing the just-uploaded object before register acquires _lock.
            repo.queue_pending_delete(TEAM, sha256, requested_at_utc=clock.now, reason="expired")
            await service.retry_pending_deletes()
        return await original_register(sha256=sha256, size=size, owner=owner)

    service.register = raced_register  # type: ignore[method-assign]
    ref = await service.put_and_register(payload, bob_owner, media_type="text/plain", name="page.txt")
    assert ref == {"sha256": sha, "size": len(payload), "media_type": "text/plain", "name": "page.txt"}
    assert calls == 3
    assert await service.get(ref) == payload
    assert repo.has_owners(TEAM, sha)


@pytest.mark.asyncio
async def test_delete_person_authorization_shared_blobs_and_idempotent_retry(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    access = AccessRepository(storage)
    access.record_person(_person(ADMIN_ID, ADMIN_REF, PersonStanding.ACTIVE, roles=("admin",)))
    access.record_person(_person(BOB_ID, BOB_REF, PersonStanding.ACTIVE, roles=("member",)))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.ACTIVE, roles=("member",)))
    people = PeopleDirectory(access, TEAM)
    clock = _Clock()
    js = FakeJetStream(now=clock.now)
    repo = BlobIndexRepository(storage)
    service = BlobService(repository=repo, people=people, js=js, team_id=TEAM, clock=clock)
    async def _async_json_reply(body: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(data=json.dumps(body).encode("utf-8"))

    empty_nc = SimpleNamespace(
        request=lambda *_args, **_kwargs: _async_json_reply({"error": {"code": 404, "err_code": 10059}}),
    )
    fake_live = SimpleNamespace(nc=empty_nc)
    dlq_service = DeadLetterService(team_id=TEAM, live_fn=lambda: fake_live, access_repository=access, people=people)  # type: ignore[arg-type,return-value]
    departure = TeamPersonDepartureService(team_id=TEAM, live_fn=lambda: fake_live, people=people, blobs=service)  # type: ignore[arg-type,return-value]
    ops = TeamPersonDataOperations(dead_letters=dlq_service, departure=departure, people=people)

    solo = b"alice-private-blob"
    shared = b"alice-and-bob-shared"
    sha_solo = sha256_unpadded(solo)
    sha_shared = sha256_unpadded(shared)

    await service.put_and_register(solo, BlobOwner(request_id="req_a_solo", person_ref=ALICE_REF), media_type="text/plain")
    await service.put_and_register(shared, BlobOwner(request_id="req_a_shared", person_ref=ALICE_REF), media_type="text/plain")
    await service.put_and_register(shared, BlobOwner(request_id="req_b_shared", person_ref=BOB_REF), media_type="text/plain")

    # 1. Refused while Alice is still active.
    active_refusal = await ops.handle_delete_person_data(
        _verified_frame(
            "team.delete_person_data",
            {"request_id": "del_1", "person": ALICE_REF},
            roles=("gateway",),
            actor_person_ref=ADMIN_REF,
        )
    )
    assert active_refusal["outcome"] == "not_allowed"

    # Disable Alice in PeopleDirectory.
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))

    # 2. Refused from a non-admin member via gateway.
    member_refusal = await ops.handle_delete_person_data(
        _verified_frame(
            "team.delete_person_data",
            {"request_id": "del_2", "person": ALICE_REF},
            roles=("gateway",),
            actor_person_ref=BOB_REF,
        )
    )
    assert member_refusal["outcome"] == "not_allowed"

    # 3. Refused from worker_host.
    worker_refusal = await ops.handle_delete_person_data(
        _verified_frame(
            "team.delete_person_data",
            {"request_id": "del_3", "person": ALICE_REF},
            roles=("worker_host",),
            runtime="worker-1",
        )
    )
    assert worker_refusal["outcome"] == "not_allowed"

    # 4. Simulate NATS failure during first delete_person_data call -> owner rows removed & pending_delete queued,
    #    reply is runtime_error; retry from lead_host finishes deletion cleanly!
    real_purge = js.purge_stream
    fail_once = {"fail": True}

    async def flaky_purge(*args: Any, **kwargs: Any) -> bool:
        if fail_once["fail"]:
            fail_once["fail"] = False
            raise RuntimeError("transient nats hiccup")
        return await real_purge(*args, **kwargs)

    js.purge_stream = flaky_purge  # type: ignore[method-assign]
    first_try = await ops.handle_delete_person_data(
        _verified_frame(
            "team.delete_person_data",
            {"request_id": "del_4", "person": ALICE_REF},
            roles=("gateway",),
            actor_person_ref=ADMIN_REF,
        )
    )
    assert first_try["outcome"] == "runtime_error"
    assert not repo.has_owners(TEAM, sha_solo)
    assert repo.has_owners(TEAM, sha_shared)
    assert repo.get_pending_delete(TEAM, sha_solo) is not None

    # Retry from lead_host (no command token required): finishes pending delete for Alice!
    second_try = await ops.handle_delete_person_data(
        _verified_frame(
            "team.delete_person_data",
            {"request_id": "del_5", "person": ALICE_REF},
            roles=("lead_host",),
            runtime="lead",
        )
    )
    assert second_try == {
        "outcome": "ok",
        "owners_removed": 0,
        "blobs_deleted": 1,
        "blobs_kept": 0,
        "dlq_copies_deleted": 0,
        "dlq_copies_kept": 0,
    }
    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, sha_solo)
    # Shared blob with Bob's live owner is untouched!
    assert await get_blob(js, TEAM, sha_shared) == shared
    assert repo.get_pending_delete(TEAM, sha_solo) is None


@pytest.mark.asyncio
async def test_maintain_fa6_sweep_departed_safety_net_and_c01_unindexed_guard(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    access = AccessRepository(storage)
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.ACTIVE))
    access.record_person(_person(BOB_ID, BOB_REF, PersonStanding.ACTIVE))
    people = PeopleDirectory(access, TEAM)
    clock = _Clock()
    js = FakeJetStream(now=clock.now)
    repo = BlobIndexRepository(storage)
    service = BlobService(
        repository=repo,
        people=people,
        js=js,
        team_id=TEAM,
        retention_days=7,
        clock=clock,
        orphan_grace_seconds=3600,
        clock_skew_seconds=8,
    )

    # 1. Orphan chunk (no metadata) and chunk named only by a deleted:true marker (FA-6).
    await js.publish(blob_chunk_subject("nuid_orphan_1"), b"orphan-chunk")
    del_payload = b"deleted-marker-chunk"
    del_res = await put_blob(
        js, TEAM, del_payload, BlobOwner(request_id="req_del", person_ref=BOB_REF), nuid_factory=lambda: "nuid_del_1"
    )
    await js.publish(
        blob_metadata_subject(blob_object_name(TEAM, del_res.sha256)),
        json.dumps(del_res.metadata.as_deleted_payload(), separators=(",", ":")).encode("utf-8"),
    )

    # 2. Indexed blob for Alice (who will be disabled while team_services is down -> safety net deletes it).
    alice_indexed = b"alice-missed-departure"
    alice_ref = await service.put_and_register(
        alice_indexed, BlobOwner(request_id="req_alice_missed", person_ref=ALICE_REF), media_type="text/plain"
    )

    # 3. Unindexed live blob uploaded by Bob that becomes older than grace+skew.
    old_unindexed = b"old-unindexed-bob"
    old_res = await put_blob(js, TEAM, old_unindexed, BlobOwner(request_id="req_old_unindexed", person_ref=BOB_REF))

    # 4. Unindexed live blob that IS registered right before cleanup acquires _lock (C01 guard!).
    raced_unindexed = b"raced-newly-indexed-bob"
    raced_res = await put_blob(js, TEAM, raced_unindexed, BlobOwner(request_id="req_raced_idx", person_ref=BOB_REF))

    # Disable Alice and advance past grace + skew (3608s), but within 7-day retention.
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    clock.advance(seconds=4000)
    js.now = clock.now

    # Simulate raced_res getting indexed right before _cleanup_unindexed_metadata runs.
    original_cleanup = service._cleanup_unindexed_metadata

    async def cleanup_with_concurrent_register(entries: Any, *, now: datetime) -> int:
        repo.register_owner(
            team_id=TEAM,
            sha256=raced_res.sha256,
            owner_kind="request",
            owner_id="req_raced_idx",
            person_ref=BOB_REF,
            size_bytes=len(raced_unindexed),
            registered_at_utc=now,
            expires_at_utc=now + timedelta(days=7),
        )
        return await original_cleanup(entries, now=now)

    service._cleanup_unindexed_metadata = cleanup_with_concurrent_register  # type: ignore[method-assign]

    report = await service.maintain()
    assert report.departed_people_cleaned == 1
    assert report.orphan_chunks_purged == 2  # nuid_orphan_1 and nuid_del_1 (FA-6)
    assert report.unindexed_blobs_deleted == 1  # only old_res, NOT raced_res (C01)

    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, alice_ref["sha256"])
    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, old_res.sha256)
    assert await get_blob(js, TEAM, raced_res.sha256) == raced_unindexed
    assert not any(m.subject == blob_chunk_subject("nuid_orphan_1") for m in js.messages)
    assert not any(m.subject == blob_chunk_subject("nuid_del_1") for m in js.messages)


# -- A4 & C06 departure wiring tests (gateway + manager + agent_host) ------------------------


@pytest.mark.asyncio
async def test_gateway_person_lifecycle_deletes_blobs_on_disable_and_remove() -> None:
    sent_commands: list[tuple[str, dict[str, Any], str, str]] = []

    class _FakeTeamServices:
        def __init__(self) -> None:
            self.outcomes = ["ok", "retry_later"]

        async def command(self, kind: str, body: dict[str, Any], *, person_ref: str, request_id: str) -> dict[str, Any]:
            sent_commands.append((kind, dict(body), person_ref, request_id))
            outcome = self.outcomes.pop(0) if self.outcomes else "ok"
            return {
                "outcome": outcome,
                "owners_removed": 2,
                "blobs_deleted": 1,
                "blobs_kept": 1,
                "dlq_copies_deleted": 3,
                "dlq_copies_kept": 4,
            }

    class _FakeRefs:
        def person_ref(self, person_id: str) -> str | None:
            return {ADMIN_ID: ADMIN_REF, ALICE_ID: ALICE_REF}.get(person_id)

    team_commands = GatewayTeamCommandService(
        team_services=_FakeTeamServices(),
        references=_FakeRefs(),
        request_ids=lambda: "01950000-0000-7000-8000-000000000099",
    )
    team_data_port = GatewayTeamData(team_commands=team_commands, references=_FakeRefs())

    @dataclass
    class _FakeUser:
        canonical_user_id: str

    actor = ResolvedGatewayUser(user=_FakeUser(ADMIN_ID), memberships=())  # type: ignore[arg-type]

    class _Identity:
        def set_user_enabled_status(self, **_kwargs: Any) -> PersonChange:
            return PersonChange(text="Disabled alice.", person_id=ALICE_ID, previous=PersonStanding.ACTIVE)

        def remove_user(self, **_kwargs: Any) -> PersonChange:
            return PersonChange(text="Removed alice.", person_id=ALICE_ID, previous=PersonStanding.ACTIVE)

    class _Records:
        def withdraw_requests(self, _person_id: str) -> int:
            return 0

        def hold(self, _person_id: str, *, until: datetime) -> HeldPersonData:
            return HeldPersonData(hold_until=until)

        def forget(self, _person_id: str, *, actor_id: str) -> ForgottenRecords:
            return ForgottenRecords()

        def held(self, _person_id: str) -> HeldPersonData | None:
            return None

        def restore(self, _person_id: str) -> HeldPersonData:
            raise AssertionError("not used")

    class _Work:
        async def request_person_standing(self, command: Any, *, actor_person_id: str) -> Any:
            return SimpleNamespace(success=True, message="settled", outcome=SimpleNamespace(
                tasks_stopped=0,
                objectives_stopped=0,
                schedules_deleted=0,
                task_records_deleted=0,
                objectives_deleted=0,
                memory_review_jobs_deleted=0,
                sandbox_deleted=False,
                held_record_count=0,
                team_schedules_moved=[],
                problems=[],
            ))

    lifecycle = GatewayPersonLifecycle(
        settings=GatewayPersonLifecycleSettings(team_id=TEAM, removed_data_retention_days=7),
        identity=_Identity(),  # type: ignore[arg-type]
        records=_Records(),
        work=_Work(),
        broker=None,
        person_schedules=None,
        clock=_Clock(),
        team_data=team_data_port,
    )

    disable_reply = await lifecycle.set_enabled(
        actor=actor, target=ALICE_ID, enabled=False, channel_name="web", chat_scope="dm"
    )
    assert "Not done" not in disable_reply
    # Second call receives retry_later (continuing in background) and is accepted with no problem!
    remove_reply = await lifecycle.remove(
        actor=actor, target=ALICE_ID, channel_name="web", chat_scope="dm"
    )
    assert "Not done" not in remove_reply
    assert len(sent_commands) == 2
    for kind, body, actor_ref, req_id in sent_commands:
        assert kind == "team.delete_person_data"
        assert body == {"request_id": req_id, "person": ALICE_REF}
        assert actor_ref == ADMIN_REF

    # When team services is unavailable, problem is reported cleanly without crashing departure.
    class _BrokenTeamServices:
        async def command(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise TeamServicesUnavailable("team services down")

    broken_team_data = GatewayTeamData(
        team_commands=GatewayTeamCommandService(
            team_services=_BrokenTeamServices(),
            references=_FakeRefs(),
            request_ids=lambda: "01950000-0000-7000-8000-000000000100",
        ),
        references=_FakeRefs(),
    )
    broken_lifecycle = GatewayPersonLifecycle(
        settings=GatewayPersonLifecycleSettings(team_id=TEAM, removed_data_retention_days=7),
        identity=_Identity(),  # type: ignore[arg-type]
        records=_Records(),
        work=_Work(),
        broker=None,
        person_schedules=None,
        clock=_Clock(),
        team_data=broken_team_data,
    )
    degraded_reply = await broken_lifecycle.set_enabled(
        actor=actor, target=ALICE_ID, enabled=False, channel_name="web", chat_scope="dm"
    )
    assert "Not done" in degraded_reply
    assert "temporary blob storage and set-aside message copies" in degraded_reply


@pytest.mark.asyncio
async def test_c06_agent_host_delete_person_data_and_manager_settle_person(tmp_path: Path) -> None:
    """C06 & A6: AgentProcessHost.delete_person_data sends team.delete_person_data via _on_host_loop,
    and settle_person accepts both ok and retry_later without reporting problems."""

    # 1. AgentProcessHost readiness checks and request shape.
    profile = SimpleNamespace(runtime="lead", teams=(TEAM,), roles=("lead_host", "worker_host"))
    host = AgentProcessHost(layout=SimpleNamespace(), profile=profile, team=TEAM, logger=logging.getLogger("test"))  # type: ignore[arg-type]
    with pytest.raises(AgentUnreachable) as not_running:
        await host.delete_person_data(ALICE_REF)
    assert not_running.value.clears is False

    loop = asyncio.get_running_loop()
    host.loop = loop
    host._joined = asyncio.Event()
    with pytest.raises(AgentUnreachable) as not_joined:
        await host.delete_person_data(ALICE_REF)
    assert not_joined.value.clears is True

    requests: list[tuple[str, dict[str, Any], dict[str, str], str]] = []

    async def fake_request(kind: str, body: dict[str, Any], audience: dict[str, str], *, team: str, timeout: Any = None) -> Any:
        requests.append((kind, dict(body), dict(audience), team))
        return SimpleNamespace(raw={"body": {
            "outcome": "ok",
            "owners_removed": 1,
            "blobs_deleted": 1,
            "blobs_kept": 0,
            "dlq_copies_deleted": 2,
            "dlq_copies_kept": 1,
        }})

    host._joined.set()
    host.host = SimpleNamespace(
        identity=SimpleNamespace(installation="install_test"),
        installation_contract=None,
        live=SimpleNamespace(request=fake_request),
    )  # type: ignore[assignment]
    reply = await host.delete_person_data(ALICE_REF, request_id="01950000-0000-7000-8000-000000000077")
    assert reply["outcome"] == "ok"
    assert requests == [
        (
            "team.delete_person_data",
            {"request_id": "01950000-0000-7000-8000-000000000077", "person": ALICE_REF},
            {"installation": "install_test", "team": TEAM, "target": "team_services", "id": TEAM},
            TEAM,
        )
    ]

    # 2. settle_person resolves person_ref via people_directory(agent).person_ref(person_id)
    #    and treats both ok and retry_later as accepted.
    storage = _storage(tmp_path, "manager_departure.db")
    access = AccessRepository(storage)
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))

    class _FakeOpsRuntime:
        async def run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

    class _FakeScheduleStore:
        def all_rows(self) -> list[dict[str, Any]]:
            return []

    agent = SimpleNamespace(
        id="lead-1",
        team_id=TEAM,
        is_team_lead=True,
        task_manager=SimpleNamespace(
            storage_settings=storage,
            operations_runtime=_FakeOpsRuntime(),
            list_user_owned_objectives=lambda **_kwargs: [],
            list_task_infos=lambda **_kwargs: [],
            list_active_tasks=lambda: [],
            list_queued_tasks=lambda: [],
            list_tasks=lambda **_kwargs: [],
            list_delegated_tasks=lambda **_kwargs: [],
        ),
        _ensure_schedule_store=lambda: _FakeScheduleStore(),
    )

    called_refs: list[str] = []

    class _FakeHost:
        team = TEAM

        async def delete_person_data(self, person_ref: str) -> dict[str, Any]:
            called_refs.append(person_ref)
            return {
                "outcome": "retry_later",
                "owners_removed": 1,
                "blobs_deleted": 1,
                "blobs_kept": 0,
                "dlq_copies_deleted": 2,
                "dlq_copies_kept": 3,
            }

    configure_process_host(_FakeHost())
    try:
        outcome = await settle_person(agent, ALICE_ID, command_id="cmd_1")
    finally:
        configure_process_host(None)

    assert outcome.standing is PersonStanding.DISABLED
    assert outcome.problems == []
    assert called_refs == [ALICE_REF]

    # 3. Startup sweep (command_id=None) defers quietly when host has not joined yet (clears=True).
    class _UnjoinedHost:
        team = TEAM

        async def delete_person_data(self, _person_ref: str) -> dict[str, Any]:
            raise AgentUnreachable("not joined yet", clears=True)

    configure_process_host(_UnjoinedHost())
    try:
        startup_outcome = await settle_person(agent, ALICE_ID, command_id=None)
    finally:
        configure_process_host(None)
    assert startup_outcome.problems == []


@pytest.mark.asyncio
async def test_c10_register_mismatch_or_missing_chunks_preserves_null_target_nuid_pending_delete(
    tmp_path: Path,
) -> None:
    """C10: Mismatched size or missing chunks during register must NOT drop a target_nuid=None pending delete,
    whereas a known old target_nuid whose chunks were purged IS cleared immediately."""

    js = FakeJetStream()
    clock = _Clock()
    storage = _storage(tmp_path)
    repo = BlobIndexRepository(storage)
    people = _seed_people(storage, [_person(ALICE_ID, ALICE_REF), _person(BOB_ID, BOB_REF)])
    access = AccessRepository(storage)
    service = BlobService(repository=repo, people=people, js=js, team_id=TEAM, clock=clock)

    def _chunk_count(nuid: str) -> int:
        subj = blob_chunk_subject(nuid)
        return sum(1 for msg in js.messages if msg.subject == subj)

    # Case 1: target_nuid=None pending delete (expired or person_departed on a system-uploaded blob).
    payload = b"shared-blob-payload-c10"
    put = await put_blob(js, TEAM, payload, BlobOwner(agent_id="system_agent"))
    reg = await service.register(
        sha256=put.sha256,
        size=len(payload),
        owner=BlobOwner(request_id="req_alice_c10", person_ref=ALICE_REF),
    )
    assert reg.outcome == "ok" and reg.present is True

    # Depart alice and queue the DB-level pending delete (target_nuid=None), simulating the window
    # after remove_person_owners releases _lock and before _execute_pending_delete_locked runs.
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    removal = repo.remove_person_owners(TEAM, ALICE_REF, clock())
    assert removal.unowned_sha256s == (put.sha256,)
    pending_before = repo.get_pending_delete(TEAM, put.sha256)
    assert pending_before is not None and pending_before.target_nuid is None

    # A concurrent blobs.register with a mismatched size must return present=False and KEEP the pending delete.
    mismatch = await service.register(
        sha256=put.sha256,
        size=len(payload) + 1,
        owner=BlobOwner(request_id="req_bob_mismatch", person_ref=BOB_REF),
    )
    assert mismatch.outcome == "ok" and mismatch.present is False
    pending_after_mismatch = repo.get_pending_delete(TEAM, put.sha256)
    assert pending_after_mismatch is not None and pending_after_mismatch.target_nuid is None

    # Now drop chunks so check_chunks_present returns False; register with matching size must still keep it.
    await js.purge_stream("OBJ_UA1_BLOBS", subject=blob_chunk_subject(put.nuid))
    missing_chunks = await service.register(
        sha256=put.sha256,
        size=len(payload),
        owner=BlobOwner(request_id="req_bob_missing", person_ref=BOB_REF),
    )
    assert missing_chunks.outcome == "ok" and missing_chunks.present is False
    assert repo.get_pending_delete(TEAM, put.sha256) is not None

    # delete_person then executes the still-queued pending delete and removes the unowned blob in NATS.
    del_res = await service.delete_person(ALICE_REF)
    assert del_res.outcome == "ok" and del_res.blobs_deleted == 1
    assert repo.get_pending_delete(TEAM, put.sha256) is None
    meta_after = await stat_blob(js, TEAM, put.sha256)
    assert meta_after is not None and meta_after.deleted is True

    # Case 2: target_nuid is set to an older nuid and newer broken metadata exists under a fresh nuid.
    payload2 = b"replaced-nuid-payload-c10"
    old_put = await put_blob(js, TEAM, payload2, BlobOwner(request_id="req_old", person_ref=BOB_REF))
    repo.queue_pending_delete(
        TEAM,
        old_put.sha256,
        requested_at_utc=clock(),
        reason="expired",
        target_nuid=old_put.nuid,
    )
    # Overwrite with broken live metadata under a fresh nuid (missing chunks).
    await js.purge_stream("OBJ_UA1_BLOBS", subject=blob_chunk_subject(old_put.nuid))
    new_put = await put_blob(js, TEAM, payload2, BlobOwner(request_id="req_new", person_ref=BOB_REF))
    assert new_put.nuid != old_put.nuid
    await js.purge_stream("OBJ_UA1_BLOBS", subject=blob_chunk_subject(new_put.nuid))
    # Re-plant an old chunk on old_put.nuid to confirm it gets purged and pending delete clears immediately.
    await js.publish(blob_chunk_subject(old_put.nuid), b"old-chunk")

    res2 = await service.register(
        sha256=old_put.sha256,
        size=len(payload2),
        owner=BlobOwner(request_id="req_bob_check", person_ref=BOB_REF),
    )
    assert res2.outcome == "ok" and res2.present is False
    assert repo.get_pending_delete(TEAM, old_put.sha256) is None
    assert _chunk_count(old_put.nuid) == 0


@pytest.mark.asyncio
async def test_c11_departed_raced_upload_delete_is_crash_safe_and_handles_concurrent_write(
    tmp_path: Path,
) -> None:
    """C11: Departed raced-upload deletion queues durable target_nuid pending delete before deleting,
    and _reconcile_pending_during_register_locked handles BlobConcurrentWrite cleanly."""

    js = FakeJetStream()
    clock = _Clock()
    storage = _storage(tmp_path)
    repo = BlobIndexRepository(storage)
    people = _seed_people(storage, [_person(ALICE_ID, ALICE_REF), _person(BOB_ID, BOB_REF)])
    access = AccessRepository(storage)
    service = BlobService(repository=repo, people=people, js=js, team_id=TEAM, clock=clock)

    def _chunk_count(nuid: str) -> int:
        subj = blob_chunk_subject(nuid)
        return sum(1 for msg in js.messages if msg.subject == subj)

    # 1. Departed person races an upload before register is refused; purge_stream fails after deleted marker.
    payload = b"departed-raced-secret-c11"
    put = await put_blob(js, TEAM, payload, BlobOwner(request_id="req_raced_c11", person_ref=ALICE_REF))
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.REMOVED))

    real_purge = js.purge_stream
    fail_once = {"fail": True}

    async def flaky_purge(stream: str, *, subject: Optional[str] = None, keep: Optional[int] = None) -> Any:
        if fail_once["fail"] and subject == blob_chunk_subject(put.nuid):
            fail_once["fail"] = False
            raise RuntimeError("transient NATS error during chunk purge")
        return await real_purge(stream, subject=subject, keep=keep)

    js.purge_stream = flaky_purge  # type: ignore[method-assign]

    refused = await service.register(
        sha256=put.sha256,
        size=len(payload),
        owner=BlobOwner(request_id="req_raced_c11", person_ref=ALICE_REF),
    )
    assert refused.outcome == "not_allowed"
    # Marker was published, chunk purge failed, and a durable pending delete row records target_nuid.
    meta_mid = await stat_blob(js, TEAM, put.sha256)
    assert meta_mid is not None and meta_mid.deleted is True
    assert _chunk_count(put.nuid) == 1
    pending = repo.get_pending_delete(TEAM, put.sha256)
    assert pending is not None
    assert pending.target_nuid == put.nuid
    assert pending.reason == "person_departed"
    assert pending.person_ref == ALICE_REF

    # Retry recovers immediately on restart/upkeep and purges the departed person's chunks.
    completed = await service.retry_pending_deletes()
    assert completed == 1
    assert repo.get_pending_delete(TEAM, put.sha256) is None
    assert _chunk_count(put.nuid) == 0

    # 2. Concurrent put_blob overwriting broken metadata during _reconcile_pending_during_register_locked
    # triggers BlobConcurrentWrite on delete_blob; _delete_target_nuid_locked purges old nuid cleanly.
    payload2 = b"concurrent-overwrite-c11"
    old_put = await put_blob(js, TEAM, payload2, BlobOwner(request_id="req_old_c11", person_ref=ALICE_REF))
    repo.queue_pending_delete(
        TEAM,
        old_put.sha256,
        requested_at_utc=clock(),
        reason="person_departed",
        target_nuid=old_put.nuid,
        person_ref=ALICE_REF,
    )
    meta_subj = blob_metadata_subject(blob_object_name(TEAM, old_put.sha256))
    real_publish = js.publish
    raced = {"done": False, "new_nuid": ""}

    async def racing_publish(subject: str, data: bytes, *, headers: Optional[dict[str, str]] = None) -> Any:
        if not raced["done"] and subject == meta_subj and json.loads(data.decode("utf-8")).get("deleted"):
            raced["done"] = True
            # Simulate a concurrent writer overwriting metadata with a fresh nuid right before CAS.
            await js.purge_stream("OBJ_UA1_BLOBS", subject=blob_chunk_subject(old_put.nuid))
            replacement = await put_blob(js, TEAM, payload2, BlobOwner(request_id="req_bob_live", person_ref=BOB_REF))
            raced["new_nuid"] = replacement.nuid
            await real_publish(blob_chunk_subject(old_put.nuid), b"leftover-old-chunk")
        return await real_publish(subject, data, headers=headers)

    js.publish = racing_publish  # type: ignore[method-assign]

    res = await service.register(
        sha256=old_put.sha256,
        size=len(payload2),
        owner=BlobOwner(request_id="req_bob_live", person_ref=BOB_REF),
    )
    assert res.outcome == "ok" and res.present is False
    assert repo.get_pending_delete(TEAM, old_put.sha256) is None
    assert _chunk_count(old_put.nuid) == 0
    # Newer live upload stays intact and registers cleanly on the retry.
    res_retry = await service.register(
        sha256=old_put.sha256,
        size=len(payload2),
        owner=BlobOwner(request_id="req_bob_live", person_ref=BOB_REF),
    )
    assert res_retry.outcome == "ok" and res_retry.present is True
    assert await get_blob(js, TEAM, old_put.sha256) == payload2


@pytest.mark.asyncio
async def test_f1_team_services_refuses_to_purge_aliased_nuid_across_delete_paths(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """F1 (SEC-1): Cross-team NUID aliasing cannot erase victim chunks via register, delete_person, or retry."""

    js = FakeJetStream()
    clock = _Clock()
    storage = _storage(tmp_path)
    repo = BlobIndexRepository(storage)
    people = _seed_people(storage, [_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED), _person(BOB_ID, BOB_REF)])
    service = BlobService(repository=repo, people=people, js=js, team_id=TEAM, clock=clock)

    victim_team = "team_victim"
    victim_bytes = b"victim confidential bytes " * 40
    victim_put = await put_blob(
        js, victim_team, victim_bytes, BlobOwner(request_id="req_vic", person_ref=BOB_REF),
        nuid_factory=lambda: "nuid_victim_shared",
    )

    # Attacker publishes forged metadata under TEAM referencing victim's nuid, attributed to departed Alice.
    forged_bytes = b"forged attacker bytes"
    forged_sha = sha256_unpadded(forged_bytes)
    forged_name = blob_object_name(TEAM, forged_sha)
    forged_meta = {
        "name": forged_name,
        "bucket": "UA1_BLOBS",
        "nuid": victim_put.nuid,
        "size": len(forged_bytes),
        "chunks": 1,
        "digest": f"SHA-256={forged_sha}=",
        "headers": BlobOwner(request_id="req_atk", person_ref=ALICE_REF).to_headers(TEAM, forged_sha),
        "options": {"max_chunk_size": 131_072},
    }
    await js.publish(blob_metadata_subject(forged_name), json.dumps(forged_meta).encode("utf-8"))

    # Trigger departed-person raced-upload cleanup through blobs.register -> refuses AND leaves victim intact!
    with caplog.at_level("WARNING"):
        refused = await service.register(
            sha256=forged_sha,
            size=len(forged_bytes),
            owner=BlobOwner(request_id="req_atk", person_ref=ALICE_REF),
        )
    assert refused.outcome == "not_allowed"
    assert "event=blob_purge_skipped_shared_nuid" in caplog.text
    assert await get_blob(js, victim_team, victim_put.sha256) == victim_bytes
    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, forged_sha)


@pytest.mark.asyncio
async def test_f3_stale_sweep_snapshot_never_clobbers_concurrent_pending_delete_or_reupload(
    tmp_path: Path,
) -> None:
    """F3 (CR-1): _cleanup_unindexed_entry skips when pending_delete exists, metadata moved, or person re-enabled."""

    from custom_transport.protocol1.blobs import BlobMetadataEntry

    js = FakeJetStream()
    clock = _Clock()
    storage = _storage(tmp_path)
    repo = BlobIndexRepository(storage)
    access = AccessRepository(storage)
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    people = PeopleDirectory(access, TEAM)
    service = BlobService(repository=repo, people=people, js=js, team_id=TEAM, clock=clock)

    payload = b"stale-snapshot-blob"
    old_put = await put_blob(
        js, TEAM, payload, BlobOwner(request_id="req_old", person_ref=ALICE_REF), nuid_factory=lambda: "nuid_stale_1"
    )
    stale_entry = BlobMetadataEntry(
        subject=blob_metadata_subject(blob_object_name(TEAM, old_put.sha256)),
        seq=old_put.seq,
        timestamp=clock.now - timedelta(days=2),
        team=TEAM,
        sha256=old_put.sha256,
        metadata=old_put.metadata,
    )

    # 1. If a concurrent delete_person queued a pending_delete, stale snapshot must abort without touching it.
    repo.queue_pending_delete(
        TEAM, old_put.sha256, requested_at_utc=clock.now, reason="person_departed", target_nuid="nuid_newer_2",
        person_ref=ALICE_REF,
    )
    assert await service._cleanup_unindexed_entry(stale_entry, cutoff=clock.now - timedelta(days=1)) is False
    still_pending = repo.get_pending_delete(TEAM, old_put.sha256)
    assert still_pending is not None and still_pending.target_nuid == "nuid_newer_2"
    repo.complete_pending_delete(TEAM, old_put.sha256)

    # 2. If person was re-enabled before lock acquisition and blob is not too_old, abort cleanly.
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.ACTIVE))
    fresh_entry = BlobMetadataEntry(
        subject=stale_entry.subject,
        seq=old_put.seq,
        timestamp=clock.now,
        team=TEAM,
        sha256=old_put.sha256,
        metadata=old_put.metadata,
    )
    assert await service._cleanup_unindexed_entry(fresh_entry, cutoff=clock.now - timedelta(days=1)) is False
    assert await get_blob(js, TEAM, old_put.sha256) == payload

    # 3. If metadata moved to a newer seq/nuid after snapshot was taken, abort without deleting newer upload.
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    await js.purge_stream("OBJ_UA1_BLOBS", subject=blob_chunk_subject("nuid_stale_1"))
    new_put = await put_blob(
        js, TEAM, payload, BlobOwner(request_id="req_new", person_ref=ALICE_REF), nuid_factory=lambda: "nuid_live_2"
    )
    assert new_put.seq != old_put.seq
    assert await service._cleanup_unindexed_entry(stale_entry, cutoff=clock.now - timedelta(days=1)) is False
    assert await get_blob(js, TEAM, old_put.sha256) == payload


@pytest.mark.asyncio
async def test_f4_full_store_batch_retries_small_blobs_and_maintain_retries_after_orphan_sweep(
    tmp_path: Path,
) -> None:
    """F4 (FA-1): Multi-blob delete retries BlobStoreFull items once larger deletes or orphan sweep free space."""

    js = FakeJetStream()
    clock = _Clock()
    storage = _storage(tmp_path)
    repo = BlobIndexRepository(storage)
    access = AccessRepository(storage)
    access.record_person(_person(ALICE_ID, ALICE_REF))
    people = PeopleDirectory(access, TEAM)
    service = BlobService(repository=repo, people=people, js=js, team_id=TEAM, clock=clock)

    small_bytes = b"tiny"
    large_bytes = b"L" * 8000
    small_ref = await service.put_and_register(
        small_bytes, BlobOwner(request_id="req_s", person_ref=ALICE_REF), media_type="text/plain"
    )
    large_ref = await service.put_and_register(
        large_bytes, BlobOwner(request_id="req_l", person_ref=ALICE_REF), media_type="text/plain"
    )

    # Lower max_bytes below current usage so deleting the small blob alone fails BlobStoreFull,
    # while deleting the large blob frees 8 KB so the second pass deletes the small blob!
    js.max_bytes = 2500
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    res = await service.delete_person(ALICE_REF)
    assert res.outcome == "ok"
    assert res.blobs_deleted == 2
    assert repo.list_pending_deletes(TEAM) == ()
    assert await stat_blob(js, TEAM, small_ref["sha256"]) is None or (await stat_blob(js, TEAM, small_ref["sha256"])).deleted  # type: ignore[union-attr]
    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, large_ref["sha256"])

    # Also test maintain() retrying pending deletes after sweep_orphan_chunks() frees space!
    js2 = FakeJetStream(now=clock.now)
    storage2 = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "m2.db")}})
    repo2 = BlobIndexRepository(storage2)
    people2 = _seed_people(storage2, [_person(BOB_ID, BOB_REF)])
    service2 = BlobService(repository=repo2, people=people2, js=js2, team_id=TEAM, clock=clock)

    await js2.publish(blob_chunk_subject("nuid_huge_orphan"), b"O" * 10_000)
    bob_ref_blob = await service2.put_and_register(
        b"x", BlobOwner(request_id="req_bob_tiny", person_ref=BOB_REF), media_type="text/plain"
    )
    repo2.remove_person_owners(TEAM, BOB_REF, clock.now)
    js2.max_bytes = 2000  # Too full until orphan chunk is swept!
    clock.advance(days=2)
    report = await service2.maintain()
    assert report.orphan_chunks_purged == 1
    assert report.pending_deletes_completed == 1
    assert repo2.list_pending_deletes(TEAM) == ()
    assert await stat_blob(js2, TEAM, bob_ref_blob["sha256"]) is None or (await stat_blob(js2, TEAM, bob_ref_blob["sha256"])).deleted  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_f5_delete_person_drains_expired_single_person_and_unattributed_pending_deletes(
    tmp_path: Path,
) -> None:
    """F5 (DI-1): remove_expired_owners preserves single-person person_ref and delete_person drains NULL pending rows."""

    js = FakeJetStream()
    clock = _Clock()
    storage = _storage(tmp_path)
    repo = BlobIndexRepository(storage)
    access = AccessRepository(storage)
    access.record_person(_person(ALICE_ID, ALICE_REF))
    access.record_person(_person(BOB_ID, BOB_REF))
    people = PeopleDirectory(access, TEAM)
    service = BlobService(repository=repo, people=people, js=js, team_id=TEAM, retention_days=7, clock=clock)

    alice_only = await service.put_and_register(
        b"alice-expired-blob", BlobOwner(request_id="req_a1", person_ref=ALICE_REF), media_type="text/plain"
    )
    shared = await service.put_and_register(
        b"alice-and-bob-expired", BlobOwner(request_id="req_a2", person_ref=ALICE_REF), media_type="text/plain"
    )
    await service.put_and_register(
        b"alice-and-bob-expired", BlobOwner(request_id="req_b2", person_ref=BOB_REF), media_type="text/plain"
    )

    # Simulate expire_blobs failing after DB removal (pending_deletes queued, blobs still live in NATS).
    clock.advance(days=8)
    removed, unowned = repo.remove_expired_owners(TEAM, clock.now)
    assert removed == 3 and set(unowned) == {alice_only["sha256"], shared["sha256"]}
    # Single-person expired blob preserves ALICE_REF; multi-person expired blob has person_ref=None.
    p_alice = repo.get_pending_delete(TEAM, alice_only["sha256"])
    p_shared = repo.get_pending_delete(TEAM, shared["sha256"])
    assert p_alice is not None and p_alice.person_ref == ALICE_REF
    assert p_shared is not None and p_shared.person_ref is None

    # Alice departs -> delete_person drains BOTH her attributed expired row AND unattributed pending rows!
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    del_res = await service.delete_person(ALICE_REF)
    assert del_res.outcome == "ok" and del_res.blobs_deleted == 2
    assert repo.list_pending_deletes(TEAM) == ()
    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, alice_only["sha256"])
    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, shared["sha256"])


@pytest.mark.asyncio
async def test_f8_blobs_register_refuses_request_owner_without_person(tmp_path: Path) -> None:
    """F8 (SEC-5): owner.type == 'request' without person is refused at repo and BlobOperations."""

    storage = _storage(tmp_path)
    repo = BlobIndexRepository(storage)
    people = _seed_people(storage, [_person(ALICE_ID, ALICE_REF)])
    service = BlobService(repository=repo, people=people, js=FakeJetStream(), team_id=TEAM)
    ops = BlobOperations(service, people)
    sha = sha256_unpadded(b"no-person-request")

    with pytest.raises(ValueError, match="person_ref"):
        repo.register_owner(
            team_id=TEAM, sha256=sha, owner_kind="request", owner_id="req_orphan",
            person_ref=None, size_bytes=17, registered_at_utc=START, expires_at_utc=START + timedelta(days=7),
        )

    reply = await ops.handle_register(
        _verified_frame(
            "blobs.register",
            {
                "request_id": "reg_no_person",
                "sha256": sha,
                "size": 17,
                "owner": {"type": "request", "request_id": "req_orphan"},
            },
        )
    )
    assert reply["outcome"] == "not_allowed"


@pytest.mark.asyncio
async def test_f12_put_and_register_and_client_put_never_abandon_final_upload(tmp_path: Path) -> None:
    """F12: Pre-check runs once, then each put_blob upload is followed by a register check (no abandoned final upload)."""

    from custom_transport.protocol1.blobs import BlobConcurrentWrite
    from custom_transport.protocol1.knowledge_blobs import ClientBlobStore

    storage = _storage(tmp_path)
    people = _seed_people(storage, [_person(ALICE_ID, ALICE_REF)])
    js = FakeJetStream()
    service = BlobService(repository=BlobIndexRepository(storage), people=people, js=js, team_id=TEAM)
    owner = BlobOwner(request_id="req_f12", person_ref=ALICE_REF)
    events: list[str] = []

    async def always_absent_register(**_kw: Any) -> Any:
        events.append("register")
        js.messages.clear()  # Simulate concurrent deletion so every retry actually uploads fresh bytes.
        from team_services.blobs import RegisterResult
        return RegisterResult(outcome="ok", present=False)

    service.register = always_absent_register  # type: ignore[method-assign]
    real_publish = js.publish

    async def track_publish(subject: str, data: bytes, *, headers: Optional[dict[str, str]] = None) -> Any:
        if subject.startswith("$O.UA1_BLOBS.M."):
            events.append("put_meta")
        return await real_publish(subject, data, headers=headers)

    js.publish = track_publish  # type: ignore[method-assign]
    with pytest.raises(BlobConcurrentWrite):
        await service.put_and_register(b"race-payload", owner, media_type="text/plain")
    # Sequence must be: pre-check register, then (put_meta, register) * 3 — ending with register, never put!
    assert events == ["register", "put_meta", "register", "put_meta", "register", "put_meta", "register"]

    # Same invariant for ClientBlobStore.put!
    client_events: list[str] = []
    js2 = FakeJetStream()
    real_publish2 = js2.publish

    async def track_publish2(subject: str, data: bytes, *, headers: Optional[dict[str, str]] = None) -> Any:
        if subject.startswith("$O.UA1_BLOBS.M."):
            client_events.append("put_meta")
        return await real_publish2(subject, data, headers=headers)

    js2.publish = track_publish2  # type: ignore[method-assign]

    async def client_register(_body: dict[str, Any]) -> dict[str, Any]:
        client_events.append("register")
        js2.messages.clear()  # Simulate concurrent deletion so every retry actually uploads fresh bytes.
        return {"outcome": "ok", "present": False}

    async def _get_js2() -> Any:
        return js2

    client = ClientBlobStore(get_js=_get_js2, team=TEAM, register=client_register, request_ids=lambda: "req_c")
    with pytest.raises(BlobConcurrentWrite):
        await client.put(b"race-payload-2", media_type="text/plain", owner=owner)
    assert client_events == ["register", "put_meta", "register", "put_meta", "register", "put_meta", "register"]


@pytest.mark.asyncio
async def test_f16_f17_f18_prepare_cleans_departed_lock_rechecks_standing_and_rejects_owner_conflict(
    tmp_path: Path,
) -> None:
    """F16 (prepare cleans departed), F17 (delete_person checks standing under lock), F18 (owner person conflict)."""

    js = FakeJetStream()
    clock = _Clock()
    storage = _storage(tmp_path)
    repo = BlobIndexRepository(storage)
    access = AccessRepository(storage)
    access.record_person(_person(ALICE_ID, ALICE_REF))
    access.record_person(_person(BOB_ID, BOB_REF))
    people = PeopleDirectory(access, TEAM)
    service = BlobService(repository=repo, people=people, js=js, team_id=TEAM, clock=clock)

    payload = b"f16-f17-f18-payload"
    ref = await service.put_and_register(
        payload, BlobOwner(request_id="req_shared_id", person_ref=ALICE_REF), media_type="text/plain"
    )

    # F18: Reusing the same (owner_kind, owner_id) for the same blob with a different person_ref is refused!
    with pytest.raises(BlobOwnerConflict):
        repo.register_owner(
            team_id=TEAM, sha256=ref["sha256"], owner_kind="request", owner_id="req_shared_id",
            person_ref=BOB_REF, size_bytes=len(payload), registered_at_utc=clock.now,
            expires_at_utc=clock.now + timedelta(days=7),
        )
    conflict_reg = await service.register(
        sha256=ref["sha256"],
        size=len(payload),
        owner=BlobOwner(request_id="req_shared_id", person_ref=BOB_REF),
    )
    assert conflict_reg.outcome == "not_allowed"

    # F17: If Alice is disabled then re-enabled while delete_person waits on _lock, delete_person refuses!
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    async with service._lock:
        del_task = asyncio.create_task(service.delete_person(ALICE_REF))
        await asyncio.sleep(0.02)
        access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.ACTIVE))
    del_refused = await del_task
    assert del_refused.outcome == "not_allowed"
    assert await get_blob(js, TEAM, ref["sha256"]) == payload

    # F16: If Alice departs while team_services is down, prepare() cleans up her indexed blobs immediately!
    access.record_person(_person(ALICE_ID, ALICE_REF, PersonStanding.DISABLED))
    await service.prepare()
    assert not repo.has_owners(TEAM, ref["sha256"])
    with pytest.raises(BlobDeleted):
        await get_blob(js, TEAM, ref["sha256"])
