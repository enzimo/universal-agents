"""The gateway as a protocol 1 host: command tokens, joining in the background, stopping, and who runs it.

assistant_gateway.message_host, assistant_gateway.nats_manager_runtime and
custom_transport.protocol1.authority_tokens; the message contract's README "On behalf
of a person".
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.message_host import (
    GATEWAY_RUNTIME,
    GatewayHostUnavailable,
    GatewayMessageHost,
    gateway_profile,
    prepared_gateway_host,
)
from assistant_gateway.nats_manager_runtime import NatsGatewayManagerRuntime
from custom_messages.wire.frame import Receiver, verify_frame
from custom_transport.protocol1.authority_tokens import command_binding_id, command_token
from custom_transport.protocol1.host_keys import HostKeys
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.joining import JoinRefused
from custom_transport.protocol1.live import LiveHost, RequestRefused
from custom_transport.protocol1.trust_state import PersistentTrustStore
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import TEAM_SERVICES_RUNTIME, team_services_profile
from tests import message_contract_support as fixtures
from tests.test_live_team_services import TEAM, broker, connect, host, installation, team_services  # noqa: F401 - broker is a fixture

REFERENCE = "p-AbCdEfGhIjKlMnOpQrStUv"
LIST_BODY = {"request_id": "cmd-1", "bank": "usermem", "sort": "memory_id", "offset": 0, "limit": 10}
TO_SERVICES = {"installation": "install_example", "team": "team_example", "target": "team_services",
               "id": "team_example"}


def _gateway_identity() -> LiveHost:
    certificate = fixtures.trust_store().host_certificate("install_example", "gateway-1", 1)
    keys = HostKeys("gateway-1", "test-gateway-key-1", fixtures.private_key("test-gateway-key-1"), bytes(32))
    return LiveHost(None, HostIdentity(keys, certificate), PersistentTrustStore(fixtures.trust_store(), Path("/x")),
                    clock=lambda: fixtures.NOW)


def _verify_at_team_services(frame: dict) -> Any:
    return verify_frame(json.dumps(frame).encode(), trust=fixtures.trust_store(),
                        receiver=Receiver("install_example", "team_example", "team_services", "team_example",
                                          "team-services-1"),
                        arrived_on=frame["subject"], now=fixtures.NOW, replay=fixtures.replay_holding())


# -- command tokens -------------------------------------------------------------------------------


def test_a_command_token_names_the_person_by_reference_and_the_team_services_accept_it() -> None:
    gateway = _gateway_identity()
    token = command_token(gateway.identity, team="team_example", person_ref=REFERENCE, command_id="cmd-1",
                          issued=fixtures.NOW)
    assert token["bound_to"] == {"type": "command", "id": "cmd-1"} and token["person"] == REFERENCE
    frame = gateway.sign("memory.list", LIST_BODY, TO_SERVICES, team="team_example", authority=token)
    verified = _verify_at_team_services(frame)
    assert verified.authority is not None and verified.authority.token.person == REFERENCE
    # Bound to its one command: the same token on another command id is refused.
    other = gateway.sign("memory.list", {**LIST_BODY, "request_id": "cmd-2"}, TO_SERVICES, team="team_example",
                         authority=token)
    with pytest.raises(Exception, match="bad_authority"):
        _verify_at_team_services(other)


@pytest.mark.parametrize("person", ["olive", "Olive Smith", "421337", "p-short", f"{REFERENCE}\n"])
def test_a_command_token_refuses_anything_but_an_opaque_reference(person: str) -> None:
    """Plan decision 64: a person's id can be a name, a chat handle or a chat id, which the contract's
    ``person`` field would accept; the gateway signs only a reference, and never repeats what it was given."""

    with pytest.raises(ValueError, match="opaque reference") as refused:
        command_token(_gateway_identity().identity, team="team_example", person_ref=person, command_id="cmd-1",
                      issued=fixtures.NOW)
    assert person.strip() not in str(refused.value)


def test_a_command_is_bound_to_the_body_field_its_kind_names() -> None:
    assert command_binding_id("memory.list", LIST_BODY) == "cmd-1"
    assert command_binding_id("tasks.steer", {"steer_id": "steer-1", "request_id": "other"}) == "steer-1"
    with pytest.raises(ValueError, match="does not carry"):
        command_binding_id("memory.list", {"bank": "usermem"})
    with pytest.raises(ValueError, match="takes no command token"):
        command_binding_id("host.join", {"join_id": "join-1"})
    with pytest.raises(ValueError, match="takes no command token"):
        command_binding_id("no.such.kind", {})


# -- the host's life, with a stand-in for the joining and upkeep ----------------------------------


class _StandInHost:
    """Stands in for ``MessageHost``: each instance is one start of the gateway's host."""

    started: list["_StandInHost"] = []
    # What each join does, in order: an exception to raise, "hang" to wait until cancelled, or None to join.
    joins: list[Any] = []
    # What keep_up does once stop is set: None returns, an exception is raised, "hang" waits until cancelled.
    on_stop: Any = None

    def __init__(self, layout: Any, profile: Any, nc: Any, **_: Any) -> None:
        self.profile, self.nc = profile, nc
        self.join_cancelled = False
        self.kept_up_until_stop = False
        self.records: list[str] = []
        self.live = object()
        _StandInHost.started.append(self)

    async def start(self, *, report: bool = True) -> None:
        assert report is False  # the gateway writes the first record itself, once it counts as joined
        step = _StandInHost.joins.pop(0) if _StandInHost.joins else None
        if step == "hang":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.join_cancelled = True
                raise
        if isinstance(step, BaseException):
            raise step

    async def publish_liveness(self, state: str = "accepting") -> None:
        self.records.append(state)

    async def keep_up(self, stop: asyncio.Event) -> None:
        await stop.wait()
        self.kept_up_until_stop = True
        self.records.append("drained")
        if _StandInHost.on_stop == "hang":
            await asyncio.Event().wait()
        if isinstance(_StandInHost.on_stop, BaseException):
            raise _StandInHost.on_stop


@pytest.fixture
def stand_in(monkeypatch) -> type[_StandInHost]:
    import assistant_gateway.message_host as message_host

    _StandInHost.started, _StandInHost.joins, _StandInHost.on_stop = [], [], None
    monkeypatch.setattr(message_host, "MessageHost", _StandInHost)
    return _StandInHost


def _gateway(tmp_path: Path) -> GatewayMessageHost:
    return GatewayMessageHost(InstanceLayout.at(tmp_path), "team_one", join_retry_seconds=0.01)


@pytest.mark.asyncio
async def test_closing_a_host_that_is_still_joining_cancels_the_join_at_once(stand_in, tmp_path: Path) -> None:
    """A join can wait two minutes for the authority; stopping does not wait for it."""

    stand_in.joins = ["hang"]
    gateway = _gateway(tmp_path)
    gateway.start(nc="connection")
    await asyncio.sleep(0.01)
    await asyncio.wait_for(gateway.close(), timeout=1)
    assert stand_in.started[0].join_cancelled is True
    # Stopped before it counted as joined: it wrote no record saying it accepts work.
    assert stand_in.started[0].records == []
    with pytest.raises(GatewayHostUnavailable, match="stopped"):
        await gateway.ready(timeout=1)


@pytest.mark.asyncio
async def test_a_joined_host_writes_its_last_record_when_closed(stand_in, tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    gateway.start(nc="connection")
    assert await gateway.ready(timeout=1) is stand_in.started[0]
    await asyncio.wait_for(gateway.close(), timeout=1)
    assert stand_in.started[0].kept_up_until_stop is True
    assert stand_in.started[0].records == ["accepting", "drained"]


@pytest.mark.asyncio
async def test_a_command_waiting_for_the_join_hears_at_once_that_the_host_stopped(stand_in, tmp_path: Path) -> None:
    stand_in.joins = ["hang"]
    gateway = _gateway(tmp_path)
    gateway.start(nc="connection")
    waiting = asyncio.create_task(gateway.command("memory.list", LIST_BODY, person_ref=REFERENCE, timeout=30))
    await asyncio.sleep(0.01)
    await gateway.close()
    with pytest.raises(GatewayHostUnavailable, match="stopped"):
        await asyncio.wait_for(waiting, timeout=1)


@pytest.mark.asyncio
async def test_a_caller_cancelled_while_the_host_stops_stays_cancelled(stand_in, tmp_path: Path) -> None:
    stand_in.on_stop = "hang"  # the last record's write does not finish
    gateway = _gateway(tmp_path)
    gateway.start(nc="connection")
    await gateway.ready(timeout=1)
    host_task = gateway._task
    closing = asyncio.create_task(gateway.close())
    await asyncio.sleep(0.01)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await asyncio.sleep(0)
    assert host_task is not None and host_task.cancelled()


@pytest.mark.asyncio
async def test_a_failure_on_the_way_out_is_logged_and_close_returns(stand_in, tmp_path: Path, caplog) -> None:
    stand_in.on_stop = RuntimeError("the connection closed under the last record")
    gateway = _gateway(tmp_path)
    gateway.start(nc="connection")
    await gateway.ready(timeout=1)
    with caplog.at_level(logging.ERROR, logger="assistant_gateway.message_host"):
        await asyncio.wait_for(gateway.close(), timeout=1)
    assert "event=gateway_host_stop_failed" in caplog.text and "the connection closed" in caplog.text


@pytest.mark.parametrize(("outcome", "says"), [("denied", "launcher lists another key"),
                                               ("protocol_mismatch", "another message contract")])
@pytest.mark.asyncio
async def test_a_refusal_that_cannot_change_is_asked_once_and_a_command_says_why(
    stand_in, tmp_path: Path, caplog, outcome: str, says: str
) -> None:
    stand_in.joins = [JoinRefused(outcome, "refused for this test")]
    gateway = _gateway(tmp_path)
    with caplog.at_level(logging.INFO, logger="assistant_gateway.message_host"):
        gateway.start(nc="connection")
        with pytest.raises(GatewayHostUnavailable, match=says):
            await gateway.command("memory.list", LIST_BODY, person_ref=REFERENCE, timeout=1)
        await asyncio.sleep(0.05)  # five retry intervals
    assert len(stand_in.started) == 1 and stand_in.joins == []
    assert gateway._task is not None and gateway._task.done()
    assert caplog.text.count("event=gateway_host_join_refused") == 1
    assert "event=gateway_host_join_failed" not in caplog.text
    await gateway.close()


@pytest.mark.asyncio
async def test_a_join_that_may_still_succeed_is_tried_again_with_one_traceback(stand_in, tmp_path: Path,
                                                                                caplog) -> None:
    stand_in.joins = [RuntimeError("no authority yet"), JoinRefused("pending", "waits for approval"), None]
    gateway = _gateway(tmp_path)
    with caplog.at_level(logging.INFO, logger="assistant_gateway.message_host"):
        gateway.start(nc="connection")
        await gateway.ready(timeout=1)
    failures = [record for record in caplog.records if "event=gateway_host_join_failed" in record.getMessage()]
    assert [(record.levelno, record.exc_info is not None) for record in failures] == [
        (logging.ERROR, True), (logging.WARNING, False)]
    await gateway.close()


@pytest.mark.asyncio
async def test_a_host_closed_and_started_again_joins_anew(stand_in, tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    gateway.start(nc="first")
    await gateway.ready(timeout=1)
    await gateway.close()
    gateway.start(nc="second")
    assert (await gateway.ready(timeout=1)).nc == "second"
    await gateway.close()


@pytest.mark.asyncio
async def test_a_command_with_no_command_id_or_a_persons_id_fails_before_waiting_for_the_host(
    stand_in, tmp_path: Path
) -> None:
    stand_in.joins = ["hang"]
    gateway = _gateway(tmp_path)
    gateway.start(nc="connection")
    with pytest.raises(ValueError, match="does not carry"):
        await asyncio.wait_for(gateway.command("memory.list", {"bank": "usermem"}, person_ref=REFERENCE), 1)
    with pytest.raises(ValueError, match="opaque reference"):
        await asyncio.wait_for(gateway.command("memory.list", LIST_BODY, person_ref="Olive Smith"), 1)
    await gateway.close()


@pytest.mark.asyncio
async def test_a_command_before_the_host_starts_says_so(tmp_path: Path) -> None:
    with pytest.raises(GatewayHostUnavailable, match="not started"):
        await GatewayMessageHost(InstanceLayout.at(tmp_path), "team_one").command(
            "memory.list", LIST_BODY, person_ref=REFERENCE)


# -- who runs the host ------------------------------------------------------------------------------


def _config(tmp_path: Path, *, prepared: bool) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(instance_name="host_test", instance_root=str(tmp_path / "instance"),
                                          team_id="team_test", timezone="UTC")
    if prepared:  # what the launcher's message services write before the gateway starts
        config.layout.trust_bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
        config.layout.trust_bootstrap_path.write_text("{}", encoding="utf-8")
    return config


def test_only_the_gateway_service_runs_the_gateway_host_and_takes_replies_on_its_inbox(tmp_path: Path) -> None:
    config = _config(tmp_path, prepared=True)
    service = NatsGatewayManagerRuntime.for_gateway_service(config)
    assert isinstance(service.message_host, GatewayMessageHost) and service.message_host.team == "team_test"
    assert service._adapter.config.reply_inbox_runtime == GATEWAY_RUNTIME
    # A command-line run (ask, chat, cron, heartbeat) and the launcher's stream setup: no host, no host inbox.
    command_line = NatsGatewayManagerRuntime.from_gateway_config(config)
    assert command_line.message_host is None and command_line._adapter.config.reply_inbox_runtime is None


def test_the_gateway_service_runs_no_host_on_an_instance_the_launcher_never_prepared(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert NatsGatewayManagerRuntime.for_gateway_service(_config(tmp_path, prepared=False)).message_host is None
        assert prepared_gateway_host(InstanceLayout.at(tmp_path / "other"), "team_one") is None
    assert caplog.text.count("event=gateway_host_not_prepared") == 2


def test_command_line_gateways_load_no_host_code() -> None:
    """The host's modules cost command-line runs half a second to import; only the gateway service loads them."""

    import subprocess
    import sys

    code = ("import sys, assistant_gateway.gateway, assistant_gateway.nats_manager_runtime; "
            "print('assistant_gateway.message_host' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                            cwd=Path(__file__).resolve().parents[1])
    assert result.stdout.strip() == "False"


@pytest.mark.asyncio
async def test_the_gateway_service_starts_its_host_on_its_connection_and_closes_it_first(tmp_path: Path) -> None:
    events: list[tuple[str, Any]] = []

    class _Adapter:
        nats_connection = "the gateway's connection"

        async def connect(self, *, reconcile_topology: bool = False) -> None:
            events.append(("connect", None))

        async def subscribe_route(self, **_: Any) -> None:
            return None

        async def subscribe_subject(self, **_: Any) -> None:
            return None

        async def close(self) -> None:
            events.append(("adapter closed", None))

    class _Host:
        runtime = GATEWAY_RUNTIME

        def start(self, nc: Any) -> None:
            events.append(("host started", nc))

        async def close(self) -> None:
            events.append(("host closed", None))
            raise RuntimeError("the host failed to stop")

    async def handler(_envelope: Any) -> None:
        return None

    runtime = NatsGatewayManagerRuntime.from_gateway_config(_config(tmp_path, prepared=True), adapter=_Adapter(),
                                                            message_host=_Host())
    await runtime.connect(on_response=handler, on_operational=handler, on_presence=handler)
    assert events[-1] == ("host started", "the gateway's connection")
    with pytest.raises(RuntimeError, match="failed to stop"):
        await runtime.close()
    # The host first, while its connection is still up; the connection is closed whatever the host did.
    assert [name for name, _ in events[-2:]] == ["host closed", "adapter closed"] and runtime.connected is False


def test_the_gateway_service_builds_its_gateway_with_the_host(tmp_path: Path, monkeypatch) -> None:
    import assistant_cli

    config = _config(tmp_path, prepared=True)
    built: dict[str, Any] = {}

    class _Stop(Exception):
        pass

    def user_gateway(*_: Any, **kwargs: Any) -> None:
        built.update(kwargs)
        raise _Stop

    monkeypatch.setattr(assistant_cli, "_load_gateway_instance_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(assistant_cli, "_use_gateway_broker_token", lambda: None)
    monkeypatch.setattr(assistant_cli, "_consume_headless_startup_notices", lambda: [])
    monkeypatch.setattr(assistant_cli, "UserGateway", user_gateway)
    with pytest.raises(_Stop):
        asyncio.run(assistant_cli._run_gateway_services("config.yaml", env_path=None, include_apps=None,
                                                         disable_apps=True))
    assert isinstance(built["manager_runtime"].message_host, GatewayMessageHost)


# -- live, on a real NATS server with the real authority and team services --------------------------


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
@pytest.mark.asyncio
async def test_the_gateway_joins_in_the_background_and_sends_a_persons_command(
    broker: str, tmp_path: Path,  # noqa: F811 - the shared live NATS fixture
) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM))
        serving = await team_services(stack, broker, layout, stop)
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        host_started = await gateway.ready(timeout=20)
        assert host_started.identity.certificate.roles == frozenset({"gateway"})
        # The team services check the command token and answer; memory is not served there yet.
        with pytest.raises(RequestRefused) as refused:
            await gateway.command("memory.list", LIST_BODY, person_ref=REFERENCE, timeout=10)
        assert refused.value.code == "unsupported"
        await gateway.close()
        stop.set()
        await serving


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
@pytest.mark.asyncio
async def test_the_gateway_verifies_the_team_services_signed_reply_to_a_persons_command(
    broker: str, tmp_path: Path,  # noqa: F811 - the shared live NATS fixture
) -> None:
    """The reply comes back on the gateway's own inbox, signed by the team services, and is verified."""

    layout = InstanceLayout.at(tmp_path / "instance")
    reply_body = json.loads(
        (Path(__file__).resolve().parents[1] / "contracts/messages/v1/examples/valid/memory.list.reply/last-page.json")
        .read_text(encoding="utf-8"))["body"]
    received: list[Any] = []

    async def answer(verified: Any) -> dict[str, Any]:
        received.append(verified)
        return reply_body

    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM))
        services = await host(stack, broker, layout, team_services_profile(TEAM))
        assert services.live is not None
        await services.live.serve("svc.team", Receiver(services.identity.installation, TEAM, "team_services", TEAM,
                                                       TEAM_SERVICES_RUNTIME), answer)
        await services.nc.flush()
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        await gateway.ready(timeout=20)

        reply = await gateway.command("memory.list", LIST_BODY, person_ref=REFERENCE, timeout=10)

        assert reply.raw["body"] == reply_body and reply.frame.issuer.runtime == TEAM_SERVICES_RUNTIME
        [request] = received
        assert request.authority is not None and request.authority.token.person == REFERENCE
        assert request.authority.token.bound_to.id == "cmd-1" and request.frame.issuer.runtime == GATEWAY_RUNTIME
        await gateway.close()


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
@pytest.mark.asyncio
async def test_a_gateway_on_another_contract_asks_to_join_once_and_says_why_it_cannot(
    broker: str, tmp_path: Path, caplog,  # noqa: F811 - the shared live NATS fixture
) -> None:
    """README "Versions": the authority answers ``protocol_mismatch``; asking again cannot change that, so the
    gateway asks once, the host logs both digests once, and a person's command says why at once."""

    layout = InstanceLayout.at(tmp_path / "instance")
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, gateway_profile(TEAM))
        gateway = GatewayMessageHost(layout, TEAM, contract_sha256="0" * 64, join_retry_seconds=0.05)
        with caplog.at_level(logging.INFO):
            gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
            with pytest.raises(GatewayHostUnavailable, match="another message contract"):
                await gateway.command("memory.list", LIST_BODY, person_ref=REFERENCE, timeout=20)
            await asyncio.sleep(0.3)  # six retry intervals
        assert caplog.text.count("event=host_protocol_mismatch") == 1  # the authority saw one join
        assert caplog.text.count("event=protocol1_host_other_contract") == 1
        assert "event=gateway_host_join_failed" not in caplog.text
        await gateway.close()
