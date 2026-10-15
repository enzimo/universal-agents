from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module_tree(relative_path: str) -> ast.Module:
    return ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))


def _gateway_class() -> ast.ClassDef:
    tree = _module_tree("assistant_gateway/gateway.py")
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "UserGateway"
    )


def test_schedule_and_delivery_methods_are_removed_from_the_runtime_host() -> None:
    removed_methods = {
        "release_due_deliveries",
        "add_cron_job",
        "list_scheduled_jobs",
        "pause_scheduled_job",
        "resume_scheduled_job",
        "disable_scheduled_job",
        "remove_scheduled_job",
        "_request_manager_command",
        "_delivery_loop",
        "emit_proactive_notice",
        "_handle_unsolicited_message",
    }
    host_methods = {
        node.name
        for node in _gateway_class().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert host_methods.isdisjoint(removed_methods)


def test_gateway_no_longer_owns_schedule_control_or_deferred_release_helpers() -> None:
    gateway_class = _gateway_class()
    method_names = {
        node.name
        for node in gateway_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    referenced_names = {
        node.id for node in ast.walk(gateway_class) if isinstance(node, ast.Name)
    }

    assert "_authorize_manager_command" not in method_names
    assert "_execute_schedule_command" not in method_names
    assert "_resolve_proactive_delivery_session" not in method_names
    assert "_ensure_manager_heartbeat_schedule" not in method_names
    assert "_ensure_manager_memory_review_schedules" not in method_names
    assert "_publish_manager_memory_review_schedule" not in method_names
    assert "_read_config_file_mtime_ns" not in method_names
    assert "_refresh_delivery_preferences_from_config_file" not in method_names
    assert "_send_delivery_receipt" not in method_names
    assert "_build_delivery_event" not in method_names
    assert "_delivery_message_type" not in method_names
    assert "delivery_message_level" not in method_names
    assert "_resolve_delivery_attachments" not in method_names
    assert "_render_envelope_text" not in method_names
    assert "summarize_delivery_batch" not in referenced_names
    assert "build_delivery_receipt_envelope" not in referenced_names


def test_schedule_and_delivery_applications_do_not_import_host_or_infrastructure() -> (
    None
):
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
        "assistant_gateway/application/manager_commands.py",
        "assistant_gateway/application/schedule_management.py",
        "assistant_gateway/application/deferred_delivery.py",
        "assistant_gateway/application/maintenance_schedules.py",
        "assistant_gateway/application/proactive_delivery.py",
        "assistant_gateway/application/delivery_presentation.py",
        "assistant_gateway/application/delivery_projection.py",
        "assistant_gateway/application/unsolicited_delivery.py",
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
