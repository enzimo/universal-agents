"""The gateway as a protocol 1 host: it joins, keeps its upkeep, and sends people's commands.

The gateway asks the team services for a person's operations with a request that
carries a command token: bound to that one command, naming the person by their opaque
reference, signed with the gateway's own key (``custom_transport.protocol1``). The host
starts in the background when the gateway's NATS runtime connects, so the gateway's
other work never waits for the installation authority; a command waits until the host
has joined (``ready``).

An instance whose launcher did not prepare the message hosts' trust folder (a gateway
run by hand, without ``docker.launch_headless``) has no bootstrap bundle: the host does
not start, and a command says so.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from custom_messages.wire.frame import VerifiedFrame
from custom_transport.protocol1.authority_tokens import command_token
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.joining import HostProfile
from release_version import release_version
from runtime_settings.instance_layout import InstanceLayout

GATEWAY_RUNTIME = "gateway"
_LOGGER = logging.getLogger(__name__)


class GatewayHostUnavailable(RuntimeError):
    """The gateway is not a protocol 1 host yet, or this instance does not run one."""


def gateway_profile(team: str) -> HostProfile:
    return HostProfile(GATEWAY_RUNTIME, (team,), ("gateway",), software={"name": "universal-agents",
                                                                        "version": release_version()})


def prepared_gateway_host(layout: InstanceLayout, team: str,
                          logger: logging.Logger = _LOGGER) -> Optional[GatewayMessageHost]:
    """The gateway's host when the launcher prepared this instance's message hosts, else ``None``.

    Without the launcher's bootstrap bundle the gateway cannot join, so it runs no host and
    says so once.
    """

    if not layout.trust_bootstrap_path.exists():
        logger.warning("The gateway does not run as a message host: this instance has no bootstrap bundle "
                       "(start it with the launcher). event=gateway_host_not_prepared path=%s",
                       layout.trust_bootstrap_path)
        return None
    return GatewayMessageHost(layout, team, logger)


@dataclass
class GatewayMessageHost:
    layout: InstanceLayout
    team: str
    logger: logging.Logger = _LOGGER
    host: Optional[MessageHost] = None
    _ready: asyncio.Event = field(default_factory=asyncio.Event)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _task: Optional[asyncio.Task[None]] = None
    # Who is live and who leads, read once joined (assistant_gateway.team_view).
    team_view: Optional[Any] = None

    def observe_team(self, view: Any) -> None:
        """Read the team's agent records into ``view`` once joined (``GatewayTeamView.run``)."""

        self.team_view = view

    def start(self, nc: Any) -> None:
        """Join and keep the host up in the background; returns at once."""

        self.host = MessageHost(self.layout, gateway_profile(self.team), nc, logger=self.logger)
        self._task = asyncio.create_task(self._run(), name="gateway_message_host")

    async def _run(self) -> None:
        assert self.host is not None
        while not self._stop.is_set():
            try:
                await self.host.start()
                break
            except Exception:
                self.logger.exception("The gateway could not join as a message host; trying again. "
                                      "event=gateway_host_join_failed")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=10)
        if self._stop.is_set():
            return
        self._ready.set()
        if self.team_view is None:
            await self.host.keep_up(self._stop)
            return
        await asyncio.gather(self.host.keep_up(self._stop), self.team_view.run(self.host, self._stop))

    async def ready(self, timeout: float) -> MessageHost:
        if self.host is None:
            raise GatewayHostUnavailable("The gateway's host has not started.")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError as error:
            raise GatewayHostUnavailable("The gateway has not joined the installation yet.") from error
        return self.host

    async def command(self, kind: str, body: dict[str, Any], *, person_ref: str, request_id: str,
                      timeout: float = 30.0) -> VerifiedFrame:
        """Ask the team services for one person's operation and return its verified reply."""

        host = await self.ready(timeout)
        assert host.live is not None
        token = command_token(host.identity, team=self.team, person_ref=person_ref, command_id=request_id,
                              issued=host.clock())
        audience = {"installation": host.identity.installation, "team": self.team, "target": "team_services",
                    "id": self.team}
        return await host.live.request(kind, body, audience, team=self.team, authority=token, timeout=timeout)

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
