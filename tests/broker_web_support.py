"""A running secure tool broker for tests of the web research tools.

The tools (Exa, Brave Search, the Crawl4AI screenshot) are synchronous and
call the broker over HTTP (``custom_tools.broker_web_requests``), so the real
broker app runs on a local port in its own thread. The broker uses the real
access gate, SQLite access store (account links), file-backed credential
catalog and, when asked, the real Cedar evaluator. Only the last hop is fake:
``credential_catalog._send_api_request`` records what the broker would send to
the service and returns a canned answer, so tests see the key the broker added.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from access_catalog.catalog import CatalogSource
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import agent_role_resolver
from access_control.identity import ContextVerifier
from custom_messages.contracts.access_v2 import AccountLinkV1, LinkOwnerKind, SubjectKind, SubjectRefV1
from secure_tool_broker import credential_catalog
from secure_tool_broker.access_gate import BrokerAccessGate
from secure_tool_broker.credential_catalog import (
    CatalogCredential,
    CatalogCredentialField,
    CatalogSourceRef,
    CredentialCatalog,
    save_catalog,
)
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.service import SecureToolBrokerService
from tests.broker_access_support import (
    BROKER_ROOT,
    TEST_TEAM,
    agent_context,
    caller_token,
    context_verify_keys,
    access_store,
    signed_task_context,
)

ROOT = Path(__file__).resolve().parents[1]
CEDAR_BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
LEAD_AGENT_TYPE = "TeamArchitect"


class FakeServiceResponse:
    """What ``_send_api_request`` returns: a status, headers and a readable body."""

    def __init__(self, payload: Any, *, status: int = 200) -> None:
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


@dataclass
class ServiceCall:
    method: str
    url: str
    headers: dict[str, str]
    body: Any


@dataclass
class RunningBroker:
    """The broker in a thread, what it sent to services, and what the tools sent to it."""

    gate: BrokerAccessGate
    base_url: str
    sent: list[ServiceCall] = field(default_factory=list)
    tool_requests: list[dict[str, Any]] = field(default_factory=list)
    answers: list[Callable[[ServiceCall], FakeServiceResponse]] = field(default_factory=list)

    def answer_with(self, payload: Any, *, status: int = 200) -> None:
        self.answers.append(lambda _call: FakeServiceResponse(payload, status=status))

    def store_credential(self, scope: CredentialOwnerScope, key: str, env_var: str, field_name: str) -> None:
        """Save a credential whose value the test put in ``env_var`` (the catalog's env source)."""

        from secure_tool_broker.credential_catalog import load_catalog

        credentials = dict(load_catalog(scope=scope).credentials)
        credentials[key] = CatalogCredential(
            display_name=key,
            purpose="Tests web research tools.",
            fields={
                field_name: CatalogCredentialField(
                    kind="token",
                    visibility="secret",
                    resolution_order=[CatalogSourceRef(source="env", env_var=env_var)],
                )
            },
        )
        save_catalog(CredentialCatalog(credentials=credentials), scope=scope)

    def link(self, tool_id: str, credential_key: str, *, person_id: str | None = None) -> AccountLinkV1:
        """Link a stored credential to ``tool_id`` for the team, or for ``person_id``."""

        tool = self.gate.catalog.tools[tool_id]
        owner_kind = LinkOwnerKind.PERSON if person_id else LinkOwnerKind.TEAM
        assert self.gate.links is not None
        from datetime import datetime, timezone

        return self.gate.links.create_link(
            AccountLinkV1(
                link_id=f"account_link_{owner_kind.value}_{tool_id.replace(':', '_')}_{credential_key}",
                team_id=TEST_TEAM,
                tool_id=tool_id,
                owner_kind=owner_kind,
                owner_id=person_id or TEST_TEAM,
                credential_key=credential_key,
                injections=tool.credential_injections,
                created_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
                created_at_utc=datetime.now(timezone.utc),
            )
        )


def fake_sealbox(monkeypatch: Any) -> dict[str, str]:
    """An in-memory Sealbox, the broker's default credential store; returns what it holds.

    ``secret set`` keeps the value sent on standard input and ``secret get``
    returns it, so a key stored when linking can be read back for a request.
    """

    import subprocess

    from secure_tool_broker.credential_sources import sealbox as sealbox_source

    held: dict[str, str] = {}

    def run(args: Any, *, input_text: str | None = None, env: Any = None, timeout_seconds: int = 30) -> Any:
        verb, key = list(args[:2]), args[2]
        if verb == ["secret", "set"]:
            held[key] = input_text or ""
            return subprocess.CompletedProcess(["sealbox-cli", *args], 0, stdout='{"version":1}\n', stderr="")
        if verb == ["secret", "get"] and key in held:
            return subprocess.CompletedProcess(["sealbox-cli", *args], 0, stdout=json.dumps({"value": held[key]}), stderr="")
        return subprocess.CompletedProcess(["sealbox-cli", *args], 1, stdout="", stderr=f"[SealboxError] secret not found: {key}")

    monkeypatch.delenv("SECURE_TOOL_BROKER_DEFAULT_CREDENTIAL_WRITE_SOURCE", raising=False)
    monkeypatch.setenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS", "0")
    monkeypatch.setattr(credential_catalog, "run_sealbox", run)
    monkeypatch.setattr(sealbox_source, "run_sealbox", run)
    monkeypatch.setattr(sealbox_source, "sealbox_tenant_environment", lambda **_kwargs: {})
    return held


def isolate_credentials(tmp_path: Path, monkeypatch: Any) -> None:
    """A file-backed credential catalog and state under ``tmp_path``; no Sealbox tenant."""

    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "credential_catalog.db"))
    monkeypatch.setattr(credential_catalog, "sealbox_tenant_environment", lambda **_kwargs: {})
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path / "broker_credentials"))


def build_gate(
    tmp_path: Path, *, authorization: bool = False, service_urls: dict[str, str] | None = None
) -> BrokerAccessGate:
    links = access_store(tmp_path / "access.db")
    catalogs = CatalogSource(None, service_urls=service_urls)
    access = None
    if authorization:
        access = AccessService(
            store=links,
            catalog=catalogs.current(),
            cedar=CedarAccessClient(binary=str(CEDAR_BINARY)),
            team_id=TEST_TEAM,
            agent_role_of=agent_role_resolver(LEAD_AGENT_TYPE),
        )
    return BrokerAccessGate(
        catalogs=catalogs,
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEST_TEAM,
        access=access,
        links=links,
    )


def start_broker(gate: BrokerAccessGate, monkeypatch: Any) -> tuple[RunningBroker, Callable[[], None]]:
    """Run the broker app on a free local port; returns it and a stop function."""

    loop = asyncio.new_event_loop()
    app = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate).build_app()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    broker = RunningBroker(gate=gate, base_url=f"http://127.0.0.1:{port}")

    def fake_send(request: Any, *, timeout: float) -> FakeServiceResponse:
        body = json.loads(request.data) if request.data else None
        call = ServiceCall(request.get_method(), request.full_url, dict(request.header_items()), body)
        broker.sent.append(call)
        answer = broker.answers.pop(0) if broker.answers else (lambda _call: FakeServiceResponse({}))
        return answer(call)

    monkeypatch.setattr(credential_catalog, "_send_api_request", fake_send)
    monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", broker.base_url)
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", caller_token())

    from custom_tools import broker_web_requests

    original_post = broker_web_requests._post

    def recording_post(url: str, body: Any, token: str, timeout: float) -> Any:
        broker.tool_requests.append(json.loads(json.dumps(body)))
        return original_post(url, body, token, timeout)

    monkeypatch.setattr(broker_web_requests, "_post", recording_post)

    def stop() -> None:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()
        if gate.access is not None:
            gate.access.cedar.close()

    return broker, stop


def work_for(monkeypatch: Any, person_id: str | None = "user-1", *, roles: tuple[str, ...] = ("member",)) -> None:
    """Make the tools' broker requests carry the signed context of a task for ``person_id`` (None: system work)."""

    if person_id is None:
        context = {
            "canonical_user_id": "",
            "principal_type": "system",
            "team_id": TEST_TEAM,
            "agent_id": "agent-1",
            "named_agent_type": "GeneralAssistant",
            "task_id": "task-system",
            "task_context": signed_task_context(None),
        }
    else:
        context = agent_context(person_id, roles=roles, task_id="task-1")
    monkeypatch.setattr(
        "custom_tools.secure_cli_tool.trusted_broker_context", lambda *, allow_system_work=False: dict(context)
    )


__all__ = [
    "CEDAR_BINARY",
    "FakeServiceResponse",
    "RunningBroker",
    "build_gate",
    "fake_sealbox",
    "isolate_credentials",
    "start_broker",
    "work_for",
]
