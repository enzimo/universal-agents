"""Shared storage contracts with lazy compatibility exports."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_LAZY_EXPORTS = {
    "DEFAULT_SQLITE_STORAGE_PATH": ("storage.config", "DEFAULT_SQLITE_STORAGE_PATH"),
    "SUPPORTED_STORAGE_BACKENDS": ("storage.backends", "SUPPORTED_STORAGE_BACKENDS"),
    "PostgresStorageSettings": ("storage.config", "PostgresStorageSettings"),
    "SqliteStorageSettings": ("storage.config", "SqliteStorageSettings"),
    "StorageSettings": ("storage.config", "StorageSettings"),
    "resolve_storage_settings": ("storage.config", "resolve_storage_settings"),
    "DeliveryRecord": ("assistant_gateway.domain.delivery", "DeliveryRecord"),
    "PendingQueryRecord": (
        "assistant_gateway.domain.request_state",
        "PendingQueryRecord",
    ),
    "RequestRecord": ("assistant_gateway.domain.request_state", "RequestRecord"),
    "ScheduledTargetRecord": (
        "assistant_gateway.domain.delivery",
        "ScheduledTargetRecord",
    ),
    "SessionConversationListRecord": (
        "assistant_gateway.domain.sessions",
        "SessionConversationListRecord",
    ),
    "SessionConversationRecord": (
        "assistant_gateway.domain.sessions",
        "SessionConversationRecord",
    ),
    "SessionListRecord": ("assistant_gateway.domain.sessions", "SessionListRecord"),
    "SessionRecord": ("assistant_gateway.domain.sessions", "SessionRecord"),
    "SessionSearchRecord": (
        "assistant_gateway.domain.sessions",
        "SessionSearchRecord",
    ),
    "AfterActionReportRepository": (
        "storage.after_action_report_repository",
        "AfterActionReportRepository",
    ),
    "HeartbeatItemRecord": ("storage.heartbeat_repository", "HeartbeatItemRecord"),
    "HeartbeatSourceRecord": (
        "storage.heartbeat_repository",
        "HeartbeatSourceRecord",
    ),
    "ObjectiveListRecord": ("storage.objective_repository", "ObjectiveListRecord"),
    "PlanListRecord": ("storage.plan_repository", "PlanListRecord"),
    "StorageBackendCapabilities": (
        "storage.runtime",
        "StorageBackendCapabilities",
    ),
    "describe_storage_capabilities": (
        "storage.runtime",
        "describe_storage_capabilities",
    ),
    "DeferredMessageSnapshotRow": (
        "storage.runtime_state_repository",
        "DeferredMessageSnapshotRow",
    ),
    "TaskRuntimeSnapshotRow": (
        "storage.runtime_state_repository",
        "TaskRuntimeSnapshotRow",
    ),
    "ScheduleRepository": ("storage.schedule_repository", "ScheduleRepository"),
    "ArchivedTaskListRecord": (
        "storage.task_archive_repository",
        "ArchivedTaskListRecord",
    ),
    "ArchivedTaskRecord": (
        "storage.task_archive_repository",
        "ArchivedTaskRecord",
    ),
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
