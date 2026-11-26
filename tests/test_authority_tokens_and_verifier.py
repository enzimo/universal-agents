"""Unit tests for protocol 1 root-task/permit authority tokens, TrustReader, and AuthorityTokenVerifier."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pytest

from access_control.authority_verifier import AuthorityTokenVerifier, IdentityRejected
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import sign_authority_token
from custom_messages.wire.kinds import AUTHORITY_TOKEN_MAX_SECONDS
from custom_messages.wire.models import AuthorityToken
from custom_transport.protocol1.authority_tokens import (
    GATEWAY_ISSUER_ROLE,
    ROOT_TASK_BINDING,
    command_token,
    is_schedule_permit_binding,
    root_task_token,
    schedule_permit_binding_id,
    schedule_permit_token,
)
from custom_transport.protocol1.bootstrap import BootstrapBundle, write_bundle
from custom_transport.protocol1.host_keys import HostKeys
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.trust_reader import TrustReader
from custom_transport.protocol1.trust_state import PersistentTrustStore
from runtime_settings.instance_layout import InstanceLayout
from tests import message_contract_support as fixtures

INSTALLATION = "install_example"
TEAM = "team_example"
ALICE_REF = "p-AbCdEfGhIjKlMnOpQrStUv"
BOB_REF = "p-ZyXwVuTsRqPoNmLkJiHgFe"
ADMIN_REF = "p-0123456789abcdefghijkl"


class _FakePeopleStore:
    def __init__(self, *people: TeamPersonV1) -> None:
        self._by_id: dict[tuple[str, str], TeamPersonV1] = {}
        self._by_ref: dict[tuple[str, str], TeamPersonV1] = {}
        for person in people:
            self.put(person)

    def put(self, person: TeamPersonV1) -> None:
        self._by_id[(person.team_id, person.person_id)] = person
        if person.person_ref:
            self._by_ref[(person.team_id, person.person_ref)] = person

    def team_person(self, team_id: str, person_id: str) -> TeamPersonV1 | None:
        return self._by_id.get((team_id, person_id))

    def team_person_by_ref(self, team_id: str, person_ref: str) -> TeamPersonV1 | None:
        return self._by_ref.get((team_id, person_ref))

    def team_people(self, team_id: str) -> list[TeamPersonV1]:
        return [p for (t, _), p in self._by_id.items() if t == team_id]

    def people_synced_at(self, team_id: str) -> object | None:
        return fixtures.NOW


def _identity_for(runtime: str, key_id: str, *, epoch: int = 1, store: Any = None) -> HostIdentity:
    trust = store if store is not None else fixtures.trust_store()
    cert = trust.host_certificate(INSTALLATION, runtime, epoch)
    assert cert is not None, f"missing fixture cert for {runtime} epoch {epoch}"
    keys = HostKeys(runtime, key_id, fixtures.private_key(key_id), bytes(32))
    return HostIdentity(keys, cert)


def _default_people() -> _FakePeopleStore:
    return _FakePeopleStore(
        TeamPersonV1(
            team_id=TEAM,
            person_id="usr_alice",
            person_ref=ALICE_REF,
            display_name="Alice",
            roles=("member",),
            standing=PersonStanding.ACTIVE,
            changed_at_utc=fixtures.NOW,
        ),
        TeamPersonV1(
            team_id=TEAM,
            person_id="usr_admin",
            person_ref=ADMIN_REF,
            display_name="Admin",
            roles=("admin", "owner"),
            standing=PersonStanding.ACTIVE,
            changed_at_utc=fixtures.NOW,
        ),
        TeamPersonV1(
            team_id=TEAM,
            person_id="usr_bob",
            person_ref=BOB_REF,
            display_name="Bob",
            roles=(),
            standing=PersonStanding.DISABLED,
            changed_at_utc=fixtures.NOW,
        ),
    )


# -- root_task_token & schedule_permit_token ------------------------------------------------------


def test_gateway_root_task_token_shape_and_lifetime_cap() -> None:
    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_123",
        request_id="message_context_abcdef0123456789",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
        lifetime_seconds=AUTHORITY_TOKEN_MAX_SECONDS * 4,
    )
    model = AuthorityToken.model_validate(token)
    assert model.bound_to.type == ROOT_TASK_BINDING
    assert model.bound_to.id == "task_123"
    assert model.bound_to.request_id == "message_context_abcdef0123456789"
    assert model.person == ALICE_REF
    assert "schedule_id" not in token
    assert token["expires_at"] == format_timestamp(fixtures.NOW + timedelta(seconds=AUTHORITY_TOKEN_MAX_SECONDS))


def test_root_task_token_explicit_expires_at_respects_cap_and_rejects_past() -> None:
    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    shorter = fixtures.NOW + timedelta(hours=2)
    token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_1",
        request_id="task_context_1",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
        expires_at=shorter,
    )
    assert token["expires_at"] == format_timestamp(shorter)

    longer = fixtures.NOW + timedelta(days=30)
    capped = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_2",
        request_id="task_context_2",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
        expires_at=longer,
    )
    assert capped["expires_at"] == format_timestamp(fixtures.NOW + timedelta(seconds=AUTHORITY_TOKEN_MAX_SECONDS))

    with pytest.raises(ValueError, match="end after"):
        root_task_token(
            gateway,
            team=TEAM,
            root_task_id="task_3",
            request_id="task_context_3",
            person_ref=ALICE_REF,
            issued=fixtures.NOW,
            expires_at=fixtures.NOW,
        )


def test_root_task_token_enforces_roles_person_ref_and_permit_separation() -> None:
    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    scheduler = _identity_for("scheduler-1", "test-scheduler-key-1")
    worker = _identity_for("worker-host-1", "test-worker-key-1", epoch=3)

    # Worker host cannot sign an authority token.
    with pytest.raises(ValueError, match="gateway or scheduler"):
        root_task_token(
            worker,
            team=TEAM,
            root_task_id="task_w",
            request_id="task_context_w",
            person_ref=ALICE_REF,
            issued=fixtures.NOW,
        )

    # Gateway cannot sign a schedule token or system work.
    with pytest.raises(ValueError, match="scheduler"):
        root_task_token(
            gateway,
            team=TEAM,
            root_task_id="task_s",
            request_id="task_context_s",
            person_ref=ALICE_REF,
            schedule_id="sched_1",
            issued=fixtures.NOW,
        )
    with pytest.raises(ValueError, match="scheduler"):
        root_task_token(
            gateway,
            team=TEAM,
            root_task_id="task_sys",
            request_id="task_context_sys",
            schedule_id="sched_sys",
            issued=fixtures.NOW,
        )

    # Neither person nor schedule is refused.
    with pytest.raises(ValueError, match="name a person"):
        root_task_token(
            scheduler,
            team=TEAM,
            root_task_id="task_empty",
            request_id="task_context_empty",
            issued=fixtures.NOW,
        )

    # Non-opaque person id is refused without echoing the input.
    with pytest.raises(ValueError, match="opaque reference") as exc:
        root_task_token(
            gateway,
            team=TEAM,
            root_task_id="task_bad_p",
            request_id="task_context_bad_p",
            person_ref="alice@example.com",
            issued=fixtures.NOW,
        )
    assert "alice@example.com" not in str(exc.value)

    # Wrong team is refused.
    with pytest.raises(ValueError, match="does not serve team"):
        root_task_token(
            gateway,
            team="team_other",
            root_task_id="task_t",
            request_id="task_context_t",
            person_ref=ALICE_REF,
            issued=fixtures.NOW,
        )

    # Permit binding ids must use schedule_permit_token.
    with pytest.raises(ValueError, match="schedule_permit_token"):
        root_task_token(
            scheduler,
            team=TEAM,
            root_task_id="schedule_permit:sched_1",
            request_id="schedule_permit:sched_1",
            person_ref=ALICE_REF,
            schedule_id="sched_1",
            issued=fixtures.NOW,
        )


def test_schedule_permit_token_shapes_and_role_check() -> None:
    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    scheduler = _identity_for("scheduler-1", "test-scheduler-key-1")

    owner_run_permit = schedule_permit_token(
        scheduler, team=TEAM, schedule_id="sched_42", person_ref=ALICE_REF, issued=fixtures.NOW
    )
    assert owner_run_permit["bound_to"] == {
        "type": ROOT_TASK_BINDING,
        "id": "schedule_permit:sched_42",
        "request_id": "schedule_permit:sched_42",
    }
    assert owner_run_permit["schedule_id"] == "sched_42"
    assert owner_run_permit["person"] == ALICE_REF
    assert is_schedule_permit_binding(owner_run_permit["bound_to"]["id"])

    sys_permit = schedule_permit_token(scheduler, team=TEAM, schedule_id="sched_sys", issued=fixtures.NOW)
    assert "person" not in sys_permit
    assert sys_permit["bound_to"]["id"] == "schedule_permit:sched_sys"

    sys_owner_permit = schedule_permit_token(
        scheduler,
        team=TEAM,
        schedule_id="sched_sys",
        person_ref=ADMIN_REF,
        owner_permit=True,
        issued=fixtures.NOW,
    )
    assert sys_owner_permit["bound_to"]["id"] == schedule_permit_binding_id("sched_sys", owner_permit=True)
    assert sys_owner_permit["person"] == ADMIN_REF

    with pytest.raises(ValueError, match="vouched owner"):
        schedule_permit_token(scheduler, team=TEAM, schedule_id="sched_sys", owner_permit=True, issued=fixtures.NOW)

    with pytest.raises(ValueError, match="scheduler"):
        schedule_permit_token(gateway, team=TEAM, schedule_id="sched_42", person_ref=ALICE_REF, issued=fixtures.NOW)


# -- AuthorityTokenVerifier -----------------------------------------------------------------------


def test_verifier_resolves_live_person_roles_and_standing() -> None:
    people = _default_people()
    verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=people, clock=lambda: fixtures.NOW)
    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_1",
        request_id="message_context_abc",
        person_ref=ADMIN_REF,
        issued=fixtures.NOW,
    )

    verified = verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW)
    assert verified.person_id == "usr_admin"
    assert verified.person_roles == ("admin", "owner")
    assert verified.root_task_id == "task_1"
    assert verified.request_id == "message_context_abc"
    assert verified.is_gateway_issued is True
    assert verified.is_scheduler_issued is False
    assert verified.issuer_kind == GATEWAY_ISSUER_ROLE
    assert GATEWAY_ISSUER_ROLE in verified.issuer_roles

    # Revoke admin role while the same token lasts: next check immediately reflects member only.
    people.put(
        TeamPersonV1(
            team_id=TEAM,
            person_id="usr_admin",
            person_ref=ADMIN_REF,
            display_name="Admin",
            roles=("member",),
            standing=PersonStanding.ACTIVE,
            changed_at_utc=fixtures.NOW,
        )
    )
    demoted = verifier.verify_root_task(AuthorityToken.model_validate(token), team_id=TEAM, now=fixtures.NOW)
    assert demoted.person_roles == ("member",)

    # Disable person: refused for active work, still attributable with empty roles when require_active_person=False.
    people.put(
        TeamPersonV1(
            team_id=TEAM,
            person_id="usr_admin",
            person_ref=ADMIN_REF,
            display_name="Admin",
            roles=(),
            standing=PersonStanding.DISABLED,
            changed_at_utc=fixtures.NOW,
        )
    )
    with pytest.raises(IdentityRejected, match="not active"):
        verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW)

    historical = verifier.verify_root_task(
        token, team_id=TEAM, now=fixtures.NOW, require_active_person=False
    )
    assert historical.person_id == "usr_admin"
    assert historical.person_roles == ()


def test_verifier_enforces_issuer_role_for_gateway_and_scheduler_sites_c04() -> None:
    verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=_default_people(), clock=lambda: fixtures.NOW)
    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    scheduler = _identity_for("scheduler-1", "test-scheduler-key-1")

    sched_admin_token = root_task_token(
        scheduler,
        team=TEAM,
        root_task_id="task_sched_admin",
        request_id="task_context_sched_admin",
        person_ref=ADMIN_REF,
        schedule_id="sched_admin",
        issued=fixtures.NOW,
    )
    # Valid for normal / scheduler checks.
    verified_sched = verifier.verify_root_task(
        sched_admin_token, team_id=TEAM, now=fixtures.NOW, require_issuer="scheduler"
    )
    assert verified_sched.is_scheduler_issued is True
    assert verified_sched.is_gateway_issued is False
    assert verified_sched.schedule_id == "sched_admin"
    assert verified_sched.person_id == "usr_admin"

    # C04: A valid scheduler-signed admin token MUST be refused at gateway-only sites.
    with pytest.raises(IdentityRejected, match="gateway-signed"):
        verifier.verify_root_task(sched_admin_token, team_id=TEAM, now=fixtures.NOW, require_issuer="gateway")

    # Gateway token is accepted for gateway-only sites and refused for scheduler-only sites.
    gw_token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_gw",
        request_id="message_context_gw",
        person_ref=ADMIN_REF,
        issued=fixtures.NOW,
    )
    assert verifier.verify_root_task(gw_token, team_id=TEAM, now=fixtures.NOW, require_issuer="gateway").is_gateway_issued
    with pytest.raises(IdentityRejected, match="scheduler-signed"):
        verifier.verify_root_task(gw_token, team_id=TEAM, now=fixtures.NOW, require_issuer="scheduler")


def test_verifier_refuses_bare_permit_and_verifies_permit_via_explicit_path_c01() -> None:
    verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=_default_people(), clock=lambda: fixtures.NOW)
    scheduler = _identity_for("scheduler-1", "test-scheduler-key-1")

    permit = schedule_permit_token(
        scheduler, team=TEAM, schedule_id="sched_99", person_ref=ALICE_REF, issued=fixtures.NOW
    )
    # C01: bare permit presented as task_context MUST be refused.
    with pytest.raises(IdentityRejected, match="schedule permit"):
        verifier.verify_root_task(permit, team_id=TEAM, now=fixtures.NOW)

    # Verified via explicit schedule permit path.
    verified_permit = verifier.verify_schedule_permit(permit, team_id=TEAM, now=fixtures.NOW)
    assert verified_permit.is_permit is True
    assert verified_permit.schedule_id == "sched_99"
    assert verified_permit.person_id == "usr_alice"

    # System-work permit (no person) succeeds with person_id=None.
    sys_permit = schedule_permit_token(scheduler, team=TEAM, schedule_id="sched_sys", issued=fixtures.NOW)
    verified_sys = verifier.verify_schedule_permit(sys_permit, team_id=TEAM, now=fixtures.NOW)
    assert verified_sys.person_id is None
    assert verified_sys.schedule_id == "sched_sys"

    # Regular root-task token is refused by verify_schedule_permit.
    run_token = root_task_token(
        scheduler,
        team=TEAM,
        root_task_id="task_run_1",
        request_id="task_context_run_1",
        person_ref=ALICE_REF,
        schedule_id="sched_99",
        issued=fixtures.NOW,
    )
    with pytest.raises(IdentityRejected, match="not a valid schedule permit"):
        verifier.verify_schedule_permit(run_token, team_id=TEAM, now=fixtures.NOW)


def test_verifier_decision_52_stored_at_vs_arrival_and_historical_mode() -> None:
    # Token valid for 1 hour from NOW.
    issued = fixtures.NOW
    expires = issued + timedelta(hours=1)
    stored_while_valid = issued + timedelta(minutes=30)
    after_expiry = issued + timedelta(hours=3)

    trust = fixtures.trust_store(now=issued)
    trust.add(fixtures.current_revocations(after_expiry, sequence=200), now=after_expiry)
    verifier = AuthorityTokenVerifier(trust, people=_default_people(), clock=lambda: after_expiry)
    gateway = _identity_for("gateway-1", "test-gateway-key-1", store=trust)
    token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_q",
        request_id="message_context_q",
        person_ref=ALICE_REF,
        issued=issued,
        expires_at=expires,
    )

    # Live call after expiry is refused.
    with pytest.raises(IdentityRejected, match="not valid"):
        verifier.verify_root_task(token, team_id=TEAM, now=after_expiry)

    # Queued message stored while valid is accepted at after_expiry (Decision 52).
    queued_ok = verifier.verify_root_task(token, team_id=TEAM, reached_at=stored_while_valid, now=after_expiry)
    assert queued_ok.person_id == "usr_alice"

    # Queued message stored AFTER expiry is refused.
    with pytest.raises(IdentityRejected, match="not valid"):
        verifier.verify_root_task(token, team_id=TEAM, reached_at=issued + timedelta(hours=2), now=after_expiry)

    # Historical check (check_expiry=False) accepts valid signature with current revocations.
    hist = verifier.verify_root_task(token, team_id=TEAM, now=after_expiry, check_expiry=False)
    assert hist.person_id == "usr_alice"


def test_verifier_rejects_missing_malformed_unknown_person_and_missing_people_store() -> None:
    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=_default_people(), clock=lambda: fixtures.NOW)
    with pytest.raises(IdentityRejected, match="no authority token"):
        verifier.verify_root_task(None, team_id=TEAM, now=fixtures.NOW)
    with pytest.raises(IdentityRejected, match="malformed"):
        verifier.verify_root_task({"not": "a token"}, team_id=TEAM, now=fixtures.NOW)

    unknown_ref_token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_unk",
        request_id="task_context_unk",
        person_ref="p-9999999999999999999999",
        issued=fixtures.NOW,
    )
    with pytest.raises(IdentityRejected, match="not known"):
        verifier.verify_root_task(unknown_ref_token, team_id=TEAM, now=fixtures.NOW)

    no_people_verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=None, clock=lambda: fixtures.NOW)
    alice_token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_np",
        request_id="task_context_np",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
    )
    with pytest.raises(IdentityRejected, match="cannot resolve"):
        no_people_verifier.verify_root_task(alice_token, team_id=TEAM, now=fixtures.NOW)


def test_verifier_rejects_forged_wrong_team_wrong_binding_revoked_and_stale_revocations() -> None:
    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=_default_people(), clock=lambda: fixtures.NOW)
    token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_sec",
        request_id="message_context_sec",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
    )

    # Forged / tampered field.
    tampered = dict(token, person=ADMIN_REF)
    with pytest.raises(IdentityRejected, match="signature"):
        verifier.verify_root_task(tampered, team_id=TEAM, now=fixtures.NOW)

    # Command token cannot pass as root-task token.
    cmd = command_token(gateway, team=TEAM, person_ref=ALICE_REF, command_id="cmd_1", issued=fixtures.NOW)
    with pytest.raises(IdentityRejected, match="root-task"):
        verifier.verify_root_task(cmd, team_id=TEAM, now=fixtures.NOW)

    # Wrong team.
    with pytest.raises(IdentityRejected, match="another team"):
        verifier.verify_root_task(token, team_id="team_other", now=fixtures.NOW)

    # Wrong installation.
    wrong_install_verifier = AuthorityTokenVerifier(
        fixtures.trust_store(), people=_default_people(), installation="install_other", clock=lambda: fixtures.NOW
    )
    with pytest.raises(IdentityRejected, match="another installation"):
        wrong_install_verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW)

    # Revoked issuer certificate.
    revoked_trust = fixtures.trust_store(add=("extra/gateway-revoked.json",))
    revoked_verifier = AuthorityTokenVerifier(revoked_trust, people=_default_people(), clock=lambda: fixtures.NOW)
    with pytest.raises(IdentityRejected, match="unrevoked"):
        revoked_verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW)

    # Historical check also rejects revoked issuer certificate.
    with pytest.raises(IdentityRejected, match="unrevoked"):
        revoked_verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW, check_expiry=False)

    # Stale revocations (2 days later without fresh revocations record).
    stale_now = fixtures.NOW + timedelta(days=2)
    with pytest.raises(IdentityRejected, match="unrevoked"):
        verifier.verify_root_task(token, team_id=TEAM, now=stale_now)

    # Token signed by gateway with a forged schedule_id is rejected even if manually constructed.
    raw_forged_sched = {
        "token_id": token["token_id"],
        "issuer": token["issuer"],
        "audience": token["audience"],
        "person": ALICE_REF,
        "schedule_id": "sched_forged",
        "bound_to": {"type": ROOT_TASK_BINDING, "id": "task_f", "request_id": "task_context_f"},
        "issued_at": token["issued_at"],
        "expires_at": token["expires_at"],
    }
    signed_forged_sched = sign_authority_token(raw_forged_sched, gateway.keys.application_key)
    with pytest.raises(IdentityRejected, match="scheduler"):
        verifier.verify_root_task(signed_forged_sched, team_id=TEAM, now=fixtures.NOW)


# -- TrustReader ----------------------------------------------------------------------------------


class _FakeStateRecords:
    """In-memory stand-in for StateRecords backed by a dict of UA1_CERTS records."""

    def __init__(self, entries: dict[str, bytes]) -> None:
        self.entries = dict(entries)
        self.watch_events: dict[str, list[tuple[str, Optional[bytes]]]] = {}

    async def keys(self, record: str, prefix: str, *, team: Optional[str] = None) -> list[str]:
        return sorted(k for k in self.entries if k.startswith(prefix))

    async def raw(self, record: str, key: str) -> Optional[tuple[int, bytes]]:
        value = self.entries.get(key)
        return (1, value) if value is not None else None

    async def watch(self, record: str, keys: str = ">", **_: Any) -> AsyncIterator[tuple[str, Optional[bytes]]]:
        for item in self.watch_events.get(keys, []):
            yield item
        await asyncio.Event().wait()


def _bootstrap_bundle_for_example() -> BootstrapBundle:
    anchors_doc = json.loads((fixtures.TRUST / "anchors.json").read_text(encoding="utf-8"))
    keys = tuple((entry["key_id"], entry["public_key"]) for entry in anchors_doc["authority_keys"])
    return BootstrapBundle(
        installation=anchors_doc["installation"],
        authority_keys=keys,
        records=(),
        partitions=8,
    )


def test_trust_reader_sync_fetch_and_watch_without_private_keys(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    write_bundle(layout.trust_bootstrap_path, _bootstrap_bundle_for_example())
    with pytest.raises(ValueError, match="reader id"):
        layout.reader_trust_dir("../escape")

    gw_cert_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    sched_cert_raw = (fixtures.TRUST / "certificates" / "scheduler-1.json").read_bytes()
    rev_raw = (fixtures.TRUST / "revocations" / "install_example.json").read_bytes()
    gw_cert_key = json.loads(gw_cert_raw)["key"]
    sched_cert_key = json.loads(sched_cert_raw)["key"]
    rev_key = json.loads(rev_raw)["key"]

    # Bucket starts with gateway cert, revocations, and one corrupted entry (must be skipped safely).
    fake_records = _FakeStateRecords(
        {
            gw_cert_key: gw_cert_raw,
            f"host.{INSTALLATION}.corrupt-host.1": b"{not-json",
            rev_key: rev_raw,
        }
    )
    persistent = PersistentTrustStore.open(
        _bootstrap_bundle_for_example(), layout.reader_trust_dir("secure-tool-broker"), fixtures.NOW
    )
    reader = TrustReader(persistent, fake_records, reader_id="secure-tool-broker", clock=lambda: fixtures.NOW)  # type: ignore[arg-type]

    asyncio.run(reader.sync_once())
    assert reader.store.host_certificate(INSTALLATION, "gateway-1", 1) is not None
    assert reader.store.revocations_current(INSTALLATION, fixtures.NOW) is True
    assert reader.store.host_certificate(INSTALLATION, "scheduler-1", 1) is None

    # Now scheduler cert appears in bucket; on-demand fetch_token_issuer pulls it in.
    fake_records.entries[sched_cert_key] = sched_cert_raw
    scheduler = _identity_for("scheduler-1", "test-scheduler-key-1")
    sched_token = root_task_token(
        scheduler,
        team=TEAM,
        root_task_id="task_sched",
        request_id="task_context_sched",
        person_ref=ALICE_REF,
        schedule_id="sched_1",
        issued=fixtures.NOW,
    )
    fetched = asyncio.run(reader.fetch_token_issuer(sched_token))
    assert fetched is True
    assert reader.store.host_certificate(INSTALLATION, "scheduler-1", 1) is not None

    # Verify the token using the reader's TrustStore and read_lock.
    verifier = AuthorityTokenVerifier(
        reader.trust, people=_default_people(), clock=lambda: fixtures.NOW, read_lock=reader.lock
    )
    verified = verifier.verify_root_task(sched_token, team_id=TEAM, now=fixtures.NOW)
    assert verified.person_id == "usr_alice" and verified.schedule_id == "sched_1"

    # Watch picks up a live revocation update and ignores a record stored under the wrong key.
    revoked_raw = (fixtures.TRUST / "extra" / "gateway-revoked.json").read_bytes()
    fake_records.watch_events[f"revocations.{INSTALLATION}"] = [(rev_key, revoked_raw)]
    fake_records.watch_events[f"host.{INSTALLATION}.>"] = [(f"host.{INSTALLATION}.wrong-key.9", gw_cert_raw)]

    async def _run_watch_briefly() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(reader.watch_and_refresh(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(_run_watch_briefly())
    gw_cert = reader.store.host_certificate(INSTALLATION, "gateway-1", 1)
    assert gw_cert is not None and reader.store.revoked(gw_cert) is True
    assert reader.store.host_certificate(INSTALLATION, "wrong-key", 9) is None

    # Another installation's token issuer is ignored by fetch_token_issuer.
    other_install_token = {
        "issuer": {"installation": "install_other", "runtime": "gateway-1", "membership_epoch": 1}
    }
    assert asyncio.run(reader.fetch_token_issuer(other_install_token)) is False
