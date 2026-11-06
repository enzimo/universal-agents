"""The team services on a real NATS server: the single-instance lock, sessions and placement, host liveness.

team_services and custom_transport.protocol1.host_runtime with the installation
authority running; the message contract's README "Agent sessions" and "State records".
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import subprocess
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path

import nats
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custom_messages.wire.trust import sign_state
from custom_transport.protocol1.host_keys import ensure_host_keys, load_authority_keys, load_host_keys
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.joining import HostProfile
from custom_transport.protocol1.live import utc_now
from docker import message_hosts
from installation_authority.expected_hosts import write_expected_hosts
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from runtime_settings.instance_layout import InstanceLayout
from team_services.lock import lock_key
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from tests.test_live_message_contract import nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
TEAM = "team_one"
WORKER = HostProfile("worker-host-1", (TEAM,), ("worker_host",), ("researcher",), 2)
OTHER = HostProfile("worker-host-2", (TEAM,), ("worker_host",), ("researcher",), 2)
TO_SERVICES = {"target": "team_services", "id": TEAM, "team": TEAM}
DEFINITION = "DQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture
def broker(tmp_path: Path):
    port = free_port()
    with (tmp_path / "nats.log").open("w") as log:
        process = subprocess.Popen([nats_server(), "-js", "-sd", str(tmp_path / "store"), "-a", "127.0.0.1", "-p", str(port)],
                                   stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        pytest.fail("NATS did not start within five seconds")
                    time.sleep(0.02)
            yield f"nats://127.0.0.1:{port}"
        finally:
            process.terminate()
            process.wait(timeout=5)


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


async def open_session(started: MessageHost, agent: str, agent_type: str = "researcher") -> dict:
    assert started.live is not None
    for _ in range(50):  # the team services may still be taking the lock
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
            second = await team_services(stack, broker, layout, second_stop)
            for _ in range(50):
                if "event=team_services_standby" in caplog.text:
                    break
                await asyncio.sleep(0.1)
        assert "event=team_services_standby" in caplog.text
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
