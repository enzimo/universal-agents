from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> list[ast.Import | ast.ImportFrom]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]


def test_gateway_domain_and_application_services_do_not_import_infrastructure() -> None:
    paths = [
        *(REPO_ROOT / "assistant_gateway" / "domain").glob("*.py"),
        REPO_ROOT / "assistant_gateway" / "ports" / "repositories.py",
        *(REPO_ROOT / "assistant_gateway" / "application").glob("*_service.py"),
    ]

    for path in paths:
        infrastructure_imports = [
            node
            for node in _imports(path)
            if (
                isinstance(node, ast.ImportFrom)
                and str(node.module or "").startswith(
                    (
                        "storage",
                        "custom_agents",
                        "custom_messages",
                        "assistant_gateway.gateway",
                    )
                )
            )
            or (
                isinstance(node, ast.Import)
                and any(
                    alias.name.startswith(
                        (
                            "storage",
                            "custom_agents",
                            "custom_messages",
                            "assistant_gateway.gateway",
                        )
                    )
                    for alias in node.names
                )
            )
        ]
        assert infrastructure_imports == [], path


def test_storage_backend_vocabulary_has_no_runtime_host_dependencies() -> None:
    backend_path = REPO_ROOT / "storage" / "backends.py"
    forbidden_prefixes = ("assistant_gateway", "custom_agents", "custom_messages")
    violations = [
        node
        for node in _imports(backend_path)
        if (
            isinstance(node, ast.ImportFrom)
            and str(node.module or "").startswith(forbidden_prefixes)
        )
        or (
            isinstance(node, ast.Import)
            and any(
                alias.name.startswith(forbidden_prefixes) for alias in node.names
            )
        )
    ]

    assert violations == []


def test_user_gateway_does_not_reach_through_state_store() -> None:
    gateway_path = REPO_ROOT / "assistant_gateway" / "gateway.py"
    tree = ast.parse(gateway_path.read_text(encoding="utf-8"))
    reach_through = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
        and node.value.attr == "state_store"
    ]

    assert reach_through == []


def test_user_gateway_does_not_own_transport_or_correlation_internals() -> None:
    gateway_path = REPO_ROOT / "assistant_gateway" / "gateway.py"
    tree = ast.parse(gateway_path.read_text(encoding="utf-8"))
    forbidden_imports = [
        node
        for node in _imports(gateway_path)
        if (
            isinstance(node, ast.ImportFrom)
            and str(node.module or "").startswith("custom_transport")
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name.startswith("custom_transport") for alias in node.names)
        )
    ]
    forbidden_attributes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr
        in {
            "_nats_adapter",
            "_pending_queues",
            "_pending_control_queues",
        }
    ]

    assert forbidden_imports == []
    assert forbidden_attributes == []


def test_manager_client_is_transport_and_infrastructure_neutral() -> None:
    client_path = REPO_ROOT / "assistant_gateway" / "application" / "manager_client.py"
    forbidden_prefixes = (
        "assistant_gateway.config",
        "assistant_gateway.gateway",
        "custom_transport",
        "storage",
    )
    violations = [
        node
        for node in _imports(client_path)
        if (
            isinstance(node, ast.ImportFrom)
            and str(node.module or "").startswith(forbidden_prefixes)
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name.startswith(forbidden_prefixes) for alias in node.names)
        )
    ]

    assert violations == []


def test_gateway_orchestration_modules_are_infrastructure_neutral() -> None:
    application_paths = [
        REPO_ROOT / "assistant_gateway" / "application" / "response_coordinator.py",
        REPO_ROOT / "assistant_gateway" / "application" / "credential_workflow.py",
        REPO_ROOT / "assistant_gateway" / "application" / "activity_notices.py",
    ]
    forbidden_prefixes = (
        "assistant_gateway.config",
        "assistant_gateway.gateway",
        "assistant_gateway.http_",
        "aiohttp",
        "custom_agents",
        "storage",
    )

    for path in application_paths:
        violations = [
            node
            for node in _imports(path)
            if (
                isinstance(node, ast.ImportFrom)
                and str(node.module or "").startswith(forbidden_prefixes)
            )
            or (
                isinstance(node, ast.Import)
                and any(
                    alias.name.startswith(forbidden_prefixes) for alias in node.names
                )
            )
        ]
        assert violations == [], path


def test_gateway_http_adapters_cannot_access_gateway_state_or_transport() -> None:
    adapter_paths = [
        REPO_ROOT / "assistant_gateway" / "http_secure_broker.py",
        REPO_ROOT / "assistant_gateway" / "http_activity_source.py",
    ]
    forbidden_prefixes = (
        "assistant_gateway.gateway",
        "assistant_gateway.ports.repositories",
        "assistant_gateway.store",
        "custom_agents",
        "custom_messages",
        "custom_transport",
        "storage",
    )

    for path in adapter_paths:
        violations = [
            node
            for node in _imports(path)
            if (
                isinstance(node, ast.ImportFrom)
                and str(node.module or "").startswith(forbidden_prefixes)
            )
            or (
                isinstance(node, ast.Import)
                and any(
                    alias.name.startswith(forbidden_prefixes) for alias in node.names
                )
            )
        ]
        assert violations == [], path


def test_user_gateway_has_no_extracted_orchestration_forwarders() -> None:
    gateway_path = REPO_ROOT / "assistant_gateway" / "gateway.py"
    tree = ast.parse(gateway_path.read_text(encoding="utf-8"))
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    removed_forwarders = {
        "_queue_manager_unavailable_response",
        "_resolve_stop_target",
        "_start_secure_credential_collection",
        "_execute_approved_signup_probe",
        "_poll_secure_credential_collection_session",
        "_publish_secure_credential_collection_result",
        "_await_terminal_response",
        "send_task_steering",
    }

    assert removed_forwarders.isdisjoint(methods)


def test_gateway_application_facades_stay_deleted() -> None:
    assert not (REPO_ROOT / "assistant_gateway" / "facade.py").exists()
    assert not (REPO_ROOT / "assistant_gateway" / "website_facade.py").exists()


def test_interactive_command_application_is_infrastructure_neutral() -> None:
    command_path = (
        REPO_ROOT / "assistant_gateway" / "application" / "interactive_commands.py"
    )
    tree = ast.parse(command_path.read_text(encoding="utf-8"))
    forbidden_prefixes = (
        "assistant_gateway.config",
        "assistant_gateway.gateway",
        "assistant_gateway.http_",
        "assistant_gateway.interactive_apps",
        "aiohttp",
        "custom_agents",
        "custom_transport",
        "storage",
    )
    infrastructure_imports = [
        node
        for node in _imports(command_path)
        if (
            isinstance(node, ast.ImportFrom)
            and str(node.module or "").startswith(forbidden_prefixes)
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name.startswith(forbidden_prefixes) for alias in node.names)
        )
    ]
    reach_through = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "gateway"
        and node.value.attr == "state_store"
    ]
    obsolete_paths = [
        REPO_ROOT / "assistant_gateway" / "interactive_apps" / "session_commands.py",
        REPO_ROOT / "assistant_gateway" / "interactive_apps" / "command_help.py",
    ]

    assert infrastructure_imports == []
    assert reach_through == []
    assert all(not path.exists() for path in obsolete_paths)
