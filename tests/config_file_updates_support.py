"""Spawned file writers without the gateway and runtime-service imports.

The spawn start method imports the worker's module in each child. Keep this
module separate so unrelated application startup cannot consume the contention
test's result deadline before either writer reaches the barrier.
"""

from pathlib import Path
from typing import Any

from config_file_updates import ConfigFileConflict, replace_config_file


def contending_write(path: str, revision: str, value: bytes, ready: Any, results: Any) -> None:
    ready.wait(timeout=10)
    try:
        replace_config_file(Path(path), value, expected_revision=revision)
        results.put("saved")
    except ConfigFileConflict:
        results.put("conflict")
