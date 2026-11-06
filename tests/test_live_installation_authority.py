"""The installation authority on a real NATS server: topology, joins, renewals and restarts.

installation_authority.service with custom_transport.protocol1.joining; the message
contract's README "Installation traffic" and "Trust over time".
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path

import nats
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import Receiver, reply_audience, sign_frame, verify_frame
from custom_messages.wire.kinds import CONTRACT_SHA256
from custom_messages.wire.models import Frame
from custom_messages.wire.trust import sign_state
from custom_transport.protocol1 import host_keys, joining
from custom_transport.protocol1.bootstrap import BootstrapBundle, read_bundle, write_bundle
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.joining import HostProfile, JoinRefused, join
from custom_transport.protocol1.live import LiveHost, answer_headers, encode, raise_if_refused, utc_now
from custom_transport.protocol1.replay import LiveReplayRecord
from custom_transport.protocol1.trust_state import PersistentTrustStore
from docker import message_hosts
from env_secrets import PROCESS_INSTALLATION_AUTHORITY, environment_for
from installation_authority.expected_hosts import ExpectedHost, write_expected_hosts
from installation_authority.service import (
    AUTHORITY_RUNTIME,
    CERTIFICATES_BUCKET,
    InstallationAuthority,
    RevocationsUnverified,
    certificate_key,
    revocations_key,
)
from runtime_settings.instance_layout import InstanceLayout
from tests.own_nats_server import own_nats_server
from tests.test_live_message_contract import nats_server
from tests.test_message_contract import body_schema_file, validator

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
INSTALLATION, TEAM = "install_one", "team_one"
WORKER = HostProfile("worker-host-1", (TEAM,), ("worker_host",), ("researcher",), 4)


@pytest.fixture
def broker(tmp_path: Path):
    with own_nats_server(nats_server(), tmp_path) as url:
        yield url


def prepare(layout: InstanceLayout) -> host_keys.HostKeys:
    """What the launcher does before it starts the authority: keys, the expected hosts and the bundle."""

    authority, _ = host_keys.ensure_authority_keys(layout, INSTALLATION)
    host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
    worker, _ = host_keys.ensure_host_keys(layout, WORKER.runtime)
    write_expected_hosts(layout.trust_expected_hosts_path, {WORKER.runtime: ExpectedHost(
        WORKER.runtime, worker.key_id, worker.public_key_text, frozenset(WORKER.roles), frozenset(WORKER.teams),
        frozenset(WORKER.agent_types), WORKER.session_limit)})
    write_bundle(layout.trust_bootstrap_path, BootstrapBundle(INSTALLATION, ((authority.key_id, authority.public_key_text),)))
    return worker


async def connect(stack: AsyncExitStack, url: str, runtime: str):
    connection = await nats.connect(url, inbox_prefix=f"_INBOX.{runtime}".encode(), allow_reconnect=False)
    stack.push_async_callback(connection.close)
    return connection


async def start_authority(stack: AsyncExitStack, url: str, layout: InstanceLayout) -> InstallationAuthority:
    authority = InstallationAuthority(layout, host_keys.load_authority_keys(layout),
                                      host_keys.load_host_keys(layout, AUTHORITY_RUNTIME))
    await authority.start(await connect(stack, url, AUTHORITY_RUNTIME), teams=[TEAM])
    return authority


def host_trust(layout: InstanceLayout, runtime: str) -> PersistentTrustStore:
    return PersistentTrustStore.open(read_bundle(layout.trust_bootstrap_path), layout.host_trust_dir(runtime), utc_now())


def forged(document: dict) -> dict:
    """``document`` signed by a key that is not the authority's: what any process with the shared NATS login can write."""

    return sign_state(document, Ed25519PrivateKey.generate())


async def put_record(nc, key: str, document: dict) -> None:
    bucket = await nc.jetstream().key_value(CERTIFICATES_BUCKET)
    await bucket.put(key, json.dumps(document).encode())


async def stored_record(nc, key: str) -> dict:
    bucket = await nc.jetstream().key_value(CERTIFICATES_BUCKET)
    return json.loads((await bucket.get(key)).value)


def revocation_entry(certificate) -> dict:
    return {"certificate_id": certificate.certificate_id, "runtime": certificate.runtime, "key_id": certificate.key_id,
            "key_expires_at": format_timestamp(certificate.expires_at), "membership_epoch": certificate.membership_epoch,
            "revoked_at": format_timestamp(utc_now()), "reason": "compromised"}


@pytest.mark.asyncio
async def test_a_listed_host_joins_and_others_are_refused(broker: str, tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        await start_authority(stack, broker, layout)
        # The bundle now carries the authority's certificate and a current revocations record.
        records = {record["record"] for record in read_bundle(layout.trust_bootstrap_path).records}
        assert records == {"certificate", "revocations"}
        worker_nc = await connect(stack, broker, WORKER.runtime)
        trust = host_trust(layout, WORKER.runtime)
        certificate = await join(worker_nc, worker_keys, trust, WORKER)
        assert (certificate.runtime, certificate.membership_epoch, certificate.roles) == ("worker-host-1", 1,
                                                                                          frozenset({"worker_host"}))
        assert certificate.teams == frozenset({TEAM}) and certificate.session_limit == 4
        # Asking again with the same key hands back the same certificate.
        assert (await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), WORKER)).membership_epoch == 1
        # A host nobody listed, a listed host asking for more, and another key for a listed runtime.
        stranger_keys, _ = host_keys.ensure_host_keys(layout, "stranger-1")
        stranger = HostProfile("stranger-1", (TEAM,), ("worker_host",))
        with pytest.raises(JoinRefused) as refused:
            await join(await connect(stack, broker, "stranger-1"), stranger_keys, host_trust(layout, "stranger-1"), stranger)
        assert refused.value.outcome == "denied" and "launcher" in refused.value.message
        greedy = HostProfile(WORKER.runtime, (TEAM,), ("worker_host", "lead_host"), ("researcher",), 4)
        with pytest.raises(JoinRefused) as refused:
            await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), greedy)
        assert refused.value.outcome == "denied"


@pytest.mark.asyncio
async def test_a_renewal_raises_the_epoch_and_the_authority_keeps_its_records_across_a_restart(broker: str,
                                                                                               tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        first = await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        trust = host_trust(layout, WORKER.runtime)
        certificate = await join(worker_nc, worker_keys, trust, WORKER)
        worker = LiveHost(worker_nc, HostIdentity(worker_keys, certificate), trust)
        reply = await worker.request("host.renew", {
            "renew_id": "renew-1", "certificate_id": certificate.certificate_id, "membership_epoch": 1,
            "profile_sha256": WORKER.digest(), "contract_sha256": CONTRACT_SHA256},
            {"installation": INSTALLATION, "team": "_install", "target": "installation_service", "id": "authority"},
            team="_install")
        renewed = reply.raw["body"]["certificate"]["body"]
        assert renewed["membership_epoch"] == 2 and renewed["replaces"] == certificate.certificate_id
        sequence = first._revocations["sequence"]
        own_epoch = first._own["body"]["membership_epoch"]
        # A restarted authority keeps its own certificate and signs revocations with a higher sequence.
        second = await start_authority(stack, broker, layout)
        assert second._own["body"]["membership_epoch"] == own_epoch
        assert second._revocations["sequence"] > sequence
        js = worker_nc.jetstream()
        hosts = await js.stream_info("KV_UA1_HOSTS")
        assert hosts.config.max_age == 60
        dead_letters = await js.consumer_info(f"UA1_DLQ_{TEAM}", f"dlq~{TEAM}")
        assert dead_letters.config.filter_subject == f"ua.v1.{TEAM}.*.dlq.notice.*"


AUTHORITY_AUDIENCE = {"installation": INSTALLATION, "team": "_install", "target": "installation_service",
                      "id": "authority"}
OTHER_CONTRACT = "0" * 64


def assert_mismatch(kind: str, body: dict) -> None:
    """A ``protocol_mismatch`` reply that meets the contract's reply schema and names the authority's digest."""

    validator(body_schema_file(f"{kind}.reply")).validate(body)
    assert body["outcome"] == "protocol_mismatch"
    assert body["accepted_contract_sha256"] == CONTRACT_SHA256


async def send_join(nc, frame: dict, trust: PersistentTrustStore) -> dict:
    """Send a join frame as it is and return the body of the verified reply."""

    data, headers = encode(frame)
    message = await nc.request(frame["subject"], data, timeout=30, headers=headers)
    raise_if_refused("host.join", frame["subject"], message, logging.getLogger(__name__))
    request = Frame.model_validate(frame)
    named = reply_audience(request)
    reply = verify_frame(message.data, trust=trust.store,
                         receiver=Receiver(named["installation"], named["team"], named["target"], named["id"],
                                           named["runtime"]),
                         arrived_on=message.subject, now=utc_now(), replay=LiveReplayRecord().lookup,
                         headers=answer_headers(message), answering=request)
    return reply.raw["body"]


@pytest.mark.asyncio
async def test_a_host_on_another_contract_learns_the_authoritys_digest_when_it_joins_or_renews(
        broker: str, tmp_path: Path) -> None:
    """README "Versions": a join or renewal naming another contract digest gets ``protocol_mismatch``.

    The reply names the digest the authority runs (``accepted_contract_sha256``), so the
    host learns why instead of getting ``handler_failed``. The host verifies the reply as
    it would any other, and its body is checked against the contract's reply schema. A
    host whose own trust store runs the other contract learns it from ``join``.
    """

    assert OTHER_CONTRACT != CONTRACT_SHA256
    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        trust = host_trust(layout, WORKER.runtime)
        certificate = await join(worker_nc, worker_keys, trust, WORKER)
        worker = LiveHost(worker_nc, HostIdentity(worker_keys, certificate), trust)
        renewal = await worker.request("host.renew", {
            "renew_id": "renew-other-contract", "certificate_id": certificate.certificate_id, "membership_epoch": 1,
            "profile_sha256": WORKER.digest(), "contract_sha256": OTHER_CONTRACT}, AUTHORITY_AUDIENCE, team="_install")
        assert_mismatch("host.renew", renewal.raw["body"])

        frame = joining.sign_join(worker_keys, INSTALLATION, WORKER, join_id="join-other-contract", issued=utc_now(),
                                  contract_sha256=OTHER_CONTRACT)
        assert_mismatch("host.join", await send_join(worker_nc, frame, trust))

        other = host_trust(layout, WORKER.runtime)
        other.store.contract_sha256 = OTHER_CONTRACT
        with pytest.raises(JoinRefused) as refused:
            await join(worker_nc, worker_keys, other, WORKER)
        assert refused.value.outcome == "protocol_mismatch"
        assert refused.value.accepted_contract_sha256 == CONTRACT_SHA256


@pytest.mark.asyncio
async def test_a_join_of_another_contract_learns_protocol_mismatch_whatever_shape_its_body_has(
        broker: str, tmp_path: Path) -> None:
    """README "Versions": the authority reads a join's digest before the rest of its body.

    Another revision may shape the body differently: here its runtime moved, a field this
    one requires is gone and one it does not know is there. Read strictly, the join was
    refused (``wrong_issuer`` at ``/body/runtime``) and the host never learned why.
    """

    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        frame = joining.sign_join(worker_keys, INSTALLATION, WORKER, join_id="join-reshaped", issued=utc_now(),
                                  contract_sha256=OTHER_CONTRACT)
        body = frame["body"]
        body["host"] = {"runtime": body.pop("runtime"), "deployment": body.pop("deployment")}
        body["queue_depths"] = {"inbox": 0}
        reshaped = sign_frame(frame, worker_keys.application_key)
        assert_mismatch("host.join", await send_join(worker_nc, reshaped, host_trust(layout, WORKER.runtime)))


@pytest.mark.asyncio
async def test_a_renewal_that_gives_up_every_agent_is_answered_on_any_contract(broker: str, tmp_path: Path) -> None:
    """README "Versions": the authority still answers a renewal that only gives up agents, so a host on another
    contract can hand its agents over; the certificate it gets grants no agent types and no sessions.

    A renewal that asks for the certificate's profile without any agent (``HostProfile.giving_up``)
    gets that on this contract too. A host on another contract that renews for anything more
    is turned away.
    """

    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    giving_up = WORKER.giving_up().digest()
    async with AsyncExitStack() as stack:
        await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        trust = host_trust(layout, WORKER.runtime)
        certificate = await join(worker_nc, worker_keys, trust, WORKER)
        assert certificate.agent_types == frozenset({"researcher"}) and certificate.session_limit == 4
        worker = LiveHost(worker_nc, HostIdentity(worker_keys, certificate), trust)

        async def renew(renew_id: str, profile: str, contract: str) -> dict:
            current = worker.identity.certificate
            reply = await worker.request("host.renew", {
                "renew_id": renew_id, "certificate_id": current.certificate_id,
                "membership_epoch": current.membership_epoch, "profile_sha256": profile, "contract_sha256": contract},
                AUTHORITY_AUDIENCE, team="_install")
            body = reply.raw["body"]
            if body["outcome"] == "ok":
                trust.add(json.dumps(body["certificate"]).encode(), now=utc_now())
                renewed = trust.store.host_certificate(INSTALLATION, WORKER.runtime,
                                                       body["certificate"]["body"]["membership_epoch"])
                assert renewed is not None and renewed.certificate_id == body["certificate"]["body"]["certificate_id"]
                worker.identity = worker.identity.with_certificate(renewed)
            return body

        other = await renew("renew-gives-up-1", giving_up, OTHER_CONTRACT)
        assert other["outcome"] == "ok", other
        granted = other["certificate"]["body"]
        assert (granted["agent_types"], granted["session_limit"], granted["membership_epoch"]) == ([], 0, 2)
        assert granted["replaces"] == certificate.certificate_id and granted["profile_sha256"] == giving_up
        assert (granted["teams"], granted["roles"]) == ([TEAM], ["worker_host"])
        # Asking for more than giving up, on another contract, is turned away.
        assert_mismatch("host.renew", await renew("renew-other-contract", WORKER.digest(), OTHER_CONTRACT))
        # On this contract the same profile renews as usual.
        same = await renew("renew-gives-up-2", giving_up, CONTRACT_SHA256)
        assert same["outcome"] == "ok" and same["certificate"]["body"]["agent_types"] == []
        assert same["certificate"]["body"]["membership_epoch"] == 3


@pytest.mark.asyncio
async def test_the_authority_signs_again_only_the_revocations_record_it_signed(broker: str, tmp_path: Path) -> None:
    """A revocations record another process wrote to the bucket can neither lift a revocation nor revoke every host."""

    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        authority = await start_authority(stack, broker, layout)
        nc = await connect(stack, broker, "writer")
        key = revocations_key(INSTALLATION)
        certificate = await join(await connect(stack, broker, WORKER.runtime), worker_keys, host_trust(layout, WORKER.runtime),
                                 WORKER)
        # A revocation the authority signed: it lists the worker's certificate.
        revoked = authority.revocations_record(sequence=authority._revocations["sequence"] + 1, revocation_epoch=2,
                                               entries=[revocation_entry(certificate)], issued=utc_now())
        await put_record(nc, key, revoked)
        signed = await authority._sign_revocations(utc_now())
        assert signed["body"]["entries"] == revoked["body"]["entries"] and signed["body"]["revocation_epoch"] == 2

        # Another process writes a record that lifts it, with a higher sequence; then one that revokes the authority.
        lifted = forged({**signed, "sequence": signed["sequence"] + 10, "body": {**signed["body"], "entries": []}})
        await put_record(nc, key, lifted)
        resigned = await authority._sign_revocations(utc_now())
        assert resigned["body"]["entries"] == revoked["body"]["entries"]
        assert resigned["sequence"] > signed["sequence"]
        # The forged record is replaced in the bucket by the authority's own.
        assert await stored_record(nc, key) == resigned

        everyone = forged({**resigned, "sequence": resigned["sequence"] + 10,
                           "body": {**resigned["body"], "entries": [revocation_entry(authority.live.identity.certificate)]}})
        await put_record(nc, key, everyone)
        # A restarted authority keeps the record it verified (the bundle's), not the bucket's.
        restarted = await start_authority(stack, broker, layout)
        assert restarted._revocations["body"]["entries"] == revoked["body"]["entries"]
        assert await stored_record(nc, key) == restarted._revocations
        held = [record for record in read_bundle(layout.trust_bootstrap_path).records if record["record"] == "revocations"]
        assert held == [restarted._revocations]


@pytest.mark.asyncio
async def test_an_authority_that_verified_no_revocations_record_does_not_start(broker: str, tmp_path: Path) -> None:
    """With a record in the bucket that does not verify and none of its own, it fails closed rather than sign an empty list."""

    layout = InstanceLayout.at(tmp_path / "instance")
    prepare(layout)
    async with AsyncExitStack() as stack:
        nc = await connect(stack, broker, "writer")
        await nc.jetstream().create_key_value(bucket=CERTIFICATES_BUCKET, history=1)
        authority = InstallationAuthority(layout, host_keys.load_authority_keys(layout),
                                          host_keys.load_host_keys(layout, AUTHORITY_RUNTIME))
        now = utc_now()
        record = forged(authority.revocations_record(sequence=int(now.timestamp() * 1000), revocation_epoch=1, entries=[],
                                                     issued=now))
        await put_record(nc, revocations_key(INSTALLATION), record)
        with pytest.raises(RevocationsUnverified) as refused:
            await authority.start(await connect(stack, broker, AUTHORITY_RUNTIME), teams=[TEAM])
        assert "bad_signature" in str(refused.value) and "trust folder" in str(refused.value)
        # Nothing was signed: the bucket still holds the other process's record and the bundle no revocations record.
        assert await stored_record(nc, revocations_key(INSTALLATION)) == record
        assert not read_bundle(layout.trust_bootstrap_path).records


@pytest.mark.asyncio
async def test_a_join_never_gets_back_a_certificate_the_authority_did_not_sign(broker: str, tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        authority = await start_authority(stack, broker, layout)
        nc = await connect(stack, broker, "writer")
        worker_nc = await connect(stack, broker, WORKER.runtime)
        first = await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), WORKER)
        key = certificate_key(INSTALLATION, WORKER.runtime, first.membership_epoch)
        stored = await stored_record(nc, key)
        # The same grants under another signature: the authority would hand it back if it took it as its own.
        await put_record(nc, key, forged(stored))
        # The host still joins: the authority issues a certificate instead of handing back the other process's, at
        # the next epoch, since hosts may already hold the certificate first named.
        again = await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), WORKER)
        assert again.roles == frozenset({"worker_host"}) and again.membership_epoch == first.membership_epoch + 1
        own_key = certificate_key(INSTALLATION, AUTHORITY_RUNTIME, authority._own["body"]["membership_epoch"])
        forged_own = forged(await stored_record(nc, own_key))
        await put_record(nc, own_key, forged_own)
        # A restarted authority does not take a forged certificate of its own either.
        restarted = await start_authority(stack, broker, layout)
        assert restarted._own != forged_own
        assert restarted._own["body"]["membership_epoch"] == authority._own["body"]["membership_epoch"] + 1
        assert await stored_record(nc, certificate_key(INSTALLATION, AUTHORITY_RUNTIME,
                                                       restarted._own["body"]["membership_epoch"])) == restarted._own


@pytest.mark.asyncio
async def test_a_join_asking_for_other_grants_gets_a_new_certificate(broker: str, tmp_path: Path) -> None:
    """A host listed for another agent type asks for it and gets a certificate naming it, not its old one back."""

    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        first = await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), WORKER)
        wider = HostProfile(WORKER.runtime, WORKER.teams, WORKER.roles, ("analyst", "researcher"), WORKER.session_limit)
        write_expected_hosts(layout.trust_expected_hosts_path, {WORKER.runtime: ExpectedHost(
            WORKER.runtime, worker_keys.key_id, worker_keys.public_key_text, frozenset(wider.roles),
            frozenset(wider.teams), frozenset(wider.agent_types), wider.session_limit)})
        second = await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), wider)
        assert second.membership_epoch == first.membership_epoch + 1 and second.replaces == first.certificate_id
        assert second.agent_types == frozenset({"analyst", "researcher"})
        # Asking for the same again hands that certificate back; asking for less gets a narrower one.
        assert (await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), wider)).membership_epoch == 2
        narrower = await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), WORKER)
        assert narrower.membership_epoch == 3 and narrower.agent_types == frozenset({"researcher"})


@pytest.mark.asyncio
async def test_the_launcher_starts_the_real_authority_and_a_listed_host_joins(broker: str, tmp_path: Path,
                                                                              monkeypatch) -> None:
    """docker.message_hosts with ``assistant_cli run-installation-authority`` as a real process."""

    config = GatewayInstanceConfig.create(instance_name="authority-test", instance_root=str(tmp_path / "instance"),
                                          team_id=TEAM, timezone="UTC")
    config.save()
    layout = config.layout
    worker_keys, _ = host_keys.ensure_host_keys(layout, WORKER.runtime)
    expected = {WORKER.runtime: ExpectedHost(WORKER.runtime, worker_keys.key_id, worker_keys.public_key_text,
                                             frozenset(WORKER.roles), frozenset(WORKER.teams), frozenset(WORKER.agent_types),
                                             WORKER.session_limit)}
    installation = message_hosts.prepare_message_host_trust(layout, expected)
    env = {key: value for key, value in os.environ.items() if not key.startswith("NATS_")}
    env["NATS_URL"] = broker
    repo = Path(__file__).resolve().parents[1]
    process = message_hosts.start_installation_authority(
        layout, message_hosts.build_installation_authority_command(config.config_path, repo_root=repo),
        environment_for(PROCESS_INSTALLATION_AUTHORITY, env), cwd=repo, timeout_seconds=60)
    try:
        async with AsyncExitStack() as stack:
            trust = host_trust(layout, WORKER.runtime)
            assert trust.store.installation == installation
            certificate = await join(await connect(stack, broker, WORKER.runtime), worker_keys, trust, WORKER)
            assert certificate.installation == installation and certificate.membership_epoch == 1
    finally:
        process.terminate()
        assert process.wait(timeout=10) == 0


@pytest.mark.asyncio
async def test_the_lead_host_asks_for_the_catalogs_agent_types_and_a_new_type_gets_a_new_certificate(
    broker: str, tmp_path: Path,
) -> None:
    """The team lead's host is listed with agent_types_from_catalog: the authority reads the catalog at each join."""

    layout = InstanceLayout.at(tmp_path / "instance")
    prepare(layout)
    lead_keys, _ = host_keys.ensure_host_keys(layout, "lead")
    write_expected_hosts(layout.trust_expected_hosts_path, {"lead": ExpectedHost(
        "lead", lead_keys.key_id, lead_keys.public_key_text, frozenset({"lead_host", "worker_host"}), frozenset({TEAM}),
        frozenset(), 8, agent_types_from_catalog=True)})
    catalog = {"TeamArchitect", "Researcher"}
    async with AsyncExitStack() as stack:
        authority = InstallationAuthority(layout, host_keys.load_authority_keys(layout),
                                          host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                                          catalog_agent_types=lambda: frozenset(catalog))
        await authority.start(await connect(stack, broker, AUTHORITY_RUNTIME), teams=[TEAM])
        lead_nc = await connect(stack, broker, "lead")

        def lead(*types: str) -> HostProfile:
            return HostProfile("lead", (TEAM,), ("lead_host", "worker_host"), tuple(types), 8)

        first = await join(lead_nc, lead_keys, host_trust(layout, "lead"), lead("Researcher", "TeamArchitect"))
        assert first.agent_types == frozenset({"Researcher", "TeamArchitect"})
        with pytest.raises(JoinRefused):
            await join(lead_nc, lead_keys, host_trust(layout, "lead"), lead("Researcher", "Invented"))
        # A specialist created while the team runs joins the catalog; asking for it gives a new certificate.
        catalog.add("Analyst")
        second = await join(lead_nc, lead_keys, host_trust(layout, "lead"), lead("Analyst", "Researcher", "TeamArchitect"))
        assert second.membership_epoch == first.membership_epoch + 1
        assert "Analyst" in second.agent_types
        # Asking for exactly the same again hands that certificate back.
        assert (await join(lead_nc, lead_keys, host_trust(layout, "lead"),
                           lead("Analyst", "Researcher", "TeamArchitect"))).membership_epoch == second.membership_epoch


@pytest.mark.asyncio
async def test_a_type_removed_from_the_catalog_does_not_stop_the_lead_host_joining_again_for_a_new_one(
    broker: str, tmp_path: Path,
) -> None:
    """A specialist type removed from the catalog leaves its running worker running, so the lead's certificate keeps it.

    The lead's host asks again for every type its certificate grants plus the new one, and the authority grants a
    join whole or not at all. Before, a type deleted from the catalog made every later join for a new type refused,
    so a specialist created afterwards never got a session until a restart. A join with the key of the host's
    current certificate keeps that certificate's types, as a renewal does; a new type must still be listed or in
    the catalog.
    """

    from custom_transport.protocol1.host_runtime import MessageHost

    layout = InstanceLayout.at(tmp_path / "instance")
    prepare(layout)
    lead_keys, _ = host_keys.ensure_host_keys(layout, "lead")
    write_expected_hosts(layout.trust_expected_hosts_path, {"lead": ExpectedHost(
        "lead", lead_keys.key_id, lead_keys.public_key_text, frozenset({"lead_host", "worker_host"}), frozenset({TEAM}),
        frozenset(), 8, agent_types_from_catalog=True)})
    catalog = {"TeamArchitect", "Researcher", "Writer"}
    async with AsyncExitStack() as stack:
        authority = InstallationAuthority(layout, host_keys.load_authority_keys(layout),
                                          host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                                          catalog_agent_types=lambda: frozenset(catalog))
        await authority.start(await connect(stack, broker, AUTHORITY_RUNTIME), teams=[TEAM])
        profile = HostProfile("lead", (TEAM,), ("lead_host", "worker_host"), ("Researcher", "TeamArchitect", "Writer"), 8)
        lead = MessageHost(layout, profile, await connect(stack, broker, "lead"))
        await lead.start()
        assert lead.identity.certificate.agent_types == frozenset({"Researcher", "TeamArchitect", "Writer"})

        # Writer's specialist is removed (its running worker keeps running) and Analyst is created.
        catalog.discard("Writer")
        catalog.add("Analyst")
        rejoined = await lead.rejoin(["Analyst"])
        assert rejoined.refusal is None and rejoined.granted == frozenset({"Analyst"})
        assert rejoined.certificate.membership_epoch == 2
        assert rejoined.certificate.agent_types == frozenset({"Analyst", "Researcher", "TeamArchitect", "Writer"})
        assert lead.identity.certificate.membership_epoch == 2

        # A type nobody listed, the catalog lacks and the certificate does not grant is still refused.
        refused = await lead.rejoin(["Invented"])
        assert refused.refused == frozenset({"Invented"}) and refused.refusal is not None
        assert refused.certificate.membership_epoch == 2
        # A join with another key never gets the certificate's types: the launcher listed this key only.
        other_key = Ed25519PrivateKey.generate()
        other_keys = host_keys.HostKeys(runtime="lead", key_id=host_keys.key_id_for("lead", other_key.public_key()),
                                        application_key=other_key, nats_seed=lead_keys.nats_seed)
        with pytest.raises(JoinRefused):
            await join(await connect(stack, broker, "lead"), other_keys, host_trust(layout, "lead"),
                       HostProfile("lead", (TEAM,), ("lead_host", "worker_host"), ("Writer",), 8))
