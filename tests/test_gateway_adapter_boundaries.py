from __future__ import annotations

import ast
from pathlib import Path

import pytest

from assistant_gateway.interactive_apps.contracts import (
    InteractiveAppApplication,
    InteractiveAppBridge,
    InteractiveAppBuildContext,
)


ADAPTER_ROOT = Path(__file__).resolve().parents[1] / "assistant_gateway" / "interactive_apps"
APPLICATION_BOUND_ADAPTERS = (
    "discord.py",
    "telegram.py",
    "whatsapp.py",
    "signal.py",
    "photon.py",
    "website.py",
)


@pytest.mark.parametrize("filename", APPLICATION_BOUND_ADAPTERS)
def test_adapter_depends_only_on_gateway_application_contract(
    filename: str,
) -> None:
    source = (ADAPTER_ROOT / filename).read_text(encoding="utf-8")
    tree = ast.parse(source)

    concrete_gateway_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "assistant_gateway.gateway"
    ]
    state_store_accesses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "state_store"
    ]
    concrete_gateway_accesses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "gateway"
    ]
    repository_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (
            node.module == "assistant_gateway.store"
            or str(node.module or "").startswith("storage.")
        )
    ]
    command_implementation_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        in {
            "assistant_gateway.interactive_apps.session_commands",
            "assistant_gateway.application.interactive_commands",
        }
    ]

    assert concrete_gateway_imports == []
    assert concrete_gateway_accesses == []
    assert state_store_accesses == []
    assert repository_imports == []
    assert command_implementation_imports == []


def test_public_package_plugin_context_exposes_no_aggregate_gateway_config() -> None:
    assert tuple(InteractiveAppBuildContext.__dataclass_fields__) == (
        "configuration",
        "application",
        "bridge",
        "runtime",
    )
    source = (ADAPTER_ROOT / "plugin_api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden_roots = {
        "storage",
        "custom_transport",
        "assistant_gateway.config",
        "assistant_gateway.gateway",
        "assistant_gateway.store",
    }
    assert not {
        module
        for module in imported_modules
        if any(module == root or module.startswith(f"{root}.") for root in forbidden_roots)
    }
    assert InteractiveAppBuildContext.__annotations__["application"] in {
        "InteractiveAppApplication",
        InteractiveAppApplication,
    }
    assert InteractiveAppBuildContext.__annotations__["bridge"] in {
        "InteractiveAppBridge",
        InteractiveAppBridge,
    }
