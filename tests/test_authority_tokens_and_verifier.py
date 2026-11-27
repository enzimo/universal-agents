"""Unit tests for protocol 1 root-task/permit authority tokens, TrustReader, and AuthorityTokenVerifier."""

from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pytest
from nats.js.errors import KeyNotFoundError

from access_control.authority_verifier import (
    AuthorityTokenVerifier,
    IdentityRejected,
    REFUSAL_BAD_SIGNATURE,
    REFUSAL_EXPIRED,
    REFUSAL_MALFORMED,
    REFUSAL_MISSING,
    REFUSAL_NO_PEOPLE_STORE,
    REFUSAL_PERMIT_NOT_ALLOWED,
    REFUSAL_PERSON_INACTIVE,
    REFUSAL_PERSON_UNKNOWN,
    REFUSAL_REVOCATIONS_NOT_CURRENT,
    REFUSAL_REVOKED_ISSUER,
    REFUSAL_UNKNOWN_ISSUER,
    REFUSAL_WRONG_INSTALLATION,
    REFUSAL_WRONG_ISSUER,
    REFUSAL_WRONG_TEAM,
)
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import sign_authority_token
from custom_messages.wire.kinds import AUTHORITY_TOKEN_MAX_SECONDS
from custom_messages.wire.models import AuthorityToken, AuthorityTokenBoundToRootTask
from custom_messages.wire.trust import sign_state
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
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.joining import HostProfile
from custom_transport.protocol1.live import LiveHost
from custom_transport.protocol1.state_records import StateRecords
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
    assert isinstance(model.bound_to, AuthorityTokenBoundToRootTask)
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
    with pytest.raises(IdentityRejected, match="not active") as exc_inactive:
        verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW)
    assert exc_inactive.value.code == REFUSAL_PERSON_INACTIVE

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
    with pytest.raises(IdentityRejected, match="gateway-signed") as exc_wrong_gw:
        verifier.verify_root_task(sched_admin_token, team_id=TEAM, now=fixtures.NOW, require_issuer="gateway")
    assert exc_wrong_gw.value.code == REFUSAL_WRONG_ISSUER

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
    with pytest.raises(IdentityRejected, match="scheduler-signed") as exc_wrong_sched:
        verifier.verify_root_task(gw_token, team_id=TEAM, now=fixtures.NOW, require_issuer="scheduler")
    assert exc_wrong_sched.value.code == REFUSAL_WRONG_ISSUER


def test_verifier_refuses_bare_permit_and_verifies_permit_via_explicit_path_c01() -> None:
    verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=_default_people(), clock=lambda: fixtures.NOW)
    scheduler = _identity_for("scheduler-1", "test-scheduler-key-1")

    permit = schedule_permit_token(
        scheduler, team=TEAM, schedule_id="sched_99", person_ref=ALICE_REF, issued=fixtures.NOW
    )
    # C01: bare permit presented as task_context MUST be refused.
    with pytest.raises(IdentityRejected, match="schedule permit") as exc_permit:
        verifier.verify_root_task(permit, team_id=TEAM, now=fixtures.NOW)
    assert exc_permit.value.code == REFUSAL_PERMIT_NOT_ALLOWED

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

    # Live call after expiry is refused with distinct REFUSAL_EXPIRED code.
    with pytest.raises(IdentityRejected, match="not valid") as exc_expired:
        verifier.verify_root_task(token, team_id=TEAM, now=after_expiry)
    assert exc_expired.value.code == REFUSAL_EXPIRED

    # Queued message stored while valid is accepted at after_expiry (Decision 52).
    queued_ok = verifier.verify_root_task(token, team_id=TEAM, reached_at=stored_while_valid, now=after_expiry)
    assert queued_ok.person_id == "usr_alice"

    # Queued message stored AFTER expiry is refused.
    with pytest.raises(IdentityRejected, match="not valid") as exc_stored_expired:
        verifier.verify_root_task(token, team_id=TEAM, reached_at=issued + timedelta(hours=2), now=after_expiry)
    assert exc_stored_expired.value.code == REFUSAL_EXPIRED

    # Historical check (check_expiry=False) accepts valid signature with current revocations.
    hist = verifier.verify_root_task(token, team_id=TEAM, now=after_expiry, check_expiry=False)
    assert hist.person_id == "usr_alice"


def test_verifier_rejects_missing_malformed_unknown_person_and_missing_people_store() -> None:
    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=_default_people(), clock=lambda: fixtures.NOW)
    with pytest.raises(IdentityRejected, match="no authority token") as exc_missing:
        verifier.verify_root_task(None, team_id=TEAM, now=fixtures.NOW)
    assert exc_missing.value.code == REFUSAL_MISSING
    with pytest.raises(IdentityRejected, match="malformed") as exc_malformed:
        verifier.verify_root_task({"not": "a token"}, team_id=TEAM, now=fixtures.NOW)
    assert exc_malformed.value.code == REFUSAL_MALFORMED

    unknown_ref_token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_unk",
        request_id="task_context_unk",
        person_ref="p-9999999999999999999999",
        issued=fixtures.NOW,
    )
    with pytest.raises(IdentityRejected, match="not known") as exc_unk_person:
        verifier.verify_root_task(unknown_ref_token, team_id=TEAM, now=fixtures.NOW)
    assert exc_unk_person.value.code == REFUSAL_PERSON_UNKNOWN

    no_people_verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=None, clock=lambda: fixtures.NOW)
    alice_token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_np",
        request_id="task_context_np",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
    )
    with pytest.raises(IdentityRejected, match="cannot resolve") as exc_no_people:
        no_people_verifier.verify_root_task(alice_token, team_id=TEAM, now=fixtures.NOW)
    assert exc_no_people.value.code == REFUSAL_NO_PEOPLE_STORE


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
    with pytest.raises(IdentityRejected, match="signature") as exc_sig:
        verifier.verify_root_task(tampered, team_id=TEAM, now=fixtures.NOW)
    assert exc_sig.value.code == REFUSAL_BAD_SIGNATURE

    # Command token cannot pass as root-task token.
    cmd = command_token(gateway, team=TEAM, person_ref=ALICE_REF, command_id="cmd_1", issued=fixtures.NOW)
    with pytest.raises(IdentityRejected, match="root-task") as exc_cmd:
        verifier.verify_root_task(cmd, team_id=TEAM, now=fixtures.NOW)
    assert exc_cmd.value.code == REFUSAL_MALFORMED

    # Wrong team.
    with pytest.raises(IdentityRejected, match="another team") as exc_team:
        verifier.verify_root_task(token, team_id="team_other", now=fixtures.NOW)
    assert exc_team.value.code == REFUSAL_WRONG_TEAM

    # Wrong installation.
    wrong_install_verifier = AuthorityTokenVerifier(
        fixtures.trust_store(), people=_default_people(), installation="install_other", clock=lambda: fixtures.NOW
    )
    with pytest.raises(IdentityRejected, match="another installation") as exc_install:
        wrong_install_verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW)
    assert exc_install.value.code == REFUSAL_WRONG_INSTALLATION

    # Revoked issuer certificate has distinct REFUSAL_REVOKED_ISSUER code.
    revoked_trust = fixtures.trust_store(add=("extra/gateway-revoked.json",))
    revoked_verifier = AuthorityTokenVerifier(revoked_trust, people=_default_people(), clock=lambda: fixtures.NOW)
    with pytest.raises(IdentityRejected, match="revoked") as exc_revoked:
        revoked_verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW)
    assert exc_revoked.value.code == REFUSAL_REVOKED_ISSUER

    # Historical check also rejects revoked issuer certificate.
    with pytest.raises(IdentityRejected, match="revoked") as exc_revoked_hist:
        revoked_verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW, check_expiry=False)
    assert exc_revoked_hist.value.code == REFUSAL_REVOKED_ISSUER

    # Stale revocations (2 days later without fresh revocations record) has distinct REFUSAL_REVOCATIONS_NOT_CURRENT code.
    stale_now = fixtures.NOW + timedelta(days=2)
    with pytest.raises(IdentityRejected, match="revocations") as exc_stale:
        verifier.verify_root_task(token, team_id=TEAM, now=stale_now)
    assert exc_stale.value.code == REFUSAL_REVOCATIONS_NOT_CURRENT

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
    with pytest.raises(IdentityRejected, match="scheduler") as exc_forged_sched:
        verifier.verify_root_task(signed_forged_sched, team_id=TEAM, now=fixtures.NOW)
    assert exc_forged_sched.value.code == REFUSAL_WRONG_ISSUER


# -- TrustReader & on-demand issuer fetching (C08, C09) -------------------------------------------


@dataclass(frozen=True)
class _FakeKvEntry:
    revision: int
    value: bytes


class _FakeKvBucket:
    def __init__(self, entries: dict[str, bytes]) -> None:
        self.entries = entries

    async def get(self, key: str) -> _FakeKvEntry:
        value = self.entries.get(key)
        if value is None:
            raise KeyNotFoundError()
        return _FakeKvEntry(revision=1, value=value)


class _FakeJetStream:
    def __init__(self, entries: dict[str, bytes]) -> None:
        self.entries = entries
        self.bucket = _FakeKvBucket(entries)

    async def key_value(self, _name: str) -> _FakeKvBucket:
        return self.bucket


class _TestStateRecords(StateRecords):
    """Real StateRecords (using real take_trust_record / take_trust_bytes) over an in-memory KV dict."""

    def __init__(self, entries: dict[str, bytes], trust: PersistentTrustStore) -> None:
        super().__init__(_FakeJetStream(entries), lambda: _identity_for("worker-host-1", "test-worker-key-1", epoch=3), trust)
        self.entries = entries
        self.watch_events: dict[str, list[tuple[str, Optional[bytes]]]] = {}
        self.reads: list[str] = []

    async def _get(self, record: str, key: str) -> Any:
        self.reads.append(key)
        return await super()._get(record, key)

    async def keys(self, record: str, prefix: str, *, team: Optional[str] = None) -> list[str]:
        return sorted(k for k in self.entries if k.startswith(prefix))

    async def watch(self, record: str, keys: str = ">", **_: Any) -> AsyncIterator[tuple[str, Optional[bytes]]]:
        for item in self.watch_events.get(keys, []):
            yield item
        await asyncio.Event().wait()


def _bootstrap_bundle_for_example(*, include_revocations: bool = False) -> BootstrapBundle:
    anchors_doc = json.loads((fixtures.TRUST / "anchors.json").read_text(encoding="utf-8"))
    keys = tuple((entry["key_id"], entry["public_key"]) for entry in anchors_doc["authority_keys"])
    records: tuple[dict[str, Any], ...] = ()
    if include_revocations:
        rev_doc = json.loads((fixtures.TRUST / "revocations" / "install_example.json").read_text(encoding="utf-8"))
        records = (rev_doc,)
    return BootstrapBundle(
        installation=anchors_doc["installation"],
        authority_keys=keys,
        records=records,
        partitions=8,
    )


def _renewed_gateway_cert_epoch_2() -> tuple[str, bytes, HostIdentity]:
    """Mint a real gateway-1 certificate at membership_epoch=2 signed by the installation authority key."""

    cert_doc = json.loads((fixtures.TRUST / "certificates" / "gateway-1.json").read_text(encoding="utf-8"))
    cert_doc["key"] = f"host.{INSTALLATION}.gateway-1.2"
    cert_doc["sequence"] = int(cert_doc["sequence"]) + 1
    cert_doc["body"] = dict(cert_doc["body"], membership_epoch=2, certificate_id="cert_gateway_1_epoch_2")
    cert_doc.pop("signature", None)
    signed = sign_state(cert_doc, fixtures.private_key(cert_doc["writer"]["key_id"]))
    raw = json.dumps(signed, separators=(",", ":")).encode("utf-8")
    # Load into a temporary TrustStore to obtain the verified HostCertificate object for signing.
    temp_store = fixtures.trust_store(extra_documents=(raw,))
    cert = temp_store.host_certificate(INSTALLATION, "gateway-1", 2)
    assert cert is not None
    keys = HostKeys("gateway-1", "test-gateway-key-1", fixtures.private_key("test-gateway-key-1"), bytes(32))
    return cert_doc["key"], raw, HostIdentity(keys, cert)


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
    entries = {
        gw_cert_key: gw_cert_raw,
        f"host.{INSTALLATION}.corrupt-host.1": b"{not-json",
        rev_key: rev_raw,
    }
    persistent = PersistentTrustStore.open(
        _bootstrap_bundle_for_example(), layout.reader_trust_dir("secure-tool-broker"), fixtures.NOW
    )
    records = _TestStateRecords(entries, persistent)
    reader = TrustReader(persistent, records, reader_id="secure-tool-broker", clock=lambda: fixtures.NOW)

    asyncio.run(reader.sync_once())
    assert reader.store.host_certificate(INSTALLATION, "gateway-1", 1) is not None
    assert reader.store.revocations_current(INSTALLATION, fixtures.NOW) is True
    assert reader.store.host_certificate(INSTALLATION, "scheduler-1", 1) is None

    # Now scheduler cert appears in bucket; on-demand fetch_token_issuer pulls it in.
    entries[sched_cert_key] = sched_cert_raw
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

    # Verify the token using the reader directly (automatically picks up trust store, lock, and fetcher).
    verifier = AuthorityTokenVerifier(reader, people=_default_people(), clock=lambda: fixtures.NOW)
    verified = verifier.verify_root_task(sched_token, team_id=TEAM, now=fixtures.NOW)
    assert verified.person_id == "usr_alice" and verified.schedule_id == "sched_1"

    # Watch picks up a live revocation update and ignores a record stored under the wrong key.
    revoked_raw = (fixtures.TRUST / "extra" / "gateway-revoked.json").read_bytes()
    records.watch_events[f"revocations.{INSTALLATION}"] = [(rev_key, revoked_raw)]
    records.watch_events[f"host.{INSTALLATION}.>"] = [(f"host.{INSTALLATION}.wrong-key.9", gw_cert_raw)]

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


def _build_bootstrap_live_host(
    tmp_path: Path,
    entries: dict[str, bytes],
    *,
    clock_fn: Any,
) -> tuple[MessageHost, LiveHost, _TestStateRecords]:
    layout = InstanceLayout.at(tmp_path)
    bundle = _bootstrap_bundle_for_example(include_revocations=True)
    persistent = PersistentTrustStore.open(bundle, layout.host_trust_dir("lead"), fixtures.NOW)
    records = _TestStateRecords(entries, persistent)
    profile = HostProfile("lead", (TEAM,), ("lead_host", "worker_host"), agent_types=("TeamLead",), session_limit=4)
    host = MessageHost(layout, profile, nc=None, clock=clock_fn)
    host._trust = persistent
    host.records = records
    lead_identity = _identity_for("worker-host-1", "test-worker-key-1", epoch=3)
    live = LiveHost(nc=None, identity=lead_identity, trust=persistent, clock=clock_fn, fetch_trust=host.fetch_trust)
    host.live = live
    return host, live, records


def test_bootstrap_only_live_host_and_trust_reader_fetch_gateway_cert_on_demand_c08(tmp_path: Path) -> None:
    """C08: starting from bootstrap-only trust (no gateway cert), async verification fetches gateway-1 on demand."""

    gw_cert_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    gw_cert_key = json.loads(gw_cert_raw)["key"]
    entries = {gw_cert_key: gw_cert_raw}

    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_first_chat",
        request_id="message_context_first_chat",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
    )

    # 1. LiveHost / MessageHost starting from bootstrap-only trust store.
    _host, live, records = _build_bootstrap_live_host(tmp_path / "host", entries, clock_fn=lambda: fixtures.NOW)
    assert live.trust.store.host_certificate(INSTALLATION, "gateway-1", 1) is None

    host_verifier = AuthorityTokenVerifier(live, people=_default_people(), clock=lambda: fixtures.NOW)
    with pytest.raises(IdentityRejected) as exc_unknown:
        host_verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW)
    assert exc_unknown.value.code == REFUSAL_UNKNOWN_ISSUER

    verified = asyncio.run(host_verifier.verify_root_task_async(token, team_id=TEAM, now=fixtures.NOW))
    assert verified.person_id == "usr_alice"
    assert verified.is_gateway_issued is True
    assert gw_cert_key in records.reads

    # Subsequent synchronous verification succeeds immediately and ensure_issuer_known does no extra read.
    read_count_before = len(records.reads)
    assert asyncio.run(host_verifier.ensure_issuer_known(token, now=fixtures.NOW)) is False
    assert len(records.reads) == read_count_before
    assert host_verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW).person_id == "usr_alice"

    # 2. TrustReader starting from bootstrap-only trust store.
    reader_layout = InstanceLayout.at(tmp_path / "reader")
    reader_persistent = PersistentTrustStore.open(
        _bootstrap_bundle_for_example(include_revocations=True),
        reader_layout.reader_trust_dir("secure-tool-broker"),
        fixtures.NOW,
    )
    reader_records = _TestStateRecords(entries, reader_persistent)
    reader = TrustReader(reader_persistent, reader_records, reader_id="secure-tool-broker", clock=lambda: fixtures.NOW)
    reader_verifier = AuthorityTokenVerifier(reader, people=_default_people(), clock=lambda: fixtures.NOW)

    with pytest.raises(IdentityRejected) as exc_reader_unknown:
        reader_verifier.verify_root_task(token, team_id=TEAM, now=fixtures.NOW)
    assert exc_reader_unknown.value.code == REFUSAL_UNKNOWN_ISSUER

    verified_reader = asyncio.run(reader_verifier.verify_root_task_async(token, team_id=TEAM, now=fixtures.NOW))
    assert verified_reader.person_id == "usr_alice"


def test_gateway_renewed_to_epoch_2_is_fetched_and_verified_c08(tmp_path: Path) -> None:
    """C08: after gateway-1 renews to epoch 2, verifiers holding only epoch 1 fetch epoch 2 on demand."""

    gw1_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    gw1_key = json.loads(gw1_raw)["key"]
    gw2_key, gw2_raw, gateway_epoch_2 = _renewed_gateway_cert_epoch_2()

    entries = {gw1_key: gw1_raw, gw2_key: gw2_raw}
    token_epoch_2 = root_task_token(
        gateway_epoch_2,
        team=TEAM,
        root_task_id="task_epoch_2",
        request_id="message_context_epoch_2",
        person_ref=ADMIN_REF,
        issued=fixtures.NOW,
    )

    # Pre-seed LiveHost with epoch 1 only; epoch 2 is in UA1_CERTS.
    _host, live, records = _build_bootstrap_live_host(tmp_path / "host", entries, clock_fn=lambda: fixtures.NOW)
    live.trust.add(gw1_raw, now=fixtures.NOW)
    assert live.trust.store.host_certificate(INSTALLATION, "gateway-1", 1) is not None
    assert live.trust.store.host_certificate(INSTALLATION, "gateway-1", 2) is None

    verifier = AuthorityTokenVerifier(live, people=_default_people(), clock=lambda: fixtures.NOW)
    with pytest.raises(IdentityRejected) as exc_unknown:
        verifier.verify_root_task(token_epoch_2, team_id=TEAM, now=fixtures.NOW)
    assert exc_unknown.value.code == REFUSAL_UNKNOWN_ISSUER

    verified = asyncio.run(verifier.verify_root_task_async(token_epoch_2, team_id=TEAM, now=fixtures.NOW))
    assert verified.person_id == "usr_admin"
    assert verified.is_gateway_issued is True
    assert gw2_key in records.reads

    # Same for TrustReader holding only epoch 1 before watch catches up.
    reader_layout = InstanceLayout.at(tmp_path / "reader")
    reader_persistent = PersistentTrustStore.open(
        _bootstrap_bundle_for_example(include_revocations=True),
        reader_layout.reader_trust_dir("secure-tool-broker"),
        fixtures.NOW,
    )
    reader_persistent.add(gw1_raw, now=fixtures.NOW)
    reader_records = _TestStateRecords(entries, reader_persistent)
    reader = TrustReader(reader_persistent, reader_records, reader_id="secure-tool-broker", clock=lambda: fixtures.NOW)
    reader_verifier = AuthorityTokenVerifier(reader, people=_default_people(), clock=lambda: fixtures.NOW)

    with pytest.raises(IdentityRejected) as exc_reader_unknown:
        reader_verifier.verify_root_task(token_epoch_2, team_id=TEAM, now=fixtures.NOW)
    assert exc_reader_unknown.value.code == REFUSAL_UNKNOWN_ISSUER

    verified_reader = asyncio.run(
        reader_verifier.verify_root_task_async(token_epoch_2, team_id=TEAM, now=fixtures.NOW)
    )
    assert verified_reader.person_id == "usr_admin"


def test_stale_revocations_refreshed_on_demand_by_async_verifier_c08(tmp_path: Path) -> None:
    """C08: when revocations go stale, async verification refreshes revocations from UA1_CERTS and retries once."""

    later = fixtures.NOW + timedelta(days=2)
    current_clock: list[datetime] = [later]
    gw1_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    rev_key = f"revocations.{INSTALLATION}"
    fresh_rev_raw = fixtures.current_revocations(later, sequence=250)
    entries = {rev_key: fresh_rev_raw}

    _host, live, records = _build_bootstrap_live_host(tmp_path / "host", entries, clock_fn=lambda: current_clock[0])
    live.trust.add(gw1_raw, now=fixtures.NOW)
    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_stale_rev",
        request_id="message_context_stale_rev",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
    )

    verifier = AuthorityTokenVerifier(live, people=_default_people(), clock=lambda: current_clock[0])
    with pytest.raises(IdentityRejected) as exc_stale:
        verifier.verify_root_task(token, team_id=TEAM, now=later)
    assert exc_stale.value.code == REFUSAL_REVOCATIONS_NOT_CURRENT

    verified = asyncio.run(verifier.verify_root_task_async(token, team_id=TEAM, now=later))
    assert verified.person_id == "usr_alice"
    assert rev_key in records.reads


def test_custom_transport_does_not_import_access_control_c09() -> None:
    """C09: custom_transport must not depend on access_control."""

    transport_root = Path(__file__).resolve().parents[1] / "custom_transport"
    offenders: list[str] = []
    for path in sorted(transport_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("access_control"):
                offenders.append(f"{path.relative_to(transport_root)}:{node.lineno} ({node.module})")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("access_control"):
                        offenders.append(f"{path.relative_to(transport_root)}:{node.lineno} ({alias.name})")
    assert offenders == []


# -- C10, C11, C13 tests --------------------------------------------------------------------------


def test_agent_process_host_fetches_issuer_from_worker_thread_c10(tmp_path: Path) -> None:
    """C10: AgentProcessHost verifies a gateway token from a worker thread by fetching once on the host loop."""

    import threading

    from custom_agents.agent_host import AgentProcessHost

    gw_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    gw_key = json.loads(gw_raw)["key"]
    entries = {gw_key: gw_raw}
    host, live, records = _build_bootstrap_live_host(tmp_path / "h", entries, clock_fn=lambda: fixtures.NOW)
    profile = HostProfile("lead", (TEAM,), ("lead_host",), agent_types=("TeamLead",), session_limit=4)
    proc = AgentProcessHost(InstanceLayout.at(tmp_path / "h"), profile, TEAM)
    proc.host = host

    state = proc.trust_state()
    assert state is not None
    assert state[0] is live.trust.store
    assert state[1] is live.trust.lock

    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_worker_thread",
        request_id="message_context_worker_thread",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
    )
    verifier = AuthorityTokenVerifier(proc, people=_default_people(), clock=lambda: fixtures.NOW)

    async def _run() -> dict[str, Any]:
        proc.loop = asyncio.get_running_loop()
        result: dict[str, Any] = {}

        def _worker() -> None:
            try:
                verified = asyncio.run(verifier.verify_root_task_async(token, team_id=TEAM, now=fixtures.NOW))
                result["person_id"] = verified.person_id
                result["gateway"] = verified.is_gateway_issued
            except Exception as exc:  # noqa: BLE001
                result["err"] = repr(exc)

        thread = threading.Thread(target=_worker)
        thread.start()
        while thread.is_alive():
            await asyncio.sleep(0.01)
        thread.join()
        return result

    outcome = asyncio.run(_run())
    assert outcome == {"person_id": "usr_alice", "gateway": True}
    assert records.reads == [gw_key]


def test_verify_schedule_permit_async_fetches_scheduler_cert_c10(tmp_path: Path) -> None:
    """C10: verify_schedule_permit_async fetches a missing scheduler certificate once and verifies the permit."""

    sched_raw = (fixtures.TRUST / "certificates" / "scheduler-1.json").read_bytes()
    sched_key = json.loads(sched_raw)["key"]
    entries = {sched_key: sched_raw}
    _host, live, records = _build_bootstrap_live_host(tmp_path / "h", entries, clock_fn=lambda: fixtures.NOW)

    scheduler = _identity_for("scheduler-1", "test-scheduler-key-1")
    permit = schedule_permit_token(
        scheduler, team=TEAM, schedule_id="sched_async_1", person_ref=ALICE_REF, issued=fixtures.NOW
    )
    verifier = AuthorityTokenVerifier(live, people=_default_people(), clock=lambda: fixtures.NOW)
    with pytest.raises(IdentityRejected) as exc_sync:
        verifier.verify_schedule_permit(permit, team_id=TEAM, now=fixtures.NOW)
    assert exc_sync.value.code == REFUSAL_UNKNOWN_ISSUER

    verified = asyncio.run(verifier.verify_schedule_permit_async(permit, team_id=TEAM, now=fixtures.NOW))
    assert verified.is_permit is True
    assert verified.is_scheduler_issued is True
    assert verified.schedule_id == "sched_async_1"
    assert verified.person_id == "usr_alice"
    assert records.reads == [sched_key]


def test_fetch_found_nothing_logs_identifiers_without_person_and_refuses_after_one_read_c10(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """C10: missing issuer triggers exactly one UA1_CERTS read, refuses unknown_issuer, and logs safe identifiers."""

    import logging

    # Empty UA1_CERTS: gateway-1 certificate is absent.
    _host, live, records = _build_bootstrap_live_host(tmp_path / "h", {}, clock_fn=lambda: fixtures.NOW)
    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    token = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_missing_issuer",
        request_id="message_context_missing_issuer",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
    )
    verifier = AuthorityTokenVerifier(live, people=_default_people(), clock=lambda: fixtures.NOW)

    with caplog.at_level(logging.WARNING, logger="access_control.authority_verifier"):
        with pytest.raises(IdentityRejected) as exc:
            asyncio.run(verifier.verify_root_task_async(token, team_id=TEAM, now=fixtures.NOW))
    assert exc.value.code == REFUSAL_UNKNOWN_ISSUER
    assert records.reads == [f"host.{INSTALLATION}.gateway-1.1"]
    assert "event=authority_token_refused" in caplog.text
    assert "code=unknown_issuer" in caplog.text
    assert "issuer_runtime=gateway-1" in caplog.text
    assert "membership_epoch=1" in caplog.text
    assert f"team={TEAM}" in caplog.text
    assert "request_id=message_context_missing_issuer" in caplog.text
    # Person reference must NEVER appear in refusal logs (C10).
    assert ALICE_REF not in caplog.text


def test_non_fetchable_refusal_codes_do_not_read_certs_c10(tmp_path: Path) -> None:
    """C10: bad_signature, revoked_issuer, and expired never trigger a UA1_CERTS read."""

    gw_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    _host, live, records = _build_bootstrap_live_host(tmp_path / "h", {}, clock_fn=lambda: fixtures.NOW)
    live.trust.add(gw_raw, now=fixtures.NOW)

    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    valid = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_nf",
        request_id="message_context_nf",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
        expires_at=fixtures.NOW + timedelta(minutes=30),
    )
    verifier = AuthorityTokenVerifier(live, people=_default_people(), clock=lambda: fixtures.NOW)

    # 1. bad_signature -> 0 reads
    tampered = dict(valid, person=ADMIN_REF)
    with pytest.raises(IdentityRejected) as exc_sig:
        asyncio.run(verifier.verify_root_task_async(tampered, team_id=TEAM, now=fixtures.NOW))
    assert exc_sig.value.code == REFUSAL_BAD_SIGNATURE
    assert records.reads == []

    # 2. expired -> 0 reads
    after_expiry = fixtures.NOW + timedelta(hours=2)
    live.trust.add(fixtures.current_revocations(after_expiry, sequence=210), now=after_expiry)
    with pytest.raises(IdentityRejected) as exc_exp:
        asyncio.run(verifier.verify_root_task_async(valid, team_id=TEAM, now=after_expiry))
    assert exc_exp.value.code == REFUSAL_EXPIRED
    assert records.reads == []

    # 3. revoked_issuer -> 0 reads (use a fresh host so step 2's future revocations sequence does not supersede the fixture)
    _host_rev, live_rev, records_rev = _build_bootstrap_live_host(tmp_path / "h_rev", {}, clock_fn=lambda: fixtures.NOW)
    live_rev.trust.add(gw_raw, now=fixtures.NOW)
    live_rev.trust.add((fixtures.TRUST / "extra" / "gateway-revoked.json").read_bytes(), now=fixtures.NOW)
    verifier_rev = AuthorityTokenVerifier(live_rev, people=_default_people(), clock=lambda: fixtures.NOW)
    with pytest.raises(IdentityRejected) as exc_rev:
        asyncio.run(verifier_rev.verify_root_task_async(valid, team_id=TEAM, now=fixtures.NOW))
    assert exc_rev.value.code == REFUSAL_REVOKED_ISSUER
    assert records_rev.reads == []


def test_explicit_trust_state_interface_and_no_read_lock_arg_c11(tmp_path: Path) -> None:
    """C11: trust resolution uses explicit trust_state() and ignores decoy .lock/.store attributes."""

    import inspect
    import threading

    from access_control.authority_verifier import _resolve_trust_source
    from custom_agents.agent_host import AgentProcessHost

    assert "read_lock" not in inspect.signature(AuthorityTokenVerifier.__init__).parameters

    host, live, _records = _build_bootstrap_live_host(tmp_path / "h", {}, clock_fn=lambda: fixtures.NOW)
    profile = HostProfile("lead", (TEAM,), ("lead_host",), agent_types=("TeamLead",), session_limit=4)
    proc = AgentProcessHost(InstanceLayout.at(tmp_path / "h"), profile, TEAM)
    proc.host = host
    reader = TrustReader(live.trust, _TestStateRecords({}, live.trust), reader_id="broker", clock=lambda: fixtures.NOW)

    for source in (live.trust, reader, live, host, proc):
        store, lock, _fetcher = _resolve_trust_source(source, explicit_fetcher=None)
        assert store is live.trust.store
        assert lock is live.trust.lock

    # Bare TrustStore still works for tests with lock=None.
    bare_store, bare_lock, bare_fetcher = _resolve_trust_source(live.trust.store, explicit_fetcher=None)
    assert bare_store is live.trust.store and bare_lock is None and bare_fetcher is None

    # A decoy object with .lock and .store but no trust_state() is NOT silently duck-walked.
    class _Decoy:
        lock = threading.Lock()
        store = live.trust.store

    decoy_store, decoy_lock, _ = _resolve_trust_source(_Decoy(), explicit_fetcher=None)
    assert decoy_store is None and decoy_lock is None


def test_authority_bindings_shared_constants_and_record_parity_c13() -> None:
    """C13: frame.py reuses authority_bindings role constants, and AuthorityTokenRecord matches wire AuthorityToken."""

    from custom_messages.wire import authority_bindings, frame

    assert frame.AUTHORITY_ISSUER_ROLES is authority_bindings.AUTHORITY_ISSUER_ROLES
    assert frame.SCHEDULE_ISSUER_ROLE == authority_bindings.SCHEDULER_ISSUER_ROLE
    assert authority_bindings.AUTHORITY_TOKEN_MAX_LIFETIME_SECONDS == AUTHORITY_TOKEN_MAX_SECONDS

    gateway = _identity_for("gateway-1", "test-gateway-key-1")
    raw = root_task_token(
        gateway,
        team=TEAM,
        root_task_id="task_parity",
        request_id="message_context_parity",
        person_ref=ALICE_REF,
        issued=fixtures.NOW,
    )
    wire_model = AuthorityToken.model_validate(raw)
    record_from_dict = authority_bindings.AuthorityTokenRecord.model_validate(raw)
    record_from_wire = authority_bindings.AuthorityTokenRecord.model_validate(wire_model)
    assert record_from_dict.model_dump(mode="json") == wire_model.model_dump(mode="json")
    assert record_from_wire.model_dump(mode="json") == wire_model.model_dump(mode="json")
    assert set(authority_bindings.AuthorityTokenRecord.model_fields) == set(AuthorityToken.model_fields)
