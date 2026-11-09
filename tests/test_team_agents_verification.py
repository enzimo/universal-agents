"""Reader recovery and identity checks using real signatures and the contract trust store."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.trust import sign_state
from custom_transport.protocol1.team_agents import TeamAgents
from tests.message_contract_support import EXAMPLES, NOW, private_key, trust_store

TEAM, INSTALL = "team_example", "install_example"
KEY = f"{TEAM}.lead"


def _example(name):
    document = json.loads((EXAMPLES / "state" / "valid" / name).read_bytes())
    return document


def _signed(document):
    return json.dumps(sign_state(document, private_key(document["writer"]["key_id"]))).encode()


def _agent(*, sequence=100, state="idle", **body):
    document = _example("agent/lead-busy.json")
    document["sequence"] = sequence
    document["body"].update(state=state, accepting_work=state != "paused", display_name="Team lead", **body)
    return document


class _Records:
    def __init__(self, store):
        self.store = store
        self.values = {record: {} for record in ("agent", "placement", "host", "agent_session")}
        self.queues = {record: asyncio.Queue() for record in self.values}
        self.trust_failure = None

    async def watch(self, record, keys, *, caught_up):
        for key, raw in list(self.values[record].items()):
            yield key, raw
        caught_up.set()
        while True:
            item = await self.queues[record].get()
            try:
                if isinstance(item, Exception):
                    raise item
                yield item
            finally:
                self.queues[record].task_done()

    async def take_trust_record(self, record, key, *, now):
        if self.trust_failure is not None:
            error, self.trust_failure = self.trust_failure, None
            raise error
        raw = self.values[record].get(key)
        return False if raw is None else self.store.add(raw, now=now)

    async def feed(self, record, key, raw):
        if raw is None:
            self.values[record].pop(key, None)
        else:
            self.values[record][key] = raw
        await self.queues[record].put((key, raw))
        await asyncio.wait_for(self.queues[record].join(), 2)


async def _until(check):
    async with asyncio.timeout(2):
        while not check():
            await asyncio.sleep(0.005)


@asynccontextmanager
async def _reading(*, store=None):
    store = store or trust_store()
    records = _Records(store)

    async def fetch(raw):
        return False

    host = SimpleNamespace(identity=SimpleNamespace(installation=INSTALL), clock=lambda: NOW,
                           live=SimpleNamespace(trust=SimpleNamespace(store=store)), records=records, fetch_trust=fetch)
    reader = TeamAgents(host, TEAM, recheck_seconds=0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(reader.run(stop))
    try:
        await asyncio.wait_for(reader.ready.wait(), 2)
        await records.feed("placement", KEY, _signed(_example("placement/lead.json")))
        await records.feed("host", "lead-host-1", _signed(_example("host/lead-host.json")))
        await records.feed("agent", KEY, _signed(_agent()))
        await _until(lambda: reader.lead() is not None)
        yield reader, records, host
    finally:
        stop.set()
        await asyncio.wait_for(task, 2)
    assert not reader.running and not reader.ready.is_set() and not reader.live()


@pytest.mark.asyncio
async def test_copied_records_cannot_relabel_an_agent_or_extend_another_hosts_liveness(caplog):
    async with _reading() as (reader, records, host):
        await records.feed("agent", f"{TEAM}.other", _signed(_agent()))
        assert reader.agent("other") is None
        await records.feed("host", "other-host", _signed(_example("host/lead-host.json")))
        assert "other-host" not in reader._hosts
        assert "code=invalid_body at=/key" in caplog.text
        host.clock = lambda: NOW + timedelta(minutes=1)
        await _until(lambda: reader.agent("lead") is None)
        assert reader.departure_reason("lead") == "host_unavailable"


@pytest.mark.asyncio
async def test_another_signing_host_cannot_poison_the_sequence_or_publish_an_exit(caplog):
    async with _reading() as (reader, records, host):
        poison = _agent(sequence=9007199254740991, state="exiting")
        poison["writer"].update(runtime="worker-host-1", key_id="test-worker-key-1")
        await records.feed("agent", KEY, _signed(poison))
        assert reader.lead() is not None
        await records.feed("agent", KEY, _signed(_agent(sequence=101, state="paused")))
        assert reader.agent("lead").state == "paused"
        assert not reader.agent("lead").takes_work
        assert "code=wrong_issuer" in caplog.text


@pytest.mark.asyncio
async def test_deleting_a_key_does_not_reset_replay_protection_and_same_sequence_conflicts_are_logged(caplog):
    async with _reading() as (reader, records, host):
        await records.feed("agent", KEY, _signed(_agent(sequence=101, state="paused")))
        for raw in (_signed(_agent()), _signed(_agent(sequence=101, state="idle")),
                    _signed(_agent(sequence=101, state="paused"))):
            await records.feed("agent", KEY, None)
            await records.feed("agent", KEY, raw)
            assert reader.agent("lead") is None
        await records.feed("agent", KEY, _signed(_agent(sequence=102)))
        assert reader.lead() is not None
        assert "code=replayed at=/sequence" in caplog.text


@pytest.mark.asyncio
async def test_session_role_is_bound_to_its_agent_host_type_and_current_lifetime():
    async with _reading() as (reader, records, host):
        original = reader.lead()
        # The same host may run workers. It still cannot reuse its lead's session
        # under another id, even after the reader has recognized the genuine lead.
        placement = _example("placement/lead.json")
        placement["key"] = f"{TEAM}.impostor"
        placement["body"]["agent"] = "impostor"
        await records.feed("placement", placement["key"], _signed(placement))
        impostor = _agent(agent="impostor")
        impostor["key"] = placement["key"]
        await records.feed("agent", impostor["key"], _signed(impostor))
        assert reader.lead() == original and reader.agent("impostor") is None
        changed = _agent(sequence=101, agent_type="researcher")
        await records.feed("agent", KEY, _signed(changed))
        assert reader.of_type("researcher") == []

        session_path = EXAMPLES / "trust" / "sessions" / "lead.json"
        session = json.loads(session_path.read_bytes())
        session["sequence"] += 1
        session["body"]["ends_at"] = format_timestamp(NOW - timedelta(seconds=1))
        await records.feed("agent_session", session["key"], _signed(session))
        await _until(lambda: reader.lead() is None)
        # Its session ended: the team services record the end when its host closes it, so it left for good.
        assert reader.departure_reason("lead") == "session_closed" and reader.left_for_good("lead")


@pytest.mark.asyncio
async def test_a_trust_timeout_retries_without_a_new_agent_write_and_failed_watches_reopen(caplog):
    async with _reading() as (reader, records, host):
        changes = []
        reader.listen(changes.append)
        original = host.live.trust.store.verify
        failure = True

        def verify(*args, **kwargs):
            nonlocal failure
            if failure:
                failure = False
                raise TimeoutError("certificate read timed out")
            return original(*args, **kwargs)

        host.live.trust.store.verify = verify
        await records.feed("agent", KEY, _signed(_agent(sequence=101, state="busy")))
        await _until(lambda: reader.agent("lead").state == "busy")
        assert reader.running and reader.ready.is_set()
        await records.queues["agent"].put(ConnectionError("watch connection lost"))
        await records.queues["agent"].join()
        await _until(lambda: reader.agent("lead") is not None)
        assert reader.agent("lead").state == "busy"
        assert changes and "event=team_agents_watch_failed" in caplog.text


@pytest.mark.asyncio
async def test_missing_session_is_retried_and_duplicate_names_do_not_pick_an_arbitrary_agent():
    async with _reading(store=trust_store(remove=["sessions/researcher-2.json"])) as (reader, records, host):
        placement = _example("placement/lead.json")
        placement["key"] = f"{TEAM}.researcher-2"
        placement["body"].update(agent="researcher-2", agent_type="researcher", host="worker-host-1")
        await records.feed("placement", placement["key"], _signed(placement))
        document = _agent(agent="researcher-2", agent_type="researcher", session="s-researcher-2-1")
        document["key"] = placement["key"]
        document["writer"].update(runtime="worker-host-1", key_id="test-worker-key-1")
        # Give the worker host a genuine liveness record by re-signing its own
        # certificate reference, rather than faking a verified record.
        host_record = _example("host/lead-host.json")
        host_record["key"] = "worker-host-1"
        host_record["writer"].update(runtime="worker-host-1", key_id="test-worker-key-1")
        certificate = host.live.trust.store.host_certificate(INSTALL, "worker-host-1", 3)
        held = host.live.trust.store.held_record("certificate", f"host.{INSTALL}.worker-host-1.3")
        host_record["body"].update(runtime="worker-host-1", certificate={"certificate_id": certificate.certificate_id,
            "membership_epoch": 3, "sha256": held[1]}, session_limit=certificate.session_limit)
        await records.feed("host", "worker-host-1", _signed(host_record))
        records.trust_failure = TimeoutError("session lookup timed out")
        await records.feed("agent", document["key"], _signed(document))
        assert reader.agent("researcher-2") is None
        session = json.loads((EXAMPLES / "trust" / "sessions" / "researcher-2.json").read_bytes())
        records.values["agent_session"][session["key"]] = _signed(session)
        await _until(lambda: reader.agent("researcher-2") is not None)
        assert reader.named("Team lead") is None
        assert reader.lead().agent == "lead"


@pytest.mark.asyncio
async def test_agent_record_expiry_is_checked_even_while_the_host_keeps_heartbeating():
    async with _reading() as (reader, records, host):
        # The host refreshes its own record, but its agent's writer has stalled.
        later = NOW + timedelta(minutes=3)
        heartbeat = _example("host/lead-host.json")
        heartbeat.update(sequence=1000, issued_at=format_timestamp(later),
                         expires_at=format_timestamp(later + timedelta(seconds=30)))
        heartbeat["body"]["heartbeat_at"] = heartbeat["issued_at"]
        host.clock = lambda: later
        await records.feed("host", "lead-host-1", _signed(heartbeat))
        await _until(lambda: reader.agent("lead") is None)
        assert reader.departure_reason("lead") == "record_expired"


@pytest.mark.asyncio
async def test_reading_a_trust_record_under_another_bucket_key_cannot_mutate_trust():
    from custom_messages.wire.refusals import FrameRefused
    from custom_transport.protocol1.state_records import StateRecords

    document = json.loads((EXAMPLES / "trust" / "sessions" / "lead.json").read_bytes())
    changes = []

    async def get(key):
        return SimpleNamespace(value=_signed(document), revision=1)

    async def bucket(name):
        return SimpleNamespace(get=get)

    records = StateRecords(SimpleNamespace(key_value=bucket), lambda: None,
                           SimpleNamespace(add=lambda *args, **kwargs: changes.append(True)))
    with pytest.raises(FrameRefused, match="invalid_body"):
        await records.take_trust_record("agent_session", "session.install_example.team_example.another", now=NOW)
    assert changes == []


@pytest.mark.asyncio
async def test_a_copied_host_record_cannot_announce_a_different_installation_contract():
    from custom_transport.protocol1.host_runtime import MessageHost

    store = trust_store()
    document = _example("host/lead-host.json")
    document["body"]["contract_sha256"] = "f" * 64
    announced = []

    async def watch(*args):
        yield "team-services-1", _signed(document)

    host = SimpleNamespace(records=SimpleNamespace(watch=watch), live=SimpleNamespace(trust=SimpleNamespace(store=store)),
                           clock=lambda: NOW, _contract_record_refused=None, profile=SimpleNamespace(runtime="reader"),
                           logger=logging.getLogger(__name__),
                           learned_other_contract=lambda *args: announced.append(args))
    await MessageHost._contract_record(host, "team-services-1", _signed(document), "team-services-1")
    assert announced == [] and host._contract_record_refused == "invalid_body"


@pytest.mark.parametrize("expires", [None, "2026-10-22T04:31:00.000Z"])
def test_a_host_on_another_contract_still_has_to_expire_within_thirty_seconds(expires):
    from custom_messages.wire.refusals import FrameRefused

    document = _example("host/lead-host.json")
    document["body"]["contract_sha256"] = "f" * 64
    if expires is None:
        document.pop("expires_at")
    else:
        document["expires_at"] = expires
    with pytest.raises(FrameRefused, match="invalid_body") as refused:
        trust_store().verify(_signed(document), record="host", now=NOW)
    assert refused.value.at == "/expires_at"


def test_a_host_on_another_contract_cannot_sign_another_hosts_identity():
    from custom_messages.wire.refusals import FrameRefused

    document = _example("host/lead-host.json")
    document["body"].update(runtime="team-services-1", contract_sha256="f" * 64)
    document["key"] = "team-services-1"
    with pytest.raises(FrameRefused, match="invalid_body") as refused:
        trust_store().verify(_signed(document), record="host", now=NOW)
    assert refused.value.at == "/key"



# -- leaving for good, also when the evidence comes late (A11) ------------------------------------


def _closed_lead_session():
    session = json.loads((EXAMPLES / "trust" / "sessions" / "lead.json").read_bytes())
    session["sequence"] += 1
    session["body"]["ends_at"] = format_timestamp(NOW - timedelta(seconds=1))
    return session["key"], _signed(session)


def _heard(reader, changes):
    """What a listener hears: the ids that changed, and which of them left for good, read in the callback."""

    def heard(changed):
        changes.append((changed, frozenset(agent for agent in changed
                                           if reader.agent(agent) is None and reader.left_for_good(agent))))

    reader.listen(heard)


@pytest.mark.asyncio
async def test_an_exit_seen_only_as_the_records_deletion_is_known_from_the_closed_session():
    async with _reading() as (reader, records, host):
        changes = []
        _heard(reader, changes)
        last = reader.agent("lead")
        # A reader that was disconnected while the agent exited sees only the deletion.
        await records.feed("agent", KEY, None)
        assert reader.agent("lead") is None and reader.departure_reason("lead") == "record_removed"
        assert not reader.left_for_good("lead")
        await records.feed("agent_session", *_closed_lead_session())

        await _until(lambda: reader.left_for_good("lead"))
        assert reader.departure_reason("lead") == "session_closed" and reader.departed_agent("lead") == last
        await asyncio.sleep(0.1)  # several more checks
        assert [exited for _, exited in changes if "lead" in exited] == [frozenset({"lead"})], "heard once"


@pytest.mark.asyncio
async def test_an_exit_whose_placement_deletion_is_read_first_is_still_recognised():
    async with _reading() as (reader, records, host):
        changes = []
        _heard(reader, changes)
        await records.feed("placement", KEY, None)
        assert reader.departure_reason("lead") == "placement_removed" and not reader.left_for_good("lead")
        # The exiting record arrives after the placement went, so it is not accepted; the session's end is.
        await records.feed("agent", KEY, _signed(_agent(sequence=101, state="exiting")))
        await records.feed("agent_session", *_closed_lead_session())

        await _until(lambda: reader.left_for_good("lead"))
        await asyncio.sleep(0.1)
        assert [exited for _, exited in changes if "lead" in exited] == [frozenset({"lead"})]


@pytest.mark.asyncio
async def test_an_exit_while_the_record_watch_reopens_is_recognised():
    async with _reading() as (reader, records, host):
        changes = []
        _heard(reader, changes)
        records.values["agent"].pop(KEY)  # it exited and its record went while the watch was down
        await records.queues["agent"].put(ConnectionError("watch connection lost"))
        await records.queues["agent"].join()
        await _until(lambda: reader.agent("lead") is None)
        await records.feed("agent_session", *_closed_lead_session())

        await _until(lambda: reader.left_for_good("lead"))
        assert reader.agent("lead") is None


@pytest.mark.asyncio
async def test_an_exiting_record_after_the_agent_went_quiet_is_heard_once():
    async with _reading() as (reader, records, host):
        changes = []
        _heard(reader, changes)
        # Its host stops reporting; the agent drops out but has not left for good.
        later = NOW + timedelta(seconds=31)
        host.clock = lambda: later
        await _until(lambda: reader.agent("lead") is None)
        assert reader.departure_reason("lead") == "host_unavailable" and not reader.left_for_good("lead")
        # Its host is back and writes that the agent is exiting, still placed there.
        await records.feed("agent", KEY, _signed(_agent(sequence=101, state="exiting")))

        await _until(lambda: reader.left_for_good("lead"))
        assert reader.departure_reason("lead") == "exiting"
        await asyncio.sleep(0.1)
        assert [exited for _, exited in changes if "lead" in exited] == [frozenset({"lead"})]


@pytest.mark.asyncio
async def test_an_agent_whose_host_only_stopped_reporting_has_not_left_for_good():
    async with _reading() as (reader, records, host):
        changes = []
        _heard(reader, changes)
        later = NOW + timedelta(seconds=31)
        host.clock = lambda: later
        await _until(lambda: reader.agent("lead") is None)
        await asyncio.sleep(0.1)

        assert reader.departure_reason("lead") == "host_unavailable" and not reader.left_for_good("lead")
        assert not any(exited for _, exited in changes)


@pytest.mark.asyncio
async def test_a_forged_exit_from_another_host_is_not_an_exit():
    """SEC-1: an exiting record signed by a host other than the agent's placement host changes nothing."""

    async with _reading() as (reader, records, host):
        changes = []
        _heard(reader, changes)
        forged = _agent(sequence=101, state="exiting")
        forged["writer"].update(runtime="worker-host-1", key_id="test-worker-key-1")
        await records.feed("agent", KEY, _signed(forged))
        await asyncio.sleep(0.1)

        assert reader.lead() is not None and not reader.left_for_good("lead") and changes == []


def _worker_host_record(host):
    host_record = _example("host/lead-host.json")
    host_record["key"] = "worker-host-1"
    host_record["writer"].update(runtime="worker-host-1", key_id="test-worker-key-1")
    certificate = host.live.trust.store.host_certificate(INSTALL, "worker-host-1", 3)
    held = host.live.trust.store.held_record("certificate", f"host.{INSTALL}.worker-host-1.3")
    host_record["body"].update(runtime="worker-host-1", certificate={"certificate_id": certificate.certificate_id,
                               "membership_epoch": 3, "sha256": held[1]}, session_limit=certificate.session_limit)
    return _signed(host_record)


@pytest.mark.asyncio
async def test_a_record_naming_the_leads_session_from_another_host_does_not_make_an_agent_lead(caplog):
    """SEC-1: a worker host's record that names the lead's session is refused; the lead stays the lead."""

    async with _reading() as (reader, records, host):
        await records.feed("host", "worker-host-1", _worker_host_record(host))
        placement = _example("placement/lead.json")
        placement["key"] = f"{TEAM}.impostor"
        placement["body"].update(agent="impostor", agent_type="researcher", host="worker-host-1")
        await records.feed("placement", placement["key"], _signed(placement))
        impostor = _agent(agent="impostor", agent_type="researcher", session="s-lead-1")
        impostor["key"] = placement["key"]
        impostor["writer"].update(runtime="worker-host-1", key_id="test-worker-key-1")
        await records.feed("agent", impostor["key"], _signed(impostor))
        await asyncio.sleep(0.1)

        assert reader.agent("impostor") is None
        assert reader.lead().agent == "lead" and [item.agent for item in reader.live().values() if item.is_lead] == ["lead"]
        assert "code=wrong_issuer" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("meanwhile", ["newer", "deleted"])
async def test_a_retry_waiting_for_trust_never_overwrites_a_newer_record_or_revives_a_deleted_one(meanwhile):
    """EVID-5: a refused record retried while its certificate is fetched is dropped once the key has moved on."""

    from custom_messages.wire.refusals import FrameRefused

    async with _reading() as (reader, records, host):
        store = host.live.trust.store
        stale = _signed(_agent(sequence=101, state="paused"))
        original, fetched, release = store.verify, [], asyncio.Event()

        def verify(raw, **kwargs):
            if raw == stale and not release.is_set():
                raise FrameRefused("unknown_key", at="/writer/key_id")
            return original(raw, **kwargs)

        async def fetch(raw):
            fetched.append(raw)
            if len(fetched) > 1:  # the retry at a later check waits while its certificate is read
                await release.wait()
            return True

        store.verify, host.fetch_trust = verify, fetch
        await records.feed("agent", KEY, stale)
        await _until(lambda: len(fetched) > 1)  # the retry is waiting now
        if meanwhile == "newer":
            await records.feed("agent", KEY, _signed(_agent(sequence=102, state="busy")))
            assert reader.agent("lead").state == "busy"
        else:
            await records.feed("agent", KEY, None)
            assert reader.agent("lead") is None
        release.set()
        await asyncio.sleep(0.1)

        if meanwhile == "newer":
            assert reader.agent("lead").state == "busy" and reader._agents["lead"].sequence == 102
        else:
            assert reader.agent("lead") is None and "lead" not in reader._agents
