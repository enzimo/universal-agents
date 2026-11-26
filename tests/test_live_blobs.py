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
from storage.blob_index_repository import BlobIndexRepository, BlobOwnerConflict, BlobSizeMismatch
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

        # F1 (SEC-1) on live operator-mode NATS: forged metadata aliasing a live blob's nuid cannot purge victim chunks.
        victim_bytes = b"live victim bytes protected from nuid aliasing"
        victim_put = await put_blob(worker.nc.jetstream(), OP_TEAM, victim_bytes, owner)
        alias_bytes = b"attacker alias metadata"
        alias_sha = sha256_unpadded(alias_bytes)
        alias_name = blob_object_name("team_other", alias_sha)
        alias_meta = {
            "name": alias_name,
            "bucket": "UA1_BLOBS",
            "nuid": victim_put.nuid,
            "size": len(alias_bytes),
            "chunks": 1,
            "digest": f"SHA-256={alias_sha}=",
            "headers": BlobOwner(agent_id="attacker-1").to_headers("team_other", alias_sha),
            "options": {"max_chunk_size": 131_072},
        }
        await admin_js.publish(blob_metadata_subject(alias_name), json.dumps(alias_meta).encode("utf-8"))
        alias_del = await delete_blob(services.nc.jetstream(), "team_other", alias_sha)
        assert alias_del.deleted is True
        assert await get_blob(edge_js, OP_TEAM, victim_put.sha256) == victim_bytes
        with pytest.raises(BlobDeleted):
            await get_blob(edge_js, "team_other", alias_sha)

        assert worker.errors.empty() and gateway.errors.empty() and edge.errors.empty() and services.errors.empty()


@pytest.mark.nats
@pytest.mark.asyncio
async def test_obj_ua1_blobs_stream_bounds_full_refusal_and_delete_reclaim(
    tmp_path: Path, broker: str, caplog: pytest.LogCaptureFixture,  # noqa: F811
) -> None:
    """InstallationAuthority enforces OBJ_UA1_BLOBS bounds (S4); full store hits 10077 and reclaims on delete (C09/F4/F14)."""

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

    async def _fill_until_slack_under_300(js: Any, owner: BlobOwner) -> int:
        filler_idx = 0
        for size in (20_000, 4_000, 1_000, 200):
            while True:
                filler_idx += 1
                try:
                    await put_blob(js, LIVE_TEAM, f"pad-{size}-{filler_idx}-".encode() + b"z" * size, owner)
                except BlobStoreFull:
                    break
        # Close any remaining slack (< ~600 B) down below 300 bytes with tiny chunk messages.
        for raw_size in (200, 80, 20, 1):
            while True:
                info_now = await js.stream_info(STREAM_NAME)
                if info_now.config.max_bytes - info_now.state.bytes < 300:
                    return int(info_now.config.max_bytes - info_now.state.bytes)
                filler_idx += 1
                try:
                    await js.publish(blob_chunk_subject(f"padraw{filler_idx}"), b"p" * raw_size)
                except nats.js.errors.APIError as exc:
                    if exc.err_code == 10077:
                        break
                    raise
        info_end = await js.stream_info(STREAM_NAME)
        return int(info_end.config.max_bytes - info_end.state.bytes)

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

        # Seed a 0-byte blob too so we test F4's keep=0 + expected_seq=0 full-store delete on live NATS!
        empty_res = await put_blob(js, LIVE_TEAM, b"", owner)
        assert await get_blob(js, LIVE_TEAM, empty_res.sha256) == b""

        # Second 600 KB blob exceeds 1 MiB cap -> BlobStoreFull without evicting first.
        second = random.Random(2).randbytes(600_000)
        with pytest.raises(BlobStoreFull):
            await put_blob(js, LIVE_TEAM, second, owner)
        assert await get_blob(js, LIVE_TEAM, res1.sha256) == first

        # Step filler sizes down until remaining slack is strictly < 300 bytes so 10077 is guaranteed!
        slack = await _fill_until_slack_under_300(js, owner)
        assert slack < 300, f"expected slack < 300 bytes to force JetStream 10077, got {slack}"

        # F4 + F14: Deleting the 0-byte blob while slack < 300 hits 10077 -> reclaim -> keep=0 -> deleted:true!
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="custom_transport.protocol1.blobs"):
            empty_del = await delete_blob(js, LIVE_TEAM, empty_res.sha256)
        assert empty_del.deleted
        assert "event=blob_delete_store_full_reclaim" in caplog.text
        assert "event=blob_delete_store_full_purge_meta" in caplog.text
        with pytest.raises(BlobDeleted):
            await get_blob(js, LIVE_TEAM, empty_res.sha256)

        # Refill slack < 300, then delete the 600 KB blob and prove it enters the 10077 reclaim branch!
        slack2 = await _fill_until_slack_under_300(js, owner)
        assert slack2 < 300
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="custom_transport.protocol1.blobs"):
            del_res = await delete_blob(js, LIVE_TEAM, res1.sha256)
        assert del_res.deleted
        assert "event=blob_delete_store_full_reclaim" in caplog.text
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
        del_alice_res = await service.delete_person(alice_ref)
        assert (del_alice_res.outcome, del_alice_res.owners_removed, del_alice_res.blobs_deleted, del_alice_res.blobs_kept) == ("ok", 2, 1, 1)
        with pytest.raises(BlobDeleted):
            await get_blob(js, LIVE_TEAM, alice_ref_blob["sha256"])
        assert await get_blob(js, LIVE_TEAM, shared_ref["sha256"]) == shared_bytes

        # Bob departs -> shared_bytes now has 0 owners and is deleted immediately!
        access.record_person(TeamPersonV1(
            team_id=LIVE_TEAM, person_id="bob", person_ref=bob_ref, standing=PersonStanding.REMOVED,
            roles=(), changed_at_utc=clock_state["now"],
        ))
        del_bob_res = await service.delete_person(bob_ref)
        assert (del_bob_res.outcome, del_bob_res.owners_removed, del_bob_res.blobs_deleted, del_bob_res.blobs_kept) == ("ok", 1, 1, 0)
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

        # 4. F13: Deterministic live CR-3 race tests covering BOTH interleavings on real NATS.
        # Interleaving A: Dave's reuse registration wins before Eve's remove_person_owners runs.
        race_a_bytes = b"cr3 interleaving A reuse wins " * 300
        race_a_ref = await client.put(
            race_a_bytes, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=BlobOwner(request_id="req_eve_a", person_ref=eve_ref)
        )
        meta_a_before = await stat_blob(js, LIVE_TEAM, race_a_ref["sha256"])
        assert meta_a_before is not None
        access.record_person(TeamPersonV1(
            team_id=LIVE_TEAM, person_id="eve", person_ref=eve_ref, standing=PersonStanding.DISABLED,
            roles=(), changed_at_utc=clock_state["now"],
        ))
        reuse_a_ref = await client.put(
            race_a_bytes, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=BlobOwner(request_id="req_dave_a", person_ref=dave_ref)
        )
        del_a_res = await service.delete_person(eve_ref)
        assert reuse_a_ref == race_a_ref
        assert del_a_res.outcome == "ok" and del_a_res.owners_removed == 1 and del_a_res.blobs_deleted == 0 and del_a_res.blobs_kept == 1
        meta_a_after = await stat_blob(js, LIVE_TEAM, race_a_ref["sha256"])
        assert meta_a_after is not None and meta_a_after.nuid == meta_a_before.nuid
        assert await get_blob(js, LIVE_TEAM, race_a_ref["sha256"]) == race_a_bytes

        # Interleaving B: delete_person deletes while client.put races, forcing present=False -> fresh upload -> second register!
        frank_ref = "p-FrankRef00000000000007"
        access.record_person(TeamPersonV1(
            team_id=LIVE_TEAM, person_id="frank", person_ref=frank_ref, standing=PersonStanding.ACTIVE,
            roles=("member",), changed_at_utc=clock_state["now"],
        ))
        race_b_bytes = b"cr3 interleaving B delete wins then reupload " * 300
        race_b_ref = await client.put(
            race_b_bytes, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=BlobOwner(request_id="req_frank_b", person_ref=frank_ref)
        )
        meta_b_before = await stat_blob(js, LIVE_TEAM, race_b_ref["sha256"])
        assert meta_b_before is not None
        access.record_person(TeamPersonV1(
            team_id=LIVE_TEAM, person_id="frank", person_ref=frank_ref, standing=PersonStanding.DISABLED,
            roles=(), changed_at_utc=clock_state["now"],
        ))

        reg_calls_b = 0
        delete_started = asyncio.Event()
        let_client_go = asyncio.Event()

        async def _racing_register(body: dict[str, Any]) -> dict[str, Any]:
            nonlocal reg_calls_b
            reg_calls_b += 1
            if reg_calls_b == 1:
                # Hold client.put's initial register until delete_person holds _lock and finishes deletion!
                delete_started.set()
                await let_client_go.wait()
            return await _register(body)

        racing_client = ClientBlobStore(
            get_js=lambda: _async_val(js), team=LIVE_TEAM, register=_racing_register, request_ids=_next_id
        )

        async def _run_delete_first() -> Any:
            await delete_started.wait()
            res = await service.delete_person(frank_ref)
            let_client_go.set()
            return res

        reuse_b_ref, del_b_res = await asyncio.gather(
            racing_client.put(
                race_b_bytes, media_type=TEXT_PLAIN_MEDIA_TYPE, owner=BlobOwner(request_id="req_dave_b", person_ref=dave_ref)
            ),
            _run_delete_first(),
        )
        assert del_b_res.outcome == "ok" and del_b_res.owners_removed == 1 and del_b_res.blobs_deleted == 1 and del_b_res.blobs_kept == 0
        assert reg_calls_b == 2  # initial pre-check got present=False, then fresh upload + second register succeeded!
        assert reuse_b_ref == race_b_ref
        meta_b_after = await stat_blob(js, LIVE_TEAM, race_b_ref["sha256"])
        assert meta_b_after is not None and not meta_b_after.deleted and meta_b_after.nuid != meta_b_before.nuid
        assert await get_blob(js, LIVE_TEAM, reuse_b_ref["sha256"]) == race_b_bytes
        assert [o.person_ref for o in service._repository.list_owners(LIVE_TEAM, reuse_b_ref["sha256"])] == [dave_ref]


@pytest.mark.nats
@pytest.mark.asyncio
async def test_live_end_to_end_person_departure_blob_deletion_and_startup_recovery(
    tmp_path: Path, broker: str, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """F15 (A4) & F21/F16: Real UserGateway + GatewayPersonLifecycle + team_services + tokenless lead_host over live NATS."""

    from assistant_gateway import team_view
    from assistant_gateway.config import GatewayInstanceConfig
    from assistant_gateway.gateway import UserGateway
    from assistant_gateway.message_host import gateway_profile
    from assistant_gateway.nats_manager_runtime import NatsGatewayManagerRuntime
    from assistant_gateway.store import GatewayStateStore
    from custom_transport.protocol1.host_runtime import MessageHost
    from custom_transport.protocol1.joining import HostProfile
    from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_operations, team_services_profile
    from tests.test_live_team_services import (
        audience as team_services_audience,
        host as start_host,
        installation,
        until_team_services_serve,
    )

    monkeypatch.setattr(team_view, "LEAD_START_SECONDS", 1.0)
    manager_db, gateway_db = tmp_path / "manager.db", tmp_path / "gateway.db"
    config = GatewayInstanceConfig.create(
        instance_name="live_departure",
        instance_root=str(tmp_path / "instance"),
        team_id=LIVE_TEAM,
        timezone="UTC",
        display_name="Team Owner",
        manager_agent_id="manager_live",
        manager_storage_sqlite_path=str(manager_db),
        state_storage_sqlite_path=str(gateway_db),
        named_agent_type="TeamArchitect",
    )
    config.transport.nats_url = broker
    config.transport.nats_servers = [broker]

    owner_id = config.user.canonical_user_id
    alice_id = "user_alice_live"
    bob_id = "user_bob_live"
    carol_id = "user_carol_live"
    stale_id = "user_stale_live"
    stale_ref = "p-StaleLive0000000000005"

    # Seed gateway users before UserGateway boots so ownership_bootstrap publishes their canonical person_refs
    # into the shared AccessRepository, matching real gateway + team_services operation.
    gateway_store = GatewayStateStore(str(gateway_db))
    for pid, dname, role in (
        (alice_id, "Alice", "member"),
        (bob_id, "Bob", "member"),
        (carol_id, "Carol", "member"),
    ):
        gateway_store.create_gateway_user(
            canonical_user_id=pid, display_name=dname, status="active", timezone="UTC",
        )
        gateway_store.create_team_membership(
            canonical_user_id=pid, team_id=LIVE_TEAM, role=role, status="active",
        )

    lead_profile = HostProfile("lead-host-1", (LIVE_TEAM,), ("lead_host",))
    worker_profile = HostProfile("worker-host-1", (LIVE_TEAM,), ("worker_host",))

    async with AsyncExitStack() as stack:
        await installation(
            stack, broker, config.layout,
            team_services_profile(LIVE_TEAM), gateway_profile(LIVE_TEAM), lead_profile, worker_profile,
        )
        # Construct UserGateway AFTER installation() prepares trust/bootstrap.json so its real MessageHost starts.
        gateway = UserGateway(
            config,
            output_sink=None,
            state_store=gateway_store,
            manager_runtime=NatsGatewayManagerRuntime.for_gateway_service(config),
        )
        access = AccessRepository(config.manager_storage_settings)
        people_dir = PeopleDirectory(access, LIVE_TEAM)
        now = datetime.now(timezone.utc)
        access.record_person(TeamPersonV1(
            team_id=LIVE_TEAM, person_id=stale_id, person_ref=stale_ref,
            standing=PersonStanding.DISABLED, roles=(), changed_at_utc=now,
        ))
        alice_ref = people_dir.person_ref(alice_id)
        bob_ref = people_dir.person_ref(bob_id)
        carol_ref = people_dir.person_ref(carol_id)
        assert alice_ref and bob_ref and carol_ref

        ts_nc = await connect_plain(stack, broker, TEAM_SERVICES_RUNTIME)
        js = ts_nc.jetstream()

        # F21 & F16: Seed a persisted pending delete AND a departed person's indexed owner BEFORE serve_team starts,
        # proving live prepare() deletes both before svc.team serves!
        repo = BlobIndexRepository(config.manager_storage_settings)
        crashed_put = await put_blob(
            js, LIVE_TEAM, b"pre-crash pending delete bytes",
            BlobOwner(request_id="req_pre_crash", person_ref=stale_ref),
        )
        repo.queue_pending_delete(
            LIVE_TEAM, crashed_put.sha256, requested_at_utc=now, reason="person_departed",
            target_nuid=crashed_put.nuid, person_ref=stale_ref,
        )
        offline_departed_put = await put_blob(
            js, LIVE_TEAM, b"offline departed person bytes",
            BlobOwner(request_id="req_offline_dep", person_ref=stale_ref),
        )
        repo.register_owner(
            team_id=LIVE_TEAM, sha256=offline_departed_put.sha256, owner_kind="request", owner_id="req_offline_dep",
            person_ref=stale_ref, size_bytes=offline_departed_put.size, registered_at_utc=now,
            expires_at_utc=now + timedelta(days=7),
        )

        stop = asyncio.Event()
        ts_host = MessageHost(config.layout, team_services_profile(LIVE_TEAM), ts_nc)
        serving = asyncio.create_task(
            serve_team(ts_host, LIVE_TEAM, stop, team_operations(config, ts_host.clock, js=js, host=ts_host))
        )
        try:
            await until_team_services_serve(ts_host, serving)

            # Startup prepare() already deleted both pre-crash and offline-departed blobs!
            with pytest.raises(BlobDeleted):
                await get_blob(js, LIVE_TEAM, crashed_put.sha256)
            with pytest.raises(BlobDeleted):
                await get_blob(js, LIVE_TEAM, offline_departed_put.sha256)
            assert repo.list_pending_deletes(LIVE_TEAM) == ()
            assert not repo.has_owners(LIVE_TEAM, offline_departed_put.sha256)

            lead_host = await start_host(stack, broker, config.layout, lead_profile)
            worker_host = await start_host(stack, broker, config.layout, worker_profile)

            await gateway.connect(start_services=False)

            # Upload and register blobs through real NATS blobs.register via lead_host!
            assert lead_host.live is not None and worker_host.live is not None

            async def _reg_via_nats(data: bytes, owner: BlobOwner, req_id: str) -> str:
                put = await put_blob(js, LIVE_TEAM, data, owner)
                body: dict[str, Any] = {
                    "request_id": req_id,
                    "sha256": put.sha256,
                    "size": put.size,
                    "owner": {"type": "request", "request_id": owner.request_id},
                    "person": owner.person_ref,
                }
                reply = await lead_host.live.request(
                    "blobs.register", body, team_services_audience(lead_host), team=LIVE_TEAM,
                )
                assert reply.raw["body"]["outcome"] == "ok" and reply.raw["body"]["present"] is True
                return put.sha256

            alice_only_sha = await _reg_via_nats(
                b"alice exclusive live bytes", BlobOwner(request_id="req_al_1", person_ref=alice_ref), "reg_al_1"
            )
            shared_sha = await _reg_via_nats(
                b"alice and bob shared live bytes", BlobOwner(request_id="req_al_sh", person_ref=alice_ref), "reg_al_sh"
            )
            await _reg_via_nats(
                b"alice and bob shared live bytes", BlobOwner(request_id="req_bo_sh", person_ref=bob_ref), "reg_bo_sh"
            )
            carol_only_sha = await _reg_via_nats(
                b"carol exclusive live bytes", BlobOwner(request_id="req_ca_1", person_ref=carol_ref), "reg_ca_1"
            )

            admin_actor = gateway._identity_service.resolve_user_by_id(owner_id)
            assert admin_actor is not None
            lifecycle = gateway._command_operations._person_lifecycle

            # 1. Non-admin member attempting team.delete_person_data through gateway command token is refused!
            non_admin_reply = await gateway._command_operations._team_commands.request(
                "team.delete_person_data", {"person": alice_ref}, person_id=bob_id,
            )
            assert non_admin_reply["outcome"] == "not_allowed"

            # 2. GatewayPersonLifecycle.set_enabled(..., enabled=False) sends real signed team.delete_person_data over NATS:
            #    Alice's unshared blob is deleted immediately while Alice+Bob's shared blob stays readable!
            await lifecycle.set_enabled(
                actor=admin_actor, target=alice_id, enabled=False, channel_name="website", chat_scope="web-chat",
            )
            with pytest.raises(BlobDeleted):
                await get_blob(js, LIVE_TEAM, alice_only_sha)
            assert await get_blob(js, LIVE_TEAM, shared_sha) == b"alice and bob shared live bytes"

            # 3. GatewayPersonLifecycle.remove(...) for Bob immediately deletes the now-unowned shared blob!
            await lifecycle.remove(
                actor=admin_actor, target=bob_id, channel_name="website", chat_scope="web-chat",
            )
            with pytest.raises(BlobDeleted):
                await get_blob(js, LIVE_TEAM, shared_sha)

            # 4. Tokenless worker_host cannot call team.delete_person_data over real NATS (sender role refused).
            access.record_person(TeamPersonV1(
                team_id=LIVE_TEAM, person_id=carol_id, person_ref=carol_ref,
                standing=PersonStanding.DISABLED, roles=(), changed_at_utc=datetime.now(timezone.utc),
            ))
            from custom_transport.protocol1.live import RequestRefused
            with pytest.raises(RequestRefused):
                await worker_host.live.request(
                    "team.delete_person_data", {"request_id": "del_worker_nope", "person": carol_ref},
                    team_services_audience(worker_host), team=LIVE_TEAM,
                )
            assert await get_blob(js, LIVE_TEAM, carol_only_sha) == b"carol exclusive live bytes"

            # 5. Tokenless lead_host MessageHost CAN call team.delete_person_data over real NATS and deletes Carol's blob!
            lead_del = await lead_host.live.request(
                "team.delete_person_data", {"request_id": "del_lead_ok", "person": carol_ref},
                team_services_audience(lead_host), team=LIVE_TEAM,
            )
            assert lead_del.raw["body"] == {
                "outcome": "ok",
                "owners_removed": 1,
                "blobs_deleted": 1,
                "blobs_kept": 0,
                "dlq_copies_deleted": 0,
                "dlq_copies_kept": 0,
            }
            with pytest.raises(BlobDeleted):
                await get_blob(js, LIVE_TEAM, carol_only_sha)
        finally:
            if gateway is not None:
                await gateway.close()
            stop.set()
            await serving


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
    """BlobIndexRepository works cleanly against a real PostgreSQL server (F5, F11, F18, F21)."""

    with _postgres_blob_storage() as settings:
        repo = BlobIndexRepository(settings)
        try:
            now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
            sha_shared = sha256_unpadded(b"pg-shared")
            sha_alice = sha256_unpadded(b"pg-alice")
            sha_huge = sha256_unpadded(b"pg-huge-3gib")
            alice_ref = "p-PgAliceRef000000000001"
            bob_ref = "p-PgBobRef00000000000002"

            # F11: 64-bit BIGINT size_bytes (>= 2 GiB) succeeds on PostgreSQL without NumericValueOutOfRange.
            three_gib = 3 * 1024 * 1024 * 1024
            huge_rec = repo.register_owner(
                team_id=LIVE_TEAM, sha256=sha_huge, owner_kind="agent", owner_id="curator-pg",
                person_ref=None, size_bytes=three_gib, registered_at_utc=now, expires_at_utc=now + timedelta(days=7),
            )
            assert huge_rec.size_bytes == three_gib
            assert [o.size_bytes for o in repo.list_owners(LIVE_TEAM, sha_huge)] == [three_gib]

            # Upsert keeps the later expires_at_utc and rejects conflicting size or person_ref (F18, F21).
            first_rec = repo.register_owner(
                team_id=LIVE_TEAM, sha256=sha_shared, owner_kind="request", owner_id="req_1",
                person_ref=alice_ref, size_bytes=9, registered_at_utc=now, expires_at_utc=now + timedelta(days=7),
            )
            extended_rec = repo.register_owner(
                team_id=LIVE_TEAM, sha256=sha_shared, owner_kind="request", owner_id="req_1",
                person_ref=alice_ref, size_bytes=9, registered_at_utc=now, expires_at_utc=now + timedelta(days=12),
            )
            assert extended_rec.expires_at_utc == now + timedelta(days=12)
            kept_later = repo.register_owner(
                team_id=LIVE_TEAM, sha256=sha_shared, owner_kind="request", owner_id="req_1",
                person_ref=alice_ref, size_bytes=9, registered_at_utc=now, expires_at_utc=first_rec.expires_at_utc,
            )
            assert kept_later.expires_at_utc == now + timedelta(days=12)
            with pytest.raises(BlobSizeMismatch):
                repo.register_owner(
                    team_id=LIVE_TEAM, sha256=sha_shared, owner_kind="request", owner_id="req_size_bad",
                    person_ref=bob_ref, size_bytes=999, registered_at_utc=now, expires_at_utc=now + timedelta(days=7),
                )
            with pytest.raises(BlobOwnerConflict):
                repo.register_owner(
                    team_id=LIVE_TEAM, sha256=sha_shared, owner_kind="request", owner_id="req_1",
                    person_ref=bob_ref, size_bytes=9, registered_at_utc=now, expires_at_utc=now + timedelta(days=7),
                )

            repo.register_owner(
                team_id=LIVE_TEAM, sha256=sha_shared, owner_kind="request", owner_id="req_2",
                person_ref=bob_ref, size_bytes=9, registered_at_utc=now, expires_at_utc=now + timedelta(days=10),
            )
            repo.register_owner(
                team_id=LIVE_TEAM, sha256=sha_alice, owner_kind="request", owner_id="req_3",
                person_ref=alice_ref, size_bytes=8, registered_at_utc=now, expires_at_utc=now + timedelta(days=7),
            )
            assert [o.owner_id for o in repo.list_owners(LIVE_TEAM, sha_shared)] == ["req_1", "req_2"]
            assert set(repo.list_indexed_person_refs(LIVE_TEAM)) == {alice_ref, bob_ref}

            # Removing Alice leaves sha_alice unowned (queued for pending delete) and sha_shared kept for Bob.
            removed = repo.remove_person_owners(LIVE_TEAM, alice_ref, now)
            assert removed.owners_removed == 2
            assert removed.unowned_sha256s == (sha_alice,)
            assert removed.kept_sha256s == (sha_shared,)
            assert not repo.has_owners(LIVE_TEAM, sha_alice)
            assert repo.has_owners(LIVE_TEAM, sha_shared)

            pending_alice = repo.list_pending_deletes(LIVE_TEAM, person_ref=alice_ref)
            assert len(pending_alice) == 1 and pending_alice[0].sha256 == sha_alice and pending_alice[0].target_nuid is None

            repo.set_pending_delete_nuid(LIVE_TEAM, sha_alice, "nuidPg123456")
            # queue_pending_delete COALESCE preserves previously captured target_nuid!
            coalesced = repo.queue_pending_delete(
                LIVE_TEAM, sha_alice, requested_at_utc=now, reason="person_departed",
                target_nuid="nuidPgDifferent", person_ref=alice_ref,
            )
            assert coalesced.target_nuid == "nuidPg123456"
            assert repo.complete_pending_delete(LIVE_TEAM, sha_alice, expected_nuid="nuidPgWrong") is False
            assert repo.complete_pending_delete(LIVE_TEAM, sha_alice, expected_nuid="nuidPg123456") is True
            assert repo.list_pending_deletes(LIVE_TEAM) == ()

            # Retention expiry queues sha_shared (preserving bob_ref) and sha_huge (person_ref=None);
            # include_unattributed=True returns both for a departing person (F5).
            expired_count, expired_unowned = repo.remove_expired_owners(LIVE_TEAM, now + timedelta(days=11))
            assert expired_count == 2
            assert set(expired_unowned) == {sha_shared, sha_huge}
            p_shared = repo.get_pending_delete(LIVE_TEAM, sha_shared)
            p_huge = repo.get_pending_delete(LIVE_TEAM, sha_huge)
            assert p_shared is not None and p_shared.person_ref == bob_ref
            assert p_huge is not None and p_huge.person_ref is None
            assert {p.sha256 for p in repo.list_pending_deletes(LIVE_TEAM, person_ref=bob_ref, include_unattributed=True)} == {
                sha_shared, sha_huge,
            }
        finally:
            repo.close()
