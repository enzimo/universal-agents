"""Shared helpers for tests that call the secure tool broker.

The broker accepts only per-process caller tokens derived from its root secret
(``access_control.caller_tokens``) and trusts who a task works for only from a
signed task context (``access_control.identity``). These helpers give tests one
fixed root, one test signing key, and ready-made request contexts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from access_catalog.catalog import CatalogSource
from access_control.account_sharing import TeamAccountSharing
from access_control.authority_verifier import AuthorityTokenVerifier, set_process_verifier
from access_control.caller_tokens import CALLER_GATEWAY, CALLER_MANAGER, derive_caller_token
from access_control.identity import (
    ENV_GATEWAY_SIGNING_KEY,
    ENV_SCHEDULER_SIGNING_KEY,
    ENV_VERIFY_KEYS,
    ISSUER_GATEWAY,
    ISSUER_SCHEDULER,
    ContextSigner,
    generate_signing_key,
    verify_keys_value,
)
from custom_messages.contracts.access_v2 import (
    AccountLinkV1,
    AccountSharing,
    CredentialInjectionV1,
    LinkOwnerKind,
    PersonStanding,
    SubjectKind,
    SubjectRefV1,
    TeamPersonV1,
)
from secure_tool_broker.access_gate import BrokerAccessGate
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

BROKER_ROOT = "test-token"
TEST_INSTALLATION = "inst-test-broker"
TEST_TEAM = "team-1"
TEST_AGENT_ID = "agent-1"
TEST_AGENT_TYPE = "GeneralAssistant"


def broker_person_ref(person_id: str) -> str:
    """Deterministic opaque ``person_ref`` for test helpers (matches ``^p-[A-Za-z0-9_-]{22}$``)."""

    digest = hashlib.sha256(f"test-person-ref:{person_id}".encode("utf-8")).hexdigest()[:22]
    return f"p-{digest}"


class _InMemoryBrokerPeople:
    """Minimal ``PeopleStore`` mapping test ``person_ref``s to ``TeamPersonV1`` records."""

    def __init__(self) -> None:
        self._by_id: dict[tuple[str, str], TeamPersonV1] = {}
        self._by_ref: dict[tuple[str, str], TeamPersonV1] = {}

    def register(
        self,
        team_id: str,
        person_id: str,
        *,
        roles: tuple[str, ...] = ("member",),
        standing: PersonStanding = PersonStanding.ACTIVE,
        person_ref: str | None = None,
    ) -> TeamPersonV1:
        ref = person_ref or broker_person_ref(person_id)
        record = TeamPersonV1(
            team_id=team_id,
            person_id=person_id,
            person_ref=ref,
            standing=standing,
            roles=tuple(sorted(set(roles))),
            changed_at_utc=datetime.now(timezone.utc),
        )
        self._by_id[(team_id, person_id)] = record
        self._by_ref[(team_id, ref)] = record
        return record

    def team_person(self, team_id: str, person_id: str) -> TeamPersonV1 | None:
        return self._by_id.get((team_id, person_id))

    def team_person_by_ref(self, team_id: str, person_ref: str) -> TeamPersonV1 | None:
        return self._by_ref.get((team_id, person_ref))

    def team_people(self, team_id: str) -> list[TeamPersonV1]:
        return [record for (tid, _pid), record in self._by_id.items() if tid == team_id]

    def people_synced_at(self, _team_id: str) -> datetime:
        return datetime.now(timezone.utc)


@lru_cache(maxsize=1)
def _protocol1_test_authority() -> dict[str, Any]:
    """One self-contained protocol 1 test installation (gateway + scheduler certs, revocations, TrustStore)."""

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from custom_messages.wire.encoding import b64url, format_timestamp
    from custom_messages.wire.trust import AuthorityKey, TrustStore, sign_state
    from custom_transport.protocol1.host_keys import HostKeys
    from custom_transport.protocol1.identity import HostIdentity

    now = datetime.now(timezone.utc)
    auth_key = Ed25519PrivateKey.generate()
    store = TrustStore(
        TEST_INSTALLATION,
        [AuthorityKey(TEST_INSTALLATION, "inst-auth-1", auth_key.public_key())],
        partition_count=16,
    )

    def _sign_authority_record(record_type: str, key: str, body: dict[str, Any], *, sequence: int) -> bytes:
        doc = {
            "protocol": "1.0-pre",
            "record": record_type,
            "key": key,
            "sequence": sequence,
            "issued_at": format_timestamp(now - timedelta(minutes=5)),
            "writer": {
                "type": "authority",
                "installation": TEST_INSTALLATION,
                "key_id": "inst-auth-1",
            },
            "body": body,
        }
        signed = sign_state(doc, auth_key)
        return json.dumps(signed, separators=(",", ":")).encode("utf-8")

    def _make_host(runtime: str, roles: list[str], *, teams: list[str] | None = None) -> HostIdentity:
        app_key = Ed25519PrivateKey.generate()
        app_pub = b64url(app_key.public_key().public_bytes_raw())
        key_id = f"k-{runtime}-1"
        cert_body = {
            "certificate_id": f"cert-{runtime}-1",
            "installation": TEST_INSTALLATION,
            "runtime": runtime,
            "membership_epoch": 1,
            "roles": roles,
            "teams": teams or [TEST_TEAM, "team-other", "team_other", "team", "team_control", "test_team", "team_test", "another_team", "default_team", "team-alpha", "team-beta"],
            "agent_types": [],
            "session_limit": 0,
            "revocation_epoch": 1,
            "application_key": {"key_id": key_id, "public_key": app_pub},
            "nats_public_key": "U" + ("A" * 55),
            "connection_class": "connected",
            "protocol": "1.0-pre",
            "deployment": "local",
            "profile_sha256": "A" * 43,
            "issued_at": format_timestamp(now - timedelta(hours=1)),
            "expires_at": format_timestamp(now + timedelta(days=30)),
            "software": {"name": "universal-agents-test", "version": "0.10.0"},
        }
        raw_cert = _sign_authority_record(
            "certificate",
            f"host.{TEST_INSTALLATION}.{runtime}.1",
            cert_body,
            sequence=1,
        )
        store.add(raw_cert, now=now)
        cert_obj = store.host_certificate(TEST_INSTALLATION, runtime, 1)
        assert cert_obj is not None
        keys = HostKeys(runtime, key_id, app_key, b"0" * 32)
        return HostIdentity(keys, cert_obj)

    gw_identity = _make_host("gateway-test-1", ["gateway"])
    sched_identity = _make_host("team-services-test-1", ["team_services", "scheduler"])
    rev_body = {
        "installation": TEST_INSTALLATION,
        "revocation_epoch": 1,
        "expires_at": format_timestamp(now + timedelta(hours=23)),
        "entries": [],
    }
    raw_rev = _sign_authority_record("revocations", f"revocations.{TEST_INSTALLATION}", rev_body, sequence=1)
    store.add(raw_rev, now=now)
    people = _InMemoryBrokerPeople()
    people.register(TEST_TEAM, "user-1", roles=("member",))
    return {
        "store": store,
        "gateway": gw_identity,
        "scheduler": sched_identity,
        "people": people,
        "make_host": _make_host,
        "extra_hosts": {},
    }


def broker_trust_store() -> Any:
    return _protocol1_test_authority()["store"]


def broker_people_store() -> _InMemoryBrokerPeople:
    return _protocol1_test_authority()["people"]


def _host_for_team(kind: str, team_id: str) -> Any:
    auth = _protocol1_test_authority()
    default_host = auth[kind]
    if team_id in default_host.certificate.teams:
        return default_host
    cache_key = (kind, team_id)
    extra = auth["extra_hosts"]
    if cache_key not in extra:
        roles = ["gateway"] if kind == "gateway" else ["team_services", "scheduler"]
        runtime = f"{kind}-extra-{len(extra) + 1}"
        extra[cache_key] = auth["make_host"](runtime, roles, teams=[team_id])
    return extra[cache_key]


def broker_gateway_identity(team_id: str = TEST_TEAM) -> Any:
    return _host_for_team("gateway", team_id)


def broker_scheduler_identity(team_id: str = TEST_TEAM) -> Any:
    return _host_for_team("scheduler", team_id)


def broker_authority_verifier(*, people: Any = None) -> AuthorityTokenVerifier:
    return AuthorityTokenVerifier(
        broker_trust_store(),
        people=people if people is not None else broker_people_store(),
    )


def use_broker_authority_verifier(*, people: Any = None) -> AuthorityTokenVerifier:
    """Install the test protocol 1 authority verifier as the process verifier."""

    verifier = broker_authority_verifier(people=people)
    set_process_verifier(verifier)
    return verifier


def signed_task_token(
    person_id: str | None = "user-1",
    *,
    team_id: str = TEST_TEAM,
    roles: tuple[str, ...] = ("member",),
    root_task_id: str = "task-1",
    request_id: str | None = None,
    expires_at: datetime | None = None,
) -> Any:
    """Return a real protocol 1 gateway-signed root-task ``AuthorityTokenRecord``."""

    from custom_messages.wire.authority_bindings import AuthorityTokenRecord

    return AuthorityTokenRecord.model_validate(
        signed_task_context(
            person_id,
            team_id=team_id,
            roles=roles,
            root_task_id=root_task_id,
            request_id=request_id,
            expires_at=expires_at,
        )
    )


def signed_scheduler_token(
    person_id: str | None = "user-1",
    *,
    team_id: str = TEST_TEAM,
    roles: tuple[str, ...] = ("member",),
    root_task_id: str = "task-sched-1",
    request_id: str | None = None,
    schedule_id: str = "sched-1",
) -> Any:
    """Return a real protocol 1 scheduler-signed root-task ``AuthorityTokenRecord``."""

    from custom_messages.wire.authority_bindings import AuthorityTokenRecord

    return AuthorityTokenRecord.model_validate(
        signed_scheduler_task_context(
            person_id,
            team_id=team_id,
            roles=roles,
            root_task_id=root_task_id,
            request_id=request_id,
            schedule_id=schedule_id,
        )
    )


def signed_schedule_permit(
    schedule_id: str = "sched-1",
    *,
    person_id: str | None = "user-1",
    team_id: str = TEST_TEAM,
    roles: tuple[str, ...] = ("member",),
    owner_permit: bool = False,
) -> Any:
    """Return a real protocol 1 scheduler-signed permit ``AuthorityTokenRecord``."""

    from custom_messages.wire.authority_bindings import AuthorityTokenRecord
    from custom_transport.protocol1.authority_tokens import schedule_permit_token

    person_ref: str | None = None
    if person_id is not None:
        record = broker_people_store().register(team_id, person_id, roles=roles)
        person_ref = record.person_ref
    return AuthorityTokenRecord.model_validate(
        schedule_permit_token(
            broker_scheduler_identity(team_id),
            team=team_id,
            schedule_id=schedule_id,
            person_ref=person_ref,
            owner_permit=owner_permit,
            issued=datetime.now(timezone.utc),
        )
    )


class BrokerGatewayTokenSigner:
    """Test double implementing ``GatewayTokenSigner`` with the shared protocol 1 gateway cert."""

    def __init__(self, *, team_id: str = TEST_TEAM) -> None:
        self._default_team = team_id

    def sign_root_task_token(
        self,
        *,
        person_ref: str,
        root_task_id: str,
        request_id: str,
        team: str | None = None,
    ) -> Any:
        from custom_messages.wire.authority_bindings import AuthorityTokenRecord
        from custom_transport.protocol1.authority_tokens import root_task_token

        target_team = team or self._default_team
        return AuthorityTokenRecord.model_validate(
            root_task_token(
                broker_gateway_identity(target_team),
                team=target_team,
                root_task_id=root_task_id,
                request_id=request_id,
                person_ref=person_ref,
                issued=datetime.now(timezone.utc),
            )
        )


def signed_scheduler_task_context(
    person_id: str | None = "user-1",
    *,
    team_id: str = TEST_TEAM,
    roles: tuple[str, ...] = ("member",),
    root_task_id: str = "task-sched-1",
    request_id: str | None = None,
    schedule_id: str = "sched-1",
) -> dict[str, Any]:
    """Return a real protocol 1 scheduler-signed root-task authority token as JSON."""

    from custom_transport.protocol1.authority_tokens import root_task_token
    from id_generation import new_id

    person_ref: str | None = None
    if person_id is not None:
        record = broker_people_store().register(team_id, person_id, roles=roles)
        person_ref = record.person_ref
    return root_task_token(
        broker_scheduler_identity(team_id),
        team=team_id,
        root_task_id=root_task_id,
        request_id=request_id or new_id("task_context"),
        person_ref=person_ref,
        schedule_id=schedule_id,
        issued=datetime.now(timezone.utc),
    )


@lru_cache(maxsize=1)
def _gateway_key() -> tuple[str, str]:
    private, public = generate_signing_key(ISSUER_GATEWAY)
    return private, verify_keys_value([public])


def context_signer() -> ContextSigner:
    return ContextSigner.from_value(ISSUER_GATEWAY, _gateway_key()[0])


def context_verify_keys() -> str:
    """The public key value for ``ACCESS_CONTEXT_VERIFY_KEYS``."""

    return _gateway_key()[1]


@lru_cache(maxsize=1)
def _scheduler_key() -> tuple[str, dict[str, str]]:
    return generate_signing_key(ISSUER_SCHEDULER)


def use_scheduler_key(monkeypatch: Any) -> None:
    """Let this process sign as the scheduler (as the manager does) and trust both test keys.

    Call ``forget_task_context_keys`` after the test: the signer, the verifier
    and the access services are cached per process.
    """

    private, public = _scheduler_key()
    monkeypatch.setenv(ENV_SCHEDULER_SIGNING_KEY, private)
    monkeypatch.setenv(ENV_VERIFY_KEYS, verify_keys_value([*json.loads(context_verify_keys()), public]))
    forget_task_context_keys()


def use_gateway_key(monkeypatch: Any) -> None:
    """Let this process sign as the gateway (``UserGateway`` reads its key at start) and trust it.

    For tests that run a real gateway against a manager over NATS: the manager
    takes a command's person only from the gateway-signed context. Call
    ``forget_task_context_keys`` after the test.
    """

    monkeypatch.setenv(ENV_GATEWAY_SIGNING_KEY, _gateway_key()[0])
    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    forget_task_context_keys()


def forget_task_context_keys() -> None:
    """Drop cached signers, process verifier, and access services between tests."""

    from custom_agents import access_runtime, task_context_runtime

    task_context_runtime._scheduler_signer.cache_clear()
    access_runtime._runtime_access.cache_clear()
    set_process_verifier(None)


def caller_token(label: str = CALLER_MANAGER, *, root: str = BROKER_ROOT) -> str:
    return derive_caller_token(root, label)


def caller_headers(label: str = CALLER_MANAGER, *, root: str = BROKER_ROOT) -> dict[str, str]:
    return {"Authorization": f"Bearer {caller_token(label, root=root)}"}


def gateway_headers(*, root: str = BROKER_ROOT) -> dict[str, str]:
    return caller_headers(CALLER_GATEWAY, root=root)


def signed_task_context(
    person_id: str | None = "user-1",
    *,
    team_id: str = TEST_TEAM,
    roles: tuple[str, ...] = ("member",),
    root_task_id: str = "task-1",
    request_id: str | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a real protocol 1 gateway-signed root-task authority token as JSON."""

    from custom_transport.protocol1.authority_tokens import root_task_token
    from id_generation import new_id

    person_ref: str | None = None
    if person_id is not None:
        record = broker_people_store().register(team_id, person_id, roles=roles)
        person_ref = record.person_ref
    return root_task_token(
        broker_gateway_identity(team_id),
        team=team_id,
        root_task_id=root_task_id,
        request_id=request_id or new_id("task_context"),
        person_ref=person_ref,
        issued=datetime.now(timezone.utc),
        expires_at=expires_at,
    )


def agent_context(
    person_id: str = "user-1",
    *,
    team_id: str = TEST_TEAM,
    agent_id: str = TEST_AGENT_ID,
    named_agent_type: str = TEST_AGENT_TYPE,
    roles: tuple[str, ...] = ("member",),
    **extra: Any,
) -> dict[str, Any]:
    """A broker request context for an agent task working for ``person_id``."""

    return {
        "canonical_user_id": person_id,
        "principal_type": "user",
        "team_id": team_id,
        "agent_id": agent_id,
        "named_agent_type": named_agent_type,
        "task_context": signed_task_context(person_id, team_id=team_id, roles=roles),
        **extra,
    }


def open_gate(
    *, team_id: str = TEST_TEAM, instance_root: str | Path | None = None
) -> BrokerAccessGate:
    """A gate with authorization disabled: protocol 1 authority-token identity and catalog checks only."""

    return BrokerAccessGate(
        catalogs=CatalogSource(instance_root),
        verifier=broker_authority_verifier(),
        team_id=team_id,
        access=None,
    )


def access_store(path: str | Path) -> AccessRepository:
    """A SQLite access store (grants and account links) at ``path``."""

    return AccessRepository(resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(path)}}))


def linked_gate(
    links: AccessRepository, *, team_id: str = TEST_TEAM, instance_root: str | Path | None = None
) -> BrokerAccessGate:
    """An open gate (authorization disabled) that finds account links in ``links``."""

    gate = open_gate(team_id=team_id, instance_root=instance_root)
    gate.links = links
    return gate


def account_link(
    tool_id: str,
    credential_key: str,
    *,
    owner_kind: LinkOwnerKind = LinkOwnerKind.PERSON,
    owner_id: str = "user-1",
    team_id: str = TEST_TEAM,
    injections: tuple[CredentialInjectionV1, ...] | None = None,
) -> AccountLinkV1:
    """An account link: which stored credential ``tool_id`` uses for a person or the team."""

    resolved_injections = injections
    if resolved_injections is None:
        tool = CatalogSource(None).current().tools.get(tool_id)
        resolved_injections = tool.credential_injections if tool is not None else ()
    return AccountLinkV1(
        link_id=f"account_link_{owner_kind.value}_{owner_id}_{tool_id.replace(':', '_')}_{credential_key}",
        team_id=team_id,
        tool_id=tool_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        credential_key=credential_key,
        injections=resolved_injections,
        created_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
        created_at_utc=datetime.now(timezone.utc),
    )


def store_account_link(links: AccessRepository, tool_id: str, credential_key: str, **kwargs: Any) -> AccountLinkV1:
    """Store an account link (``account_link`` arguments) and return it."""

    return links.create_link(account_link(tool_id, credential_key, **kwargs))


def team_sharing(links: AccessRepository, *, team_id: str = TEST_TEAM) -> TeamAccountSharing:
    """The team's sharing choices in ``links`` (``access_control.account_sharing``)."""

    return TeamAccountSharing(links, team_id)


def choose_sharing(links: AccessRepository, tool_id: str, sharing: AccountSharing, *, team_id: str = TEST_TEAM) -> None:
    """Record an administrator's sharing choice for a shipped tool."""

    tool = CatalogSource(None).current().tools[tool_id]
    team_sharing(links, team_id=team_id).choose_tool(tool, sharing, by="admin")
