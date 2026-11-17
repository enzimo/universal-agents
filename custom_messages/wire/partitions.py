"""An agent's partition and the keys that name partitions (contracts/messages/v1/README.md, "Partitions").

The installation's NATS account maps every agent's inbox, control queue and live-control
address to one partition of its team: FNV-1a (32 bits) over the UTF-8 bytes of the team id
followed at once by the agent id, modulo the installation's partition count. This is what
nats-server's ``partition(COUNT,1,3)`` computes over the subject's team and agent tokens, so
the partition computed here is the stream NATS stores the agent's messages in. Hosts own
whole partitions, and the keys of agent records and partition ownership records name them.

The count is fixed when an installation is created (``transport.queues.partitions``, default
``DEFAULT_PARTITION_COUNT``). Until the step that adds that setting, callers use the default.

The arrival suffix: a frame's signed ``subject`` is the unmapped one, so a receiver of a
mapped address takes the partition token off the subject the frame arrived on, after checking
it is the agent's partition, and compares the rest with the signed subject
(``signed_subject_of``). The bundled server applies no mapping yet, so running receivers still
compare exact subjects; the release that installs the mapping switches them to this.
"""

from __future__ import annotations

from typing import Iterable, Optional

from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT, MAX_PARTITION_COUNT, PARTITIONED_ADDRESSES, SUBJECT_PREFIX
from custom_messages.wire.refusals import FrameRefused

# The middle word of a partition ownership record's key, <team>.partition.<partition>, in UA1_PLACEMENT.
PARTITION_OWNER_WORD = "partition"

_FNV_OFFSET, _FNV_PRIME, _FNV_MASK = 0x811C9DC5, 0x01000193, 0xFFFFFFFF
# A subject is ua.v1.<team>.<sender>.<address>; the mapped addresses' fixed words, with the
# position of the agent's own token in the address.
_ROOT = tuple(SUBJECT_PREFIX.split("{team}", 1)[0].rstrip(".").split("."))
_MAPPED = {tuple(word for word in address.split(".") if word != "{agent}"): address.split(".").index("{agent}")
           for address in PARTITIONED_ADDRESSES}
# The token counts of mapped subjects without their partition token.
_MAPPED_LENGTHS = sorted({len(_ROOT) + 2 + len(words) + 1 for words in _MAPPED})


def check_partition_count(count: int) -> int:
    """The count itself, when it is a count an installation may choose (1 to ``MAX_PARTITION_COUNT``)."""

    if type(count) is not int or not 1 <= count <= MAX_PARTITION_COUNT:
        raise ValueError(f"a partition count is a whole number from 1 to {MAX_PARTITION_COUNT}, not {count!r}")
    return count


def agent_partition(team: str, agent: str, count: int = DEFAULT_PARTITION_COUNT) -> int:
    """The partition of ``agent`` in ``team``: FNV-1a over the team id then the agent id, modulo ``count``."""

    check_partition_count(count)
    value = _FNV_OFFSET
    for byte in (team + agent).encode("utf-8"):
        value = ((value ^ byte) * _FNV_PRIME) & _FNV_MASK
    return value % count


def agent_record_key(team: str, agent: str, count: int = DEFAULT_PARTITION_COUNT) -> str:
    """An agent record's key in ``UA1_AGENTS``: ``<team>.<partition>.<agent>``.

    Hosts' write grants name the partitions they own, so a host writes only the records of
    its own partitions' agents.
    """

    return f"{team}.{agent_partition(team, agent, count)}.{agent}"


def partition_owner_key(team: str, partition: int, count: int = DEFAULT_PARTITION_COUNT) -> str:
    """A partition ownership record's key in ``UA1_PLACEMENT``: ``<team>.partition.<partition>``."""

    if type(partition) is not int or not 0 <= partition < check_partition_count(count):
        raise ValueError(f"a partition is a whole number from 0 to {count - 1}, not {partition!r}")
    return f"{team}.{PARTITION_OWNER_WORD}.{partition}"


def every_partition(teams: Iterable[str], count: int = DEFAULT_PARTITION_COUNT) -> dict[str, tuple[int, ...]]:
    """Every partition of each team, for a role whose grants name a team's whole set of partitions.

    The team services own no partitions, but they withdraw queued work from every partition
    stream of each team they serve, so membership code fills their ``{partition}`` grants with
    this, not with ownership records (README, "Filling the templates").
    """

    numbers = tuple(range(check_partition_count(count)))
    return {team: numbers for team in teams}


def _mapped_agent(tokens: list[str]) -> Optional[tuple[str, str]]:
    """(team, agent) when ``tokens`` are a mapped address's subject without the partition token, else None."""

    head = len(_ROOT) + 2  # the root, the team and the sender slot
    address = tokens[head:]
    if tuple(tokens[:len(_ROOT)]) != _ROOT:
        return None
    for words, agent_at in _MAPPED.items():
        if len(address) == len(words) + 1 and tuple(address[:agent_at] + address[agent_at + 1:]) == words:
            return tokens[len(_ROOT)], address[agent_at]
    return None


def arrival_subject(signed_subject: str, count: int = DEFAULT_PARTITION_COUNT) -> str:
    """The subject NATS delivers a frame signed with ``signed_subject`` on: the agent's partition appended for a
    mapped address (an agent's inbox, control queue or live control address), any other subject unchanged."""

    found = _mapped_agent(signed_subject.split("."))
    return signed_subject if found is None else f"{signed_subject}.{agent_partition(*found, count)}"


def signed_subject_of(arrived_on: str, count: int = DEFAULT_PARTITION_COUNT) -> str:
    """The signed subject of a frame that arrived on ``arrived_on``: the arrival suffix checked and taken off.

    A mapped address must arrive with exactly one more token, the agent's partition at the
    installation's count; a missing token, an extra one or another partition is refused as
    ``subject_mismatch``, like any other subject that differs from the signed one. Any other
    subject is returned unchanged for the receiver's exact comparison.
    """

    tokens = arrived_on.split(".")
    for length in _MAPPED_LENGTHS:
        found = _mapped_agent(tokens[:length])
        if found is not None:
            if tokens[length:] != [str(agent_partition(*found, count))]:
                raise FrameRefused("subject_mismatch", "a mapped address arrives with its agent's partition, once",
                                   at="/subject")
            return ".".join(tokens[:length])
    return arrived_on
