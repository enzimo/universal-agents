"""Each placed agent's consumers follow its team's placements, against stand-ins (contract README, "Who sets up the streams").

installation_authority.placements.PlacedConsumers: a placement that verifies gets the
agent's two consumers; a deleted placement removes them once its queues hold nothing and
keeps them while they hold work; the catch-up at start reads the current placements, looks
up no deleted key, and sweeps the consumers of agents no longer placed; a failed change is
tried again after a wait that doubles, from the placement as it is under the lock. The same
with the real team services and nats-server 2.15.1 is in tests/test_live_placed_consumers.py.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Optional

import pytest

from custom_messages.wire.refusals import FrameRefused
from installation_authority.placements import PlacedConsumers
from installation_authority.queues import AgentBacklog

TEAM = "team_a"


def placement(agent: str, *, team: str = TEAM, signed: bool = True, body_agent: Optional[str] = None) -> bytes:
    """A stand-in placement record: ``signed`` says whether it verifies."""

    return json.dumps({"key": f"{team}.{agent}", "team": team, "agent": body_agent or agent, "signed": signed}).encode()


def verify(raw: bytes) -> Any:
    record = json.loads(raw)
    if not record["signed"]:
        raise FrameRefused("bad_signature", at="/signature")
    return SimpleNamespace(document=SimpleNamespace(key=record["key"]),
                           body=SimpleNamespace(team=record["team"], agent=record["agent"]))


class StandInRecords:
    """``UA1_PLACEMENT`` as a dict, and a watch that gives its values, then the changes ``change`` makes."""

    def __init__(self, **placements: bytes) -> None:
        self.values = {f"{TEAM}.{agent}": raw for agent, raw in placements.items()}
        # Delete markers the bucket still holds, given among the watch's first values.
        self.deleted: list[str] = []
        self.changes: asyncio.Queue[tuple[str, Optional[bytes]]] = asyncio.Queue()
        self.watched: list[str] = []
        self.reads: list[str] = []
        self.fail_watches = 0

    async def watch(self, record: str, keys: str = ">", *, team: Optional[str] = None,
                    caught_up: Optional[asyncio.Event] = None) -> AsyncIterator[tuple[str, Optional[bytes]]]:
        assert record == "placement"
        self.watched.append(keys)
        if self.fail_watches:
            self.fail_watches -= 1
            raise ConnectionError("the watch could not be opened")
        for key in self.deleted:
            yield key, None
        for key, raw in sorted(self.values.items()):
            yield key, raw
        assert caught_up is not None
        caught_up.set()
        while True:
            yield await self.changes.get()

    async def raw(self, record: str, key: str) -> Optional[tuple[int, bytes]]:
        self.reads.append(key)
        value = self.values.get(key)
        return None if value is None else (1, value)

    def change(self, agent: str, raw: Optional[bytes]) -> None:
        key = f"{TEAM}.{agent}"
        if raw is None:
            self.values.pop(key, None)
        else:
            self.values[key] = raw
        self.changes.put_nowait((key, raw))


class StandInConsumers:
    """Which agents have their two consumers, and what their queues hold."""

    def __init__(self, *agents: str) -> None:
        self.agents = set(agents)
        self.backlogs: dict[str, AgentBacklog] = {}
        self.calls: list[tuple[str, str]] = []

    async def ensure_agent_consumers(self, team: str, agent: str) -> None:
        self.calls.append(("ensure", agent))
        self.agents.add(agent)

    async def agent_backlog(self, team: str, agent: str) -> AgentBacklog:
        self.calls.append(("backlog", agent))
        return self.backlogs.get(agent, AgentBacklog(0, 0, 0))

    async def delete_agent_consumers(self, team: str, agent: str) -> int:
        self.calls.append(("delete", agent))
        if agent not in self.agents:
            return 0
        self.agents.discard(agent)
        return 2

    async def agents_with_consumers(self, team: str) -> set[str]:
        self.calls.append(("list", team))
        return set(self.agents)


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def eventually(check: Callable[[], bool], timeout: float = 5) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "not within the time allowed"
        await asyncio.sleep(0.005)


async def following(records: StandInRecords, consumers: StandInConsumers, scenario: Any, **options: Any) -> PlacedConsumers:
    placed = PlacedConsumers(records, consumers, verify=verify, teams=[TEAM], **{"recheck_seconds": 3600, **options})
    stop = asyncio.Event()
    running = asyncio.create_task(placed.run(stop))
    try:
        await asyncio.wait_for(placed.ready.wait(), timeout=5)
        await scenario(placed)
    finally:
        stop.set()
        await asyncio.wait_for(running, timeout=5)
    return placed


def test_a_placement_that_verifies_gets_its_agents_consumers_and_one_that_does_not_gets_none(caplog) -> None:
    records = StandInRecords(**{"researcher-1": placement("researcher-1"),
                                "researcher-2": placement("researcher-2", signed=False),
                                "researcher-3": placement("researcher-3", body_agent="researcher-4")})
    consumers = StandInConsumers()

    async def scenario(placed: PlacedConsumers) -> None:
        records.change("writer-1", placement("writer-1"))
        await settle()

    with caplog.at_level("INFO"):
        asyncio.run(following(records, consumers, scenario))
    assert consumers.agents == {"researcher-1", "writer-1"}
    assert records.watched == [f"{TEAM}.*"], "two-part keys only: partition ownership records are never read"
    assert f"event=placement_not_verified team={TEAM} key={TEAM}.researcher-2 code=bad_signature" in caplog.text
    assert f"event=placement_not_verified team={TEAM} key={TEAM}.researcher-3 code=invalid_body at=/body/agent" \
        in caplog.text


def test_an_ended_placement_whose_queues_are_empty_loses_its_consumers(caplog) -> None:
    records = StandInRecords(**{"researcher-1": placement("researcher-1")})
    consumers = StandInConsumers()

    async def scenario(placed: PlacedConsumers) -> None:
        records.change("researcher-1", None)
        await settle()

    with caplog.at_level("INFO"):
        asyncio.run(following(records, consumers, scenario))
    assert consumers.agents == set()
    assert f"event=agent_consumers_removed team={TEAM} agent=researcher-1" in caplog.text


def test_consumers_holding_work_stay_until_their_queues_are_empty(caplog) -> None:
    records = StandInRecords(**{"researcher-1": placement("researcher-1")})
    consumers = StandInConsumers()
    consumers.backlogs["researcher-1"] = AgentBacklog(pending=1, ack_pending=1, stored=2)

    async def scenario(placed: PlacedConsumers) -> None:
        records.change("researcher-1", None)
        await settle()
        assert consumers.agents == {"researcher-1"}
        await placed.recheck()  # still holding work: kept, logged once
        assert consumers.agents == {"researcher-1"}
        consumers.backlogs["researcher-1"] = AgentBacklog(pending=0, ack_pending=0, stored=1)  # past its deliveries
        await placed.recheck()
        assert consumers.agents == {"researcher-1"}
        consumers.backlogs.clear()
        await placed.recheck()
        assert consumers.agents == set()
        consumers.calls.clear()
        await placed.recheck()
        assert consumers.calls == [], "nothing left to check"

    with caplog.at_level("INFO"):
        asyncio.run(following(records, consumers, scenario))
    assert caplog.text.count("event=agent_consumers_kept") == 1
    assert f"event=agent_consumers_kept team={TEAM} agent=researcher-1 pending=1 ack_pending=1 stored=2" in caplog.text
    assert f"event=agent_consumers_removed team={TEAM} agent=researcher-1" in caplog.text


def test_an_agent_placed_again_while_its_consumers_are_kept_keeps_them() -> None:
    records = StandInRecords(**{"researcher-1": placement("researcher-1")})
    consumers = StandInConsumers()
    consumers.backlogs["researcher-1"] = AgentBacklog(pending=3, ack_pending=0, stored=3)

    async def scenario(placed: PlacedConsumers) -> None:
        records.change("researcher-1", None)
        await settle()
        records.change("researcher-1", placement("researcher-1"))
        await settle()
        consumers.backlogs.clear()
        consumers.calls.clear()
        await placed.recheck()
        assert consumers.calls == [], "no longer kept: it is placed"

    asyncio.run(following(records, consumers, scenario))
    assert consumers.agents == {"researcher-1"}


def test_a_placement_written_again_before_its_consumers_are_removed_keeps_them() -> None:
    """The key is read again under the lock: the watch's deletion may be older than the bucket."""

    records = StandInRecords(**{"researcher-1": placement("researcher-1")})
    consumers = StandInConsumers()

    async def scenario(placed: PlacedConsumers) -> None:
        records.changes.put_nowait((f"{TEAM}.researcher-1", None))  # the bucket already holds it again
        await settle()

    asyncio.run(following(records, consumers, scenario))
    assert consumers.agents == {"researcher-1"} and ("delete", "researcher-1") not in consumers.calls


def test_the_catch_up_looks_up_no_deleted_key_and_sweeps_agents_no_longer_placed(caplog) -> None:
    """C06: deletions among the first values only note the key; the sweep keeps every present key's consumers."""

    records = StandInRecords(**{"researcher-1": placement("researcher-1"),
                                "researcher-2": placement("researcher-2", signed=False)})
    records.deleted = [f"{TEAM}.gone-{number}" for number in range(200)]
    consumers = StandInConsumers("researcher-2", "ended-1", "ended-2")  # what the stream holds from before
    consumers.backlogs["ended-2"] = AgentBacklog(pending=1, ack_pending=0, stored=1)

    async def scenario(placed: PlacedConsumers) -> None:
        pass

    with caplog.at_level("INFO"):
        asyncio.run(following(records, consumers, scenario))
    assert consumers.agents == {"researcher-1", "researcher-2", "ended-2"}
    assert not [call for call in consumers.calls if call[1].startswith("gone-")]
    assert not [read for read in records.reads if ".gone-" in read]
    assert f"event=agent_consumers_kept team={TEAM} agent=ended-2" in caplog.text
    assert f"event=placed_consumers_caught_up team={TEAM} placed=2 unplaced=2" in caplog.text


def test_a_failed_watch_is_opened_again_and_catches_up_again(caplog) -> None:
    records = StandInRecords(**{"researcher-1": placement("researcher-1")})
    records.fail_watches = 1
    consumers = StandInConsumers("ended-1")

    async def scenario(placed: PlacedConsumers) -> None:
        pass

    with caplog.at_level("INFO"):
        asyncio.run(following(records, consumers, scenario, retry_seconds=0.01))
    assert records.watched == [f"{TEAM}.*", f"{TEAM}.*"]
    assert f"event=placement_watch_failed team={TEAM}" in caplog.text
    assert consumers.agents == {"researcher-1"}


def test_a_failed_sweep_is_tried_again_at_the_next_check(caplog) -> None:
    class FailingOnce(StandInConsumers):
        failed = False

        async def agents_with_consumers(self, team: str) -> set[str]:
            if not self.failed:
                self.failed = True
                raise ConnectionError("no answer")
            return await super().agents_with_consumers(team)

    records = StandInRecords()
    consumers = FailingOnce("ended-1")

    async def run() -> None:
        placed = PlacedConsumers(records, consumers, verify=verify, teams=[TEAM], recheck_seconds=3600)
        stop = asyncio.Event()
        running = asyncio.create_task(placed.run(stop))
        await settle()
        assert not placed.ready.is_set() and consumers.agents == {"ended-1"}
        await placed.recheck()
        assert placed.ready.is_set() and consumers.agents == set()
        stop.set()
        await running

    with caplog.at_level("INFO"):
        asyncio.run(run())
    assert f"event=placed_consumers_sweep_failed team={TEAM}" in caplog.text


def test_a_key_that_names_no_valid_agent_is_left_alone(caplog) -> None:
    records = StandInRecords()
    records.values[f"{TEAM}.Not An Agent"] = b"{}"
    consumers = StandInConsumers()

    async def scenario(placed: PlacedConsumers) -> None:
        records.changes.put_nowait((f"{TEAM}.Not An Agent", None))
        await settle()

    with caplog.at_level("INFO"):
        asyncio.run(following(records, consumers, scenario))
    assert f"event=placement_key_refused team={TEAM} key={TEAM}.Not An Agent" in caplog.text
    assert [call for call in consumers.calls if call[0] != "list"] == []


@pytest.mark.parametrize("teams", [("team_a", "team_b")])
def test_every_team_served_is_followed(teams: tuple[str, ...]) -> None:
    records = StandInRecords()
    consumers = StandInConsumers()

    async def run() -> None:
        placed = PlacedConsumers(records, consumers, verify=verify, teams=teams, recheck_seconds=3600)
        stop = asyncio.Event()
        running = asyncio.create_task(placed.run(stop))
        await asyncio.wait_for(placed.ready.wait(), timeout=5)
        stop.set()
        await running

    asyncio.run(run())
    assert sorted(records.watched) == ["team_a.*", "team_b.*"]


def test_one_agents_failed_change_leaves_the_others_and_is_tried_again_after_a_wait_that_doubles(caplog) -> None:
    """C25 of the step 2a review: the first wait is retry_seconds, each failure doubles it, up to recheck_seconds."""

    class FullPartition(StandInConsumers):
        refusals = 4

        async def ensure_agent_consumers(self, team: str, agent: str) -> None:
            if agent == "researcher-1" and self.refusals:
                self.refusals -= 1
                raise RuntimeError("maximum consumers limit reached")
            await super().ensure_agent_consumers(team, agent)

    records = StandInRecords(**{"researcher-1": placement("researcher-1"), "researcher-2": placement("researcher-2")})
    consumers = FullPartition()

    async def scenario(placed: PlacedConsumers) -> None:
        records.change("writer-1", placement("writer-1"))
        await settle()
        assert consumers.agents == {"researcher-2", "writer-1"}, "the watch carried on"
        await eventually(lambda: "researcher-1" in consumers.agents)
        consumers.calls.clear()
        await asyncio.sleep(0.1)
        assert consumers.calls == [], "tried again until it worked, then done"

    with caplog.at_level("INFO"):
        asyncio.run(following(records, consumers, scenario, retry_seconds=0.01, recheck_seconds=0.04))
    assert re.findall(r"agent=researcher-1 action=(\w+) retry_in=([\d.]+)", caplog.text) == [
        ("create", "0.01"), ("retry", "0.02"), ("retry", "0.04"), ("retry", "0.04")]
    assert "event=placement_watch_failed" not in caplog.text


def test_a_retry_reads_the_placement_under_the_lock_so_a_deletion_handled_meanwhile_is_not_undone() -> None:
    """C25 of the step 2a review: the retry's read and its change both happen under the one lock.

    The creation failed, so a retry is due. It reads the placement (still there) and its
    answer is on its way when the placement is deleted and the watch handles the deletion.
    The removal waits for the lock, so it comes after the retry's creation and removes it.
    """

    class ReplyOnItsWay(StandInRecords):
        def __init__(self, **placements: bytes) -> None:
            super().__init__(**placements)
            self.holding, self.release = asyncio.Event(), asyncio.Event()

        async def raw(self, record: str, key: str) -> Optional[tuple[int, bytes]]:
            answer = await super().raw(record, key)
            if not self.holding.is_set():  # the first read is the retry's
                self.holding.set()
                await self.release.wait()
            return answer

    class FailsOnce(StandInConsumers):
        failed = False

        async def ensure_agent_consumers(self, team: str, agent: str) -> None:
            if not self.failed:
                self.failed = True
                raise TimeoutError("no reply")
            await super().ensure_agent_consumers(team, agent)

    records = ReplyOnItsWay(**{"researcher-1": placement("researcher-1")})
    consumers = FailsOnce()

    async def scenario(placed: PlacedConsumers) -> None:
        await asyncio.wait_for(records.holding.wait(), timeout=5)
        records.change("researcher-1", None)
        await settle()
        records.release.set()
        await eventually(lambda: ("delete", "researcher-1") in consumers.calls)

    asyncio.run(following(records, consumers, scenario, retry_seconds=0.01))
    assert [call for call in consumers.calls if call[0] != "list"] == [
        ("ensure", "researcher-1"), ("backlog", "researcher-1"), ("delete", "researcher-1")]
    assert consumers.agents == set()
