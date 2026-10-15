"""Resolved settings consumed by the standalone collector."""

from dataclasses import dataclass

from runtime_settings.telemetry import ActivityCollectorSettings
from storage.config import StorageSettings


@dataclass(frozen=True)
class CollectorRuntimeSettings:
    activity_collector: ActivityCollectorSettings
    storage: StorageSettings
    team_id: str
