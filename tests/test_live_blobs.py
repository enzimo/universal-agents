"""Live tests for UA1_BLOBS on nats-server 2.15.1 (operator-mode permissions) and PostgreSQL (A6).

Covers:
- Operator-mode permission enforcement using real generated role JWTs: edge_adapter can read
  blobs via direct STREAM.MSG.GET (S2) and cannot write, create consumers, list streams, or
  purge; writers cannot purge or rollup; tampered chunk bytes are refused with BlobCorrupted;
  deleted blobs are refused with BlobDeleted.
- OBJ_UA1_BLOBS stream bounds (S4) provisioned by InstallationAuthority: max_bytes,
  max_consumers, discard=new, BlobStoreFull refusal without evicting live blobs, and
  delete_blob space reclaim on a full store (C09).
- Multi-owner retention expiry, immediate person-departure deletion (A4), FA-6 orphan and
  deleted-marker chunk sweeping, and CR-3 concurrent reuse vs deletion on live NATS.
- Persistent PostgreSQL blob index operations on a real PostgreSQL instance (LIVE_TEST_POSTGRES_DSN).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from contextlib import AsyncExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import nats.errors
import nats.js.errors
import pytest
from nats.js.api import DiscardPolicy, StreamConfig
from nats.nuid import NUID

from access_control.people_directory import PeopleDirectory
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT
from custom_messages.wire.queues import object_store_stream_config, object_store_streams
from custom_transport.protocol1.blobs import (
    CHUNK_SUBJECT_PREFIX,
    STREAM_NAME,
    BlobCorrupted,
    BlobDeleted,
    BlobOwner,
    BlobStoreFull,
    blob_chunk_subject,
    blob_metadata_subject,
    blob_object_name,
    delete_blob,
    get_blob,
    put_blob,
    sha256_unpadded,
    stat_blob,
    sweep_orphan_chunks,
)
from custom_transport.protocol1.contract_files import transport as contract_transport
from custom_transport.protocol1.host_keys import load_authority_keys, load_host_keys
from custom_transport.protocol1.knowledge_blobs import TEXT_PLAIN_MEDIA_TYPE, ClientBlobStore
from docker import message_hosts
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from runtime_settings.instance_layout import InstanceLayout
from storage.access_repository import AccessRepository
from storage.blob_index_repository import BlobIndexRepository
from storage.config import StorageSettings, resolve_storage_settings
from team_services.blobs import BlobOperations, BlobService
from tests.queue_limits_support import queue_limits
from tests.team_services_support import checked_reply, stand_in_frame
from tests.test_live_message_contract import (
    TEAM as OP_TEAM,
    connect as connect_operator,
    isolated_broker,  # noqa: F401 - pytest fixture
)
from tests.test_live_team_services import (
    TEAM as LIVE_TEAM,
    broker,  # noqa: F401 - pytest fixture
    connect as connect_plain,
)

pytestmark = [pytest.mark.integration, pytest.mark.live]


def _tight_queue_limits(blob_max_bytes: int):
    return queue_limits(blobs=blob_max_bytes)


@pytest.mark.nats
@pytest.mark.asyncio
async def test_operator_mode_blob_permissions_tampered_digest_and_s2_enforcement(isolated_broker) -> None:  # noqa: F811
    """Real generated operator-mode role permissions enforce S2, read/write/delete separation, and digest checks."""

    transport = contract_transport()
    (blob_stream,) = object_store_streams(transport)
    stream_cfg = object_store_stream_config(transport, blob_stream, _tight_queue_limits(16 * 1024 * 1024))

    async with AsyncExitStack() as stack:
        admin = await connect_operator(stack, isolated_broker, "admin")
        worker = await connect_operator(stack, isolated_broker, "worker")
        gateway = await connect_operator(stack, isolated_broker, "gateway")
        edge = await connect_operator(stack, isolated_broker, "edge")
        services = await connect_operator(stack, isolated_broker, "services")

        await admin.nc.jetstream().add_stream(config=StreamConfig.from_response(stream_cfg))

        owner = BlobOwner(request_id="req_op_live_1", person_ref="p-AliceRef00000000000001")
        multi_chunk = random.Random(42).randbytes(3 * 131_072 + 123)
        put_res = await put_blob(worker.nc.jetstream(), OP_TEAM, multi_chunk, owner)
        assert put_res.chunks == 4 and not put_res.reused

        # Empty 0-byte blob also round-trips cleanly.
        empty_res = await put_blob(gateway.nc.jetstream(), OP_TEAM, b"", owner)
        assert empty_res.chunks == 0 and empty_res.size == 0

        # edge_adapter reads multi-chunk and empty blobs via S2 direct STREAM.MSG.GET (no consumers created).
        edge_js = edge.nc.jetstream()
        assert await get_blob(edge_js, OP_TEAM, put_res.sha256, expected_size=len(multi_chunk)) == multi_chunk
        assert await get_blob(edge_js, OP_TEAM, empty_res.sha256, expected_size=0) == b""
        assert (await admin.nc.jetstream().stream_info(STREAM_NAME)).state.consumer_count == 0

        # S2: edge_adapter cannot create consumers, list stream names, write chunks/metadata, or purge.
        await edge.publish_refused(f"$JS.API.CONSUMER.CREATE.{STREAM_NAME}")
        await edge.publish_refused(f"{CHUNK_SUBJECT_PREFIX}{NUID().next().decode()}")
        await edge.publish_refused(blob_metadata_subject(blob_object_name(OP_TEAM, put_res.sha256)))
        await edge.publish_refused(f"$JS.API.STREAM.PURGE.{STREAM_NAME}")
        bare_store = await edge.nc.jetstream(timeout=1).object_store("UA1_BLOBS")
        with pytest.raises(nats.errors.TimeoutError):
            await bare_store.get(blob_object_name(OP_TEAM, put_res.sha256))
        await edge.refused("$JS.API.STREAM.NAMES")

        # Writers cannot purge or use Nats-Rollup headers.
        await worker.publish_refused(f"$JS.API.STREAM.PURGE.{STREAM_NAME}")
        with pytest.raises(nats.js.errors.APIError) as rollup_err:
            await worker.nc.jetstream().publish(
                blob_metadata_subject(blob_object_name(OP_TEAM, put_res.sha256)),
                b"{}",
                headers={"Nats-Rollup": "all"},
            )
        assert rollup_err.value.err_code == 10111

        # Tampered chunk bytes under a forged metadata nuid are refused with BlobCorrupted.
        tampered_nuid = NUID().next().decode()
        original = b"authentic bytes for digest check"
        tampered = b"TAMPERED! bytes for digest check"
        orig_sha = sha256_unpadded(original)
        orig_name = blob_object_name(OP_TEAM, orig_sha)
        admin_js = admin.nc.jetstream()
        await admin_js.publish(blob_chunk_subject(tampered_nuid), tampered)
        bad_meta = {
            "name": orig_name,
            "bucket": "UA1_BLOBS",
            "nuid": tampered_nuid,
            "size": len(original),
            "chunks": 1,
            "digest": f"SHA-256={orig_sha}=",
            "headers": owner.to_headers(OP_TEAM, orig_sha),
            "options": {"max_chunk_size": 131_072},
        }
        await admin_js.publish(blob_metadata_subject(orig_name), json.dumps(bad_meta).encode("utf-8"))
        with pytest.raises(BlobCorrupted, match="SHA-256 mismatch"):
            await get_blob(edge_js, OP_TEAM, orig_sha)

        # Only team_services deletes: deleted marker + chunk purge -> edge gets BlobDeleted.
        deleted = await delete_blob(services.nc.jetstream(), OP_TEAM, put_res.sha256)
        assert deleted.deleted and not deleted.already_deleted
        with pytest.raises(BlobDeleted):
            await get_blob(edge_js, OP_TEAM, put_res.sha256)
        assert worker.errors.empty() and gateway.errors.empty() and edge.errors.empty() and services.errors.empty()


@pytest.mark.nats
@pytest.mark.asyncio
async def test_obj_ua1_blobs_stream_bounds_full_refusal_and_delete_reclaim(tmp_path: Path, broker: str) -> None:  # noqa: F811
    """InstallationAuthority enforces OBJ_UA1_BLOBS bounds (S4); full store refuses writes and reclaims on delete."""

    layout = InstanceLayout(tmp_path / "instance")
    message_hosts.prepare_message_host_trust(layout, {}, partitions=DEFAULT_PARTITION_COUNT)
    blob_cap = 1024 * 1024
    authority = InstallationAuthority(
        layout,
        load_authority_keys(layout),
        load_host_keys(layout, AUTHORITY_RUNTIME),
        partitions=DEFAULT_PARTITION_COUNT,
        queues=_tight_queue_limits(blob_cap),
    )

    async with AsyncExitStack() as stack:
        nc = await connect_plain(stack, broker, AUTHORITY_RUNTIME)
        await authority.start(nc, teams=[LIVE_TEAM])
        js = nc.jetstream()

        info = await js.stream_info(STREAM_NAME)
        assert info.config.max_bytes == blob_cap
        assert info.config.max_consumers == 64
        assert info.config.discard == DiscardPolicy.NEW
        assert info.config.allow_rollup_hdrs is False
        assert info.config.deny_delete is True

        owner = BlobOwner(request_id="req_bounds_1", person_ref="p-OwnerRef00000000000001")
        first = random.Random(1).randbytes(600_000)
        res1 = await put_blob(js, LIVE_TEAM, first, owner)
        assert await get_blob(js, LIVE_TEAM, res1.sha256) == first

        # Second 600 KB blob exceeds 1 MiB cap -> BlobStoreFull without evicting first.
        second = random.Random(2).randbytes(600_000)
        with pytest.raises(BlobStoreFull):
            await put_blob(js, LIVE_TEAM, second, owner)
        assert await get_blob(js, LIVE_TEAM, res1.sha256) == first

        # Fill remaining slack with small blobs until even a metadata publish would hit max_bytes.
        filler_idx = 0
        while True:
            filler_idx += 1
            try:
                await put_blob(js, LIVE_TEAM, f"pad-{filler_idx}-".encode() + b"z" * 20_000, owner)
            except BlobStoreFull:
                break

        # Deleting the first blob while the store is full reclaims chunks first and still writes the deleted marker.
        del_res = await delete_blob(js, LIVE_TEAM, res1.sha256)
        assert del_res.deleted
        with pytest.raises(BlobDeleted):
            await get_blob(js, LIVE_TEAM, res1.sha256)

        # Space is now reclaimed, so storing a fresh 300 KB blob succeeds.
        third = random.Random(3).randbytes(300_000)
        res3 = await put_blob(js, LIVE_TEAM, third, owner)
        assert await get_blob(js, LIVE_TEAM, res3.sha256) == third


@pytest.mark.nats
@pytest.mark.asyncio
async def test_live_multi_owner_retention_departure_deletion_fa6_sweep_and_cr3_race(
    tmp_path: Path, broker: str,  # noqa: F811
) -> None:
    """Real NATS 2.15.1 + BlobService: multi-owner departure, retention, FA-6 sweep, and CR-3 reuse vs delete."""

    layout = InstanceLayout(tmp_path / "instance")
    message_hosts.prepare_message_host_trust(layout, {}, partitions=DEFAULT_PARTITION_COUNT)
    authority = InstallationAuthority(
        layout,
        load_authority_keys(layout),
        load_host_keys(layout, AUTHORITY_RUNTIME),
        partitions=DEFAULT_PARTITION_COUNT,
        queues=_tight_queue_limits(16 * 1024 * 1024),
    )

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "blobs.db")}})
    access = AccessRepository(storage)
    clock_state = {"now": datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)}

    alice_ref = "p-AliceRef00000000000001"
    bob_ref = "p-BobRef0000000000000002"
    carol_ref = "p-CarolRef00000000000003"
    dave_ref = "p-DaveRef000000000000004"
    eve_ref = "p-EveRef0000000000000005"
    admin_ref = "p-AdminRef00000000000006"

    for pid, pref, roles in (
        ("alice", alice_ref, ("member",)),
        ("bob", bob_ref, ("member",)),
        ("carol", carol_ref, ("member",)),
        ("dave", dave_ref, ("member",)),
        ("eve", eve_ref, ("member",)),
        ("admin", admin_ref, ("owner", "admin")),
    ):
        access.record_person(TeamPersonV1(
            team_id=LIVE_TEAM, person_id=pid, person_ref=pref, standing=PersonStanding.ACTIVE,
            roles=roles, changed_at_utc=clock_state["now"],
        ))

    people = PeopleDirectory(access, LIVE_TEAM)

    async with AsyncExitStack() as stack:
        nc = await connect_plain(stack, broker, AUTHORITY_RUNTIME)
        await authority.start(nc, teams=[LIVE_TEAM])
        js = nc.jetstream()

        service = BlobService(
            repository=BlobIndexRepository(storage),
            people=people,
            js=js,
            team_id=LIVE_TEAM,
            retention_days=7,
            clock=lambda: clock_state["now"],
            logger=logging.getLogger("test.live_blobs"),
        )
        ops = BlobOperations(service, people, logging.getLogger("test.live_blobs"))

        reg_counter = {"n": 0}

        async def _register(body: dict[str, Any]) -> dict[str, Any]:
            frame = stand_in_frame(LIVE_TEAM, "blobs.register", body, person_ref=None, request_id=body["request_id"])
            return checked_reply("blobs.register", await ops.handle_register(frame))

        def _next_id() -> str:
            reg_counter["n"] += 1
            return f"live_reg_{reg_counter['n']}"

        client = ClientBlobStore(get_js=lambda: _async_val(js), team=LIVE_TEAM, register=_register, request_ids=_next_id)

        # 1. Multi-owner & immediate person-departure deletion (A4).
        shared_bytes = b"shared team document bytes " * 500
        alice_only_bytes = b"alice private knowledge bytes " * 500
        shared_ref = await client.put(
            shared_bytes, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=BlobOwner(request_id="req_alice_shared", person_ref=alice_ref)
        )
        shared_ref_bob = await client.put(
            shared_bytes, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=BlobOwner(request_id="req_bob_shared", person_ref=bob_ref)
        )
        assert shared_ref == shared_ref_bob
        alice_ref_blob = await client.put(
            alice_only_bytes, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=BlobOwner(request_id="req_alice_priv", person_ref=alice_ref)
        )

        # Alice departs -> alice_only_bytes deleted immediately; shared_bytes kept for Bob!
        access.record_person(TeamPersonV1(
            team_id=LIVE_TEAM, person_id="alice", person_ref=alice_ref, standing=PersonStanding.DISABLED,
            roles=(), changed_at_utc=clock_state["now"],
        ))
        del_alice_frame = stand_in_frame(
            LIVE_TEAM, "blobs.delete_person", {"request_id": "del_alice_1", "person": alice_ref},
            person_ref=admin_ref, request_id="del_alice_1",
        )
        del_alice_reply = checked_reply("blobs.delete_person", await ops.handle_delete_person(del_alice_frame))
        assert del_alice_reply == {"outcome": "ok", "owners_removed": 2, "blobs_deleted": 1, "blobs_kept": 1}
        with pytest.raises(BlobDeleted):
            await get_blob(js, LIVE_TEAM, alice_ref_blob["sha256"])
        assert await get_blob(js, LIVE_TEAM, shared_ref["sha256"]) == shared_bytes

        # Bob departs -> shared_bytes now has 0 owners and is deleted immediately!
        access.record_person(TeamPersonV1(
            team_id=LIVE_TEAM, person_id="bob", person_ref=bob_ref, standing=PersonStanding.REMOVED,
            roles=(), changed_at_utc=clock_state["now"],
        ))
        del_bob_frame = stand_in_frame(
            LIVE_TEAM, "blobs.delete_person", {"request_id": "del_bob_1", "person": bob_ref},
            person_ref=admin_ref, request_id="del_bob_1",
        )
        del_bob_reply = checked_reply("blobs.delete_person", await ops.handle_delete_person(del_bob_frame))
        assert del_bob_reply == {"outcome": "ok", "owners_removed": 1, "blobs_deleted": 1, "blobs_kept": 0}
        with pytest.raises(BlobDeleted):
            await get_blob(js, LIVE_TEAM, shared_ref["sha256"])

        # 2. Retention expiry deletes Carol's blob once retention_days passes.
        carol_bytes = b"carol expiring blob " * 400
        carol_ref_blob = await client.put(
            carol_bytes, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=BlobOwner(request_id="req_carol_1", person_ref=carol_ref)
        )
        assert await get_blob(js, LIVE_TEAM, carol_ref_blob["sha256"]) == carol_bytes
        clock_state["now"] = clock_state["now"] + timedelta(days=8)
        await service.maintain()
        with pytest.raises(BlobDeleted):
            await get_blob(js, LIVE_TEAM, carol_ref_blob["sha256"])

        # 3. FA-6 orphan chunk and deleted-marker chunk sweep on real NATS.
        live_bytes = b"live blob kept during sweep"
        live_ref = await client.put(
            live_bytes, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=BlobOwner(request_id="req_live_sweep", person_ref=dave_ref)
        )
        # Raw orphan chunk subject with no metadata.
        orphan_nuid = NUID().next().decode()
        await js.publish(blob_chunk_subject(orphan_nuid), b"abandoned writer chunk")
        # Interrupted delete: blob has chunks + a deleted:true marker written directly without chunk purge.
        interrupted_bytes = b"interrupted delete chunk payload"
        interrupted_put = await put_blob(
            js, LIVE_TEAM, interrupted_bytes, BlobOwner(request_id="req_interrupted_1", person_ref=dave_ref)
        )
        interrupted_meta = await stat_blob(js, LIVE_TEAM, interrupted_put.sha256)
        assert interrupted_meta is not None
        await js.publish(
            blob_metadata_subject(interrupted_meta.name),
            json.dumps(interrupted_meta.as_deleted_payload(), separators=(",", ":")).encode("utf-8"),
        )
        # Real NATS sets message timestamps to wall-clock UTC; sweep with future `now` past grace+skew.
        future_now = datetime.now(timezone.utc) + timedelta(days=2)
        sweep = await sweep_orphan_chunks(js, now=future_now)
        assert blob_chunk_subject(orphan_nuid) in sweep.purged_chunk_subjects
        assert blob_chunk_subject(interrupted_meta.nuid) in sweep.purged_chunk_subjects
        assert await get_blob(js, LIVE_TEAM, live_ref["sha256"]) == live_bytes

        # 4. CR-3 concurrent reuse (Dave) vs last-owner deletion (Eve departing) on the same bytes.
        race_bytes = b"cr3 concurrent reuse vs deletion payload " * 300
        race_ref = await client.put(
            race_bytes, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=BlobOwner(request_id="req_eve_race", person_ref=eve_ref)
        )
        access.record_person(TeamPersonV1(
            team_id=LIVE_TEAM, person_id="eve", person_ref=eve_ref, standing=PersonStanding.DISABLED,
            roles=(), changed_at_utc=clock_state["now"],
        ))
        dave_owner = BlobOwner(request_id="req_dave_race", person_ref=dave_ref)
        reuse_ref, _del_res = await asyncio.gather(
            client.put(race_bytes, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=dave_owner),
            service.delete_person(eve_ref),
        )
        assert reuse_ref == race_ref
        assert await get_blob(js, LIVE_TEAM, reuse_ref["sha256"]) == race_bytes
        assert service._repository.has_owners(LIVE_TEAM, reuse_ref["sha256"])


async def _async_val(value: Any) -> Any:
    return value


@contextmanager
def _postgres_blob_storage() -> Iterator[StorageSettings]:
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL blob index tests")
    schema = "blob_index_test_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.mark.postgres
def test_postgres_blob_index_owners_retention_departure_and_pending_deletes() -> None:
    """BlobIndexRepository works cleanly against a real PostgreSQL server."""

    with _postgres_blob_storage() as settings:
        repo = BlobIndexRepository(settings)
        try:
            now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
            sha_shared = sha256_unpadded(b"pg-shared")
            sha_alice = sha256_unpadded(b"pg-alice")
            alice_ref = "p-PgAliceRef000000000001"
            bob_ref = "p-PgBobRef00000000000002"

            repo.register_owner(
                team_id=LIVE_TEAM, sha256=sha_shared, owner_kind="request", owner_id="req_1",
                person_ref=alice_ref, size_bytes=9, registered_at_utc=now, expires_at_utc=now + timedelta(days=7),
            )
            repo.register_owner(
                team_id=LIVE_TEAM, sha256=sha_shared, owner_kind="request", owner_id="req_2",
                person_ref=bob_ref, size_bytes=9, registered_at_utc=now, expires_at_utc=now + timedelta(days=10),
            )
            repo.register_owner(
                team_id=LIVE_TEAM, sha256=sha_alice, owner_kind="request", owner_id="req_3",
                person_ref=alice_ref, size_bytes=8, registered_at_utc=now, expires_at_utc=now + timedelta(days=7),
            )
            assert set(repo.list_indexed_person_refs(LIVE_TEAM)) == {alice_ref, bob_ref}

            # Removing Alice leaves sha_alice unowned (queued for pending delete) and sha_shared kept for Bob.
            removed = repo.remove_person_owners(LIVE_TEAM, alice_ref, now)
            assert removed.owners_removed == 2
            assert removed.unowned_sha256s == (sha_alice,)
            assert removed.kept_sha256s == (sha_shared,)
            assert not repo.has_owners(LIVE_TEAM, sha_alice)
            assert repo.has_owners(LIVE_TEAM, sha_shared)

            pending = repo.list_pending_deletes(LIVE_TEAM)
            assert len(pending) == 1 and pending[0].sha256 == sha_alice and pending[0].target_nuid is None

            repo.set_pending_delete_nuid(LIVE_TEAM, sha_alice, "nuidPg123456")
            pending_with_nuid = repo.list_pending_deletes(LIVE_TEAM)
            assert pending_with_nuid[0].target_nuid == "nuidPg123456"
            repo.complete_pending_delete(LIVE_TEAM, sha_alice, expected_nuid="nuidPg123456")
            assert repo.list_pending_deletes(LIVE_TEAM) == ()

            # Retention expiry queues sha_shared once Bob's owner expires.
            expired_count, expired_unowned = repo.remove_expired_owners(LIVE_TEAM, now + timedelta(days=11))
            assert expired_count == 1
            assert expired_unowned == (sha_shared,)
            assert not repo.has_owners(LIVE_TEAM, sha_shared)
            assert len(repo.list_pending_deletes(LIVE_TEAM)) == 1
        finally:
            repo.close()
