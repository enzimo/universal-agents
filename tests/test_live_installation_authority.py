"""The installation authority on a real NATS server: topology, joins, renewals and restarts.

installation_authority.service with custom_transport.protocol1.joining; the message
contract's README "Installation traffic" and "Trust over time".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from pathlib import Path

import nats
import pytest
from nats.js.errors import APIError, KeyNotFoundError, NotFoundError
from nats.js.api import StorageType, StreamConfig
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.buckets import TEAM_PLACEHOLDER, bucket_name, bucket_stream
from custom_messages.wire.frame import Receiver, reply_audience, sign_frame, verify_frame
from custom_messages.wire.kinds import CONTRACT_SHA256
from custom_messages.wire.models import Frame
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import kept_for, sign_state
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1 import host_keys, joining
from custom_transport.protocol1.contract_files import transport
from custom_transport.protocol1.bootstrap import BootstrapBundle, read_bundle, write_bundle
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.joining import HostProfile, JoinRefused, join
from custom_transport.protocol1.live import LiveHost, RequestRefused, answer_headers, encode, raise_if_refused, utc_now
from custom_transport.protocol1.replay import LiveReplayRecord
from custom_transport.protocol1.trust_state import PersistentTrustStore
from docker import message_hosts
from env_secrets import PROCESS_INSTALLATION_AUTHORITY, environment_for
from installation_authority.expected_hosts import ExpectedHost, write_expected_hosts
from installation_authority.ledger import ISSUED_FILENAME
from installation_authority.service import (
    AUTHORITY_RUNTIME,
    CERTIFICATE_LIFETIME,
    CERTIFICATES_BUCKET,
    MAX_EPOCH,
    REVOCATIONS_LIFETIME,
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


def prepare(layout: InstanceLayout, *others: HostProfile) -> host_keys.HostKeys:
    """What the launcher does before it starts the authority: keys, the expected hosts and the bundle.

    It lists ``WORKER`` and ``others`` and returns ``WORKER``'s keys.
    """

    authority, _ = host_keys.ensure_authority_keys(layout, INSTALLATION)
    host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
    list_hosts(layout, WORKER, *others)
    write_bundle(layout.trust_bootstrap_path, BootstrapBundle(INSTALLATION, ((authority.key_id, authority.public_key_text),)))
    return host_keys.load_host_keys(layout, WORKER.runtime)


def list_hosts(layout: InstanceLayout, *profiles: HostProfile) -> None:
    """The launcher's list: each profile's runtime with its key, allowed what the profile asks for."""

    hosts = {}
    for profile in profiles:
        keys, _ = host_keys.ensure_host_keys(layout, profile.runtime)
        hosts[profile.runtime] = ExpectedHost(profile.runtime, keys.key_id, keys.public_key_text, frozenset(profile.roles),
                                              frozenset(profile.teams), frozenset(profile.agent_types),
                                              profile.session_limit)
    write_expected_hosts(layout.trust_expected_hosts_path, hosts)


class Clock:
    """A clock the test moves: the authority's and the hosts' wall clock."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def connect(stack: AsyncExitStack, url: str, runtime: str):
    connection = await nats.connect(url, inbox_prefix=f"_INBOX.{runtime}".encode(), allow_reconnect=False)
    stack.push_async_callback(connection.close)
    return connection


async def start_authority(stack: AsyncExitStack, url: str, layout: InstanceLayout, **options) -> InstallationAuthority:
    authority = InstallationAuthority(layout, host_keys.load_authority_keys(layout),
                                      host_keys.load_host_keys(layout, AUTHORITY_RUNTIME), **options)
    await authority.start(await connect(stack, url, AUTHORITY_RUNTIME), teams=[TEAM])
    return authority


async def stop_authority(authority: InstallationAuthority) -> None:
    """Stop answering, as a stopped process does: a restarted authority is then the only one serving."""

    assert authority.live is not None
    await authority.live.nc.close()


def host_trust(layout: InstanceLayout, runtime: str, now: datetime | None = None) -> PersistentTrustStore:
    return PersistentTrustStore.open(read_bundle(layout.trust_bootstrap_path), layout.host_trust_dir(runtime),
                                     now or utc_now())


async def renew(worker: LiveHost, renew_id: str, *, profile: str = "", contract: str = CONTRACT_SHA256,
                keep: bool = True, **body: object) -> dict:
    """Renew ``worker``'s certificate and return the reply's body; ``keep`` takes the new certificate, as a host does.

    ``keep=False`` is a reply the host never got: it still signs with its old certificate.
    """

    current = worker.identity.certificate
    reply = await worker.request("host.renew", {
        "renew_id": renew_id, "certificate_id": current.certificate_id, "membership_epoch": current.membership_epoch,
        "profile_sha256": profile or WORKER.digest(), "contract_sha256": contract, **body}, AUTHORITY_AUDIENCE,
        team="_install")
    answer = reply.raw["body"]
    validator(body_schema_file("host.renew.reply")).validate(answer)
    if keep and answer["outcome"] == "ok":
        now = worker.clock()
        worker.trust.add(json.dumps(answer["certificate"]).encode(), now=now)
        renewed = worker.trust.store.host_certificate(INSTALLATION, current.runtime,
                                                      answer["certificate"]["body"]["membership_epoch"])
        assert renewed is not None
        worker.identity = worker.identity.with_certificate(renewed)
    return answer


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
async def test_authority_creates_per_team_buckets_with_the_contracts_write_restrictions(broker: str, tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    prepare(layout)
    async with AsyncExitStack() as stack:
        authority = await start_authority(stack, broker, layout)
        teams = (TEAM, "team_other")
        await authority.ensure_topology(iter(teams))
        js = (await connect(stack, broker, "reader")).jetstream()
        spec = transport()
        for template in spec["key_value"]:
            for team in teams if TEAM_PLACEHOLDER in template else (None,):
                settings = bucket_stream(spec, template, team)
                config = (await js.stream_info(settings["name"])).config
                for field, expected in settings.items():
                    assert getattr(config, field) == expected, (template, field)
                await js.key_value(bucket_name(template, team))
        bucket = await js.key_value("UA1_AGENTS")
        await bucket.put(f"{TEAM}.agent1", b"kept")
        with pytest.raises(APIError):
            await js.publish(f"$KV.UA1_AGENTS.{TEAM}.agent2", b"erase all", headers={"Nats-Rollup": "all"})
        await authority.ensure_topology(iter(teams))
        assert (await bucket.get(f"{TEAM}.agent1")).value == b"kept"
        assert (await js.stream_info("KV_UA1_CERTS")).config.num_replicas == 1
        # Each team's dead-letter stream comes with phase 3's dead letters (plan decision 71), not now.
        with pytest.raises(NotFoundError):
            await js.stream_info(f"UA1_DLQ_{TEAM}")


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
        renewed = (await renew(worker, "renew-1"))["certificate"]["body"]
        assert renewed["membership_epoch"] == 2 and renewed["replaces"] == certificate.certificate_id
        assert renewed["profile_sha256"] == WORKER.digest()
        sequence = first._revocations["sequence"]
        own_epoch = first._own["body"]["membership_epoch"]
        await stop_authority(first)
        # A restarted authority keeps its own certificate and signs revocations with a higher sequence.
        second = await start_authority(stack, broker, layout)
        assert second._own["body"]["membership_epoch"] == own_epoch
        assert second._revocations["sequence"] > sequence
        # It still recognises the host's certificate, which it read back from the bucket: the host renews again.
        again = (await renew(worker, "renew-2"))["certificate"]["body"]
        assert again["membership_epoch"] == 3 and again["replaces"] == renewed["certificate_id"]
        hosts = await worker_nc.jetstream().stream_info("KV_UA1_HOSTS")
        assert hosts.config.max_age == transport()["key_value"]["UA1_HOSTS"]["per_key_ttl_seconds"]


@pytest.mark.asyncio
async def test_a_renewal_retried_after_a_lost_reply_gets_the_same_certificate(broker: str, tmp_path: Path) -> None:
    """README "Renewing", "Stored outcomes": the retry keeps its renewal id and gets the stored answer, also after a
    restart; a new renewal id is a new renewal, which replaces the certificate that signs it."""

    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        first = await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        trust = host_trust(layout, WORKER.runtime)
        certificate = await join(worker_nc, worker_keys, trust, WORKER)
        worker = LiveHost(worker_nc, HostIdentity(worker_keys, certificate), trust)
        lost = (await renew(worker, "renew-lost", keep=False))["certificate"]
        assert lost["body"]["membership_epoch"] == 2 and lost["body"]["replaces"] == certificate.certificate_id
        # Still signed by certificate 1, within its overlap: the same certificate, not another one.
        assert (await renew(worker, "renew-lost", keep=False))["certificate"] == lost
        # A new renewal replaces the certificate that signs it, never the newest stored one.
        other = (await renew(worker, "renew-other", keep=False))["certificate"]["body"]
        assert other["membership_epoch"] == 3 and other["replaces"] == certificate.certificate_id
        await stop_authority(first)
        await start_authority(stack, broker, layout)
        assert (await renew(worker, "renew-lost"))["certificate"] == lost
        assert worker.identity.certificate.membership_epoch == 2


@pytest.mark.asyncio
async def test_a_renewed_certificate_states_the_profile_it_grants(broker: str, tmp_path: Path) -> None:
    """README "Identifiers": a renewal that asks for a wider profile keeps its grants, and its certificate says so."""

    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    wider = HostProfile(WORKER.runtime, WORKER.teams, WORKER.roles, ("analyst", "researcher"), 8)
    async with AsyncExitStack() as stack:
        await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        trust = host_trust(layout, WORKER.runtime)
        worker = LiveHost(worker_nc, HostIdentity(worker_keys, await join(worker_nc, worker_keys, trust, WORKER)), trust)
        body = (await renew(worker, "renew-wider", profile=wider.digest()))["certificate"]["body"]
        assert body["profile_sha256"] == WORKER.digest() and body["agent_types"] == ["researcher"]


@pytest.mark.asyncio
async def test_a_revoked_host_cannot_renew(broker: str, tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        authority = await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        trust = host_trust(layout, WORKER.runtime)
        certificate = await join(worker_nc, worker_keys, trust, WORKER)
        worker = LiveHost(worker_nc, HostIdentity(worker_keys, certificate), trust)
        revoked = authority.revocations_record(sequence=authority._revocations["sequence"] + 1, revocation_epoch=2,
                                               entries=[revocation_entry(certificate)], issued=utc_now())
        await put_record(await connect(stack, broker, "writer"), revocations_key(INSTALLATION), revoked)
        await authority._sign_revocations(utc_now())
        with pytest.raises(RequestRefused) as refused:
            await renew(worker, "renew-revoked")
        assert refused.value.code == "revoked"


@pytest.mark.asyncio
async def test_a_host_the_list_no_longer_covers_cannot_renew_its_grants(broker: str, tmp_path: Path) -> None:
    """The launcher's list decides at each renewal too: a delisted or narrowed host joins again instead."""

    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        trust = host_trust(layout, WORKER.runtime)
        worker = LiveHost(worker_nc, HostIdentity(worker_keys, await join(worker_nc, worker_keys, trust, WORKER)), trust)
        write_expected_hosts(layout.trust_expected_hosts_path, {})
        assert (await renew(worker, "renew-delisted"))["outcome"] == "not_allowed"
        list_hosts(layout, HostProfile(WORKER.runtime, WORKER.teams, WORKER.roles, (), WORKER.session_limit))
        assert (await renew(worker, "renew-narrowed"))["outcome"] == "not_allowed"
        list_hosts(layout, WORKER)
        assert (await renew(worker, "renew-listed"))["outcome"] == "ok"


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
        # And it narrows a certificate that still holds agents.
        rejoined = await join(worker_nc, worker_keys, trust, WORKER)
        assert rejoined.agent_types == frozenset({"researcher"}) and rejoined.membership_epoch == 4
        worker.identity = worker.identity.with_certificate(rejoined)
        narrowed = await renew("renew-gives-up-3", giving_up, CONTRACT_SHA256)
        assert (narrowed["certificate"]["body"]["agent_types"], narrowed["certificate"]["body"]["session_limit"]) == ([], 0)
        # A give-up of another contract that this revision cannot read (it names another certificate) is turned away.
        current = worker.identity.certificate
        unreadable = await worker.request("host.renew", {
            "renew_id": "renew-gives-up-unreadable", "certificate_id": certificate.certificate_id,
            "membership_epoch": current.membership_epoch, "profile_sha256": giving_up, "contract_sha256": OTHER_CONTRACT},
            AUTHORITY_AUDIENCE, team="_install")
        assert_mismatch("host.renew", unreadable.raw["body"])


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
        settings = bucket_stream(transport(), CERTIFICATES_BUCKET)
        settings["storage"] = StorageType.FILE
        await nc.jetstream().add_stream(config=StreamConfig(**settings))
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
        await stop_authority(authority)
        # A restarted authority does not take a forged certificate of its own either: it keeps the one it signed,
        # from the bundle, and writes it over the forged one.
        restarted = await start_authority(stack, broker, layout)
        assert restarted._own == authority._own
        assert await stored_record(nc, own_key) == authority._own


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
async def test_each_join_the_launchers_list_does_not_cover_is_denied_with_its_reason(broker: str, tmp_path: Path) -> None:
    """Another key for a listed runtime, another team, agent type, more sessions, another connection class."""

    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    other_keys, _ = host_keys.ensure_host_keys(InstanceLayout.at(tmp_path / "elsewhere"), WORKER.runtime)
    async with AsyncExitStack() as stack:
        await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        trust = host_trust(layout, WORKER.runtime)
        cases = [
            (other_keys, WORKER, "key_mismatch"),
            (worker_keys, HostProfile(WORKER.runtime, (TEAM, "team_two"), WORKER.roles, WORKER.agent_types, 4), "team_not_allowed"),
            (worker_keys, HostProfile(WORKER.runtime, WORKER.teams, WORKER.roles, ("analyst",), 4), "role_not_allowed"),
            (worker_keys, HostProfile(WORKER.runtime, WORKER.teams, WORKER.roles, WORKER.agent_types, 5), "over_limit"),
            (worker_keys, HostProfile(WORKER.runtime, WORKER.teams, WORKER.roles, WORKER.agent_types, 4, "intermittent"),
             "not_approved"),
            (worker_keys, HostProfile(WORKER.runtime, WORKER.teams, WORKER.roles, WORKER.agent_types, 4,
                                      deployment="third_party"), "not_approved"),
        ]
        for number, (keys, profile, reason) in enumerate(cases):
            frame = joining.sign_join(keys, INSTALLATION, profile, join_id=f"join-case-{number}", issued=utc_now())
            body = await send_join(worker_nc, frame, trust)
            validator(body_schema_file("host.join.reply")).validate(body)
            assert (body["outcome"], body.get("reason")) == ("denied", reason), profile
        # Nothing was issued for any of them; the listed host still joins as listed.
        assert (await join(worker_nc, worker_keys, trust, WORKER)).membership_epoch == 1


@pytest.mark.asyncio
async def test_the_authority_keeps_its_records_current_by_the_wall_clock(broker: str, tmp_path: Path) -> None:
    """Plan 13B.3: the revocations record is signed again when six hours old, also after the machine slept past its
    end, and the authority renews its own certificate at two thirds of its life and signs with the new one. A host
    that read the bundle before that renewal still joins: it reads the bundle again for the new certificate."""

    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    started = utc_now()
    clock = Clock(started)
    async with AsyncExitStack() as stack:
        authority = await start_authority(stack, broker, layout, clock=clock)
        own, revocations = authority._own, authority._revocations
        lifetime = parse_timestamp(own["body"]["expires_at"]) - parse_timestamp(own["body"]["issued_at"])
        assert lifetime == CERTIFICATE_LIFETIME
        assert parse_timestamp(revocations["body"]["expires_at"]) - parse_timestamp(revocations["issued_at"]) \
            == REVOCATIONS_LIFETIME
        early_trust = host_trust(layout, WORKER.runtime, clock())
        # A host on another machine gets its own copy of the bundle when it is set up.
        early_bundle = layout.trust_bootstrap_path.read_bytes()
        clock.now = started + timedelta(hours=5)
        await authority.upkeep(clock())
        assert authority._revocations == revocations
        # The machine slept through the record's end: the first check after waking signs it again.
        clock.now = started + timedelta(hours=30)
        await authority.upkeep(clock())
        assert authority._revocations["sequence"] > revocations["sequence"]
        assert parse_timestamp(authority._revocations["issued_at"]) - parse_timestamp(revocations["issued_at"]) \
            >= timedelta(hours=29)
        assert authority._own == own
        clock.now = started + timedelta(days=20, minutes=1)
        await authority.upkeep(clock())
        renewed = authority._own["body"]
        assert renewed["membership_epoch"] == own["body"]["membership_epoch"] + 1
        assert renewed["replaces"] == own["body"]["certificate_id"]
        assert authority.live is not None and authority.live.identity.certificate.membership_epoch == renewed["membership_epoch"]
        assert authority._own in read_bundle(layout.trust_bootstrap_path).records
        reader = await connect(stack, broker, "reader")
        assert await stored_record(reader, authority._own["key"]) == authority._own

        worker_nc = await connect(stack, broker, WORKER.runtime)
        with pytest.raises(FrameRefused) as refused:
            await join(worker_nc, worker_keys, early_trust, WORKER, clock=clock)
        assert refused.value.code == "unknown_key"
        # On that other machine the copy never learns of the renewal, and is never read again: the host reads the
        # certificate the reply names from UA1_CERTS (MessageHost.fetch_trust) and checks it against the copy's
        # authority keys.
        elsewhere = InstanceLayout.at(tmp_path / "another-machine")
        shutil.copytree(layout.host_trust_dir(WORKER.runtime), elsewhere.host_trust_dir(WORKER.runtime))
        elsewhere.trust_bootstrap_path.write_bytes(early_bundle)
        remote = MessageHost(elsewhere, WORKER, worker_nc, clock=clock)
        await remote.start(report=False)
        assert remote.identity.certificate.membership_epoch == 1
        assert elsewhere.trust_bootstrap_path.read_bytes() == early_bundle


@pytest.mark.asyncio
async def test_a_host_gets_its_certificate_back_until_its_last_third(broker: str, tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    started = utc_now()
    clock = Clock(started)
    async with AsyncExitStack() as stack:
        authority = await start_authority(stack, broker, layout, clock=clock)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        assert (await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime, clock()), WORKER,
                           clock=clock)).membership_epoch == 1
        clock.now = started + timedelta(days=19)
        await authority.upkeep(clock())  # as it does every minute: its revocations record stays current
        assert (await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime, clock()), WORKER,
                           clock=clock)).membership_epoch == 1
        clock.now = started + timedelta(days=21)
        await authority.upkeep(clock())
        assert (await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime, clock()), WORKER,
                           clock=clock)).membership_epoch == 2


@pytest.mark.asyncio
async def test_the_authority_starts_again_after_its_certificates_bucket_was_lost(broker: str, tmp_path: Path) -> None:
    """The trust folder survived the NATS data: the authority writes its certificate back, and no epoch names two
    certificates, so a host that holds certificate 1 is never given another certificate 1."""

    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        first = await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        before = await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), WORKER)
        await stop_authority(first)
        await worker_nc.jetstream().delete_stream("KV_UA1_CERTS")
        second = await start_authority(stack, broker, layout)
        assert second._own == first._own
        assert await stored_record(worker_nc, first._own["key"]) == first._own
        after = await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), WORKER)
        assert after.membership_epoch == before.membership_epoch + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("issue_record_lost", [False, True])
async def test_the_authoritys_kept_revocations_record_outlives_a_lost_bundle(broker: str, tmp_path: Path,
                                                                              issue_record_lost: bool) -> None:
    """An older record the authority genuinely signed, written back to the bucket after the bundle was rebuilt,
    does not lift a revocation: the authority signs the next record from the copy it keeps, or, with its issue
    record lost too, from the one its trust store keeps (trust-state.json)."""

    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        authority = await start_authority(stack, broker, layout)
        writer = await connect(stack, broker, "writer")
        oldest = authority._revocations
        certificate = await join(await connect(stack, broker, WORKER.runtime), worker_keys,
                                 host_trust(layout, WORKER.runtime), WORKER)
        revoked = authority.revocations_record(sequence=oldest["sequence"] + 1, revocation_epoch=2,
                                               entries=[revocation_entry(certificate)], issued=utc_now())
        await put_record(writer, revocations_key(INSTALLATION), revoked)
        signed = await authority._sign_revocations(utc_now())
        await stop_authority(authority)
        bundle = read_bundle(layout.trust_bootstrap_path)
        write_bundle(layout.trust_bootstrap_path, bundle.with_records(()))
        await put_record(writer, revocations_key(INSTALLATION), oldest)
        if issue_record_lost:
            (layout.host_trust_dir(AUTHORITY_RUNTIME) / ISSUED_FILENAME).unlink()
        restarted = await start_authority(stack, broker, layout)
        assert restarted._revocations["body"]["entries"] == signed["body"]["entries"]
        assert restarted._revocations["body"]["revocation_epoch"] == 2
        assert await stored_record(writer, revocations_key(INSTALLATION)) == restarted._revocations


@pytest.mark.asyncio
async def test_old_certificates_are_deleted_once_forgotten_and_revoked_ones_kept(broker: str, tmp_path: Path) -> None:
    other = HostProfile("worker-host-2", (TEAM,), ("worker_host",), ("researcher",), 1)
    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout, other)
    started = utc_now()
    clock = Clock(started)
    async with AsyncExitStack() as stack:
        authority = await start_authority(stack, broker, layout, clock=clock)
        reader = await connect(stack, broker, "reader")
        kept_certificate = await join(await connect(stack, broker, WORKER.runtime), worker_keys,
                                      host_trust(layout, WORKER.runtime), WORKER, clock=clock)
        other_keys = host_keys.load_host_keys(layout, other.runtime)
        revoked_certificate = await join(await connect(stack, broker, other.runtime), other_keys,
                                         host_trust(layout, other.runtime), other, clock=clock)
        revoked = authority.revocations_record(sequence=authority._revocations["sequence"] + 1, revocation_epoch=2,
                                               entries=[revocation_entry(revoked_certificate)], issued=clock())
        await put_record(reader, revocations_key(INSTALLATION), revoked)
        await authority._sign_revocations(clock())
        first_own = authority._own
        forgotten_after = kept_for("certificate")
        assert forgotten_after is not None
        clock.now = started + forgotten_after + timedelta(days=1)
        await authority.upkeep(clock())
        bucket = await reader.jetstream().key_value(CERTIFICATES_BUCKET)
        for gone in (certificate_key(INSTALLATION, WORKER.runtime, kept_certificate.membership_epoch), first_own["key"]):
            with pytest.raises(KeyNotFoundError):
                await bucket.get(gone)
        assert (await stored_record(reader, certificate_key(INSTALLATION, other.runtime, 1)))["body"]["runtime"] \
            == other.runtime
        assert await stored_record(reader, authority._own["key"]) == authority._own


@pytest.mark.asyncio
async def test_a_damaged_or_forged_newer_certificate_neither_returns_a_replaced_one_nor_blocks_joins(
        broker: str, tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    wider = HostProfile(WORKER.runtime, WORKER.teams, WORKER.roles, ("analyst", "researcher"), WORKER.session_limit)
    async with AsyncExitStack() as stack:
        await start_authority(stack, broker, layout)
        writer = await connect(stack, broker, "writer")
        worker_nc = await connect(stack, broker, WORKER.runtime)
        first = await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), WORKER)
        list_hosts(layout, wider)
        second = await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), wider)
        assert second.membership_epoch == 2
        second_key = certificate_key(INSTALLATION, WORKER.runtime, 2)
        await put_record(writer, second_key, forged(await stored_record(writer, second_key)))
        # A key at the highest epoch the contract allows, written by any process with the shared login.
        await put_record(writer, certificate_key(INSTALLATION, WORKER.runtime, MAX_EPOCH),
                         forged(await stored_record(writer, certificate_key(INSTALLATION, WORKER.runtime, 1))))
        # Asking for certificate 1's grants again: not certificate 1, which certificate 2 replaced, but a new one.
        again = await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), WORKER)
        assert again.membership_epoch == 3 and again.certificate_id != first.certificate_id


@pytest.mark.asyncio
async def test_a_host_that_joins_before_the_authority_serves_waits_for_it(broker: str, tmp_path: Path) -> None:
    """The join waits through "no responders"; the bundle it opened held no authority certificate yet, so it reads
    the certificate of the reply from UA1_CERTS."""

    layout = InstanceLayout.at(tmp_path / "instance")
    prepare(layout)
    async with AsyncExitStack() as stack:
        worker = MessageHost(layout, WORKER, await connect(stack, broker, WORKER.runtime))
        waiting = asyncio.create_task(worker.start(report=False))
        await asyncio.sleep(1.5)
        assert not waiting.done()
        await start_authority(stack, broker, layout)
        await asyncio.wait_for(waiting, timeout=30)
        assert worker.identity.certificate.membership_epoch == 1
