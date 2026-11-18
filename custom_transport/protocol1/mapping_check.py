"""The check that the NATS server maps agents' addresses to their partitions (contract README, "Partitions").

The installation's account maps every agent's inbox, control queue and live control address
to one more token, the agent's partition at the installation's count. Hosts subscribe to an
agent's live control address with that token, so a server without the mappings, or with
another count, leaves every agent's control requests unanswered, and from step 6 on would
store queued work in no partition stream or in another one. The bundled server's generated
configuration carries the mappings; an external server's operator writes them by hand. So the
installation authority runs this check before it serves, and the live-test harness runs it
once against the shared test server.

The check subscribes to its own probe addresses, publishes an unsigned, empty message to the
live control address of a few probe agents, and reads where each arrived. The probe scope
(``PARTITION_PROBE_SCOPE``) stands where a team id goes, and no team id starts with ``_``, so no
team's receivers or streams see these messages; the sender slot is the checking runtime, as
in every subject it publishes. The probe agents' ids carry a random part, so two checks at
once on one server tell their messages apart.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from custom_messages.wire.kinds import MAX_PARTITION_COUNT, PARTITION_PROBE_ADDRESS, PARTITION_PROBE_SCOPE, SUBJECT_PREFIX
from custom_messages.wire.partitions import account_mappings, agent_partition, check_partition_count

LOGGER = logging.getLogger(__name__)
# How long the check waits for its probes. They travel to the server and back on one
# connection, which takes milliseconds; a probe that has not arrived by then never will.
PROBE_WAIT_SECONDS = 5.0
# More candidate ids than any count needs: a handful tell every other count apart.
_MAX_CANDIDATES = 10_000

# Why a check failed: the server applies no mapping, maps with another count, or nothing came back.
UNMAPPED, OTHER_COUNT, NO_ANSWER = "unmapped", "other_count", "no_answer"


class PartitionMappingError(RuntimeError):
    """The server does not put agents' addresses in their partitions at the installation's count."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def probe_agents(count: int, nonce: str) -> tuple[str, ...]:
    """Probe agent ids such that every other count from 1 to the contract's maximum gives one of them another partition.

    A server mapping with another count then delivers at least one probe with a token the
    installation's count does not give.
    """

    check_partition_count(count)
    untold = set(range(1, MAX_PARTITION_COUNT + 1)) - {count}
    chosen: list[str] = []
    for index in range(_MAX_CANDIDATES):
        if not untold:
            return tuple(chosen)
        agent = f"probe-{nonce}-{index}"
        expected = agent_partition(PARTITION_PROBE_SCOPE, agent, count)
        told = {other for other in untold if agent_partition(PARTITION_PROBE_SCOPE, agent, other) != expected}
        if told:
            chosen.append(agent)
            untold -= told
    raise RuntimeError(f"no probe agents tell counts {sorted(untold)} from {count}")  # never for FNV-1a


def _address_prefix(sender: str) -> str:
    """The probe addresses up to the agent token: ua.v1.<probe scope>.<sender>.ctl.agent."""

    return SUBJECT_PREFIX.format(team=PARTITION_PROBE_SCOPE, sender=sender) + "." + PARTITION_PROBE_ADDRESS.split("{agent}")[0]


def _mapping_lines(count: int) -> str:
    return "; ".join(f"{source} -> {target}" for source, target in sorted(account_mappings(count).items()))


async def check_partition_mapping(nc: Any, *, sender: str, count: int,
                                  wait_seconds: float = PROBE_WAIT_SECONDS) -> None:
    """Return when the server maps agents' addresses at ``count``; ``PartitionMappingError`` otherwise.

    ``nc`` is the checking process's NATS connection, ``sender`` its runtime id (the sender
    slot of what it publishes). Changes nothing on the server: it holds one subscription
    while it waits.
    """

    agents = probe_agents(count, secrets.token_hex(4))
    expected = {agent: str(agent_partition(PARTITION_PROBE_SCOPE, agent, count)) for agent in agents}
    prefix = _address_prefix(sender)
    arrived: dict[str, str] = {}  # agent -> the tokens after it ("" when it arrived unmapped)
    all_arrived = asyncio.Event()

    async def on_message(message: Any) -> None:
        agent, _, tokens = str(message.subject)[len(prefix):].partition(".")
        if agent in expected and agent not in arrived:  # another check's probes are not this one's
            arrived[agent] = tokens
            if len(arrived) == len(expected):
                all_arrived.set()

    subscription = await nc.subscribe(prefix + ">", cb=on_message)
    try:
        # Probes sent on the subscribing connection reach the server after the subscription.
        for agent in agents:
            await nc.publish(prefix + agent, b"")
        await nc.flush()
        try:
            await asyncio.wait_for(all_arrived.wait(), wait_seconds)
        except asyncio.TimeoutError:
            pass
    finally:
        await subscription.unsubscribe()
    _judge(arrived, expected, sender=sender, count=count, prefix=prefix)
    LOGGER.info("The NATS server maps agents' addresses at the installation's partition count. "
                "event=partition_mapping_checked count=%s probes=%s", count, len(agents))


def _judge(arrived: dict[str, str], expected: dict[str, str], *, sender: str, count: int, prefix: str) -> None:
    lines = _mapping_lines(count)
    if any(tokens == "" for tokens in arrived.values()):
        raise PartitionMappingError(
            UNMAPPED,
            f"The NATS server does not apply the partition mappings: a probe to {prefix}<agent> arrived without its "
            f"partition token. Add these mappings to the installation's account (at transport.queues.partitions "
            f"{count}): {lines}")
    wrong = {agent: tokens for agent, tokens in arrived.items() if tokens != expected[agent]}
    if wrong:
        matching = [other for other in range(1, MAX_PARTITION_COUNT + 1)
                    if all(tokens == str(agent_partition(PARTITION_PROBE_SCOPE, agent, other))
                           for agent, tokens in arrived.items())]
        server = f"partition count {matching[0]}" if len(matching) == 1 else "another partition count"
        raise PartitionMappingError(
            OTHER_COUNT,
            f"The NATS server maps agents' addresses with {server}, but transport.queues.partitions is {count}. "
            f"The count is fixed when the installation is created: set the setting back, or correct the server's "
            f"mappings to: {lines}")
    missing = sorted(set(expected) - set(arrived))
    if missing:
        raise PartitionMappingError(
            NO_ANSWER,
            f"The partition mapping check got no answer within its wait: nothing came back on {prefix}> for "
            f"{len(missing)} of {len(expected)} probes. The NATS login of runtime {sender} must be allowed to publish "
            f"and subscribe to {prefix}>, and the installation's account must hold these mappings: {lines}")
