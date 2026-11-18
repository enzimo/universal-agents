"""The check that the NATS server maps agents' addresses to their partitions (contract README, "Partitions").

The installation's account maps every agent's inbox, control queue and live control address
to one more token, the agent's partition at the installation's count, with one mapping line
per address. Hosts subscribe to an agent's live control address with that token, so a server
without the live control line, or with another count, leaves every agent's control requests
unanswered; a wrong or missing inbox or control-queue line would, from step 6 on, store queued
work in no partition stream or in another one, where it waits unread until the stream's age
limit. The bundled server's generated configuration carries the mappings; an external
server's operator writes them by hand. So the installation authority runs this check before
it serves, and the live-test harness runs it once against the shared test server.

The check subscribes to its own probe addresses, publishes an unsigned, empty message to each
mapped address of a few probe agents, and judges each mapping line by where its probes
arrived, so a refusal names the line that is missing or maps with another count. The probe
scope (``PARTITION_PROBE_SCOPE``) stands where a team id goes, and no team id starts with
``_``, so no team's receivers or streams see these messages; the sender slot is the checking
runtime, as in every subject it publishes. The probe agents' ids carry a random part, so two
checks at once on one server tell their messages apart.

The check sees the server its connection is on. A server applies the mappings to what its own
clients publish, and a message that reaches it from another server of a cluster is not mapped
again, so with accounts kept in the servers' configuration files every server of a cluster
needs the lines; an account in a signed account record (operator mode) carries them to every
server.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from custom_messages.wire.kinds import MAX_PARTITION_COUNT, PARTITION_PROBE_SCOPE, PARTITIONED_ADDRESSES, SUBJECT_PREFIX
from custom_messages.wire.partitions import (
    account_mappings,
    agent_partition,
    check_partition_count,
    mapping_line,
    mapping_source,
)

LOGGER = logging.getLogger(__name__)
# How long the check waits for its probes. They travel to the server and back on one
# connection, which takes milliseconds; a probe that has not arrived by then never will.
PROBE_WAIT_SECONDS = 5.0
# The fewest probe agents a check sends, beyond the ones that tell the counts up to the
# contract's maximum apart (see probe_agents).
MIN_PROBES = 16
# More candidate ids than any count needs: a handful tell every other count apart.
_MAX_CANDIDATES = 10_000
# What writes the mapping lines at a count, named in the refusals for source checkouts only: the
# published image keeps no .py files, and the script writes a top-level mappings block, which
# serves only a server whose logins are in no named account (G3 of pull request 152's review).
_LINES_SCRIPT = "python scripts/write_nats_mapping_config.py FILE --partitions {count}"

# Why a mapping line failed: the server applies no mapping there, maps with another count, or
# none of the line's probes came back (to the whole check: the login or the connection).
UNMAPPED, OTHER_COUNT, NO_ANSWER = "unmapped", "other_count", "no_answer"


class PartitionMappingError(RuntimeError):
    """The server does not put agents' addresses in their partitions at the installation's count.

    ``problems`` maps the source of each failing mapping line (``ua.v1.*.*.<address>``) to why
    it failed: ``UNMAPPED``, ``OTHER_COUNT`` or ``NO_ANSWER``.
    """

    def __init__(self, problems: dict[str, str], message: str) -> None:
        super().__init__(message)
        self.problems = dict(problems)


def probe_agents(count: int, nonce: str) -> tuple[str, ...]:
    """Probe agent ids such that every other count from 1 to the contract's maximum gives one of them another partition.

    A server mapping with another count up to the maximum then delivers at least one probe
    with a token the installation's count does not give. An operator's typing mistake can be
    any number, so the ids continue to ``MIN_PROBES``: a count above the maximum is caught by
    that minimum, not by the choice. The worst case is a multiple of the installation's count,
    where each probe keeps its token with chance count/multiple, at most one half, so all of
    them do in at most one check in 65,536 (2**-16); every other count far less often.
    """

    check_partition_count(count)
    untold = set(range(1, MAX_PARTITION_COUNT + 1)) - {count}
    chosen: list[str] = []
    for index in range(_MAX_CANDIDATES):
        if not untold and len(chosen) >= MIN_PROBES:
            return tuple(chosen)
        agent = f"probe-{nonce}-{index}"
        expected = agent_partition(PARTITION_PROBE_SCOPE, agent, count)
        told = {other for other in untold if agent_partition(PARTITION_PROBE_SCOPE, agent, other) != expected}
        if told or not untold:
            chosen.append(agent)
            untold -= told
    raise RuntimeError(f"no probe agents tell counts {sorted(untold)} from {count}")  # never for FNV-1a


def probe_root(sender: str) -> str:
    """Where the check's probes go: ua.v1.<probe scope>.<sender>., followed by a mapped address."""

    return SUBJECT_PREFIX.format(team=PARTITION_PROBE_SCOPE, sender=sender) + "."


def _mapping_lines(count: int) -> str:
    return ", ".join(mapping_line(source, target) for source, target in sorted(account_mappings(count).items()))


def _lines_advice(count: int) -> str:
    """Where the mapping lines go and the lines themselves, then the script for a source checkout."""

    return (f"Add these mapping lines inside the account the instance logs in to (in the server's configuration "
            f"file, the account's mappings block): {_mapping_lines(count)}. In a source checkout, "
            f"{_LINES_SCRIPT.format(count=count)} writes them as a top-level mappings block, which serves a server "
            f"whose logins are in no named account.")


async def check_partition_mapping(nc: Any, *, sender: str, count: int,
                                  wait_seconds: float = PROBE_WAIT_SECONDS) -> None:
    """Return when the server maps every agent address at ``count``; ``PartitionMappingError`` otherwise.

    ``nc`` is the checking process's NATS connection, ``sender`` its runtime id (the sender
    slot of what it publishes). Changes nothing on the server: it holds one subscription
    while it waits.
    """

    agents = probe_agents(count, secrets.token_hex(4))
    root = probe_root(sender)
    # Each probe's unmapped subject, with the address and agent it stands for. Every mapped
    # address has the same number of tokens, so an arrival is either a probe's subject as
    # published or that subject and one more token.
    probes = {root + address.replace("{agent}", agent): (address, agent)
              for address in PARTITIONED_ADDRESSES for agent in agents}
    arrived: dict[tuple[str, str], str] = {}  # (address, agent) -> its arrival token ("" when unmapped)
    all_arrived = asyncio.Event()

    async def on_message(message: Any) -> None:
        subject = str(message.subject)
        head, _, token = subject.rpartition(".")
        if subject in probes:
            probe, tokens = probes[subject], ""
        elif head in probes:
            probe, tokens = probes[head], token
        else:  # another check's probes, or a line that sends them elsewhere
            return
        if probe not in arrived:
            arrived[probe] = tokens
            if len(arrived) == len(probes):
                all_arrived.set()

    subscription = await nc.subscribe(root + ">", cb=on_message)
    try:
        # Probes sent on the subscribing connection reach the server after the subscription.
        for subject in probes:
            await nc.publish(subject, b"")
        await nc.flush()
        try:
            await asyncio.wait_for(all_arrived.wait(), wait_seconds)
        except asyncio.TimeoutError:
            pass
    finally:
        await subscription.unsubscribe()
    _judge(arrived, agents, sender=sender, count=count, root=root, wait_seconds=wait_seconds)
    LOGGER.info("The NATS server maps agents' addresses at the installation's partition count. "
                "event=partition_mapping_checked count=%s probes=%s lines=%s", count, len(agents),
                len(PARTITIONED_ADDRESSES))


def _judge(arrived: dict[tuple[str, str], str], agents: tuple[str, ...], *, sender: str, count: int, root: str,
           wait_seconds: float) -> None:
    if not arrived:
        raise PartitionMappingError(
            {mapping_source(address): NO_ANSWER for address in PARTITIONED_ADDRESSES},
            f"The partition mapping check got no answer within {wait_seconds:g} s: none of its probes came back on "
            f"{root}>. The NATS login of runtime {sender} must be allowed to publish and subscribe to {root}>, or the "
            f"connection to the server was lost. The account must also map agents' addresses at "
            f"transport.queues.partitions {count}. {_lines_advice(count)}")
    problems: dict[str, str] = {}
    findings: list[str] = []
    for address in PARTITIONED_ADDRESSES:
        source = mapping_source(address)
        tokens = {agent: arrived[(address, agent)] for agent in agents if (address, agent) in arrived}
        reason, finding = _judge_line(address, tokens, agents, count=count, root=root)
        if reason is not None:
            problems[source] = reason
            findings.append(f"the line for {source} {finding}")
    if not problems:
        return
    advice = ""
    if OTHER_COUNT in problems.values():
        advice = ("The partition count is fixed when the installation is created: if transport.queues.partitions was "
                  "changed, set it back; otherwise correct the server's lines. ")
    raise PartitionMappingError(
        problems,
        f"The NATS server does not map agents' addresses at transport.queues.partitions {count}: "
        f"{'; '.join(findings)}. {advice}{_lines_advice(count)}")


def _judge_line(address: str, tokens: dict[str, str], agents: tuple[str, ...], *, count: int,
                root: str) -> tuple[str | None, str]:
    """Why one mapping line failed, and the words for it; ``(None, "")`` when every probe arrived with its token."""

    probe = root + address.replace("{agent}", "<agent>")
    if any(token == "" for token in tokens.values()):
        return UNMAPPED, f"is missing: a probe to {probe} arrived without a partition token"
    if any(token != str(agent_partition(PARTITION_PROBE_SCOPE, agent, count)) for agent, token in tokens.items()):
        matching = [other for other in range(1, MAX_PARTITION_COUNT + 1)
                    if all(token == str(agent_partition(PARTITION_PROBE_SCOPE, agent, other))
                           for agent, token in tokens.items())]
        server = f"partition count {matching[0]}" if len(matching) == 1 else "another partition count"
        return OTHER_COUNT, f"maps with {server}"
    if len(tokens) < len(agents):
        return NO_ANSWER, (f"sent {len(agents) - len(tokens)} of {len(agents)} probes to {probe} somewhere the check "
                           f"does not hear: its target differs from the one below")
    return None, ""
