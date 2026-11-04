"""The gateway as a protocol 1 host: command tokens, joining in the background, and an unprepared instance.

assistant_gateway.message_host and custom_transport.protocol1.authority_tokens; the
message contract's README "On behalf of a person".
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from assistant_gateway.message_host import (
    GATEWAY_RUNTIME,
    GatewayHostUnavailable,
    GatewayMessageHost,
    gateway_profile,
    prepared_gateway_host,
)
from custom_messages.wire.frame import Receiver, verify_frame
from custom_transport.protocol1.authority_tokens import command_token
from custom_transport.protocol1.host_keys import HostKeys
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.live import LiveHost, RequestRefused
from custom_transport.protocol1.trust_state import PersistentTrustStore
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import team_services_profile
from tests import message_contract_support as fixtures
from tests.test_live_team_services import TEAM, broker, connect, installation, team_services  # noqa: F401 - broker is a fixture


def test_a_command_token_names_the_person_by_reference_and_the_team_services_accept_it() -> None:
    certificate = fixtures.trust_store().host_certificate("install_example", "gateway-1", 1)
    keys = HostKeys("gateway-1", "test-gateway-key-1", fixtures.private_key("test-gateway-key-1"), bytes(32))
    gateway = LiveHost(None, HostIdentity(keys, certificate), PersistentTrustStore(fixtures.trust_store(), Path("/x"), {}, {}),
                       clock=lambda: fixtures.NOW)
    token = command_token(gateway.identity, team="team_example", person_ref="p-AbCdEfGhIjKlMnOpQrStUv",
                          command_id="cmd-1", issued=fixtures.NOW)
    assert token["bound_to"] == {"type": "command", "id": "cmd-1"} and token["person"] == "p-AbCdEfGhIjKlMnOpQrStUv"
    frame = gateway.sign("memory.list", {"request_id": "cmd-1", "bank": "usermem", "sort": "memory_id", "offset": 0,
                                         "limit": 10},
                         {"installation": "install_example", "team": "team_example", "target": "team_services",
                          "id": "team_example"}, team="team_example", authority=token)
    verified = verify_frame(json.dumps(frame).encode(), trust=fixtures.trust_store(),
                            receiver=Receiver("install_example", "team_example", "team_services", "team_example",
                                              "team-services-1"),
                            arrived_on=frame["subject"], now=fixtures.NOW, replay=fixtures.replay_holding())
    assert verified.authority is not None and verified.authority.token.person == "p-AbCdEfGhIjKlMnOpQrStUv"
    # Bound to its one command: the same token on another command id is refused.
    other = gateway.sign("memory.list", {"request_id": "cmd-2", "bank": "usermem", "sort": "memory_id", "offset": 0,
                                         "limit": 10},
                         {"installation": "install_example", "team": "team_example", "target": "team_services",
                          "id": "team_example"}, team="team_example", authority=token)
    with pytest.raises(Exception, match="bad_authority"):
        verify_frame(json.dumps(other).encode(), trust=fixtures.trust_store(),
                     receiver=Receiver("install_example", "team_example", "team_services", "team_example",
                                       "team-services-1"),
                     arrived_on=other["subject"], now=fixtures.NOW, replay=fixtures.replay_holding())


def test_an_instance_the_launcher_did_not_prepare_runs_no_gateway_host(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert prepared_gateway_host(InstanceLayout.at(tmp_path), "team_one") is None
    assert "event=gateway_host_not_prepared" in caplog.text


@pytest.mark.asyncio
async def test_a_command_before_the_host_starts_says_so(tmp_path: Path) -> None:
    with pytest.raises(GatewayHostUnavailable, match="not started"):
        await GatewayMessageHost(InstanceLayout.at(tmp_path), "team_one").command(
            "memory.list", {}, person_ref="p-AbCdEfGhIjKlMnOpQrStUv", request_id="cmd-1")


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
        host = await gateway.ready(timeout=20)
        assert host.identity.certificate.roles == frozenset({"gateway"})
        # The team services check the command token and answer; memory is not served there yet.
        with pytest.raises(RequestRefused) as refused:
            await gateway.command("memory.list", {"request_id": "cmd-1", "bank": "usermem", "sort": "memory_id",
                                                  "offset": 0, "limit": 10},
                                  person_ref="p-AbCdEfGhIjKlMnOpQrStUv", request_id="cmd-1", timeout=10)
        assert refused.value.code == "unsupported"
        await gateway.close()
        stop.set()
        await serving
