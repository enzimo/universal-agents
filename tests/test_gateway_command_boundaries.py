from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_gateway.application.person_setup import PersonSetupChoices
from assistant_gateway.application.broker_sign_in import GatewayBrokerSignIns
from assistant_gateway.application.commands import (
    AccountLinkCommandHandler,
    AccountLinkCommandRequest,
    AuthenticationCommandHandler,
    AuthenticationCommandRequest,
    GatewayActorContext,
    IdentityCommandHandler,
    IdentityCommandRequest,
    ManagerControlCommandRequest,
    MemoryTopicCommandHandler,
    MemoryTopicCommandRequest,
    MessageSubmissionCommandRequest,
    SessionCommandResult,
    is_session_command,
    tour_start_request_text,
)
from assistant_gateway.application.interactive_commands import (
    GatewayCommandRequest,
    GatewayCommandService,
    GatewayCommandSettings,
)
from assistant_gateway.application.manager_control_command_service import (
    GatewayManagerControlCommandService,
)
from assistant_gateway.application.website_application import (
    WebHostedMCPServerListV1,
    WebMCPConnectionListV1,
    WebMCPLifecycleResultV1,
)
from assistant_gateway.application.message_submission_command_service import (
    GatewayMessageSubmissionCommandService,
)
from custom_messages.contracts.control_v1 import ManagerRuntimeWorkCommand, RuntimeObjectiveSummary

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMAND_ROOT = REPO_ROOT / "assistant_gateway" / "application" / "commands"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(str(node.module or ""))
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def test_extracted_command_handlers_are_runtime_and_infrastructure_neutral() -> None:
    forbidden_prefixes = (
        "assistant_gateway.config",
        "assistant_gateway.gateway",
        "assistant_gateway.interactive_apps",
        "assistant_gateway.ports.repositories",
        "assistant_gateway.store",
        "custom_agents",
        "custom_messages",
        "custom_transport",
        "storage",
    )

    violations: list[tuple[str, str]] = []
    for path in COMMAND_ROOT.glob("*.py"):
        for imported in _imported_modules(path):
            if imported.startswith(forbidden_prefixes):
                violations.append((path.name, imported))

    assert violations == []


def test_dispatcher_has_no_gateway_shaped_runtime_port() -> None:
    path = REPO_ROOT / "assistant_gateway" / "application" / "interactive_commands.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_names = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    function_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    async_function_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
    }

    assert "_handle_auth_command" not in async_function_names
    assert "_handle_user_command" not in function_names
    assert "GatewayCommandRuntimePort" not in class_names


def test_residual_dispatcher_has_no_session_persistence_or_lifecycle_surface() -> None:
    path = REPO_ROOT / "assistant_gateway" / "application" / "interactive_commands.py"
    assert "assistant_gateway.ports.repositories" not in _imported_modules(path)
    assert "assistant_gateway.gateway" not in _imported_modules(path)


def test_operational_control_application_has_no_runtime_host_or_adapter_imports() -> None:
    path = REPO_ROOT / "assistant_gateway/application/operational_control.py"
    forbidden_prefixes = (
        "assistant_gateway.config",
        "assistant_gateway.gateway",
        "assistant_gateway.http",
        "assistant_gateway.nats",
        "assistant_gateway.store",
        "custom_transport",
        "storage",
    )

    assert not any(
        imported.startswith(forbidden_prefixes)
        for imported in _imported_modules(path)
    )


def test_user_gateway_does_not_reintroduce_operational_control_delegates() -> None:
    path = REPO_ROOT / "assistant_gateway/gateway.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    gateway_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "UserGateway"
    )
    methods = {
        node.name: node
        for node in gateway_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert {
        "_publish_operational_envelope",
        "send_stop_request",
        "send_context_debug_dump_control",
    }.isdisjoint(methods)


def test_dispatcher_does_not_own_command_domains() -> None:
    path = REPO_ROOT / "assistant_gateway" / "application" / "interactive_commands.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    async_function_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
    }
    assert "_current_owner_canonical_user_id" not in function_names
    assert "tour_start_request_text" not in function_names
    assert "_handle_permissions_command" not in async_function_names
    assert "/permissions" not in path.read_text(encoding="utf-8"), "the old /permissions command is gone"
    assert "json" not in _imported_modules(path)
    assert "shlex" not in _imported_modules(path)
    assert "time_presentation" not in _imported_modules(path)


def test_session_command_classifier_excludes_other_application_domains() -> None:
    assert is_session_command("/new") is True
    assert is_session_command("/list_sessions 8-14") is True
    assert is_session_command("/switch_conversation 2") is True
    assert is_session_command("/new unexpected") is False
    assert is_session_command("/list_conversations unexpected") is False
    assert is_session_command("/stop") is False
    assert is_session_command("/tour") is False
    assert is_session_command("/permissions") is False


class _SessionCommands:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return SessionCommandResult("session_selected", "sessions-rendered")


@pytest.mark.asyncio
async def test_dispatcher_delegates_session_commands_with_typed_actor_context() -> None:
    session_commands = _SessionCommands()
    service = GatewayCommandService(
        settings=GatewayCommandSettings(docs_base_url="https://agents.example.test"),
        authentication_commands=object(),  # type: ignore[arg-type]
        account_link_commands=object(),  # type: ignore[arg-type]
        account_sharing_commands=object(),  # type: ignore[arg-type]
        access_commands=object(),  # type: ignore[arg-type]
        identity_commands=object(),  # type: ignore[arg-type]
        memory_topic_commands=object(),  # type: ignore[arg-type]
        message_submission_commands=object(),  # type: ignore[arg-type]
        manager_control_commands=object(),  # type: ignore[arg-type]
        session_commands=session_commands,  # type: ignore[arg-type]
    )

    result = await service.execute(
        GatewayCommandRequest(
            raw_text="/list_sessions 2-4",
            current_session_key="session_original",
            channel_name="telegram",
            user_id="tg_alice",
            chat_id="chat_1",
            thread_id="thread_1",
            canonical_user_id="usr_alice",
            enable_debug_commands=False,
        )
    )

    assert result.handled is True
    assert result.session_key == "session_selected"
    assert result.text == "sessions-rendered"
    assert len(session_commands.requests) == 1
    request = session_commands.requests[0]
    assert request.command == "/list_sessions 2-4"
    assert request.current_session_key == "session_original"
    assert request.debug_commands_enabled is False
    assert request.actor == GatewayActorContext(
        channel_name="telegram",
        user_id="tg_alice",
        chat_id="chat_1",
        thread_id="thread_1",
        canonical_user_id="usr_alice",
    )


class _AccountLinkCommands:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return "account-link-rendered"


@pytest.mark.asyncio
async def test_dispatcher_delegates_link_command_with_typed_actor_context() -> None:
    account_link_commands = _AccountLinkCommands()
    service = GatewayCommandService(
        settings=GatewayCommandSettings(docs_base_url="https://agents.example.test"),
        authentication_commands=object(),  # type: ignore[arg-type]
        account_link_commands=account_link_commands,  # type: ignore[arg-type]
        account_sharing_commands=object(),  # type: ignore[arg-type]
        access_commands=object(),  # type: ignore[arg-type]
        identity_commands=object(),  # type: ignore[arg-type]
        memory_topic_commands=object(),  # type: ignore[arg-type]
        message_submission_commands=object(),  # type: ignore[arg-type]
        manager_control_commands=object(),  # type: ignore[arg-type]
        session_commands=object(),  # type: ignore[arg-type]
    )

    result = await service.execute(
        GatewayCommandRequest(
            raw_text="/link http:portainer team",
            current_session_key="session_original",
            channel_name="telegram",
            user_id="tg_alice",
            chat_id="chat_1",
            canonical_user_id="usr_alice",
        )
    )

    assert result.handled is True
    assert result.session_key == "session_original"
    assert result.text == "account-link-rendered"
    (request,) = account_link_commands.requests
    assert request.command == "/link http:portainer team"
    assert request.actor == GatewayActorContext(
        channel_name="telegram",
        user_id="tg_alice",
        chat_id="chat_1",
        canonical_user_id="usr_alice",
    )


@pytest.mark.asyncio
async def test_dispatcher_delegates_accounts_command_with_typed_actor_context() -> None:
    account_sharing_commands = _AccountLinkCommands()
    service = GatewayCommandService(
        settings=GatewayCommandSettings(docs_base_url="https://agents.example.test"),
        authentication_commands=object(),  # type: ignore[arg-type]
        account_link_commands=object(),  # type: ignore[arg-type]
        account_sharing_commands=account_sharing_commands,  # type: ignore[arg-type]
        access_commands=object(),  # type: ignore[arg-type]
        identity_commands=object(),  # type: ignore[arg-type]
        memory_topic_commands=object(),  # type: ignore[arg-type]
        message_submission_commands=object(),  # type: ignore[arg-type]
        manager_control_commands=object(),  # type: ignore[arg-type]
        session_commands=object(),  # type: ignore[arg-type]
    )

    result = await service.execute(
        GatewayCommandRequest(
            raw_text="/accounts private http:exa",
            current_session_key="session_original",
            channel_name="telegram",
            user_id="tg_alice",
            chat_id="chat_1",
            canonical_user_id="usr_alice",
        )
    )

    assert result.handled is True and result.text == "account-link-rendered"
    (request,) = account_sharing_commands.requests
    assert request.command == "/accounts private http:exa"
    assert request.actor.canonical_user_id == "usr_alice"


class _MessageSubmissionOperations:
    def __init__(self) -> None:
        self.resolved = []
        self.submitted = []

    def canonical_user_id_for_context(self, **_context):
        return "usr_resolved"

    def resolve_session(self, **request):
        self.resolved.append(request)
        return SimpleNamespace(session_key="session_tour")

    async def send_user_message(self, text, **request):
        self.submitted.append({"text": text, **request})
        return SimpleNamespace(text="Tour started.")


@pytest.mark.asyncio
async def test_message_submission_command_preserves_actor_and_query_handler() -> None:
    operations = _MessageSubmissionOperations()
    service = GatewayMessageSubmissionCommandService(operations)  # type: ignore[arg-type]
    query_handler = object()

    result = await service.execute(
        MessageSubmissionCommandRequest(
            command="/tour",
            current_session_key="session_original",
            actor=GatewayActorContext(
                channel_name="cli",
                user_id="local-user",
                chat_id="local-chat",
                canonical_user_id="usr_explicit",
            ),
            query_handler=query_handler,
        )
    )

    assert result.session_key == "session_tour"
    assert result.text == "Tour started."
    assert operations.resolved == [
        {
            "session_key": "session_original",
            "channel_name": "cli",
            "user_id": "local-user",
            "chat_id": "local-chat",
            "thread_id": None,
            "canonical_user_id": "usr_explicit",
        }
    ]
    assert operations.submitted == [
        {
            "text": tour_start_request_text(),
            "session_key": "session_tour",
            "channel_name": "cli",
            "user_id": "local-user",
            "chat_id": "local-chat",
            "thread_id": None,
            "query_handler": query_handler,
            "canonical_user_id": "usr_explicit",
        }
    ]


class _ManagerControlOperations:
    def __init__(self) -> None:
        self.stop_requests = []

    async def stop(self, request):
        self.stop_requests.append(request)
        if request.action == "list_objectives":
            return SimpleNamespace(
                text="Objectives listed.",
                raw_envelope=SimpleNamespace(
                    msg_content=ManagerRuntimeWorkCommand(
                        action="list_objectives",
                        scope="objective",
                        objectives=[
                            RuntimeObjectiveSummary(
                                objective_id="objective_1",
                                record_digest="a" * 64,
                                title="Test Objective",
                                desired_outcome="Verify exact command routing.",
                                status="active",
                                horizon="finite",
                                priority="normal",
                                updated_at="2026-08-24T12:00:00Z",
                            )
                        ],
                    )
                ),
            )
        return SimpleNamespace(text="Stopped active task.")


@pytest.mark.asyncio
async def test_manager_control_command_forwards_typed_actor_route() -> None:
    operations = _ManagerControlOperations()
    service = GatewayManagerControlCommandService(operations)  # type: ignore[arg-type]

    result = await service.execute(
        ManagerControlCommandRequest(
            command="/stop",
            current_session_key="session_active",
            actor=GatewayActorContext(
                channel_name="website",
                user_id="browser_1",
                chat_id="web-chat",
                thread_id="thread_1",
            ),
        )
    )

    assert result.session_key == "session_active"
    assert result.text == "Stopped active task."
    assert len(operations.stop_requests) == 1
    stop_request = operations.stop_requests[0]
    assert stop_request.session_key == "session_active"
    assert stop_request.route.channel_name == "website"
    assert stop_request.route.user_id == "browser_1"
    assert stop_request.route.chat_id == "web-chat"
    assert stop_request.route.thread_id == "thread_1"
    assert stop_request.route.canonical_user_id is None
    assert stop_request.action == "inventory"
    assert stop_request.scope == "user"


@pytest.mark.asyncio
async def test_manager_control_command_parses_exact_stop_and_objective_scopes() -> None:
    operations = _ManagerControlOperations()
    service = GatewayManagerControlCommandService(operations)  # type: ignore[arg-type]
    actor = GatewayActorContext(canonical_user_id="usr_alice")

    for command in (
        "/stop task task_1",
        "/stop conversation",
        "/stop session",
        "/stop all",
        "/pause objective objective_1",
        "/resume objective objective_1",
    ):
        await service.execute(
            ManagerControlCommandRequest(
                command=command,
                current_session_key="session_active",
                actor=actor,
            )
        )

    parsed = [
        request
        for request in operations.stop_requests
        if request.action != "list_objectives"
    ]
    assert [request.action for request in parsed] == [
        "cancel",
        "cancel",
        "cancel",
        "cancel",
        "pause_objective",
        "resume_objective",
    ]
    assert [request.scope for request in parsed] == [
        "task",
        "conversation",
        "session",
        "user",
        "objective",
        "objective",
    ]
    assert parsed[0].target_task_id == "task_1"
    assert parsed[-1].target_objective_id == "objective_1"
    assert parsed[-1].expected_objective_digest == "a" * 64


@pytest.mark.asyncio
async def test_mcp_command_uses_current_exact_digest_and_authenticated_context() -> None:
    class _MCPControls:
        def __init__(self) -> None:
            self.requests = []

        def list_web_chat_mcp_connections(self, _request):
            return WebMCPConnectionListV1.model_validate(
                {
                    "connections": [
                        {
                            "connection_id": "connection_exact",
                            "record_digest": "c" * 64,
                            "lifecycle": "active",
                            "connection_name": "reviewed-api",
                            "transport": "streamable_http",
                            "source": "agent_proposal",
                            "source_locator": "agent:proposal",
                            "tool_name_prefix": "reviewed",
                            "target_agent_types": ["TeamArchitect"],
                            "tool_id": "mcp:reviewed-api",
                            "operation_count": 1,
                            "operations": ["search"],
                            "credential_mode": "none",
                            "legacy_sse": False,
                            "from_operator_configuration": False,
                            "created_at_utc": "2026-08-24T12:00:00Z",
                            "updated_at_utc": "2026-08-24T12:00:00Z",
                            "can_decide": False,
                            "available_actions": ["disable"],
                        }
                    ]
                }
            )

        def list_web_chat_hosted_mcp_servers(self, _request):
            return WebHostedMCPServerListV1(servers=())

        async def control_web_chat_mcp_lifecycle(self, request):
            self.requests.append(request)
            return WebMCPLifecycleResultV1(
                resource_kind=request.resource_kind,
                resource_id=request.resource_id,
                action=request.action,
                status="disabled",
                request_digest=request.expected_digest,
                result_digest="d" * 64,
            )

    mcp = _MCPControls()
    service = GatewayManagerControlCommandService(
        _ManagerControlOperations(),  # type: ignore[arg-type]
        mcp_controls=mcp,
    )
    result = await service.execute(
        ManagerControlCommandRequest(
            command="/mcp connection disable connection_exact",
            current_session_key="session_active",
            actor=GatewayActorContext(
                channel_name="website",
                user_id="usr_alice",
                chat_id="web-chat",
                canonical_user_id="usr_alice",
            ),
        )
    )

    assert result.text == "MCP connection connection_exact is now disabled."
    assert len(mcp.requests) == 1
    request = mcp.requests[0]
    assert request.expected_digest == "c" * 64
    assert request.expected_lifecycle == "active"
    assert request.context.canonical_user_id == "usr_alice"
    assert request.context.session_key == "session_active"


class _AuthenticationIdentity:
    def __init__(self, roles: tuple[str, ...] = ("member",)) -> None:
        self.contexts: list[dict[str, str | None]] = []
        self.roles = roles

    def canonical_user_id_for_context(self, **context: str | None) -> str:
        self.contexts.append(context)
        return "usr_alice"

    def team_roles_for_person(self, canonical_user_id: str) -> tuple[str, ...]:
        return self.roles


class _SecureBroker:
    auth_unavailable_message = None

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def user_form_url(self, internal_url: str) -> str:
        return internal_url.replace("broker.internal", "agents.example.test")

    async def create_auth_session(self, **request: object) -> dict[str, object]:
        self.requests.append(request)
        return {
            "method": "web",
            "login_url": "https://broker.internal/auth/github/v1.signed",
        }


class _LinkResults:
    """Records the pages /link asks the gateway to watch."""

    def __init__(self) -> None:
        self.sign_ins: list[dict[str, object]] = []

    def watch_account_link(self, **request: object) -> None:
        raise AssertionError("a sign-in never opens a link form")

    def watch_sign_in(self, **request: object) -> None:
        self.sign_ins.append(request)


@pytest.mark.asyncio
async def test_link_sign_in_resolves_actor_and_builds_trusted_context() -> None:
    """/link <sign-in tool> opens the tool's sign-in page for the person who typed it."""

    identity = _AuthenticationIdentity()
    broker = _SecureBroker()
    link_results = _LinkResults()
    handler = AccountLinkCommandHandler(
        identity=identity,
        account_links=None,  # type: ignore[arg-type] - a sign-in never opens a link form
        sign_ins=GatewayBrokerSignIns(secure_broker=broker, team_id="team_ua"),  # type: ignore[arg-type]
        link_results=link_results,
    )

    result = await handler.execute(
        AccountLinkCommandRequest(
            command="/link cli:github web",
            actor=GatewayActorContext(
                channel_name="telegram",
                user_id="tg_alice",
                chat_id="chat_1",
            ),
            session_key="session_telegram",
        )
    )

    assert identity.contexts == [
        {
            "channel_name": "telegram",
            "user_id": "tg_alice",
            "chat_id": "chat_1",
        }
    ]
    assert broker.requests == [
        {
            "provider": "github",
            "method": "web",
            "context": {
                "canonical_user_id": "usr_alice",
                "team_id": "team_ua",
                "principal_type": "user",
            },
            "options": None,
        }
    ]
    assert result.kind == "broker_authentication"
    assert result.label == "Open"
    assert result.url == "https://agents.example.test/auth/github/v1.signed"
    assert result.url not in result.description
    assert result.receipt == "GitHub sign-in page opened."
    # The gateway watches the page through the broker's internal address.
    [watch] = link_results.sign_ins
    assert watch["restart_command"] == "/link cli:github", "web is the default, so the command to repeat needs no method"
    assert watch["page"].status_url == "https://broker.internal/auth/github/v1.signed/status"
    assert watch["target"].session_key == "session_telegram"
    assert watch["target"].correlation_id.startswith("link_cmd")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "x"])
async def test_auth_team_client_vouches_for_the_persons_roles(provider: str) -> None:
    identity = _AuthenticationIdentity(roles=("admin",))
    broker = _SecureBroker()
    handler = AuthenticationCommandHandler(
        identity=identity,
        secure_broker=broker,  # type: ignore[arg-type]
        team_id="team_ua",
    )

    await handler.execute(
        AuthenticationCommandRequest(
            command=f"/auth {provider} team_client",
            actor=GatewayActorContext(channel_name="website", user_id="usr_alice", chat_id="web"),
        )
    )

    assert broker.requests == [
        {
            "provider": provider,
            "method": "team_client",
            "context": {
                "canonical_user_id": "usr_alice",
                "team_id": "team_ua",
                "principal_type": "user",
                "person_roles": ["admin"],
            },
            "options": None,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/auth github", "/auth github web", "/auth google", "/auth google credentials", "/auth x"])
async def test_auth_no_longer_signs_in_to_tools(command: str) -> None:
    """Tool sign-ins moved to /link; /auth names it instead of opening a page."""

    broker = _SecureBroker()
    handler = AuthenticationCommandHandler(
        identity=_AuthenticationIdentity(),
        secure_broker=broker,  # type: ignore[arg-type]
        team_id="team_ua",
    )

    result = await handler.execute(
        AuthenticationCommandRequest(
            command=command,
            actor=GatewayActorContext(channel_name="website", user_id="usr_alice", chat_id="web"),
        )
    )

    assert broker.requests == []
    assert "/link <tool_id>" in str(result)


class _IdentityOperations:
    def __init__(self) -> None:
        self.invites: list[dict[str, object]] = []

        self.registrations: list[dict[str, object]] = []
        self.team_keys: list[dict[str, object]] = []

    async def create_new_user_web_invite(self, **request: object) -> str:
        self.invites.append(request)
        return "invite-created"

    async def register_new_user_for_channel(self, **request: object) -> str:
        self.registrations.append(request)
        return "registered"

    def set_gateway_user_team_keys(self, **request: object) -> str:
        self.team_keys.append(request)
        return "team-keys-set"


@pytest.mark.asyncio
async def test_identity_handler_parses_invite_into_narrow_port_call() -> None:
    operations = _IdentityOperations()
    handler = IdentityCommandHandler(operations)  # type: ignore[arg-type]

    result = await handler.execute(
        IdentityCommandRequest(
            command='/user invite --new "Alice Example" --label "First Browser"',
            actor=GatewayActorContext(
                channel_name="web",
                user_id="web_alice",
                chat_id="chat_1",
                canonical_user_id="usr_admin",
            ),
            channel_binding_enabled=False,
        )
    )

    assert result == "invite-created"
    assert operations.invites == [
        {
            "target": None,
            "display_name": "Alice Example",
            "label": "First Browser",
            "create_new": True,
            "channel_name": "web",
            "user_id": "web_alice",
            "chat_id": "chat_1",
            "canonical_user_id": "usr_admin",
            "choices": PersonSetupChoices(),
        }
    ]


def _actor() -> GatewayActorContext:
    return GatewayActorContext(channel_name="telegram", user_id="42", chat_id="chat_1", canonical_user_id="usr_admin")


@pytest.mark.asyncio
async def test_adding_a_person_takes_the_model_key_and_shared_folder_choices() -> None:
    operations = _IdentityOperations()
    handler = IdentityCommandHandler(operations)  # type: ignore[arg-type]

    await handler.execute(IdentityCommandRequest(command='/user 123 "Alice" --own-keys --shared Research_1', actor=_actor()))
    await handler.execute(
        IdentityCommandRequest(command='/user invite --new "Bob" --team-keys --shared team/notes', actor=_actor())
    )

    registered = operations.registrations[0]
    assert (registered["target_user_id"], registered["display_name"]) == ("123", "Alice")
    assert registered["choices"] == PersonSetupChoices(team_model_keys=False, shared_folder="research_1")
    assert operations.invites[0]["choices"] == PersonSetupChoices(team_model_keys=True, shared_folder="notes")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('/user 123 "Alice" --team-keys --own-keys', "not both"),
        ('/user 123 "Alice" --shared "../etc"', "folder name"),
        ("/user status --team-keys", "apply only when adding"),
        ("/user team-keys usr_alice maybe", "Usage: /user team-keys"),
    ],
)
async def test_unusable_setup_options_say_what_to_type(command: str, expected: str) -> None:
    operations = _IdentityOperations()

    result = await IdentityCommandHandler(operations).execute(  # type: ignore[arg-type]
        IdentityCommandRequest(command=command, actor=_actor())
    )

    assert expected in result
    assert operations.registrations == [] and operations.team_keys == []


@pytest.mark.asyncio
async def test_team_keys_reaches_the_port_with_the_decision() -> None:
    operations = _IdentityOperations()

    result = await IdentityCommandHandler(operations).execute(  # type: ignore[arg-type]
        IdentityCommandRequest(command="/user team-keys usr_alice ON", actor=_actor())
    )

    assert result == "team-keys-set"
    assert (operations.team_keys[0]["target"], operations.team_keys[0]["allowed"]) == ("usr_alice", True)


class _MemoryTopicOperations:
    def __init__(self) -> None:
        self.topic_requests: list[dict[str, object]] = []

    async def execute_topic_command(self, **request: object) -> tuple[str, str]:
        self.topic_requests.append(request)
        return "session_topic", "topic-switched"


@pytest.mark.asyncio
async def test_memory_topic_handler_preserves_session_result_and_actor_context() -> (
    None
):
    operations = _MemoryTopicOperations()
    handler = MemoryTopicCommandHandler(operations)  # type: ignore[arg-type]

    result = await handler.execute(
        MemoryTopicCommandRequest(
            command="/switch_topic engineering",
            current_session_key="session_original",
            actor=GatewayActorContext(
                channel_name="discord",
                user_id="discord_alice",
                chat_id="channel_1",
                thread_id="thread_1",
            ),
        )
    )

    assert result.session_key == "session_topic"
    assert result.text == "topic-switched"
    assert operations.topic_requests == [
        {
            "command": "/switch_topic engineering",
            "channel_name": "discord",
            "user_id": "discord_alice",
            "chat_id": "channel_1",
            "thread_id": "thread_1",
            "session_key": "session_original",
        }
    ]


class _RoleAwareIdentity(_AuthenticationIdentity):
    def __init__(self, roles: tuple[str, ...]) -> None:
        super().__init__()
        self.roles = roles

    def team_roles_for_person(self, canonical_user_id: str) -> tuple[str, ...]:
        assert canonical_user_id == "usr_alice"
        return self.roles


class _RefusingBroker(_SecureBroker):
    async def create_auth_session(self, **request: object) -> dict[str, object]:
        from assistant_gateway.application.ports import GatewaySecureBrokerRefusal

        self.requests.append(request)
        raise GatewaySecureBrokerRefusal(
            "Only a team administrator or owner can set the team's Google sign-in app.",
            status=403,
            reason_code="google_team_client_admin_required",
        )


@pytest.mark.asyncio
async def test_team_google_client_sends_the_persons_roles_for_the_broker_to_decide() -> None:
    broker = _SecureBroker()
    handler = AuthenticationCommandHandler(
        identity=_RoleAwareIdentity(("admin",)),
        secure_broker=broker,  # type: ignore[arg-type]
        team_id="team_ua",
    )

    await handler.execute(
        AuthenticationCommandRequest(
            command="/auth google team_client",
            actor=GatewayActorContext(channel_name="website", user_id="u", chat_id="c", canonical_user_id="usr_alice"),
        )
    )

    assert broker.requests[0]["method"] == "team_client"
    assert broker.requests[0]["context"]["person_roles"] == ["admin"]


@pytest.mark.asyncio
async def test_a_refused_team_google_client_is_explained_in_plain_words() -> None:
    handler = AuthenticationCommandHandler(
        identity=_RoleAwareIdentity(("member",)),
        secure_broker=_RefusingBroker(),  # type: ignore[arg-type]
        team_id="team_ua",
    )

    result = await handler.execute(
        AuthenticationCommandRequest(
            command="/auth google team_client",
            actor=GatewayActorContext(channel_name="website", user_id="u", chat_id="c", canonical_user_id="usr_alice"),
        )
    )

    assert result == (
        "Could not start Google setup. Only a team administrator or owner can set the team's Google sign-in app."
    )
