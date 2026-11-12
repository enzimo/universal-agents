"""The live scenarios' Website start: the gateway's links name the port the app serves on.

Stand-in gateway and app classes, so no live service: the module is not named
``test_live_*``, which the suite reserves for modules marked ``live``.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from tests import live_website_support

pytestmark = pytest.mark.asyncio

_free_port = live_website_support.available_loopback_port


@contextlib.contextmanager
def _held_ports(count: int) -> Iterator[list[int]]:
    """Listen on ``count`` loopback ports, as other programs holding them would."""

    with contextlib.ExitStack() as stack:
        listeners = [stack.enter_context(socket.socket()) for _ in range(count)]
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
        yield [int(listener.getsockname()[1]) for listener in listeners]


class _Recorder:
    """Stand-ins for UserGateway and InteractiveAppManager that log what happens to them.

    The stand-in app binds its port for real, so a port another listener holds fails with
    the operating system's "address already in use".
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, ports: list[int]) -> None:
        self.events: list[str] = []
        self.gateways: list[Any] = []
        recorder = self
        picked = iter(ports)

        class StandInGateway:
            def __init__(self, config: GatewayInstanceConfig, output_sink: object = None) -> None:
                # UserGateway copies the Website address into its links when it is built.
                self.web_chat_url = f"{config.internal_website_base_url}/webchat"
                self.application = object()
                self.name = f"gateway {len(recorder.gateways) + 1}"
                recorder.gateways.append(self)
                recorder.events.append(f"built {self.name}")

            async def __aenter__(self) -> StandInGateway:
                return self

            async def __aexit__(self, *_exc: object) -> None:
                recorder.events.append(f"closed {self.name}")

        class StandInAppManager:
            def __init__(self, config: GatewayInstanceConfig, application: object, *, include_apps: list[str]) -> None:
                assert include_apps == ["website"]
                self.port = config.interactive_apps.internal_website.bind_port
                self.listener: socket.socket | None = None

            @property
            def has_running_apps(self) -> bool:
                return self.listener is not None

            async def start(self) -> None:
                listener = socket.socket()
                # Like an asyncio server on Linux; a port with a listener still fails to bind.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    listener.bind(("127.0.0.1", self.port))
                    listener.listen()
                except OSError:
                    listener.close()
                    raise
                self.listener = listener
                recorder.events.append(f"started app on {self.port}")

            async def stop(self) -> None:
                if self.listener is not None:
                    self.listener.close()
                    self.listener = None
                recorder.events.append(f"stopped app on {self.port}")

        self.app_manager_class = StandInAppManager
        monkeypatch.setattr(live_website_support, "UserGateway", StandInGateway)
        monkeypatch.setattr(live_website_support, "InteractiveAppManager", StandInAppManager)
        monkeypatch.setattr(live_website_support, "available_loopback_port", lambda: next(picked))

    async def ready(self, gateway: object) -> None:
        self.events.append(f"ready {gateway.name}")  # type: ignore[attr-defined]


def _config(tmp_path: Path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="website_support", instance_root=str(tmp_path), team_id="team_test", timezone="UTC"
    )
    config.network.public_origin = "http://127.0.0.1"
    return config


async def test_the_gateways_links_name_the_port_the_website_app_serves_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    free = _free_port()
    recorder = _Recorder(monkeypatch, ports=[free])

    async with live_website_support.gateway_with_website(_config(tmp_path), ready=recorder.ready) as (
        gateway,
        app_manager,
        port,
    ):
        assert port == free and app_manager.has_running_apps
        assert recorder.gateways[0].web_chat_url == f"http://127.0.0.1:{free}/webchat"
        assert gateway is recorder.gateways[0]

    assert recorder.events == [
        "built gateway 1",
        "ready gateway 1",
        f"started app on {free}",
        f"stopped app on {free}",
        "closed gateway 1",
    ]


async def test_a_taken_port_starts_the_gateway_and_the_app_again_on_another_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=live_website_support.__name__)
    with _held_ports(1) as [taken]:
        free = _free_port()
        recorder = _Recorder(monkeypatch, ports=[taken, free])

        async with live_website_support.gateway_with_website(_config(tmp_path), ready=recorder.ready) as (
            gateway,
            _app_manager,
            port,
        ):
            assert port == free
            assert recorder.gateways[1].web_chat_url == f"http://127.0.0.1:{free}/webchat"
            assert gateway is recorder.gateways[1]
            # The first gateway, whose links name the taken port, is closed before the second is built.
            assert recorder.events[:4] == ["built gateway 1", "ready gateway 1", "closed gateway 1", "built gateway 2"]

    assert f"event=live_website_port_taken port={taken} attempt=1 of=3" in caplog.text
    assert recorder.events[-2:] == [f"stopped app on {free}", "closed gateway 2"]


@pytest.mark.parametrize(
    ("failure", "error", "events_after_build"),
    [
        ("ready", "the manager's route never appeared", ["closed gateway 1"]),
        ("start", "permission denied", ["ready gateway 1", "closed gateway 1"]),
        (
            "body",
            "the scenario failed",
            ["ready gateway 1", "started app on {port}", "stopped app on {port}", "closed gateway 1"],
        ),
    ],
)
async def test_the_gateway_is_closed_and_the_app_stopped_however_the_start_or_the_block_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, error: str, events_after_build: list[str]
) -> None:
    free = _free_port()
    recorder = _Recorder(monkeypatch, ports=[free])

    async def route_never_appears(_gateway: object) -> None:
        raise RuntimeError("the manager's route never appeared")

    async def refused_start(_self: object) -> None:
        raise OSError(errno.EACCES, "permission denied")

    if failure == "start":
        monkeypatch.setattr(recorder.app_manager_class, "start", refused_start)
    ready = route_never_appears if failure == "ready" else recorder.ready

    with pytest.raises((RuntimeError, OSError), match=error):
        async with live_website_support.gateway_with_website(_config(tmp_path), ready=ready):
            raise RuntimeError("the scenario failed")

    # Only "address already in use" starts again; anything else ends the start as it is.
    assert recorder.events == ["built gateway 1", *(event.format(port=free) for event in events_after_build)]


async def test_a_start_gives_up_when_every_picked_port_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _held_ports(live_website_support.WEBSITE_PORT_ATTEMPTS) as taken:
        recorder = _Recorder(monkeypatch, ports=taken)

        with pytest.raises(AssertionError, match="Every port picked for the Website app was taken"):
            async with live_website_support.gateway_with_website(_config(tmp_path), ready=recorder.ready):
                pass

    assert [event for event in recorder.events if event.startswith("closed")] == [
        "closed gateway 1",
        "closed gateway 2",
        "closed gateway 3",
    ]
