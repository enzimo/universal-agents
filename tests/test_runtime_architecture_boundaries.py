"""Static ratchets for runtime boundaries and composition hosts."""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

# Services whose staged plans are applied through apply_change
# (access_control.changes): they describe a plan and apply it with the
# approval they are given, and never decide authority themselves.
CHANGE_SERVICES = (
    ("custom_agents/runtime_restart_service.py", "RuntimeRestartService"),
    ("custom_agents/runtime_settings_service.py", "RuntimeSettingsService"),
    ("custom_agents/generated_agent_provisioning.py", "GeneratedAgentProvisioningService"),
    ("custom_agents/skill_installation.py", "SkillInstallationService"),
    ("custom_agents/skill_uninstallation.py", "SkillUninstallationService"),
    ("custom_agents/mcp_hosting_changes.py", "HostedMCPServerChangeService"),
    ("custom_agents/execution_changes.py", "ManagedServiceChangeService"),
    ("custom_agents/execution_changes.py", "SSHInteractionChangeService"),
    ("custom_agents/system_schedule_changes.py", "SystemScheduleChangeService"),
)
OLD_AUTHORITY_MODULE_PREFIXES = (
    "custom_agents.authorization",
    "custom_agents.permission_",
    "custom_agents.persistent_permission_policy",
    "custom_agents.admin_elevation",
    "storage.permission_repository",
)

CATALOG_DEPENDENT_CANARY_MARKERS = (
    "GeneratedAgentProvisioningService(",
    "SkillInstallationService(",
)
# The old permission system, removed in access Phase 4d. Its replacement is
# access_control plus custom_agents.access_runtime.
OLD_PERMISSION_SYSTEM_PATHS = (
    "custom_agents/admin_elevation.py",
    "custom_agents/authorization.py",
    "custom_agents/authorization_access_request.py",
    "custom_agents/authorization_administration.py",
    "custom_agents/authorization_administration_outcomes.py",
    "custom_agents/authorization_compatibility.py",
    "custom_agents/authorization_lifecycle_compatibility.py",
    "custom_agents/authorization_message_compatibility.py",
    "custom_agents/authorization_protocol.py",
    "custom_agents/authorization_provider.py",
    "custom_agents/authorization_rollout.py",
    "custom_agents/authorization_snapshot_distribution.py",
    "custom_agents/cedar_authorization.py",
    "custom_agents/delegated_interrupt_runtime.py",
    "custom_agents/delegated_tool_policy.py",
    "custom_agents/emergency_deny.py",
    "custom_agents/framework_invocation_target.py",
    "custom_agents/human_interactions.py",
    "custom_agents/permission_control_plane.py",
    "custom_agents/permission_delegation.py",
    "custom_agents/permission_message_runtime.py",
    "custom_agents/permission_resume_authority.py",
    "custom_agents/permission_user_approval.py",
    "custom_agents/permission_workflow.py",
    "custom_agents/persistent_permission_policy.py",
    "custom_agents/task_steering_dispatch.py",
    "custom_messages/contracts/authorization_user_copy.py",
    "custom_messages/contracts/authorization_v1.py",
    "custom_messages/contracts/human_interaction_v1.py",
    "custom_messages/permission_workflow.py",
    "custom_tools/providers/permissions.py",
    "scripts/reset_permission_storage.py",
    "storage/human_interaction_repository.py",
    "storage/permission_ports.py",
    "storage/permission_repository.py",
    "storage/permission_schema.py",
    # The last part of 4d-5: the old schemas, definitions, tools and the
    # Rust evaluator's version 1 protocol.
    "data/schemas/authorization",
    "ua_definitions/authorization",
    "scripts/emergency_deny_sign.py",
    "scripts/export_authorization_schemas.py",
    "scripts/verify_authorization_compatibility.py",
    "scripts/verify_authorization_lifecycle_compatibility.py",
    "scripts/verify_authorization_message_compatibility.py",
    "rust/ua-cedar-authorizer/src/message_compatibility.rs",
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    return imported


def _runtime_imports(path: Path) -> list[str]:
    """Collect imports that execute at runtime, excluding TYPE_CHECKING blocks."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []

    def visit(node: ast.AST) -> None:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            for child in node.orelse:
                visit(child)
            return
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return imported


def _loads_agent_catalog_composition(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "custom_agents.agent_catalog" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (
            node.module == "custom_agents.agent_catalog"
            or (
                node.module == "custom_agents"
                and any(alias.name == "agent_catalog" for alias in node.names)
            )
        ):
            return True
    return False


@cache
def _runtime_module_graph() -> dict[str, set[str]]:
    paths = [
        path
        for path in REPO_ROOT.rglob("*.py")
        if not any(
            part in {".venv", "tests", "target", "node_modules", "deleteme"}
            for part in path.relative_to(REPO_ROOT).parts
        )
    ]
    modules = {
        (
            ".".join(path.relative_to(REPO_ROOT).parts[:-1])
            if path.name == "__init__.py"
            else ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
        ): path
        for path in paths
    }
    graph = {module: set() for module in modules}
    for module, path in modules.items():
        for imported in _runtime_imports(path):
            target = max(
                (
                    candidate
                    for candidate in modules
                    if imported == candidate or imported.startswith(candidate + ".")
                ),
                key=len,
                default=None,
            )
            if target and target != module:
                graph[module].add(target)
    return graph


def _strongly_connected_components(
    graph: dict[str, set[str]],
) -> list[frozenset[str]]:
    next_index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    components: list[frozenset[str]] = []

    def visit(module: str) -> None:
        nonlocal next_index
        indices[module] = next_index
        low_links[module] = next_index
        next_index += 1
        stack.append(module)
        on_stack.add(module)
        for dependency in graph[module]:
            if dependency not in indices:
                visit(dependency)
                low_links[module] = min(low_links[module], low_links[dependency])
            elif dependency in on_stack:
                low_links[module] = min(low_links[module], indices[dependency])
        if low_links[module] != indices[module]:
            return
        component: set[str] = set()
        while stack:
            dependency = stack.pop()
            on_stack.remove(dependency)
            component.add(dependency)
            if dependency == module:
                break
        if len(component) > 1:
            components.append(frozenset(component))

    for module in graph:
        if module not in indices:
            visit(module)
    return components


def _has_dependency_path(graph: dict[str, set[str]], source: str, target: str) -> bool:
    pending = [source]
    visited = {source}
    while pending:
        current = pending.pop()
        for dependency in graph[current]:
            if dependency == target:
                return True
            if dependency not in visited:
                visited.add(dependency)
                pending.append(dependency)
    return False


@pytest.mark.parametrize(
    ("downstream", "upstream"),
    (
        (
            "secure_tool_broker.credential_automations",
            "secure_tool_broker.executors",
        ),
        ("secure_tool_broker.xurl_executor", "secure_tool_broker.hub_cli_executor"),
        ("custom_agents.runtime_setting_registry", "custom_agents.runtime_settings_service"),
    ),
)
def test_resolved_runtime_import_cycles_cannot_return(
    downstream: str, upstream: str
) -> None:
    graph = _runtime_module_graph()

    assert not _has_dependency_path(graph, downstream, upstream)


def test_production_runtime_import_graph_is_acyclic() -> None:
    assert _strongly_connected_components(_runtime_module_graph()) == []


@pytest.mark.parametrize(
    ("downstream", "forbidden_upstream"),
    (
        ("custom_agents.strands_agent", "custom_agents.strands_team"),
        ("custom_agents.sdk.strands.mcp", "custom_tools.registry"),
        ("custom_tools.non_interactive_shell", "custom_tools.registry"),
        ("custom_tools.python_env_tool", "custom_tools.registry"),
    ),
)
def test_removed_runtime_back_imports_cannot_return(
    downstream: str,
    forbidden_upstream: str,
) -> None:
    assert forbidden_upstream not in _runtime_module_graph()[downstream]


def test_obsolete_aggregate_tool_paths_stay_deleted() -> None:
    assert not (REPO_ROOT / "custom_tools/strands_tools.py").exists()
    assert not (REPO_ROOT / "custom_agents/tool_runtime_context.py").exists()


def test_obsolete_phase_4b_module_paths_stay_deleted() -> None:
    obsolete = (
        "assistant_gateway/http_hosting.py",
        "custom_agents/identifiers.py",
        "custom_agents/managed_mcp_server.py",
        "custom_agents/mcp_connections.py",
        "custom_agents/mcp_hosting.py",
        "custom_agents/permission_models.py",
        "assistant_gateway/facade.py",
        "assistant_gateway/facade_context.py",
        "assistant_gateway/website_facade.py",
        "assistant_gateway/interactive_apps/session_commands.py",
    )

    assert [path for path in obsolete if (REPO_ROOT / path).exists()] == []


def test_old_permission_system_paths_stay_deleted() -> None:
    assert [path for path in OLD_PERMISSION_SYSTEM_PATHS if (REPO_ROOT / path).exists()] == []


def test_secure_broker_import_does_not_load_runtime_host_packages() -> None:
    probe = """
import sys
import secure_tool_broker.service

forbidden_prefixes = (
    "assistant_gateway",
    "custom_agents",
    "custom_tools",
    "strands",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden_prefixes)
)
if loaded:
    raise SystemExit("broker import loaded runtime hosts: " + ", ".join(loaded))
"""

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_tool_providers_do_not_import_catalog_composition() -> None:
    violations: list[tuple[str, str]] = []
    for path in (REPO_ROOT / "custom_tools/providers").glob("*.py"):
        for imported in _runtime_imports(path):
            if imported == "custom_agents.agent_catalog":
                violations.append((path.name, imported))

    assert violations == []


def test_tool_provider_modules_remain_bounded() -> None:
    violations: list[tuple[str, int]] = []
    for path in (REPO_ROOT / "custom_tools/providers").glob("[!_]*.py"):
        statement_count = sum(
            isinstance(node, ast.stmt)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
        if statement_count > 650:
            violations.append((path.name, statement_count))

    assert violations == []


def test_cross_process_message_kernel_does_not_import_runtime_hosts() -> None:
    forbidden_prefixes = (
        "assistant_gateway",
        "custom_agents",
        "custom_tools",
        "custom_transport",
        "secure_tool_broker",
        "storage",
    )
    violations: list[tuple[str, str]] = []
    for path in (REPO_ROOT / "custom_messages").rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith(forbidden_prefixes):
                violations.append((path.name, imported))

    assert violations == []


def test_agent_runtime_does_not_import_concrete_remote_runtime_hosts() -> None:
    forbidden_prefixes = (
        "assistant_gateway.gateway",
        "secure_tool_broker.executors",
        "secure_tool_broker.service",
    )
    violations: list[tuple[str, str]] = []
    for path in (REPO_ROOT / "custom_agents").rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith(forbidden_prefixes):
                violations.append((str(path.relative_to(REPO_ROOT)), imported))

    assert violations == []


def test_standalone_capability_canaries_load_catalog_composition_root() -> None:
    violations: list[str] = []
    for path in (REPO_ROOT / "tests").glob("live*.py"):
        source = path.read_text(encoding="utf-8")
        if not any(marker in source for marker in CATALOG_DEPENDENT_CANARY_MARKERS):
            continue
        if not _loads_agent_catalog_composition(path):
            violations.append(path.name)

    assert violations == []


def test_broker_does_not_import_agent_gateway_or_strands_runtimes() -> None:
    forbidden_prefixes = ("assistant_gateway", "custom_agents", "strands")
    violations: list[tuple[str, str]] = []
    for path in (REPO_ROOT / "secure_tool_broker").rglob("*.py"):
        for imported in _imports(path):
            relative_path = str(path.relative_to(REPO_ROOT))
            if imported.startswith(forbidden_prefixes):
                violations.append((relative_path, imported))

    assert violations == []


def test_mcp_control_plane_is_runtime_neutral() -> None:
    forbidden_prefixes = (
        "assistant_gateway",
        "custom_agents",
        "custom_tools",
        "secure_tool_broker",
        "storage",
        "strands",
    )
    violations: list[tuple[str, str]] = []
    for path in (REPO_ROOT / "mcp_control").rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith(forbidden_prefixes):
                violations.append((str(path.relative_to(REPO_ROOT)), imported))

    assert violations == []


def test_authorized_operation_kernel_remains_transport_and_domain_neutral() -> None:
    kernel = REPO_ROOT / "custom_agents/governance/authorized_operations.py"
    forbidden_prefixes = (
        "assistant_gateway",
        "custom_agents",
        "custom_messages",
        "custom_tools",
        "custom_transport",
        "secure_tool_broker",
        "storage",
    )

    violations = [
        imported
        for imported in _runtime_imports(kernel)
        if imported.startswith(forbidden_prefixes)
    ]

    assert violations == []


@pytest.mark.parametrize(("relative_path", "class_name"), CHANGE_SERVICES)
def test_change_services_describe_and_apply_without_deciding_authority(
    relative_path: str,
    class_name: str,
) -> None:
    path = REPO_ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    methods = {node.name: node for node in class_node.body if isinstance(node, ast.FunctionDef)}
    assert {"describe", "apply"} <= set(methods)
    assert [argument.arg for argument in methods["apply"].args.args] == ["self", "change_id", "approval"]
    assert not [name for name in _imports(path) if name.startswith(OLD_AUTHORITY_MODULE_PREFIXES)]


@pytest.mark.parametrize(
    "relative_path",
    [relative_path for relative_path, _ in CHANGE_SERVICES],
)
def test_change_services_do_not_reintroduce_kernel_helpers(
    relative_path: str,
) -> None:
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    forbidden_helpers = {
        "_actor",
        "_atomic_write",
        "_atomic_write_json",
        "_bytes_hash",
        "_canonical_json",
        "_current_task_id",
        "_json_hash",
        "_load_plan",
        "_plan_digest",
    }
    module_functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert module_functions.isdisjoint(forbidden_helpers)


def test_website_capabilities_cannot_flatten_into_the_core_application() -> None:
    website_tree = ast.parse(
        (REPO_ROOT / "assistant_gateway/application/website_application.py").read_text(
            encoding="utf-8"
        )
    )
    facet_class_names = {
        "WebIdentityApplication",
        "WebSessionApplication",
        "WebOperationsApplication",
    }
    facet_methods = {
        node.name
        for class_node in website_tree.body
        if isinstance(class_node, ast.ClassDef) and class_node.name in facet_class_names
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    core_tree = ast.parse(
        (REPO_ROOT / "assistant_gateway/application/gateway_application.py").read_text(
            encoding="utf-8"
        )
    )
    core_class = next(
        node
        for node in core_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GatewayApplication"
    )
    core_methods = {
        node.name
        for node in core_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert facet_methods
    assert core_methods.isdisjoint(facet_methods)

    adapter_tree = ast.parse(
        (REPO_ROOT / "assistant_gateway/interactive_apps/website.py").read_text(
            encoding="utf-8"
        )
    )
    flat_calls = {
        node.attr
        for node in ast.walk(adapter_tree)
        if isinstance(node, ast.Attribute)
        and node.attr in facet_methods
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "application"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    }
    assert flat_calls == set()


def test_gateway_application_layer_is_runtime_neutral() -> None:
    forbidden_prefixes = (
        "assistant_gateway.config",
        "assistant_gateway.gateway",
        "assistant_gateway.store",
        "custom_agents",
        "secure_tool_broker",
        "storage",
        "nats",
        "aiohttp",
        "httpx",
    )
    violations: list[tuple[str, str]] = []
    for path in (REPO_ROOT / "assistant_gateway/application").rglob("*.py"):
        for imported in _runtime_imports(path):
            if imported.startswith(forbidden_prefixes):
                violations.append((str(path.relative_to(REPO_ROOT)), imported))

    assert violations == []


def test_public_application_contracts_remain_typed_and_persistence_neutral() -> None:
    contract_paths = (
        REPO_ROOT / "assistant_gateway/application/gateway_application.py",
        REPO_ROOT / "assistant_gateway/application/route_context.py",
        REPO_ROOT / "assistant_gateway/application/procedures.py",
        REPO_ROOT / "assistant_gateway/application/access_reviews.py",
        REPO_ROOT / "assistant_gateway/application/website_application.py",
        REPO_ROOT / "custom_messages/contracts/installed_permissions_v1.py",
    )
    forbidden_import_prefixes = (
        "assistant_gateway.domain",
        "assistant_gateway.ports.repositories",
        "assistant_gateway.store",
        "storage",
    )
    violations: list[str] = []

    def annotation_uses(annotation: ast.expr | None, name: str) -> bool:
        return annotation is not None and any(
            isinstance(node, ast.Name) and node.id == name
            for node in ast.walk(annotation)
        )

    for path in contract_paths:
        relative_path = str(path.relative_to(REPO_ROOT))
        for imported in _runtime_imports(path):
            if imported.startswith(forbidden_import_prefixes):
                violations.append(f"{relative_path}: imports {imported}")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for class_node in (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
        ):
            for node in class_node.body:
                if isinstance(node, ast.AnnAssign):
                    for forbidden_name in ("Any", "dict"):
                        if annotation_uses(node.annotation, forbidden_name):
                            violations.append(
                                f"{relative_path}:{class_node.name} field uses "
                                f"{forbidden_name}"
                            )
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_") and not node.name.startswith("__"):
                    continue
                if node.args.kwarg is not None:
                    violations.append(
                        f"{relative_path}:{class_node.name}.{node.name} uses **kwargs"
                    )
                annotations = [
                    argument.annotation
                    for argument in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                    )
                ] + [node.returns]
                for forbidden_name in ("Any", "dict"):
                    if any(
                        annotation_uses(annotation, forbidden_name)
                        for annotation in annotations
                    ):
                        violations.append(
                            f"{relative_path}:{class_node.name}.{node.name} uses "
                            f"{forbidden_name}"
                        )

    assert violations == []


def test_user_gateway_remains_a_lifecycle_and_composition_host() -> None:
    tree = ast.parse(
        (REPO_ROOT / "assistant_gateway/gateway.py").read_text(encoding="utf-8")
    )
    gateway_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "UserGateway"
    )
    method_names = {
        node.name
        for node in gateway_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert method_names == {
        "__init__",
        "bind_event_sink",
        "connect",
        "close",
        "__aenter__",
        "__aexit__",
    }


def test_user_gateway_does_not_reintroduce_web_identity_forwarders() -> None:
    tree = ast.parse(
        (REPO_ROOT / "assistant_gateway/gateway.py").read_text(encoding="utf-8")
    )
    gateway_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "UserGateway"
    )
    method_names = {
        node.name
        for node in gateway_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert method_names.isdisjoint(
        {
            "activate_web_user_enrollment_token",
            "consume_web_user_invite",
            "is_web_chat_user_active",
            "issue_web_user_enrollment_token",
            "resolve_web_user_token",
            "revoke_pending_web_user_enrollment_token",
        }
    )


def test_generated_agent_provisioning_does_not_reabsorb_runtime_hydration() -> None:
    generated_tree = ast.parse(
        (REPO_ROOT / "custom_agents/generated_agent_provisioning.py").read_text(
            encoding="utf-8"
        )
    )
    generated_functions = {
        node.name
        for node in generated_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "apply_generated_agent_provisioning" not in generated_functions
    assert "custom_agents.generated_agent_provisioning" not in _runtime_imports(
        REPO_ROOT / "custom_agents/generated_agent_runtime.py"
    )


def test_capability_attachments_and_tool_groups_are_gone() -> None:
    """Tools follow grants (access plan Phase 7); nothing attaches tool groups any more."""

    for relative in (
        "custom_agents/capability_attachments.py",
        "custom_agents/capability_runtime.py",
        "custom_agents/capability_registry.py",
        "custom_agents/delegation_registry.py",
        "custom_tools/tool_registry_port.py",
        "custom_messages/contracts/capability_v1.py",
        "ua_definitions/capability_registry",
    ):
        assert not (REPO_ROOT / relative).exists(), relative


@pytest.mark.parametrize(
    ("relative_path", "class_name", "method_budget", "statement_budget"),
    (
        ("assistant_gateway/gateway.py", "UserGateway", 6, 125),
        (
            "assistant_gateway/application/runtime_application.py",
            "GatewayRuntimeApplication",
            23,
            85,
        ),
        (
            "assistant_gateway/application/website_runtime_application.py",
            "WebsiteRuntimeApplication",
            7,
            25,
        ),
        ("custom_agents/strands_agent.py", "UniversalAgent", 230, 1_815),
        ("custom_agents/task_manager.py", "TaskManager", 207, 1_355),
        (
            "secure_tool_broker/service.py",
            "SecureToolBrokerService",
            65,
            876,
        ),
    ),
)
def test_large_facades_cannot_grow(
    relative_path: str,
    class_name: str,
    method_budget: int,
    statement_budget: int,
) -> None:
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    methods = [
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    statements = sum(isinstance(node, ast.stmt) for node in ast.walk(class_node))

    # Host/application budgets are Phase 2 limits; legacy runtimes remain ratchets.
    assert len(methods) <= method_budget
    assert statements <= statement_budget


# ADR-019: durable work is pulled, never pushed. Only the transport adapter
# subscribes to NATS, and nothing creates a deliver group (the push consumer
# shape that handed work to busy agents).
NATS_SUBSCRIBERS = {"custom_transport/nats_adapter.py", "custom_transport/nats_pull.py"}
NATS_CLIENT_NAMES = ("_js", "js", "jetstream", "_nc", "nc", "nats_client")


def _production_python_files() -> list[Path]:
    return [
        path
        for path in REPO_ROOT.rglob("*.py")
        if not any(
            part in {".venv", "tests", "target", "node_modules", "deleteme"}
            for part in path.relative_to(REPO_ROOT).parts
        )
    ]


def test_work_is_pulled_and_only_the_transport_adapter_subscribes() -> None:
    offenders: list[str] = []
    for path in _production_python_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if any(keyword.arg == "deliver_group" for keyword in node.keywords):
                offenders.append(f"{relative}:{node.lineno} deliver_group")
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"subscribe", "pull_subscribe", "pull_subscribe_bind"}
                and ast.unparse(func.value).split(".")[-1] in NATS_CLIENT_NAMES
                and relative not in NATS_SUBSCRIBERS
            ):
                offenders.append(f"{relative}:{node.lineno} {func.attr}")
    assert offenders == []
