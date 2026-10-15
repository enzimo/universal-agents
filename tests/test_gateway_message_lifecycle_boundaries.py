from __future__ import annotations

import ast
from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]


def _module_tree(relative_path: str) -> ast.Module:
    return ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))


def test_message_control_methods_are_removed_from_the_runtime_host() -> None:
    removed_methods = {
        "resolve_pending_query_for_session",
        "request_task_progress_status",
        "reply_to_task_query",
        "reply_to_pending_query",
        "send_user_message",
        "_build_gateway_context",
        "_manager_principal",
    }
    tree = _module_tree("assistant_gateway/gateway.py")
    gateway_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "UserGateway"
    )
    host_methods = {
        node.name
        for node in gateway_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert host_methods.isdisjoint(removed_methods)


def test_gateway_no_longer_owns_progress_or_clarification_message_types() -> None:
    tree = _module_tree("assistant_gateway/gateway.py")
    referenced_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }

    assert "TaskProgressStatus" not in referenced_names
    assert "TaskStatus" not in referenced_names
    assert "selected_choice_ids_from_text" not in referenced_names
    assert "restore_task_query" not in referenced_names


def test_message_lifecycle_modules_do_not_import_gateway_or_infrastructure() -> None:
    forbidden_prefixes = (
        "assistant_gateway.gateway",
        "assistant_gateway.config",
        "assistant_gateway.store",
        "assistant_gateway.nats_manager_runtime",
        "storage",
        "aiohttp",
        "httpx",
    )
    for relative_path in (
        "assistant_gateway/application/request_context.py",
        "assistant_gateway/application/task_progress.py",
        "assistant_gateway/application/clarification.py",
        "assistant_gateway/application/interactive_submission.py",
        "assistant_gateway/application/local_responses.py",
    ):
        tree = _module_tree(relative_path)
        imports = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(
            imported.startswith(prefix)
            for imported in imports
            for prefix in forbidden_prefixes
        ), relative_path
