"""The team services on a real NATS server: the single-instance lock, sessions and placement, host liveness.

team_services and custom_transport.protocol1.host_runtime with the installation
authority running; the message contract's README "Agent sessions" and "State records".
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path

import nats
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.kinds import PROTOCOL
from custom_messages.wire.trust import sign_state
from custom_transport.protocol1.host_keys import ensure_host_keys, load_authority_keys, load_host_keys
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.joining import HostProfile
from custom_transport.protocol1.live import utc_now
from docker import message_hosts
from installation_authority.expected_hosts import write_expected_hosts
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from runtime_settings.instance_layout import InstanceLayout
from team_services import process as team_services_process
from team_services.lock import lock_key
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from tests.own_nats_server import own_nats_server, own_nats_server_process
from tests.test_live_message_contract import nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
TEAM = "team_one"
WORKER = HostProfile("worker-host-1", (TEAM,), ("worker_host",), ("researcher",), 2)
OTHER = HostProfile("worker-host-2", (TEAM,), ("worker_host",), ("researcher",), 2)
TO_SERVICES = {"target": "team_services", "id": TEAM, "team": TEAM}
DEFINITION = "DQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0"


@pytest.fixture
def broker(tmp_path: Path):
    with own_nats_server(nats_server(), tmp_path) as url:
        yield url


async def connect(stack: AsyncExitStack, url: str, runtime: str):
    connection = await nats.connect(url, inbox_prefix=f"_INBOX.{runtime}".encode(), allow_reconnect=False)
    stack.push_async_callback(connection.close)
    return connection


async def installation(stack: AsyncExitStack, url: str, layout: InstanceLayout, *profiles: HostProfile) -> None:
    """What the launcher does: keys, the expected hosts, then a running authority."""

    hosts = {profile.runtime: message_hosts.expected_host(layout, profile) for profile in profiles}
    message_hosts.prepare_message_host_trust(layout, hosts)
    authority = InstallationAuthority(layout, load_authority_keys(layout), load_host_keys(layout, AUTHORITY_RUNTIME))
    await authority.start(await connect(stack, url, AUTHORITY_RUNTIME), teams=[TEAM])


async def team_services(stack: AsyncExitStack, url: str, layout: InstanceLayout, stop: asyncio.Event) -> asyncio.Task:
    return (await team_services_host(stack, url, layout, stop))[1]


async def team_services_host(stack: AsyncExitStack, url: str, layout: InstanceLayout,
                             stop: asyncio.Event) -> tuple[MessageHost, asyncio.Task]:
    host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, url, TEAM_SERVICES_RUNTIME))
    return host, asyncio.create_task(serve_team(host, TEAM, stop))


async def host(stack: AsyncExitStack, url: str, layout: InstanceLayout, profile: HostProfile) -> MessageHost:
    started = MessageHost(layout, profile, await connect(stack, url, profile.runtime))
    await started.start()
    return started


def audience(started: MessageHost) -> dict[str, str]:
    return {"installation": started.identity.installation, **TO_SERVICES}


async def open_session(started: MessageHost, agent: str, agent_type: str = "researcher", *, attempts: int = 50) -> dict:
    assert started.live is not None
    for _ in range(attempts):  # the team services may still be taking the lock
        try:
            reply = await started.live.request("sessions.open", {"request_id": f"open-{agent}", "agent": agent,
                                                                 "agent_type": agent_type, "definition_sha256": DEFINITION},
                                               audience(started), team=TEAM)
            return reply.raw["body"]
        except Exception as error:  # no responders while the team services start
            if "no responders" not in str(error):
                raise
            await asyncio.sleep(0.1)
    raise AssertionError("the team services never answered")


@pytest.mark.asyncio
async def test_a_host_gets_renews_and_closes_its_agents_sessions(broker: str, tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER, OTHER)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        opened = await open_session(worker, "researcher-1")
        assert opened["outcome"] == "ok"
        session = opened["session"]
        assert (session["body"]["agent_role"], session["body"]["runtime"]) == ("worker", WORKER.runtime)
        # The team services' certificate names their role only: they sign no authority tokens and run no schedules.
        assert worker.live is not None
        services_certificate = worker.live.trust.store.issued_certificate(json.loads(
            (await (await worker.nc.jetstream().key_value("UA1_CERTS")).get(
                f"host.{session['body']['installation']}.{TEAM_SERVICES_RUNTIME}.1")).value))
        assert services_certificate.roles == frozenset({"team_services"})
        # The host keeps the session and may now sign for the agent.
        assert worker.live is not None
        worker.live.trust.add(json.dumps(session).encode(), now=worker.clock())
        worker.live.identity = worker.live.identity.with_session("researcher-1", session["body"]["session"])
        assert worker.live.identity.issuer(TEAM, "researcher-1")["session"] == session["body"]["session"]
        # Another host cannot take the agent; a host cannot run more than its limit, or another type.
        other = await host(stack, broker, layout, OTHER)
        refused = await open_session(other, "researcher-1")
        assert (refused["outcome"], refused["host"]) == ("already_placed", WORKER.runtime)
        assert (await open_session(worker, "researcher-2"))["outcome"] == "ok"
        assert (await open_session(worker, "researcher-3"))["outcome"] == "limit_reached"
        assert (await open_session(other, "writer-1", agent_type="writer"))["outcome"] == "not_allowed"
        renewed = await worker.live.request("sessions.renew", {"request_id": "renew-1", "session": session["body"]["session"]},
                                            audience(worker), team=TEAM)
        assert renewed.raw["body"]["session"]["sequence"] == 2
        closed = await worker.live.request("sessions.close", {"request_id": "close-1",
                                                              "session": session["body"]["session"], "reason": "exited"},
                                           audience(worker), team=TEAM)
        assert closed.raw["body"]["outcome"] == "ok"
        # Its placement is gone, so the other host may now run researcher-1.
        assert (await open_session(other, "researcher-1"))["outcome"] == "ok"
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_hosts_report_liveness_that_others_verify(broker: str, tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, WORKER, OTHER)
        worker = await host(stack, broker, layout, WORKER)
        other = await host(stack, broker, layout, OTHER)
        assert other.records is not None
        report = await other.records.read_report("host", WORKER.runtime, now=other.clock())
        assert report is not None
        body = report.record.body
        assert (body.runtime, body.state, body.certificate.membership_epoch) == (WORKER.runtime, "accepting", 1)
        assert body.contract_sha256 == worker.liveness_body("accepting")["contract_sha256"]
        stop = asyncio.Event()
        stop.set()
        await worker.keep_up(stop)
        drained = await other.records.read_report("host", WORKER.runtime, now=other.clock(),
                                                  held=(report.record.document.sequence, report.record.digest))
        assert drained is not None and drained.record.body.state == "drained"


@pytest.mark.asyncio
async def test_a_second_team_services_instance_waits_and_takes_over(broker: str, tmp_path: Path, caplog) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    first_stop, second_stop = asyncio.Event(), asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER)
        first = await team_services(stack, broker, layout, first_stop)
        worker = await host(stack, broker, layout, WORKER)
        assert (await open_session(worker, "researcher-1"))["outcome"] == "ok"
        with caplog.at_level(logging.WARNING):
            standby, second = await team_services_host(stack, broker, layout, second_stop)
            for _ in range(50):
                if "event=team_services_standby" in caplog.text:
                    break
                await asyncio.sleep(0.1)
        assert "event=team_services_standby" in caplog.text
        # The standby has not written the team services' host record: it is the serving instance's.
        hosts = await worker.nc.jetstream().key_value("UA1_HOSTS")
        assert standby._started_at is not None
        serving_record = json.loads((await hosts.get(TEAM_SERVICES_RUNTIME)).value)
        assert serving_record["body"]["started_at"] != format_timestamp(standby._started_at)
        # The first stops and releases the lock; the standby takes over at once and serves.
        with caplog.at_level(logging.INFO):
            first_stop.set()
            await asyncio.wait_for(first, timeout=10)
            for _ in range(150):
                if caplog.text.count("event=team_services_lock_held") >= 1:
                    break
                await asyncio.sleep(0.1)
        assert "event=team_services_lock_held" in caplog.text
        entry = await (await worker.nc.jetstream().key_value("UA1_LOCKS")).get(lock_key(TEAM))
        assert json.loads(entry.value)["runtime"] == TEAM_SERVICES_RUNTIME
        # Serving now, it writes the record.
        for _ in range(50):
            taken_over = json.loads((await hosts.get(TEAM_SERVICES_RUNTIME)).value)
            if taken_over["body"]["started_at"] == format_timestamp(standby._started_at):
                break
            await asyncio.sleep(0.1)
        assert taken_over["body"]["started_at"] == format_timestamp(standby._started_at)
        assert taken_over["body"]["state"] == "accepting"
        assert (await open_session(worker, "researcher-2"))["outcome"] == "ok"
        second_stop.set()
        await asyncio.wait_for(second, timeout=10)
        assert ensure_host_keys(layout, WORKER.runtime)[1] is False


@pytest.mark.asyncio
async def test_operations_not_served_yet_are_answered_unsupported() -> None:
    """team_services.service: a kind without a handler here is refused at once (README "Refused requests")."""

    from types import SimpleNamespace

    from custom_transport.protocol1.live import Unsupported
    from team_services.service import TeamServices

    services = TeamServices(SimpleNamespace(open=None, renew=None, close=None))  # type: ignore[arg-type]
    assert services.served == frozenset({"sessions.open", "sessions.renew", "sessions.close"})
    with pytest.raises(Unsupported):
        await services.handle(SimpleNamespace(frame=SimpleNamespace(kind="shared_work.count")))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_host_that_renews_its_certificate_keeps_writing_records_readers_accept(broker: str, tmp_path: Path) -> None:
    """Plan 13B.12: after a renewal, host records, placements and sessions are signed with the new certificate."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER, OTHER)
        services, serving = await team_services_host(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        other = await host(stack, broker, layout, OTHER)
        assert other.records is not None and services.records is not None
        assert (await open_session(worker, "researcher-1"))["outcome"] == "ok"
        # Renewal comes on day 20 of 30; here both hosts renew at once.
        await worker.renew()
        await services.renew()
        assert worker.identity.certificate.membership_epoch == services.identity.certificate.membership_epoch == 2

        # The worker's next host record names the new certificate and is signed with it.
        await worker.publish_liveness("accepting")
        report = await other.records.read_report("host", WORKER.runtime, now=other.clock())
        assert report is not None and report.record.signer is not None
        assert report.record.signer.membership_epoch == report.record.body.certificate.membership_epoch == 2

        # The team services place and sign with theirs.
        opened = await open_session(worker, "researcher-2")
        assert opened["outcome"] == "ok" and opened["session"]["writer"]["membership_epoch"] == 2
        placement = await other.records.read_report("placement", f"{TEAM}.researcher-2", now=other.clock())
        assert placement is not None and placement.record.signer is not None
        assert placement.record.signer.membership_epoch == 2

        # Once the old certificates sign nothing more (5 minutes after their replacement), what the
        # hosts write is still accepted.
        later = utc_now() + timedelta(minutes=6)
        body = placement.record.document.model_dump(mode="json")["body"]
        moved = services.records.signed("placement", f"{TEAM}.researcher-2", body,
                                        sequence=placement.record.document.sequence + 1, issued_at=later)
        accepted = other.live.trust.store.verify(json.dumps(moved).encode(), record="placement", now=later)
        assert accepted.signer is not None and accepted.signer.membership_epoch == 2
        host_record = worker.records.signed("host", WORKER.runtime, worker.liveness_body("accepting"),
                                            sequence=report.record.document.sequence + 1, issued_at=later,
                                            expires_at=later + timedelta(seconds=30))
        assert other.live.trust.store.verify(json.dumps(host_record).encode(), record="host", now=later).signer is not None
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


def forged(document: dict, **body: object) -> dict:
    """``document`` with ``body`` changes, signed by a key that is not the team services': what any process with the
    shared NATS login can write to the buckets."""

    return sign_state({**document, "body": {**document["body"], **body}}, Ed25519PrivateKey.generate())


async def put_raw(nc, bucket: str, key: str, value: bytes) -> None:
    await (await nc.jetstream().key_value(bucket)).put(key, value)


async def renew_session(started: MessageHost, session: str) -> dict:
    assert started.live is not None
    reply = await started.live.request("sessions.renew", {"request_id": f"renew-{session}-{time.monotonic_ns()}",
                                                          "session": session}, audience(started), team=TEAM)
    return reply.raw["body"]


@pytest.mark.asyncio
async def test_the_team_services_use_only_the_placements_and_sessions_they_signed(broker: str, tmp_path: Path,
                                                                                  caplog) -> None:
    """Every process shares one NATS login, so any of them can write UA1_PLACEMENT and UA1_CERTS."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER, OTHER)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        writer = await connect(stack, broker, "writer")
        opened = await open_session(worker, "researcher-1")
        session = opened["session"]
        name, installation_id = session["body"]["session"], session["body"]["installation"]
        placement = json.loads((await (await writer.jetstream().key_value("UA1_PLACEMENT")).get(f"{TEAM}.researcher-1")).value)

        with caplog.at_level(logging.WARNING):
            # A session written for a lead the team services never placed, with its placement: renewing it must not
            # get it signed.
            lead = forged(session, session="s-lead-1-1", agent="lead-1", agent_type="TeamArchitect", agent_role="lead")
            lead["key"] = f"session.{installation_id}.{TEAM}.s-lead-1-1"
            lead = forged(lead)
            await put_raw(writer, "UA1_CERTS", lead["key"], json.dumps(lead).encode())
            lead_placement = forged({**placement, "key": f"{TEAM}.lead-1"}, agent="lead-1", agent_type="TeamArchitect")
            await put_raw(writer, "UA1_PLACEMENT", f"{TEAM}.lead-1", json.dumps(lead_placement).encode())
            assert (await renew_session(worker, "s-lead-1-1"))["outcome"] == "not_found"

            # A genuine session written over with a lead role: the renewal re-signs what the team services signed.
            await put_raw(writer, "UA1_CERTS", session["key"], json.dumps(forged(session, agent_role="lead")).encode())
            renewed = await renew_session(worker, name)
            assert renewed["outcome"] == "ok" and renewed["session"]["body"]["agent_role"] == "worker"
            assert renewed["session"]["sequence"] == session["sequence"] + 1

            # A placement written for an agent on another host does not keep it off this one, and an unreadable
            # placement does not stop the host's sessions.
            elsewhere = forged({**placement, "key": f"{TEAM}.researcher-9"}, agent="researcher-9", host=OTHER.runtime)
            await put_raw(writer, "UA1_PLACEMENT", f"{TEAM}.researcher-9", json.dumps(elsewhere).encode())
            await put_raw(writer, "UA1_PLACEMENT", f"{TEAM}.researcher-8", b"not a record")
            placed = await open_session(worker, "researcher-9")
            assert placed["outcome"] == "ok" and placed["session"]["body"]["runtime"] == WORKER.runtime
            # A genuine placement copied under another agent's key is not that agent's either.
            await put_raw(writer, "UA1_PLACEMENT", f"{TEAM}.researcher-7", json.dumps(placement).encode())
            assert (await open_session(worker, "researcher-7"))["outcome"] == "limit_reached"
        refusals = [record.getMessage() for record in caplog.records if "event=team_services_record_refused" in
                    record.getMessage()]
        assert any(f"key=session.{installation_id}.{TEAM}.s-lead-1-1" in line and "code=bad_signature" in line
                   for line in refusals)
        assert any(f"key={TEAM}.researcher-8" in line and "code=malformed" in line for line in refusals)
        assert any(f"key={TEAM}.researcher-7" in line for line in refusals)
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_a_session_signed_before_the_team_services_renewed_and_restarted_is_still_renewed(broker: str,
                                                                                                tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    first_stop, second_stop = asyncio.Event(), asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER)
        services, first = await team_services_host(stack, broker, layout, first_stop)
        worker = await host(stack, broker, layout, WORKER)
        session = (await open_session(worker, "researcher-1"))["session"]
        await services.renew()
        first_stop.set()
        await asyncio.wait_for(first, timeout=10)
        # A new process, whose trust store has only the renewed certificate: the session's signer is read from
        # UA1_CERTS.
        restarted, second = await team_services_host(stack, broker, layout, second_stop)
        for _ in range(50):
            try:
                renewed = await renew_session(worker, session["body"]["session"])
                break
            except Exception as error:  # no responders while the new instance starts
                if "no responders" not in str(error):
                    raise
                await asyncio.sleep(0.1)
        assert renewed["outcome"] == "ok" and renewed["session"]["writer"]["membership_epoch"] == 2
        assert restarted.identity.certificate.membership_epoch == 2
        second_stop.set()
        await asyncio.wait_for(second, timeout=10)


@pytest.mark.asyncio
async def test_a_closed_session_is_not_renewed_when_its_agent_is_placed_again(broker: str, tmp_path: Path) -> None:
    """A renewal signs again only a session that has not ended and whose placement is the current one."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        assert worker.live is not None
        first = (await open_session(worker, "researcher-1"))["session"]["body"]["session"]
        closed = await worker.live.request("sessions.close", {"request_id": "close-1", "session": first,
                                                              "reason": "exited"}, audience(worker), team=TEAM)
        assert closed.raw["body"]["outcome"] == "ok"
        # The agent is placed on the same host again: a new placement, and a new session naming it.
        second = (await open_session(worker, "researcher-1"))["session"]["body"]["session"]
        assert second != first
        refused = await renew_session(worker, first)
        assert refused["outcome"] == "not_allowed", refused
        assert (await renew_session(worker, second))["outcome"] == "ok"
        # Handed over: the placement stays (a move to another host comes with phase 3's custody windows), yet the
        # ended session is not renewed.
        handed = await worker.live.request("sessions.close", {"request_id": "close-2", "session": second,
                                                              "reason": "handed_over"}, audience(worker), team=TEAM)
        assert handed.raw["body"]["outcome"] == "ok"
        assert (await renew_session(worker, second))["outcome"] == "not_allowed"
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


OTHER_CONTRACT = "0" * 64


def run_on_another_contract(started: MessageHost, digest: str = OTHER_CONTRACT) -> None:
    """A host that joined on this contract runs ``digest`` from now on: its records and renewals name it, and it
    reads others' records against it.

    That is how a host that kept running while the rest of its installation moved to
    another pre-release contract looks from every side; a host started on another contract
    could not join at all (``protocol_mismatch``).
    """

    assert started.live is not None
    started.contract_sha256 = digest
    started.live.trust.store.contract_sha256 = digest


@pytest.mark.asyncio
async def test_a_host_on_another_contract_gets_no_new_placement_but_keeps_its_sessions(broker: str, tmp_path: Path,
                                                                                       caplog) -> None:
    """README "Versions": readers place no agent on a host whose record names another contract, but it still
    holds the agents it runs, so their sessions are renewed and closed as usual."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        assert worker.live is not None
        session = (await open_session(worker, "researcher-1"))["session"]["body"]["session"]
        run_on_another_contract(worker)
        await worker.publish_liveness("draining")
        refused = await open_session(worker, "researcher-2")
        assert refused["outcome"] == "not_allowed" and "another message contract" in refused["message"], refused
        assert "event=agent_session_refused_other_contract" in caplog.text and OTHER_CONTRACT in caplog.text
        assert (await renew_session(worker, session))["outcome"] == "ok"
        closed = await worker.live.request("sessions.close", {"request_id": "close-1", "session": session,
                                                              "reason": "handed_over"}, audience(worker), team=TEAM)
        assert closed.raw["body"]["outcome"] == "ok"
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_a_host_on_another_contract_learns_it_drains_and_renews_only_to_give_up_its_agents(
        broker: str, tmp_path: Path) -> None:
    """README "Versions": the host learns from the team services' host record, which it reads when it starts and
    whenever it changes; a renewal for its agents is turned away, its record says ``draining`` and then
    ``drained``, and its renewal that gives up every agent gets a certificate that grants none."""

    from custom_messages.wire.kinds import CONTRACT_SHA256

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER)
        services, serving = await team_services_host(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        assert worker.live is not None and worker.records is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while services.records is None or await worker.records.read_report("host", TEAM_SERVICES_RUNTIME,
                                                                             now=utc_now()) is None:
            assert loop.time() < deadline, "the team services wrote no host record"
            await asyncio.sleep(0.1)
        learned: list[str] = []
        worker.on_other_contract(learned.append)
        run_on_another_contract(worker)
        following = asyncio.create_task(worker.follow_contract(TEAM_SERVICES_RUNTIME))
        while not learned:
            assert loop.time() < deadline, "the host never read the team services' record"
            await asyncio.sleep(0.05)
        assert learned == [CONTRACT_SHA256] and worker.installation_contract == CONTRACT_SHA256

        epoch = worker.identity.certificate.membership_epoch
        await worker.renew()  # for its agents: turned away, and nothing changes
        assert worker.identity.certificate.membership_epoch == epoch

        async def own_record_state() -> str:
            # Readers of the installation's contract cannot read its body; a reader of its own contract can.
            stored = await worker.records.read_report("host", WORKER.runtime, now=utc_now())  # type: ignore[union-attr]
            assert stored is not None and not stored.record.other_contract
            return str(stored.record.body.state)

        await worker.drain()
        assert await own_record_state() == "draining"
        await worker.finish_drain()
        assert await own_record_state() == "drained"
        given_up = worker.identity.certificate
        assert given_up.membership_epoch == epoch + 1
        assert (given_up.agent_types, given_up.session_limit) == (frozenset(), 0)
        following.cancel()
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_a_host_joins_again_for_an_agent_type_added_while_it_runs(broker: str, tmp_path: Path, caplog) -> None:
    """MessageHost.rejoin: every running agent holds a session, a specialist created at runtime too."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER, OTHER)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        other = await host(stack, broker, layout, OTHER)
        assert other.records is not None
        assert (await open_session(worker, "researcher-1"))["outcome"] == "ok"
        assert (await open_session(worker, "analyst-1", agent_type="analyst"))["outcome"] == "not_allowed"

        # Not listed for this host yet: the authority refuses, and the host keeps its certificate.
        refused = await worker.rejoin(["analyst"])
        assert (refused.granted, refused.refused, refused.not_certifiable) == (frozenset(), frozenset({"analyst"}),
                                                                             frozenset())
        assert refused.refusal is not None and refused.refusal.startswith("denied")
        assert refused.certificate == worker.identity.certificate and worker.identity.certificate.membership_epoch == 1

        # Listed now (the team's catalog gained it): joining again grants it at the next epoch. A name no
        # certificate can carry is never asked for, and the caller is told.
        wider = HostProfile(WORKER.runtime, WORKER.teams, WORKER.roles, ("analyst", "researcher"), WORKER.session_limit)
        write_expected_hosts(layout.trust_expected_hosts_path, {
            profile.runtime: message_hosts.expected_host(layout, profile)
            for profile in (team_services_profile(TEAM), wider, OTHER)})
        too_long = "a-specialist-type-name-too-long"
        with caplog.at_level(logging.WARNING):
            rejoined = await worker.rejoin(["analyst", too_long])
        assert (rejoined.granted, rejoined.refused, rejoined.not_certifiable) == (frozenset({"analyst"}), frozenset(),
                                                                                frozenset({too_long}))
        assert rejoined.refusal is None and f"agent_type={too_long}" in caplog.text
        assert worker.identity.certificate == rejoined.certificate
        assert rejoined.certificate.membership_epoch == 2
        assert rejoined.certificate.agent_types == frozenset({"analyst", "researcher"})
        assert worker.profile.agent_types == ("analyst", "researcher")

        # The host signs everything with the new certificate: a session for the new type, its host record.
        assert (await open_session(worker, "analyst-1", agent_type="analyst"))["outcome"] == "ok"
        await worker.publish_liveness("accepting")
        report = await other.records.read_report("host", WORKER.runtime, now=other.clock())
        assert report is not None and report.record.signer is not None
        assert report.record.signer.membership_epoch == report.record.body.certificate.membership_epoch == 2
        # A type already granted needs no join.
        assert (await worker.rejoin(["researcher"])).certificate.membership_epoch == 2
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_the_team_services_host_record_appears_only_once_they_serve(broker: str, tmp_path: Path) -> None:
    """Plan 13B.4: they take the lock before they publish their host record or serve; the record means they answer."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER)
        worker = await host(stack, broker, layout, WORKER)
        services = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        published = services.publish_liveness
        answered_then: list[str] = []

        async def publish_liveness(state: str = "accepting") -> None:
            if not answered_then:  # the first record: does svc.team answer already?
                try:
                    answered_then.append((await open_session(worker, "researcher-1", attempts=1))["outcome"])
                except AssertionError:
                    answered_then.append("no responders")
            await published(state)

        services.publish_liveness = publish_liveness  # type: ignore[method-assign]
        serving = asyncio.create_task(serve_team(services, TEAM, stop))
        hosts = await worker.nc.jetstream().key_value("UA1_HOSTS")
        for _ in range(100):
            try:
                await hosts.get(TEAM_SERVICES_RUNTIME)
                break
            except Exception:
                await asyncio.sleep(0.05)
        assert answered_then == ["ok"]
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_an_instance_that_lost_the_lock_leaves_the_host_record_to_the_one_serving(broker: str, tmp_path: Path,
                                                                                        monkeypatch) -> None:
    from team_services import lock as lock_module

    monkeypatch.setattr(lock_module, "RENEW_EVERY", timedelta(seconds=0.2))
    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER)
        worker = await host(stack, broker, layout, WORKER)
        serving = await team_services(stack, broker, layout, stop)
        assert (await open_session(worker, "researcher-1"))["outcome"] == "ok"
        # Another instance takes the lock (as after this one's lease ran out) and serves.
        later = format_timestamp(utc_now() + timedelta(seconds=30))
        await put_raw(worker.nc, "UA1_LOCKS", lock_key(TEAM),
                      json.dumps({"runtime": TEAM_SERVICES_RUNTIME, "instance": "another", "lease_ends_at": later}).encode())
        with pytest.raises(lock_module.LockLost):
            await asyncio.wait_for(serving, timeout=10)
        record = json.loads((await (await worker.nc.jetstream().key_value("UA1_HOSTS")).get(TEAM_SERVICES_RUNTIME)).value)
        assert record["body"]["state"] == "accepting"


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["unsubscribe", "drained_record"])
async def test_a_stop_whose_last_steps_fail_still_releases_the_lock(broker: str, tmp_path: Path, failing: str) -> None:
    """A failed unsubscribe or "drained" host record still releases the lock, and its error is still raised.

    Otherwise the next instance, such as the restart of every service, waits out the lease and the clock tolerance.
    """

    from types import SimpleNamespace

    from nats.js.errors import KeyNotFoundError

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER)
        worker = await host(stack, broker, layout, WORKER)
        services = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        if failing == "drained_record":
            published = services.publish_liveness

            async def publish_liveness(state: str = "accepting") -> None:
                if state == "drained":
                    raise RuntimeError("NATS did not store the drained record")
                await published(state)

            services.publish_liveness = publish_liveness  # type: ignore[method-assign]
        else:
            started = services.start

            async def start(*, report: bool = True):
                live = await started(report=report)
                serve = live.serve

                async def serve_and_fail_to_unsubscribe(*args, **kwargs):
                    subscription = await serve(*args, **kwargs)

                    async def unsubscribe() -> None:
                        await subscription.unsubscribe()
                        raise RuntimeError("the connection dropped while unsubscribing")

                    return SimpleNamespace(unsubscribe=unsubscribe)

                live.serve = serve_and_fail_to_unsubscribe  # type: ignore[method-assign]
                return live

            services.start = start  # type: ignore[method-assign]
        serving = asyncio.create_task(serve_team(services, TEAM, stop))
        assert (await open_session(worker, "researcher-1"))["outcome"] == "ok"
        stop.set()
        with pytest.raises(RuntimeError, match="drained record|unsubscribing"):
            await asyncio.wait_for(serving, timeout=10)
        locks = await worker.nc.jetstream().key_value("UA1_LOCKS")
        with pytest.raises(KeyNotFoundError):
            await locks.get(lock_key(TEAM))


@pytest.mark.asyncio
async def test_a_standby_that_takes_over_signs_with_the_certificate_the_serving_instance_renewed(
        broker: str, tmp_path: Path, caplog) -> None:
    """Every instance runs as host team-services: a standby that joined before the serving instance renewed
    their certificate must sign with the newest one once it serves, before it writes anything.

    Its old certificate signs nothing new from five minutes after its replacement (README "Trust over time"), so
    readers would refuse its host record, sessions and placements, and even its own renewal.
    """

    layout = InstanceLayout.at(tmp_path / "instance")
    first_stop, second_stop = asyncio.Event(), asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER, OTHER)
        serving, first = await team_services_host(stack, broker, layout, first_stop)
        worker = await host(stack, broker, layout, WORKER)
        other = await host(stack, broker, layout, OTHER)
        assert other.records is not None and other.live is not None
        assert (await open_session(worker, "researcher-1"))["outcome"] == "ok"
        with caplog.at_level(logging.WARNING):
            standby, second = await team_services_host(stack, broker, layout, second_stop)
            for _ in range(50):
                if "event=team_services_standby" in caplog.text:
                    break
                await asyncio.sleep(0.1)
        assert "event=team_services_standby" in caplog.text
        assert standby.identity.certificate.membership_epoch == 1
        # While the standby waits, the serving instance renews (day 20 of 30), then stops.
        await serving.renew()
        assert serving.identity.certificate.membership_epoch == 2
        first_stop.set()
        await asyncio.wait_for(first, timeout=10)
        hosts = await worker.nc.jetstream().key_value("UA1_HOSTS")
        for _ in range(150):
            record = json.loads((await hosts.get(TEAM_SERVICES_RUNTIME)).value)
            if record["body"]["started_at"] == format_timestamp(standby._started_at):
                break
            await asyncio.sleep(0.1)
        assert record["body"]["started_at"] == format_timestamp(standby._started_at)

        # Its first host record is signed with the renewed certificate, and readers accept it.
        report = await other.records.read_report("host", TEAM_SERVICES_RUNTIME, now=other.clock())
        assert report is not None and report.record.signer is not None
        assert report.record.signer.membership_epoch == report.record.body.certificate.membership_epoch == 2
        # So is a session it opens.
        opened = await open_session(worker, "researcher-2")
        assert opened["outcome"] == "ok" and opened["session"]["writer"]["membership_epoch"] == 2
        assert other.live.trust.add(json.dumps(opened["session"]).encode(), now=other.clock()) is True
        # And once the replaced certificate signs nothing more, what it writes is still accepted.
        later = utc_now() + timedelta(minutes=6)
        assert standby.records is not None
        host_record = standby.records.signed("host", TEAM_SERVICES_RUNTIME, standby.liveness_body("accepting"),
                                             sequence=report.record.document.sequence + 1, issued_at=later,
                                             expires_at=later + timedelta(seconds=30))
        accepted = other.live.trust.store.verify(json.dumps(host_record).encode(), record="host", now=later)
        assert accepted.signer is not None and accepted.signer.membership_epoch == 2
        second_stop.set()
        await asyncio.wait_for(second, timeout=10)


# --- closing, placing again, the lead role, refusals, signing placements again ------------------------


async def close_session(started: MessageHost, session: str, reason: str = "exited") -> dict:
    assert started.live is not None
    reply = await started.live.request("sessions.close", {"request_id": f"close-{session}-{time.monotonic_ns()}",
                                                          "session": session, "reason": reason},
                                       audience(started), team=TEAM)
    return reply.raw["body"]


async def stored_session(reader: MessageHost, installation_id: str, session: str) -> dict:
    entry = await (await reader.nc.jetstream().key_value("UA1_CERTS")).get(f"session.{installation_id}.{TEAM}.{session}")
    return json.loads(entry.value)


async def placement_of(reader: MessageHost, agent: str):
    assert reader.records is not None
    return await reader.records.read_report("placement", f"{TEAM}.{agent}", now=reader.clock())


@pytest.mark.asyncio
async def test_a_repeated_or_late_close_changes_nothing_and_never_removes_a_newer_placement(broker: str,
                                                                                              tmp_path: Path) -> None:
    """README "Agent sessions": closing a session that has already ended answers ok with its recorded end and changes
    nothing, and a session's close removes only its own placement, so no other host may take a running agent."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER, OTHER)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        other = await host(stack, broker, layout, OTHER)
        first = (await open_session(worker, "researcher-1"))["session"]
        installation_id, s1 = first["body"]["installation"], first["body"]["session"]
        ended = (await close_session(worker, s1))["ended_at"]
        s2 = (await open_session(worker, "researcher-1"))["session"]["body"]["session"]

        # A retry of the first close, its reply lost: the recorded end, nothing signed again, the new placement kept.
        assert await close_session(worker, s1) == {"outcome": "ok", "ended_at": ended}
        kept = await stored_session(worker, installation_id, s1)
        assert (kept["sequence"], kept["body"]["ends_at"]) == (2, ended)
        assert (await renew_session(worker, s2))["outcome"] == "ok"
        assert (await open_session(other, "researcher-1"))["outcome"] == "already_placed"

        # Placed again without a close: closing the superseded session leaves the newer placement too.
        s3 = (await open_session(worker, "researcher-1"))["session"]["body"]["session"]
        assert (await close_session(worker, s2))["outcome"] == "ok"
        assert (await renew_session(worker, s3))["outcome"] == "ok"
        assert (await open_session(other, "researcher-1"))["outcome"] == "already_placed"
        # Its own close still removes it.
        assert (await close_session(worker, s3))["outcome"] == "ok"
        assert (await open_session(other, "researcher-1"))["outcome"] == "ok"
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_a_session_whose_agent_was_placed_again_without_a_close_is_not_renewed(broker: str, tmp_path: Path) -> None:
    """README "Agent sessions": an open for an agent already placed on the asking host places it there again, and the
    earlier session, naming the earlier placement, can no longer be renewed."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        s1 = (await open_session(worker, "researcher-1"))["session"]["body"]["session"]
        s2 = (await open_session(worker, "researcher-1"))["session"]["body"]["session"]
        refused = await renew_session(worker, s1)
        assert refused["outcome"] == "not_allowed" and "earlier placement" in refused["message"]
        assert (await renew_session(worker, s2))["outcome"] == "ok"
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_the_placement_sequence_never_goes_back_after_a_close(broker: str, tmp_path: Path) -> None:
    """The placement's signed sequence is its fencing token: a reader that keeps the highest one it saw must accept
    the agent's next placement, also after a close deleted the key."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER, OTHER)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        other = await host(stack, broker, layout, OTHER)
        await open_session(worker, "researcher-1")
        s2 = (await open_session(worker, "researcher-1"))["session"]["body"]["session"]
        earlier = await placement_of(other, "researcher-1")
        assert earlier is not None
        await close_session(worker, s2)
        assert await placement_of(other, "researcher-1") is None
        assert (await open_session(other, "researcher-1"))["outcome"] == "ok"
        assert other.records is not None
        held = (earlier.record.document.sequence, earlier.record.digest)
        later = await other.records.read_report("placement", f"{TEAM}.researcher-1", now=other.clock(), held=held)
        assert later is not None and later.record.document.sequence > earlier.record.document.sequence
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_an_empty_placement_value_does_not_keep_the_agent_from_being_placed(broker: str, tmp_path: Path,
                                                                                    caplog) -> None:
    """Any process on the shared NATS login can write the key; one that holds no record counts as absent, and its
    revision lets the placement replace it."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        writer = await connect(stack, broker, "writer")
        await put_raw(writer, "UA1_PLACEMENT", f"{TEAM}.researcher-5", b"")
        with caplog.at_level(logging.WARNING):
            assert (await open_session(worker, "researcher-5"))["outcome"] == "ok"
        assert f"key={TEAM}.researcher-5 code=empty" in caplog.text
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


LEAD = HostProfile("lead-host", (TEAM,), ("lead_host", "worker_host"), ("TeamArchitect", "researcher"), 4)
WORKER_OF_LEAD_TYPE = HostProfile("worker-host-3", (TEAM,), ("worker_host",), ("TeamArchitect",), 1)


@pytest.mark.asyncio
async def test_only_the_team_leads_type_on_a_lead_host_gets_the_lead_role(broker: str, tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), LEAD, WORKER_OF_LEAD_TYPE)
        serving = await team_services(stack, broker, layout, stop)
        lead = await host(stack, broker, layout, LEAD)
        worker = await host(stack, broker, layout, WORKER_OF_LEAD_TYPE)
        assert (await open_session(lead, "lead-1", "TeamArchitect"))["session"]["body"]["agent_role"] == "lead"
        assert (await open_session(lead, "researcher-1"))["session"]["body"]["agent_role"] == "worker"
        assert (await open_session(worker, "lead-2", "TeamArchitect"))["session"]["body"]["agent_role"] == "worker"
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_every_refused_session_request_is_logged(broker: str, tmp_path: Path, caplog) -> None:
    """Plan 13B.12: a session request its certificate does not allow is refused, and the refusal is logged."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER, OTHER)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        other = await host(stack, broker, layout, OTHER)
        with caplog.at_level(logging.WARNING):
            await open_session(worker, "researcher-1")
            await open_session(worker, "researcher-2")
            outcomes = [(await open_session(worker, "writer-1", "writer"))["outcome"],
                        (await open_session(other, "researcher-1"))["outcome"],
                        (await open_session(worker, "researcher-3"))["outcome"],
                        (await renew_session(other, "s-nobody-1"))["outcome"]]
        assert outcomes == ["not_allowed", "already_placed", "limit_reached", "not_found"]
        refusals = [record.getMessage() for record in caplog.records if "event=agent_session_refused " in
                    record.getMessage()]
        for outcome, agent, runtime in [("not_allowed", "writer-1", WORKER.runtime),
                                        ("already_placed", "researcher-1", OTHER.runtime),
                                        ("limit_reached", "researcher-3", WORKER.runtime),
                                        ("not_found", "-", OTHER.runtime)]:
            assert any(f"outcome={outcome} team={TEAM} agent={agent} host={runtime}" in line for line in refusals), outcome
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_a_delete_under_a_revision_the_key_moved_past_is_reported_not_raised(broker: str, tmp_path: Path) -> None:
    """nats-py reports a refused delete as a bad request, not as the error it raises for updates."""

    layout = InstanceLayout.at(tmp_path / "instance")
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, WORKER)
        worker = await host(stack, broker, layout, WORKER)
        assert worker.records is not None
        key = f"{TEAM}.researcher-1"
        first = await worker.records.put("placement", key, {"note": "first"})
        second = await worker.records.put("placement", key, {"note": "second"})
        assert await worker.records.delete("placement", key, last_revision=first) is False
        assert await worker.records.revision("placement", key) == second
        assert await worker.records.delete("placement", key, last_revision=second) is True
        assert await worker.records.revision("placement", key) == 0


@pytest.mark.asyncio
async def test_the_team_services_sign_their_placements_again_after_they_renew(broker: str, tmp_path: Path,
                                                                              monkeypatch) -> None:
    """README "State reports outlive certificates": a reader that forgot the certificate that signed a placement
    refuses it, so the team services write their placements again under a current certificate, and the sessions
    that name those placements still renew."""

    monkeypatch.setattr(team_services_process, "MAINTENANCE_INTERVAL", timedelta(milliseconds=200))
    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER, OTHER)
        services, serving = await team_services_host(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        other = await host(stack, broker, layout, OTHER)
        session = (await open_session(worker, "researcher-1"))["session"]["body"]["session"]
        before = await placement_of(other, "researcher-1")
        assert before is not None and before.record.signer.membership_epoch == 1
        await services.renew()
        for _ in range(50):
            after = await placement_of(other, "researcher-1")
            if after is not None and after.record.signer.membership_epoch == 2:
                break
            await asyncio.sleep(0.1)
        assert after is not None and after.record.signer.membership_epoch == 2, "signed again under the new certificate"
        assert after.revision != before.revision and after.record.body.placed_at == before.record.body.placed_at
        assert after.record.document.sequence > before.record.document.sequence
        # The same placement: its session still renews, and no other host may take the agent.
        assert (await renew_session(worker, session))["outcome"] == "ok"
        assert (await open_session(other, "researcher-1"))["outcome"] == "already_placed"
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_the_team_services_delete_sessions_receivers_have_forgotten(broker: str, tmp_path: Path,
                                                                          monkeypatch) -> None:
    """README "Trust over time": writers delete a record from UA1_CERTS once receivers forget it."""

    from custom_messages.wire.trust import kept_for

    monkeypatch.setattr(team_services_process, "MAINTENANCE_INTERVAL", timedelta(milliseconds=200))
    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER)
        services, serving = await team_services_host(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        current = (await open_session(worker, "researcher-1"))["session"]
        assert services.records is not None
        installation_id = current["body"]["installation"]
        written = utc_now() - kept_for("agent_session") - timedelta(minutes=1)
        old_key = f"session.{installation_id}.{TEAM}.s-researcher-9-1"
        old = services.records.signed("agent_session", old_key, {
            **current["body"], "session": "s-researcher-9-1", "agent": "researcher-9", "placement_revision": 1,
            "started_at": format_timestamp(written), "ends_at": format_timestamp(written + timedelta(days=7))},
            sequence=1, issued_at=written)
        await services.records.put("agent_session", old_key, old)
        certificates = await worker.nc.jetstream().key_value("UA1_CERTS")
        for _ in range(50):
            keys = await certificates.keys()
            if old_key not in keys:
                break
            await asyncio.sleep(0.1)
        assert old_key not in keys and current["key"] in keys
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


def forged_host_record(attacker: MessageHost, victim: str) -> bytes:
    """A host record under ``victim``'s key, naming another contract, signed with the attacker's own key."""

    identity = attacker.identity
    certificate = identity.certificate
    now = attacker.clock()
    body = {**attacker.liveness_body("accepting"), "runtime": victim, "contract_sha256": OTHER_CONTRACT}
    document = {"record": "host", "protocol": PROTOCOL, "key": victim,
                "writer": {"type": "host", "installation": certificate.installation, "runtime": certificate.runtime,
                           "membership_epoch": certificate.membership_epoch, "key_id": identity.keys.key_id},
                "sequence": int(now.timestamp() * 1000) + 10_000_000, "issued_at": format_timestamp(now),
                "expires_at": format_timestamp(now + timedelta(seconds=30)), "body": body}
    return json.dumps(sign_state(document, identity.keys.application_key)).encode()


@pytest.mark.asyncio
async def test_a_host_cannot_say_another_host_runs_another_contract(broker: str, tmp_path: Path) -> None:
    """A host writes only its own liveness: a record another host signed under its key, or under the team services',
    neither keeps it from being placed nor makes the hosts that follow the team services drain."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), WORKER, OTHER)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        attacker = await host(stack, broker, layout, OTHER)
        assert (await open_session(worker, "researcher-1"))["outcome"] == "ok"
        hosts_bucket = await attacker.nc.jetstream().key_value("UA1_HOSTS")
        await hosts_bucket.put(WORKER.runtime, forged_host_record(attacker, WORKER.runtime))
        assert (await open_session(worker, "researcher-2"))["outcome"] == "ok"

        learned: list[str] = []
        worker.on_other_contract(learned.append)
        following = asyncio.create_task(worker.follow_contract())
        await asyncio.sleep(0.5)
        await hosts_bucket.put(TEAM_SERVICES_RUNTIME, forged_host_record(attacker, TEAM_SERVICES_RUNTIME))
        await asyncio.sleep(1.0)
        following.cancel()
        assert learned == [] and worker.installation_contract is None
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_the_team_services_keep_their_lease_while_they_prepare_to_serve(broker: str, tmp_path: Path,
                                                                              monkeypatch) -> None:
    """A slow catch-up before serving, such as a due renewal waiting 30 seconds for the authority, must not let the
    lease run out while a standby waits: both would serve. Here the lease lasts 3 seconds, the tolerance 1."""

    from team_services import lock as lock_module

    monkeypatch.setattr(lock_module, "LEASE", timedelta(seconds=3))
    monkeypatch.setattr(lock_module, "RENEW_EVERY", timedelta(seconds=1))
    monkeypatch.setattr(lock_module, "_SKEW", timedelta(seconds=1))
    monkeypatch.setattr(lock_module, "_LAST_RENEWAL", timedelta(milliseconds=500))
    layout = InstanceLayout.at(tmp_path / "instance")
    first_stop, second_stop = asyncio.Event(), asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM))
        first = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        preparing, proceed = asyncio.Event(), asyncio.Event()
        take_newest = first.take_newest_certificate

        async def slow_catch_up() -> None:
            preparing.set()
            await proceed.wait()
            await take_newest()

        first.take_newest_certificate = slow_catch_up  # type: ignore[method-assign]
        first_task = asyncio.create_task(serve_team(first, TEAM, first_stop))
        await asyncio.wait_for(preparing.wait(), timeout=30)
        second, second_task = await team_services_host(stack, broker, layout, second_stop)
        await asyncio.sleep(6)  # twice the lease and the tolerance
        assert second._reported_state is None, "the standby kept waiting"
        proceed.set()
        for _ in range(100):
            if first._reported_state is not None:
                break
            await asyncio.sleep(0.1)
        assert (first._reported_state, second._reported_state) == ("accepting", None)
        first_stop.set()
        await asyncio.wait_for(first_task, timeout=10)
        for _ in range(100):  # the clean stop released the lock: the standby serves now
            if second._reported_state is not None:
                break
            await asyncio.sleep(0.1)
        assert second._reported_state == "accepting"
        second_stop.set()
        await asyncio.wait_for(second_task, timeout=10)


@pytest.mark.asyncio
async def test_the_lock_is_kept_through_a_nats_pause_and_a_standby_waits_through_one(tmp_path: Path, caplog) -> None:
    """Plan 13B.12, decisions 76 and 79, on a real server with the client's reconnecting defaults: renewals that time
    out while NATS is paused are tried again within the lease, and a standby taking the lock waits through a pause."""

    from team_services.lock import LOCKS_BUCKET, SingleInstanceLock

    with own_nats_server_process(nats_server(), tmp_path) as (url, server):
        first_connection, second_connection = await nats.connect(url), await nats.connect(url)
        try:
            await first_connection.jetstream().create_key_value(bucket=LOCKS_BUCKET)
            first = SingleInstanceLock.on_connection(first_connection, TEAM, TEAM_SERVICES_RUNTIME, utc_now)
            assert await first.acquire(asyncio.Event())
            stop = asyncio.Event()
            keeping = asyncio.create_task(first.keep(stop))
            with caplog.at_level(logging.INFO, logger="team_services.lock"):
                await asyncio.sleep(9)  # past the first renewal
                server.send_signal(signal.SIGSTOP)
                await asyncio.sleep(8)
                server.send_signal(signal.SIGCONT)
                await asyncio.sleep(12)
                assert not keeping.done(), "kept through the pause"
                held = json.loads((await (await first_connection.jetstream().key_value(LOCKS_BUCKET)).get(
                    lock_key(TEAM))).value)
                assert held["instance"] == first.instance
                assert "event=team_services_lock_renew_failed" in caplog.text
                assert "event=team_services_lock_renewed" in caplog.text

                second = SingleInstanceLock.on_connection(second_connection, TEAM, TEAM_SERVICES_RUNTIME, utc_now)
                second_stop = asyncio.Event()
                waiting = asyncio.create_task(second.acquire(second_stop))
                await asyncio.sleep(2)
                server.send_signal(signal.SIGSTOP)
                await asyncio.sleep(6)
                server.send_signal(signal.SIGCONT)
                await asyncio.sleep(3)
                assert not waiting.done(), "the standby waited through the pause"
                stop.set()
                await keeping
                await first.release()
                assert await asyncio.wait_for(waiting, timeout=20) is True
        finally:
            await second_connection.close()
            await first_connection.close()
