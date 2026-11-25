"""Live tests for protocol 1 dead letters on nats-server 2.15.1 under real generated operator-mode permissions (A8, C24).

Covers every A8 scenario plus C24 against an isolated operator-mode NATS 2.15.1 broker using the
real generated role JWTs from ``contracts/messages/v1/generated/permissions/*.json``:
- A receiver publishes raw copies and notices only under its own runtime id and cannot read or
  delete from ``UA1_DLQ_<team>``; another team's copies are unreachable to both receivers and
  other teams' ``team_services``.
- A poison message is refused, set aside with a raw copy and signed ``dlq.notice``, acknowledged
  only after both are stored, and reported to administrators (Inbox + chat, idempotent across
  restarts).
- FA-8 crash between raw copy and notice: redelivery derives the identical copy id from the real
  ``$JS.ACK`` nanosecond timestamp and ``max_msgs_per_subject: 1`` replaces the half-finished copy
  without leaving an orphan.
- Full ``UA1_DLQ_<team>`` hold: refused message stays alive with ``+WPI`` past multiple
  ``ack_wait`` periods without incrementing deliveries, logs once, and completes once room returns.
- CRIT-1 sweep under real operator-mode grants for BOTH sweep owners:
  1) ``team_services`` sweeping ``UA1_PART_<team>~*`` and ``UA1_POOL_WORK_<team>`` (leaving
     pending/in-flight messages untouched);
  2) ``InstallationAuthority`` with its real generated ``installation_authority`` NATS user
     sweeping ``UA1_TEAM_WORK_<team>`` into ``UA1_DLQ_<team>`` and ``team_services`` reporting it (C24).
- Immediate departed-person deletion via ``team.delete_person_data`` over real ``svc.team``,
  deleting protocol 1 and ``upgrade_old_format`` raw copies naming the departed person (without
  leaking personal prompt text) while keeping active people's copies, plus orphan raw-copy cleanup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import nats
import nats.js.errors
import pytest
from nats.js.api import AckPolicy, ConsumerConfig

from access_control.people import PERSON_ROLE_ADMIN, PERSON_ROLE_MEMBER, PERSON_ROLE_OWNER
from access_control.people_directory import PeopleDirectory
from custom_messages.contracts.access_v2 import AccessNoticeKind, AccessNoticeV1, PersonStanding, TeamPersonV1
from custom_messages.wire.encoding import b64url
from custom_messages.wire.frame import Receiver
from custom_messages.wire.permissions import HostPlacement, host_permissions
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.bootstrap import read_bundle
from custom_transport.protocol1.dead_letters import (
    DeadLetterSetter,
    delete_stream_message,
    dlq_notice_consumer_name,
    dlq_notice_subject,
    dlq_raw_subject,
    dlq_stream_name,
    get_stream_message,
    message_stored_timestamp_ns,
)
from custom_transport.protocol1.host_keys import HostKeys, ensure_host_keys, load_authority_keys, load_host_keys
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.live import LiveHost
from custom_transport.protocol1.trust_state import PersistentTrustStore
from docker import message_hosts
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from runtime_settings.instance_layout import InstanceLayout
from storage.access_repository import AccessRepository
from storage.config import StorageSettings, resolve_storage_settings
from team_services.dead_letters import (
    DeadLetterService,
    TeamPersonDataOperations,
    TeamPersonDepartureService,
)
from tests import message_contract_support as fixtures
from tests.nats_operator_support import OperatorAccount
from tests.queue_limits_support import queue_limits
from tests.test_live_message_contract import (
    COUNT,
    INSTALLATION,
    POOL_STREAM,
    TEAM,
    TRANSPORT,
    Client,
    agent_stream,
    arrived,
    connect,
    consumer_config,
    consumer_name,
    create_topology,
    isolated_broker,  # noqa: F401 - pytest fixture
    role_document,
    stream_config,
)
from tests.test_team_services_dead_letters import RecordingChatSender

pytestmark = [pytest.mark.integration, pytest.mark.live]

ADMIN_ID = "person-admin-live"
ADMIN_REF = "p-adminlive0000000000001"
ALICE_ID = "person-alice-live"
ALICE_REF = "p-alicelive0000000000001"
BOB_ID = "person-bob-live"
BOB_REF = "p-boblive000000000000001"


def _fixture_identity(runtime: str, key_id: str, epoch: int) -> HostIdentity:
    certificate = fixtures.trust_store().host_certificate(INSTALLATION, runtime, epoch)
    assert certificate is not None
    keys = HostKeys(runtime=runtime, key_id=key_id, application_key=fixtures.private_key(key_id), nats_seed=bytes(32))
    return HostIdentity(keys, certificate)


def _live_host_on_client(nc: Any, runtime: str, key_id: str, epoch: int) -> LiveHost:
    trust = PersistentTrustStore(fixtures.trust_store(), Path("/nonexistent"))
    return LiveHost(nc, _fixture_identity(runtime, key_id, epoch), trust, clock=lambda: fixtures.NOW)


def _access_and_people(tmp_path: Path, name: str = "live_dlq.db") -> tuple[AccessRepository, PeopleDirectory]:
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / name)}})
    access = AccessRepository(storage)
    for pid, pref, roles in (
        (ADMIN_ID, ADMIN_REF, (PERSON_ROLE_OWNER,)),
        (ALICE_ID, ALICE_REF, (PERSON_ROLE_MEMBER,)),
        (BOB_ID, BOB_REF, (PERSON_ROLE_MEMBER,)),
    ):
        access.record_person(
            TeamPersonV1(
                team_id=TEAM,
                person_id=pid,
                person_ref=pref,
                standing=PersonStanding.ACTIVE,
                roles=roles,
                changed_at_utc=fixtures.NOW,
            )
        )
    return access, PeopleDirectory(access, TEAM)


async def _add_dlq_stream_and_consumer(
    admin_js: Any,
    *,
    team: str = TEAM,
    max_bytes: int = -1,
    ack_wait: float = 60.0,
) -> str:
    dlq_stream = dlq_stream_name(team)
    cfg = stream_config("UA1_DLQ_{team}", team=team)
    cfg.max_bytes = max_bytes
    await admin_js.add_stream(config=cfg)
    spec = TRANSPORT["consumers"]["dead_letters"]
    await admin_js.add_consumer(
        dlq_stream,
        config=ConsumerConfig(
            durable_name=spec["name"].format(team=team),
            filter_subject=spec["filter"].format(team=team),
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=ack_wait,
            max_deliver=spec["max_deliveries"],
        ),
    )
    return dlq_stream


def _legacy_user_prompt_bytes(*, person_id: str, task_id: str, request_id: str, prompt_text: str) -> bytes:
    payload = {
        "msg_content": {
            "message_class": "Task",
            "task_id": task_id,
            "request_id": request_id,
            "task_description": prompt_text,
        },
        "msg_meta": {
            "from_agent_name": "gateway",
            "msg_correlation_id": request_id,
            "gateway_context": {
                "channel_name": "web",
                "chat_id": "chat-live",
                "user_id": person_id,
                "canonical_user_id": person_id,
            },
        },
        "msg_header": {
            "from_agent_id": "gateway",
            "to_routing_path": "team_lead",
            "response_routing_key": "gateway",
            "idempotency_key": request_id,
        },
    }
    return json.dumps(payload).encode("utf-8")


async def _connect_runtime_login(stack: AsyncExitStack, broker: Any, runtime: str, login: Any) -> Client:
    """Connect using an explicit operator-mode user JWT with ``_INBOX.<runtime>``."""

    errors: asyncio.Queue[Any] = asyncio.Queue()
    nc = await nats.connect(
        broker.url,
        inbox_prefix=f"_INBOX.{runtime}".encode(),
        error_cb=errors.put,
        allow_reconnect=False,
        **login.login(),
    )
    stack.push_async_callback(nc.close)
    return Client(nc, errors)


@pytest.mark.nats
@pytest.mark.asyncio
async def test_live_operator_permissions_raw_copy_scoping_and_team_isolation(isolated_broker: Any) -> None:  # noqa: F811
    """A8: A receiver writes raw copies/notices only under its own runtime id, cannot read/delete UA1_DLQ,
    and another team's copies are unreachable to both receivers and other teams' team_services."""

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        worker = await connect(stack, isolated_broker, "worker")
        services = await connect(stack, isolated_broker, "services")
        other_worker = await connect(stack, isolated_broker, "other_worker")

        admin_js = admin.nc.jetstream()
        team_dlq = await _add_dlq_stream_and_consumer(admin_js, team=TEAM)
        other_dlq = await _add_dlq_stream_and_consumer(admin_js, team="team_other")

        worker_live = _live_host_on_client(worker.nc, "worker-host-1", "test-worker-key-1", 3)
        setter = DeadLetterSetter(worker_live)
        stored = await setter.set_aside(
            raw_bytes=b'{"broken":"payload"}',
            reason="malformed",
            original_subject=f"ua.v1.{TEAM}.lead-host-1.work.agent.researcher-2",
            original_stream=agent_stream("researcher-2"),
            original_stream_sequence=1,
            deliveries=1,
            stored_at=fixtures.NOW,
            team=TEAM,
        )
        assert stored.raw_subject == dlq_raw_subject(TEAM, "worker-host-1", stored.copy_id)
        assert stored.notice_subject == dlq_notice_subject(TEAM, "worker-host-1", stored.copy_id)

        # Receiver cannot publish raw copies or notices under another runtime id.
        await worker.publish_refused(dlq_raw_subject(TEAM, "lead-host-1", stored.copy_id), b"forged")
        await worker.publish_refused(dlq_notice_subject(TEAM, "lead-host-1", stored.copy_id), b"{}")
        # Receiver cannot read, inspect, pull, or delete from UA1_DLQ_<team>.
        await worker.publish_refused(f"$JS.API.STREAM.MSG.GET.{team_dlq}", json.dumps({"seq": 1}).encode())
        await worker.publish_refused(f"$JS.API.STREAM.INFO.{team_dlq}", b"")
        await worker.publish_refused(f"$JS.API.STREAM.MSG.DELETE.{team_dlq}", json.dumps({"seq": 1}).encode())
        await worker.publish_refused(f"$JS.API.CONSUMER.MSG.NEXT.{team_dlq}.{dlq_notice_consumer_name(TEAM)}", b'{"batch":1}')

        # Another team's worker writes into UA1_DLQ_team_other; neither worker nor team_example's team_services can reach it.
        other_copy_id = "f" * 64
        other_raw_subj = dlq_raw_subject("team_other", "worker-host-9", other_copy_id)
        await other_worker.nc.jetstream().publish(other_raw_subj, b"other team secret copy")
        await worker.publish_refused(other_raw_subj, b"cross-team write")
        await worker.publish_refused(f"$JS.API.STREAM.MSG.GET.{other_dlq}", json.dumps({"last_by_subj": other_raw_subj}).encode())
        await services.publish_refused(f"$JS.API.STREAM.MSG.GET.{other_dlq}", json.dumps({"last_by_subj": other_raw_subj}).encode())
        await services.publish_refused(f"$JS.API.STREAM.MSG.DELETE.{other_dlq}", json.dumps({"seq": 1}).encode())
        await services.publish_refused(
            f"$JS.API.CONSUMER.MSG.NEXT.{other_dlq}.{dlq_notice_consumer_name('team_other')}", b'{"batch":1}'
        )

        # Own team_services CAN read and delete its own team's raw copy.
        fetched = await get_stream_message(services.nc, team_dlq, last_by_subj=stored.raw_subject)
        assert fetched is not None and fetched.data == b'{"broken":"payload"}'
        assert await delete_stream_message(services.nc, team_dlq, fetched.seq) is True
        assert await get_stream_message(services.nc, team_dlq, last_by_subj=stored.raw_subject) is None

        assert worker.errors.empty() and services.errors.empty() and other_worker.errors.empty()


@pytest.mark.nats
@pytest.mark.asyncio
async def test_live_poison_message_set_aside_watch_reports_and_fa8_crash_recovery(
    isolated_broker: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    """A8 (A1, A2, A5): Poison message set aside and reported to admins (idempotent on restart),
    plus FA-8 crash between raw copy and notice leaving exactly one raw copy and one notice."""

    access, people = _access_and_people(tmp_path)
    chat = RecordingChatSender()

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        services = await connect(stack, isolated_broker, "services")

        admin_js = admin.nc.jetstream()
        await create_topology(admin, ["researcher-2"])
        part_stream = agent_stream("researcher-2")
        ctrl_consumer = consumer_name("agent_control", agent="researcher-2")
        # Recreate agent_control consumer with a short 1s ack_wait so FA-8 redelivery runs quickly.
        await admin_js.delete_consumer(part_stream, ctrl_consumer)
        ctrl_cfg = consumer_config("agent_control", agent="researcher-2")
        ctrl_cfg.ack_wait = 1.0
        await admin_js.add_consumer(part_stream, config=ctrl_cfg)
        dlq_stream = await _add_dlq_stream_and_consumer(admin_js, team=TEAM)

        worker_live = _live_host_on_client(worker.nc, "worker-host-1", "test-worker-key-1", 3)
        services_live = _live_host_on_client(services.nc, "team-services-1", "test-services-key-1", 2)
        setter = DeadLetterSetter(worker_live)
        dlq_service = DeadLetterService(
            team_id=TEAM,
            live_fn=lambda: services_live,
            access_repository=access,
            people=people,
            chat_sender=chat,
            clock=lambda: fixtures.NOW,
            report_quiet_seconds=0.0,
            fetch_timeout_seconds=1.0,
        )

        # 1. Poison message (tampered signature) delivered to worker -> verified -> set aside -> reported to admins.
        bad_frame = dict(fixtures.example_frame("task.stop"))
        bad_frame["signature"] = b64url(b"\x00" * 64)
        poison_bytes = json.dumps(bad_frame).encode("utf-8")
        pub_ack = await lead.nc.jetstream().publish(bad_frame["subject"], poison_bytes)
        assert pub_ack.stream == part_stream

        sub = await worker.nc.jetstream().pull_subscribe_bind(ctrl_consumer, stream=part_stream)
        (msg1,) = await sub.fetch(1, timeout=2.0)
        receiver = Receiver(INSTALLATION, TEAM, "agent", "researcher-2", "worker-host-1", "worker")
        with pytest.raises(FrameRefused) as refused:
            await worker_live.verify(
                bytes(msg1.data),
                receiver=receiver,
                now=fixtures.NOW,
                replay=lambda _k: None,
                **arrived(bad_frame, msg1),
            )
        assert refused.value.code == "bad_signature"

        res1 = await setter.set_aside_and_ack(
            msg1, reason=refused.value.code, team=TEAM, pointer=refused.value.known_at
        )
        assert res1 is not None and res1.raw_stored is True
        assert (await admin_js.stream_info(part_stream)).state.messages == 0

        assert await dlq_service.poll_notices_once() == 1
        notices = access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))
        assert len(notices) == 1
        notice1, _ = notices[0]
        assert notice1.kind is AccessNoticeKind.MESSAGES_SET_ASIDE
        assert res1.copy_id in notice1.summary
        assert "signature did not verify" in notice1.summary
        assert len(chat.sent) == 1

        # Restarted DeadLetterService (fresh instance, same DB) does NOT repeat already-reported notice.
        restarted = DeadLetterService(
            team_id=TEAM,
            live_fn=lambda: services_live,
            access_repository=access,
            people=people,
            chat_sender=chat,
            clock=lambda: fixtures.NOW,
            report_quiet_seconds=0.0,
            fetch_timeout_seconds=0.3,
        )
        assert await restarted.poll_notices_once() == 0
        assert await restarted.recover_exhausted_notices() == 0
        assert len(access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))) == 1
        assert len(chat.sent) == 1

        # 2. FA-8 crash between raw copy and notice: publish raw copy on delivery 1 without notice/ack,
        #    let NATS redeliver after ack_wait=1s, and verify restarted setter reuses copy_id with zero orphans.
        crash_bytes = b"{broken json for fa8 live crash test"
        await lead.nc.jetstream().publish(bad_frame["subject"], crash_bytes)
        (delivery1,) = await sub.fetch(1, timeout=2.0)
        stored_ns_1 = message_stored_timestamp_ns(delivery1)
        copy_id_1 = setter.copy_id_for(
            stream=delivery1.metadata.stream,
            stream_sequence=delivery1.metadata.sequence.stream,
            stored_at=stored_ns_1,
        )
        # Crash right after storing the raw copy (no notice published, no ack sent).
        await worker.nc.jetstream().publish(dlq_raw_subject(TEAM, "worker-host-1", copy_id_1), crash_bytes)
        assert (await admin_js.stream_info(dlq_stream)).state.messages == 3  # 2 from poison1 + 1 half-finished raw copy

        # Wait for real NATS redelivery after 1s ack_wait; fresh setter finishes set-aside.
        (delivery2,) = await sub.fetch(1, timeout=3.0)
        assert delivery2.metadata.num_delivered == 2
        restarted_setter = DeadLetterSetter(worker_live)
        res2 = await restarted_setter.set_aside_and_ack(delivery2, reason="malformed", team=TEAM)
        assert res2 is not None
        assert res2.copy_id == copy_id_1
        # max_msgs_per_subject: 1 replaced the half-finished copy in place -> exactly 4 messages total (2 raw + 2 notice).
        assert (await admin_js.stream_info(dlq_stream)).state.messages == 4
        assert (await admin_js.stream_info(part_stream)).state.messages == 0

        assert await restarted.poll_notices_once() == 1
        assert len(access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))) == 2
        assert len(chat.sent) == 2

        assert lead.errors.empty() and worker.errors.empty() and services.errors.empty()


@pytest.mark.nats
@pytest.mark.asyncio
async def test_live_full_dlq_stream_holds_with_wpi_and_recovers_when_room_returns(
    isolated_broker: Any,  # noqa: F811
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A8 (A3, C18): With UA1_DLQ_<team> full, set_aside_and_ack holds with +WPI past multiple ack_wait
    periods without another delivery being counted, logs once, and finishes + acks once room returns."""

    access, people = _access_and_people(tmp_path)
    chat = RecordingChatSender()

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        services = await connect(stack, isolated_broker, "services")

        admin_js = admin.nc.jetstream()
        await create_topology(admin, ["researcher-2"])
        part_stream = agent_stream("researcher-2")
        ctrl_consumer = consumer_name("agent_control", agent="researcher-2")
        await admin_js.delete_consumer(part_stream, ctrl_consumer)
        ctrl_cfg = consumer_config("agent_control", agent="researcher-2")
        ctrl_cfg.ack_wait = 1.0
        ctrl_cfg.max_deliver = 3
        await admin_js.add_consumer(part_stream, config=ctrl_cfg)
        dlq_stream = await _add_dlq_stream_and_consumer(admin_js, team=TEAM, max_bytes=4096)

        # Fill UA1_DLQ_<team> until 10077 so any new set-aside is refused.
        filler_seqs: list[int] = []
        worker_js = worker.nc.jetstream()
        for idx in range(40):
            cid = f"{idx:064x}"
            try:
                ack = await worker_js.publish(dlq_raw_subject(TEAM, "worker-host-1", cid), b"x" * 256)
                filler_seqs.append(ack.seq)
            except nats.js.errors.APIError as exc:
                if exc.err_code == 10077:
                    break
                raise
        assert filler_seqs, "expected filler messages before hitting max_bytes=4096"

        poison_subject = f"ua.v1.{TEAM}.lead-host-1.work.control.researcher-2"
        await lead.nc.jetstream().publish(poison_subject, b"payload held while dlq full")
        sub = await worker_js.pull_subscribe_bind(ctrl_consumer, stream=part_stream)
        (msg,) = await sub.fetch(1, timeout=2.0)
        assert msg.metadata.num_delivered == 1

        worker_live = _live_host_on_client(worker.nc, "worker-host-1", "test-worker-key-1", 3)
        services_live = _live_host_on_client(services.nc, "team-services-1", "test-services-key-1", 2)
        dlq_logger = logging.getLogger("test.live_dlq_full_hold")
        setter = DeadLetterSetter(
            worker_live,
            ack_wait_seconds=1.0,
            initial_retry_seconds=0.15,
            max_retry_seconds=0.25,
            logger=dlq_logger,
        )

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="test.live_dlq_full_hold"):
            hold_task = asyncio.create_task(
                setter.set_aside_and_ack(msg, reason="malformed", team=TEAM)
            )
            # Hold past >2 full ack_wait periods (ack_wait=1.0s) while UA1_DLQ stays full.
            await asyncio.sleep(2.3)
            assert not hold_task.done()
            cinfo = await admin_js.consumer_info(part_stream, ctrl_consumer)
            assert cinfo.num_ack_pending == 1
            assert cinfo.num_redelivered == 0
            assert cinfo.delivered.consumer_seq == 1
            hold_logs = [r for r in caplog.records if "event=protocol1_dlq_stream_full_holding" in r.getMessage()]
            assert len(hold_logs) == 1

            # Free room using real team_services delete permission!
            for seq in filler_seqs:
                assert await delete_stream_message(services.nc, dlq_stream, seq) is True

            result = await asyncio.wait_for(hold_task, timeout=3.0)

        assert result is not None and result.raw_stored is True
        assert (await admin_js.stream_info(part_stream)).state.messages == 0
        cinfo_after = await admin_js.consumer_info(part_stream, ctrl_consumer)
        assert cinfo_after.num_ack_pending == 0 and cinfo_after.num_pending == 0

        # Notice watch verifies the newly written raw copy and reports it to administrators.
        dlq_service = DeadLetterService(
            team_id=TEAM,
            live_fn=lambda: services_live,
            access_repository=access,
            people=people,
            chat_sender=chat,
            clock=lambda: fixtures.NOW,
            report_quiet_seconds=0.0,
            fetch_timeout_seconds=1.0,
        )
        assert await dlq_service.poll_notices_once() == 1
        assert len(access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))) == 1
        assert result.copy_id in chat.sent[0]["text"]

        assert lead.errors.empty() and worker.errors.empty() and services.errors.empty()


@pytest.mark.nats
@pytest.mark.asyncio
async def test_live_crit1_sweep_by_team_services_and_installation_authority(
    isolated_broker: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    """A8 & C24 (A4): Both CRIT-1 sweep owners work under real generated operator-mode JWTs:
    1) team_services sweeps UA1_PART_<team>~* and UA1_POOL_WORK_<team> without touching pending/in-flight messages;
    2) real InstallationAuthority with its generated installation_authority user sweeps UA1_TEAM_WORK_<team>
       into UA1_DLQ_<team> and team_services' watch verifies and reports it."""

    access, people = _access_and_people(tmp_path, name="sweep_ts.db")
    chat = RecordingChatSender()

    # -- Part 1: team_services sweeping partition and pool streams under real operator-mode JWTs --
    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        services = await connect(stack, isolated_broker, "services")

        admin_js = admin.nc.jetstream()
        await create_topology(admin, ["researcher-2"])
        part_stream = agent_stream("researcher-2")
        pool_stream = POOL_STREAM.format(team=TEAM)
        inbox_consumer = consumer_name("agent_inbox", agent="researcher-2")
        pool_consumer = consumer_name("pool", pool="researcher")

        await admin_js.delete_consumer(part_stream, inbox_consumer)
        inbox_cfg = consumer_config("agent_inbox", agent="researcher-2")
        inbox_cfg.ack_wait = 2.0
        inbox_cfg.max_deliver = 2
        await admin_js.add_consumer(part_stream, config=inbox_cfg)

        pool_cfg = consumer_config("pool", pool="researcher")
        pool_cfg.ack_wait = 2.0
        pool_cfg.max_deliver = 2
        await admin_js.add_consumer(pool_stream, config=pool_cfg)
        await _add_dlq_stream_and_consumer(admin_js, team=TEAM)

        # Exhaust seq 1 in partition and pool streams; real pull loops keep pulling, which causes JetStream 2.15.1
        # to drop the exhausted message out of num_ack_pending when the post-max_deliver pull arrives.
        await lead.nc.jetstream().publish(f"ua.v1.{TEAM}.lead-host-1.work.agent.researcher-2", b"exhausted-partition-msg")
        await lead.nc.jetstream().publish(f"ua.v1.{TEAM}.lead-host-1.work.pool.researcher", b"exhausted-pool-msg")

        part_sub = await worker.nc.jetstream().pull_subscribe_bind(inbox_consumer, stream=part_stream)
        for attempt in (1, 2):
            (m,) = await part_sub.fetch(1, timeout=2.0)
            assert m.metadata.sequence.stream == 1 and m.metadata.num_delivered == attempt
            await m.nak()
        with pytest.raises((TimeoutError, asyncio.TimeoutError, Exception)):
            await part_sub.fetch(1, timeout=0.2)
        # Publish seq 2 now so it remains genuinely pending (unpulled) during the sweep.
        await lead.nc.jetstream().publish(f"ua.v1.{TEAM}.lead-host-1.work.agent.researcher-2", b"still-pending-msg")

        pool_sub = await worker.nc.jetstream().pull_subscribe_bind(pool_consumer, stream=pool_stream)
        for attempt in (1, 2):
            (m,) = await pool_sub.fetch(1, timeout=2.0)
            assert m.metadata.sequence.stream == 1 and m.metadata.num_delivered == attempt
            await m.nak()
        with pytest.raises((TimeoutError, asyncio.TimeoutError, Exception)):
            await pool_sub.fetch(1, timeout=0.2)

        for _ in range(40):
            ci_p = await admin_js.consumer_info(part_stream, inbox_consumer)
            ci_pool = await admin_js.consumer_info(pool_stream, pool_consumer)
            if ci_p.num_ack_pending == 0 and ci_pool.num_ack_pending == 0:
                break
            await asyncio.sleep(0.05)

        services_live = _live_host_on_client(services.nc, "team-services-1", "test-services-key-1", 2)
        dlq_service = DeadLetterService(
            team_id=TEAM,
            live_fn=lambda: services_live,
            access_repository=access,
            people=people,
            chat_sender=chat,
            clock=lambda: fixtures.NOW,
            report_quiet_seconds=0.0,
            fetch_timeout_seconds=1.0,
        )

        summary1 = await dlq_service.sweep_work_streams()
        assert summary1.messages_swept == 2
        assert summary1.streams_failed == 0
        assert summary1.stopped_full is False
        # Pending seq 2 in partition stream is still untouched!
        assert (await admin_js.stream_info(part_stream)).state.messages == 1
        assert (await admin_js.stream_info(pool_stream)).state.messages == 0

        # Second sweep is idempotent (0 swept).
        summary2 = await dlq_service.sweep_work_streams()
        assert summary2.messages_swept == 0

        # Notice watch reports both swept delivery_limit messages to administrators.
        assert await dlq_service.poll_notices_once() == 2
        notices = access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))
        assert len(notices) == 1
        sweep_notice, _ = notices[0]
        assert "ran out of allowed deliveries" in sweep_notice.summary
        assert part_stream in sweep_notice.summary and pool_stream in sweep_notice.summary
        assert lead.errors.empty() and worker.errors.empty() and services.errors.empty()

    # -- Part 2 (C24): real InstallationAuthority with its real generated installation_authority user --
    # Sweeps an exhausted message in UA1_TEAM_WORK_<team> into UA1_DLQ_<team>, and team_services reports it.
    layout = InstanceLayout(tmp_path / "auth_instance")
    message_hosts.prepare_message_host_trust(layout, {}, partitions=COUNT)
    auth_keys = load_authority_keys(layout)
    auth_host_keys = load_host_keys(layout, AUTHORITY_RUNTIME)
    real_inst = auth_keys.installation
    auth_team = "team_auth_sweep"

    # Build real generated operator-mode NATS users for both installation_authority and team_services!
    account: OperatorAccount = isolated_broker.account
    auth_perms = host_permissions(
        role_document("installation_authority"),
        HostPlacement(
            AUTHORITY_RUNTIME,
            (auth_team,),
            partitions={},
            pools={auth_team: ()},
            edges={auth_team: ()},
            installation=real_inst,
            peers=(),
        ),
    )
    ts_runtime = "team-services-auth"
    ts_perms = host_permissions(
        role_document("team_services"),
        HostPlacement(
            ts_runtime,
            (auth_team,),
            partitions={auth_team: tuple(range(COUNT))},
            pools={auth_team: ()},
            edges={auth_team: ()},
            installation=real_inst,
            peers=(),
        ),
    )
    auth_user = account.user(AUTHORITY_RUNTIME, auth_perms)
    ts_user = account.user(ts_runtime, ts_perms)

    authority = InstallationAuthority(
        layout,
        auth_keys,
        auth_host_keys,
        partitions=COUNT,
        queues=queue_limits(),
    )

    auth_storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "sweep_auth.db")}})
    auth_access = AccessRepository(auth_storage)
    auth_access.record_person(
        TeamPersonV1(
            team_id=auth_team,
            person_id=ADMIN_ID,
            person_ref=ADMIN_REF,
            standing=PersonStanding.ACTIVE,
            roles=(PERSON_ROLE_OWNER,),
            changed_at_utc=datetime.now(timezone.utc),
        )
    )
    auth_people = PeopleDirectory(auth_access, auth_team)
    auth_chat = RecordingChatSender()

    async with AsyncExitStack() as stack:
        auth_client = await _connect_runtime_login(stack, isolated_broker, AUTHORITY_RUNTIME, auth_user)
        ts_client = await _connect_runtime_login(stack, isolated_broker, ts_runtime, ts_user)

        # Real InstallationAuthority starts and provisions all streams/consumers under its real operator-mode JWT!
        await authority.start(auth_client.nc, teams=[auth_team])

        team_work_stream = f"UA1_TEAM_WORK_{auth_team}"
        team_consumer = f"team~{auth_team}"
        # Publish a message to UA1_TEAM_WORK_<team> and exhaust its max_deliveries via team_services' real consumer!
        await ts_client.nc.jetstream().publish(
            f"ua.v1.{auth_team}.{ts_runtime}.work.team",
            b'{"exhausted":"team_work_message"}',
        )
        max_deliver = int(TRANSPORT["consumers"]["team_services"]["max_deliveries"])
        ts_work_sub = await ts_client.nc.jetstream().pull_subscribe_bind(team_consumer, stream=team_work_stream)
        for attempt in range(1, max_deliver + 1):
            (msg,) = await ts_work_sub.fetch(1, timeout=2.0)
            assert msg.metadata.num_delivered == attempt
            await msg.nak()
        with pytest.raises((TimeoutError, asyncio.TimeoutError, Exception)):
            await ts_work_sub.fetch(1, timeout=0.2)

        for _ in range(40):
            ci = await auth_client.nc.jetstream().consumer_info(team_work_stream, team_consumer)
            if ci.num_ack_pending == 0:
                break
            await asyncio.sleep(0.05)
        assert (await auth_client.nc.jetstream().stream_info(team_work_stream)).state.messages == 1

        # Real InstallationAuthority sweeps UA1_TEAM_WORK_<team> into UA1_DLQ_<team> under its real JWT!
        auth_sweep = await authority.sweep_delivery_limits()
        assert auth_sweep.messages_swept == 1
        assert auth_sweep.streams_failed == 0
        assert auth_sweep.stopped_full is False
        assert (await auth_client.nc.jetstream().stream_info(team_work_stream)).state.messages == 0
        # Repeated sweep is a no-op.
        assert (await authority.sweep_delivery_limits()).messages_swept == 0

        # Build a real team_services LiveHost trusting the authority's bootstrap bundle and verify/report the notice!
        bundle = read_bundle(layout.trust_bootstrap_path)
        ts_keys, _ = ensure_host_keys(layout, ts_runtime)
        ts_cert_doc = authority.certificate_record(
            _cert_request_for_team_services(ts_runtime, auth_team, ts_keys),
            epoch=1,
            revocation_epoch=1,
            issued=datetime.now(timezone.utc),
        )
        ts_trust = PersistentTrustStore.open(bundle, layout.host_trust_dir(ts_runtime), datetime.now(timezone.utc))
        ts_trust.add(json.dumps(ts_cert_doc).encode("utf-8"), now=datetime.now(timezone.utc))
        ts_cert = ts_trust.store.host_certificate(real_inst, ts_runtime, 1)
        assert ts_cert is not None
        ts_live = LiveHost(ts_client.nc, HostIdentity(ts_keys, ts_cert), ts_trust)

        ts_dlq = DeadLetterService(
            team_id=auth_team,
            live_fn=lambda: ts_live,
            access_repository=auth_access,
            people=auth_people,
            chat_sender=auth_chat,
            report_quiet_seconds=0.0,
            fetch_timeout_seconds=1.0,
        )
        assert await ts_dlq.poll_notices_once() == 1
        auth_notices = auth_access.notices_for(auth_team, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))
        assert len(auth_notices) == 1
        auth_notice, _ = auth_notices[0]
        assert team_work_stream in auth_notice.summary
        assert f"set aside by {AUTHORITY_RUNTIME}" in auth_notice.summary
        assert "ran out of allowed deliveries" in auth_notice.summary
        assert len(auth_chat.sent) == 1

        assert auth_client.errors.empty() and ts_client.errors.empty()


def _cert_request_for_team_services(runtime: str, team: str, keys: HostKeys) -> Any:
    from installation_authority.service import CertificateRequest

    return CertificateRequest(
        runtime=runtime,
        teams=(team,),
        roles=("team_services",),
        agent_types=(),
        session_limit=0,
        connection_class="connected",
        key_id=keys.key_id,
        public_key=keys.public_key_text,
        nats_public_key=keys.nats_public_key,
    )


@pytest.mark.nats
@pytest.mark.asyncio
async def test_live_departure_deletion_including_old_format_copy_and_orphan_cleanup(
    isolated_broker: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    """A8 (A6, C01, C04, C06, C21): Unified team.delete_person_data over real operator-mode svc.team
    immediately deletes departed person's protocol 1 and upgrade_old_format raw copies while keeping
    active people's copies; watch never leaks departed prompt text; scan_raw_copies cleans old orphans."""

    access, people = _access_and_people(tmp_path)
    chat = RecordingChatSender()

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        services = await connect(stack, isolated_broker, "services")

        admin_js = admin.nc.jetstream()
        dlq_stream = await _add_dlq_stream_and_consumer(admin_js, team=TEAM)

        lead_live = _live_host_on_client(lead.nc, "lead-host-1", "test-lead-key-1", 3)
        worker_live = _live_host_on_client(worker.nc, "worker-host-1", "test-worker-key-1", 3)
        services_live = _live_host_on_client(services.nc, "team-services-1", "test-services-key-1", 2)

        setter = DeadLetterSetter(worker_live)
        alice_p1_auth = json.dumps({"protocol": "1.0-pre", "authority": {"person": ALICE_REF}, "body": {}}).encode()
        alice_p1_body = json.dumps({"protocol": "1.0-pre", "body": {"person": ALICE_REF}}).encode()
        alice_old = _legacy_user_prompt_bytes(
            person_id=ALICE_ID,
            task_id="task-alice-live-1",
            request_id="req-alice-live-1",
            prompt_text="Alice private confidential prompt text",
        )
        bob_old = _legacy_user_prompt_bytes(
            person_id=BOB_ID,
            task_id="task-bob-live-1",
            request_id="req-bob-live-1",
            prompt_text="Bob active prompt text",
        )

        res_alice_auth = await setter.set_aside(
            raw_bytes=alice_p1_auth,
            reason="malformed",
            original_subject=f"ua.v1.{TEAM}.lead-host-1.work.agent.researcher-2",
            original_stream=agent_stream("researcher-2"),
            original_stream_sequence=1,
            deliveries=1,
            stored_at=fixtures.NOW,
            team=TEAM,
        )
        res_alice_body = await setter.set_aside(
            raw_bytes=alice_p1_body,
            reason="invalid_body",
            original_subject=f"ua.v1.{TEAM}.lead-host-1.work.agent.researcher-2",
            original_stream=agent_stream("researcher-2"),
            original_stream_sequence=2,
            deliveries=1,
            stored_at=fixtures.NOW,
            team=TEAM,
        )
        res_alice_old = await setter.set_aside(
            raw_bytes=alice_old,
            reason="upgrade_old_format",
            original_subject="ua.legacy.work.team_lead",
            original_stream="UA_WORK",
            original_stream_sequence=3,
            deliveries=1,
            stored_at=fixtures.NOW,
            team=TEAM,
        )
        res_bob_old = await setter.set_aside(
            raw_bytes=bob_old,
            reason="upgrade_old_format",
            original_subject="ua.legacy.work.team_lead",
            original_stream="UA_WORK",
            original_stream_sequence=4,
            deliveries=1,
            stored_at=fixtures.NOW,
            team=TEAM,
        )
        # Also publish an orphaned raw copy (no matching notice) from worker.
        orphan_copy_id = "e" * 64
        orphan_subject = dlq_raw_subject(TEAM, "worker-host-1", orphan_copy_id)
        await worker.nc.jetstream().publish(orphan_subject, b"abandoned orphan raw copy")

        # Serve TeamPersonDataOperations on real svc.team under team_services operator-mode JWT!
        clock_state = {"now": fixtures.NOW}
        dlq_service = DeadLetterService(
            team_id=TEAM,
            live_fn=lambda: services_live,
            access_repository=access,
            people=people,
            chat_sender=chat,
            clock=lambda: clock_state["now"],
            report_quiet_seconds=0.0,
            fetch_timeout_seconds=1.0,
            orphan_grace_seconds=600,
        )
        departure = TeamPersonDepartureService(
            team_id=TEAM,
            live_fn=lambda: services_live,
            people=people,
            blobs=None,
        )
        ops = TeamPersonDataOperations(dead_letters=dlq_service, departure=departure, people=people)
        services_receiver = Receiver(INSTALLATION, TEAM, "team_services", TEAM, "team-services-1")
        await services_live.serve("svc.team", services_receiver, ops.handle_delete_person_data)
        await services.nc.flush()

        # Alice departs -> lead_host sends real signed team.delete_person_data over svc.team!
        access.record_person(
            TeamPersonV1(
                team_id=TEAM,
                person_id=ALICE_ID,
                person_ref=ALICE_REF,
                standing=PersonStanding.DISABLED,
                roles=(),
                changed_at_utc=fixtures.NOW,
            )
        )
        to_services = {"installation": INSTALLATION, "team": TEAM, "target": "team_services", "id": TEAM}
        reply = await lead_live.request(
            "team.delete_person_data",
            {"request_id": "live_dep_alice_1", "person": ALICE_REF},
            to_services,
            team=TEAM,
        )
        assert reply.raw["body"] == {
            "outcome": "ok",
            "owners_removed": 0,
            "blobs_deleted": 0,
            "blobs_kept": 0,
            "dlq_copies_deleted": 3,
            "dlq_copies_kept": 2,
        }

        # All 3 Alice raw copies (including upgrade_old_format) are gone; Bob's copy and orphan remain.
        for deleted_res in (res_alice_auth, res_alice_body, res_alice_old):
            with pytest.raises(nats.js.errors.NotFoundError):
                await admin_js.get_last_msg(dlq_stream, deleted_res.raw_subject)
        assert (await admin_js.get_last_msg(dlq_stream, res_bob_old.raw_subject)).data == bob_old
        assert (await admin_js.get_last_msg(dlq_stream, orphan_subject)).data == b"abandoned orphan raw copy"

        # Notice watch reports Bob's upgrade_old_format identifiers (no prompt text) and never leaks Alice's prompt text.
        assert await dlq_service.poll_notices_once() == 4
        notices = access.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))
        assert len(notices) == 1
        dep_notice, _ = notices[0]
        summary = dep_notice.summary
        assert "task-bob-live-1" in summary and "req-bob-live-1" in summary and BOB_ID in summary
        assert "Alice private confidential prompt text" not in summary
        assert "Bob active prompt text" not in summary

        # Advance clock well past real NATS message stored time + orphan grace -> scan_raw_copies deletes the orphan
        # while keeping Bob's paired copy!
        clock_state["now"] = datetime.now(timezone.utc) + timedelta(hours=2)
        raw_report = await dlq_service.scan_raw_copies()
        assert raw_report.orphans_deleted == 1
        assert raw_report.departed_deleted == 0
        with pytest.raises(nats.js.errors.NotFoundError):
            await admin_js.get_last_msg(dlq_stream, orphan_subject)
        assert (await admin_js.get_last_msg(dlq_stream, res_bob_old.raw_subject)).data == bob_old

        assert lead.errors.empty() and worker.errors.empty() and services.errors.empty()


@contextmanager
def _postgres_access_storage() -> Iterator[StorageSettings]:
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL dead-letter access store tests")
    schema = "dlq_access_test_" + uuid4().hex
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
def test_postgres_dlq_reported_notices_and_scan_cursors() -> None:
    """AccessRepository protocol 1 DLQ receipts and scan cursors work cleanly on real PostgreSQL (C02, C10, C20)."""

    with _postgres_access_storage() as settings:
        repo = AccessRepository(settings)
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        other_team = "team_other"
        notice1 = AccessNoticeV1(
            notice_id="dlq-pg-notice-1",
            team_id=TEAM,
            recipient_roles=(PERSON_ROLE_ADMIN,),
            kind=AccessNoticeKind.MESSAGES_SET_ASIDE,
            title="Messages set aside",
            summary="2 messages set aside",
            created_at_utc=now,
        )
        receipts1 = [("worker-host-1", "a" * 64), ("worker-host-1", "b" * 64)]

        # Atomic insert of AccessNoticeV1 + per-copy receipts.
        created, inserted = repo.record_dlq_notice_report(TEAM, notice1, receipts1, reported_at_utc=now)
        assert created is True and inserted == 2
        assert repo.dlq_reported_copy_keys(TEAM, receipts1) == set(receipts1)
        assert repo.dlq_reported_copy_keys(other_team, receipts1) == set()

        # Replaying the same receipts is a no-op even under a different notice_id (C02).
        notice_dup = notice1.model_copy(update={"notice_id": "dlq-pg-notice-dup"})
        created_dup, inserted_dup = repo.record_dlq_notice_report(
            TEAM, notice_dup, receipts1, reported_at_utc=now + timedelta(minutes=1)
        )
        assert created_dup is False and inserted_dup == 0
        assert len(repo.notices_for(TEAM, person_id=ADMIN_ID, roles=(PERSON_ROLE_ADMIN,))) == 1

        # Batched lookup over >200 keys exercises chunking and distinguishes (runtime, copy_id) pairs (C20).
        many_keys = [("worker-host-2", f"{i:064x}") for i in range(250)]
        notice_many = notice1.model_copy(
            update={"notice_id": "dlq-pg-notice-many", "created_at_utc": now + timedelta(days=5)}
        )
        created_many, inserted_many = repo.record_dlq_notice_report(
            TEAM, notice_many, [receipts1[0], *many_keys], reported_at_utc=now + timedelta(days=5)
        )
        assert created_many is True and inserted_many == 250
        found = repo.dlq_reported_copy_keys(
            TEAM,
            [("other-runtime", receipts1[0][1]), receipts1[0], many_keys[0], many_keys[-1]],
        )
        assert found == {receipts1[0], many_keys[0], many_keys[-1]}

        # Pruning deletes only receipts older than before_utc for the target team.
        notice_other = notice1.model_copy(update={"notice_id": "dlq-pg-notice-other", "team_id": other_team})
        repo.record_dlq_notice_report(other_team, notice_other, [receipts1[0]], reported_at_utc=now)
        pruned = repo.prune_dlq_reported_notices(TEAM, before_utc=now + timedelta(days=1))
        assert pruned == 2
        assert repo.dlq_reported_copy_keys(TEAM, receipts1) == set()
        assert repo.dlq_reported_copy_keys(other_team, [receipts1[0]]) == {receipts1[0]}
        assert len(repo.dlq_reported_copy_keys(TEAM, many_keys)) == 250

        # Scan cursor defaults to (0, "") and upserts BIGINT sequences cleanly via ON CONFLICT.
        dlq_stream = dlq_stream_name(TEAM)
        assert repo.get_dlq_scan_cursor(TEAM, dlq_stream) == (0, "")
        big_seq = 5_000_000_000
        repo.set_dlq_scan_cursor(
            TEAM,
            dlq_stream,
            big_seq,
            stream_created="2026-11-25T10:00:00Z",
            updated_at_utc=now,
        )
        assert repo.get_dlq_scan_cursor(TEAM, dlq_stream) == (big_seq, "2026-11-25T10:00:00Z")
        repo.set_dlq_scan_cursor(
            TEAM,
            dlq_stream,
            big_seq + 42,
            stream_created="2026-11-25T11:00:00Z",
            updated_at_utc=now + timedelta(hours=1),
        )
        assert repo.get_dlq_scan_cursor(TEAM, dlq_stream) == (big_seq + 42, "2026-11-25T11:00:00Z")
