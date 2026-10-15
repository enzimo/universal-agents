"""Shared helpers for tests that call the secure tool broker.

The broker accepts only per-process caller tokens derived from its root secret
(``access_control.caller_tokens``) and trusts who a task works for only from a
signed task context (``access_control.identity``). These helpers give tests one
fixed root, one test signing key, and ready-made request contexts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from access_catalog.catalog import CatalogSource
from access_control.account_sharing import TeamAccountSharing
from access_control.caller_tokens import CALLER_GATEWAY, CALLER_MANAGER, derive_caller_token
from access_control.identity import (
    ENV_GATEWAY_SIGNING_KEY,
    ENV_SCHEDULER_SIGNING_KEY,
    ENV_VERIFY_KEYS,
    ISSUER_GATEWAY,
    ISSUER_SCHEDULER,
    ContextSigner,
    ContextVerifier,
    generate_signing_key,
    verify_keys_value,
)
from custom_messages.contracts.access_v2 import (
    AccountLinkV1,
    AccountSharing,
    CredentialInjectionV1,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
)
from secure_tool_broker.access_gate import BrokerAccessGate
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

BROKER_ROOT = "test-token"
TEST_TEAM = "team-1"
TEST_AGENT_ID = "agent-1"
TEST_AGENT_TYPE = "GeneralAssistant"


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
    """Drop the cached scheduler signer, verifier and access services so keys are read again."""

    from custom_agents import access_runtime, task_context_runtime

    task_context_runtime._scheduler_signer.cache_clear()
    task_context_runtime._verifier.cache_clear()
    access_runtime._runtime_access.cache_clear()


def caller_token(label: str = CALLER_MANAGER, *, root: str = BROKER_ROOT) -> str:
    return derive_caller_token(root, label)


def caller_headers(label: str = CALLER_MANAGER, *, root: str = BROKER_ROOT) -> dict[str, str]:
    return {"Authorization": f"Bearer {caller_token(label, root=root)}"}


def gateway_headers(*, root: str = BROKER_ROOT) -> dict[str, str]:
    return caller_headers(CALLER_GATEWAY, root=root)


def signed_task_context(
    person_id: str | None = "user-1", *, team_id: str = TEST_TEAM, roles: tuple[str, ...] = ("member",)
) -> dict[str, Any]:
    return context_signer().issue(team_id=team_id, person_id=person_id, person_roles=roles).model_dump(mode="json")


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
    """A gate with authorization disabled: identity and catalog checks only."""

    return BrokerAccessGate(
        catalogs=CatalogSource(instance_root),
        verifier=ContextVerifier.from_value(context_verify_keys()),
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
    injections: tuple[CredentialInjectionV1, ...] = (),
) -> AccountLinkV1:
    """An account link: which stored credential ``tool_id`` uses for a person or the team."""

    return AccountLinkV1(
        link_id=f"account_link_{owner_kind.value}_{owner_id}_{tool_id.replace(':', '_')}_{credential_key}",
        team_id=team_id,
        tool_id=tool_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        credential_key=credential_key,
        injections=injections,
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
