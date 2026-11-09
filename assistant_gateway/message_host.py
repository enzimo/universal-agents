"""The gateway as a protocol 1 host: it joins, keeps its upkeep, and sends people's commands.

The gateway asks the team services for a person's operations with a request that
carries a command token: bound to that one command, naming the person by their opaque
reference, signed with the gateway's own key (``custom_transport.protocol1``). The host
starts in the background when the gateway's NATS runtime connects, so the gateway's
other work never waits for the installation authority; a command waits until the host
has joined (``ready``).

Only the gateway service runs the host (``NatsGatewayManagerRuntime.for_gateway_service``:
``run-gateway``, or ``run-app``, which runs it with one interactive app in its place).
Command-line runs (``ask``, ``chat``, ``cron``, ``heartbeat``) open a gateway of their own,
but a second process joining as host ``gateway`` would load the gateway's key and, when
it ends, mark the running gateway's host record ``drained``; so they run none.

An instance whose launcher never prepared the message hosts' trust folder has no
bootstrap bundle: the gateway service then runs no host and logs
``gateway_host_not_prepared`` once. A gateway service started by hand on an instance the
launcher prepared joins like one the launcher started, as long as the launcher listed the
gateway as it runs (``trust/expected-hosts.json``).

Two answers to the join cannot change while the process runs, so the host stops asking
and a command says why at once: ``protocol_mismatch`` (README "Versions": while the label
is a pre-release, a gateway that runs another contract than the installation cannot
join until it runs the installation's release) and ``denied`` (the launcher listed
another key, team or connection for the gateway). A join that still waits for approval
(``pending``) or an authority that does not answer is tried again every 10 seconds; the
first failure is logged with its traceback, later ones in one line.

Stopping (``close``) cancels a host that is still joining, since a join can wait two
minutes for the authority and holds nothing yet; a host that has joined writes its first
record (``accepting``) only once it counts as joined, so stopping then lets it stop its
upkeep and write its last one (``drained``). A command still waiting for the join is
told at once that the host stopped. A failure on the way out is logged, never raised,
so the gateway still closes its NATS connection; a caller that is itself cancelled stays
cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from custom_messages.wire.frame import VerifiedFrame
from custom_messages.wire.kinds import CONTRACT_SHA256
from custom_transport.protocol1.authority_tokens import command_binding_id, command_token, require_person_ref
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.joining import HostProfile, JoinRefused
from release_version import release_version
from runtime_settings.instance_layout import InstanceLayout

GATEWAY_RUNTIME = "gateway"
GATEWAY_ROLE = "gateway"
# How long the host waits before it asks to join again after a join that may still succeed failed.
JOIN_RETRY_SECONDS = 10.0
# The join answer that may still turn into a certificate; every other refusal is final (module docstring).
_JOIN_PENDING = "pending"
# What a person's command says when the authority refused the join for good, by the join's outcome.
_FINAL_REFUSALS = {
    "protocol_mismatch": ("This gateway runs another message contract than the installation; install the "
                          "installation's release here and start it again."),
    "denied": ("The installation does not take this gateway as it runs: the launcher lists another key, team or "
               "connection for it. Start the instance with the launcher."),
}
_LOGGER = logging.getLogger(__name__)


class GatewayHostUnavailable(RuntimeError):
    """The gateway is not a protocol 1 host yet, or this process does not run one."""


def gateway_profile(team: str) -> HostProfile:
    return HostProfile(GATEWAY_RUNTIME, (team,), (GATEWAY_ROLE,), software={"name": "universal-agents",
                                                                            "version": release_version()})


def prepared_gateway_host(layout: InstanceLayout, team: str,
                          logger: logging.Logger = _LOGGER) -> Optional[GatewayMessageHost]:
    """The gateway service's host when the launcher prepared this instance's message hosts, else ``None``.

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
    # The contract this gateway runs: its code's, unless a test runs it on another.
    contract_sha256: str = CONTRACT_SHA256
    join_retry_seconds: float = JOIN_RETRY_SECONDS
    _ready: asyncio.Event = field(default_factory=asyncio.Event)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _task: Optional[asyncio.Task[None]] = None
    # Who is live and who leads, read once joined (assistant_gateway.team_view).
    team_view: Optional[Any] = None
    _joined: bool = False
    # Why it cannot join while this process runs (module docstring).
    _refusal: Optional[str] = None

    def observe_team(self, view: Any) -> None:
        """Read the team's agent records into ``view`` once joined (``GatewayTeamView.run``)."""

        self.team_view = view

    @property
    def runtime(self) -> str:
        """The host id, which also names the reply inbox of the connection it runs on (``reply_inbox_runtime``)."""

        return GATEWAY_RUNTIME

    def start(self, nc: Any) -> None:
        """Join and keep the host up in the background; returns at once. After ``close`` it joins anew."""

        if self._task is not None and not self._task.done():
            self.logger.warning("The gateway's message host is already running; it is not started again. "
                                "event=gateway_host_already_started")
            return
        self._ready, self._stop = asyncio.Event(), asyncio.Event()
        self._joined, self._refusal = False, None
        self.host = MessageHost(self.layout, gateway_profile(self.team), nc, logger=self.logger,
                                contract_sha256=self.contract_sha256)
        self._task = asyncio.create_task(self._run(), name="gateway_message_host")

    async def _run(self) -> None:
        assert self.host is not None
        failures = 0
        while True:
            if self._stop.is_set():
                return
            try:
                # Its first record is written below, once it counts as joined (module docstring).
                await self.host.start(report=False)
                break
            except JoinRefused as refused:
                if refused.outcome != _JOIN_PENDING:
                    self._refuse(refused)
                    return
                failures += 1
                self._join_failed(failures, refused)
            except Exception as error:
                failures += 1
                self._join_failed(failures, error)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.join_retry_seconds)
        self._joined = True
        self._ready.set()
        try:
            await self.host.publish_liveness("accepting")
        except Exception:
            self.logger.exception("The gateway's first host record could not be written; the upkeep writes it again. "
                                  "event=gateway_host_first_record_failed")
        if self.team_view is None:
            await self.host.keep_up(self._stop)
            return
        await asyncio.gather(self.host.keep_up(self._stop), self.team_view.run(self.host, self._stop))

    def _refuse(self, refused: JoinRefused) -> None:
        self._refusal = _FINAL_REFUSALS.get(
            refused.outcome, f"The installation refused this gateway's join ({refused.outcome}): {refused.message}")
        self.logger.error("The installation refused the gateway's join; it does not ask again until it starts again. "
                          "event=gateway_host_join_refused outcome=%s message=%s", refused.outcome, refused.message)
        self._ready.set()

    def _join_failed(self, failures: int, error: BaseException) -> None:
        if failures == 1:
            self.logger.error("The gateway could not join as a message host; trying again every %s seconds. "
                              "event=gateway_host_join_failed attempt=1", self.join_retry_seconds, exc_info=error)
            return
        self.logger.warning("The gateway could not join as a message host; trying again. "
                            "event=gateway_host_join_failed attempt=%d error=%s", failures, error)

    async def ready(self, timeout: float) -> MessageHost:
        if self.host is None:
            raise GatewayHostUnavailable("The gateway's host has not started.")
        if self._stop.is_set():
            raise GatewayHostUnavailable("The gateway's host has stopped.")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError as error:
            raise GatewayHostUnavailable("The gateway has not joined the installation yet.") from error
        if self._refusal is not None:
            raise GatewayHostUnavailable(self._refusal)
        if not self._joined:  # woken by close()
            raise GatewayHostUnavailable("The gateway's host has stopped.")
        return self.host

    async def command(self, kind: str, body: dict[str, Any], *, person_ref: str,
                      timeout: float = 30.0) -> VerifiedFrame:
        """Ask the team services for one person's operation and return its verified reply.

        The token is bound to the command id the contract names for ``kind``
        (``command_binding_id``: ``request_id`` for most, ``steer_id`` for ``tasks.steer``).
        """

        # Before waiting for the join: either mistake is the caller's, and nothing is signed.
        command_id = command_binding_id(kind, body)
        require_person_ref(person_ref)
        host = await self.ready(timeout)
        assert host.live is not None
        token = command_token(host.identity, team=self.team, person_ref=person_ref, command_id=command_id,
                              issued=host.clock())
        audience = {"installation": host.identity.installation, "team": self.team, "target": "team_services",
                    "id": self.team}
        return await host.live.request(kind, body, audience, team=self.team, authority=token, timeout=timeout)

    async def close(self) -> None:
        """Stop the host (module docstring): raises only when the caller itself is cancelled."""

        self._stop.set()
        self._ready.set()  # a command still waiting for the join hears that the host stopped
        task, self._task = self._task, None
        if task is None:
            return
        if not self._joined:
            task.cancel()
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()  # the caller is being cancelled: leave nothing of the host running behind it
            raise
        if not task.cancelled() and task.exception() is not None:
            self.logger.error("The gateway's message host stopped with an error; the gateway closes its connection "
                              "anyway. event=gateway_host_stop_failed", exc_info=task.exception())
