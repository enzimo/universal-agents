"""Canonical onboarding reconciliation and report generation.

This module is deliberately independent of gateway runtime state. Both the CLI
and web onboarding engine use the same deterministic comparison rules so that
the operator sees an identical report regardless of the onboarding surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class OnboardingReport:
    mode: str
    config_path: Path
    detail_path: Path
    config_added_paths: tuple[str, ...]
    config_updated_paths: tuple[str, ...]
    config_removed_paths: tuple[str, ...]
    env_added_keys: tuple[str, ...]
    env_updated_keys: tuple[str, ...]
    env_removed_keys: tuple[str, ...]
    created_paths: tuple[Path, ...]


def read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read a YAML mapping, treating a missing or non-mapping document as empty."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return dict(data) if isinstance(data, dict) else {}


def _flatten_config_paths(data: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(data, dict):
        flattened: dict[str, Any] = {}
        for key, value in data.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten_config_paths(value, child_prefix))
        return flattened
    if isinstance(data, list):
        # Canonical JSON avoids false updates caused by Python repr differences
        # and sorts mapping keys nested inside lists.
        return {prefix: json.dumps(data, sort_keys=True, default=str)}
    return {prefix: data}


def diff_config_paths(
    before: dict[str, Any],
    after: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    before_flat = _flatten_config_paths(before)
    after_flat = _flatten_config_paths(after)
    before_paths = set(before_flat)
    after_paths = set(after_flat)
    added = tuple(sorted(after_paths - before_paths))
    removed = tuple(sorted(before_paths - after_paths))
    updated = tuple(
        sorted(
            path
            for path in before_paths & after_paths
            if before_flat[path] != after_flat[path]
        )
    )
    return added, updated, removed


def diff_env_keys(
    before: dict[str, str],
    after: dict[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    before_keys = set(before)
    after_keys = set(after)
    added = tuple(sorted(after_keys - before_keys))
    removed = tuple(sorted(before_keys - after_keys))
    updated = tuple(
        sorted(key for key in before_keys & after_keys if before[key] != after[key])
    )
    return added, updated, removed


def onboarding_report_detail_path(instance_root: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return (
        Path(instance_root).expanduser().resolve()
        / "onboarding_reports"
        / f"onboarding_report_{timestamp}.md"
    )


def _render_report_list(items: tuple[str, ...]) -> str:
    if not items:
        return "- none\n"
    return "".join(f"- `{item}`\n" for item in items)


def write_onboarding_report(report: OnboardingReport) -> None:
    report.detail_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Onboarding Reconciliation Report\n\n"
        f"- Mode: `{report.mode}`\n"
        f"- Instance config: `{report.config_path}`\n\n"
        "## Config Paths Added\n\n"
        f"{_render_report_list(report.config_added_paths)}\n"
        "## Config Paths Updated\n\n"
        f"{_render_report_list(report.config_updated_paths)}\n"
        "## Config Paths Removed\n\n"
        f"{_render_report_list(report.config_removed_paths)}\n"
        "## Env Keys Added\n\n"
        f"{_render_report_list(report.env_added_keys)}\n"
        "## Env Keys Updated\n\n"
        f"{_render_report_list(report.env_updated_keys)}\n"
        "## Env Keys Removed\n\n"
        f"{_render_report_list(report.env_removed_keys)}\n"
        "## Paths Written Or Ensured\n\n"
        f"{_render_report_list(tuple(str(path) for path in report.created_paths))}\n"
        "Secret values are intentionally omitted from this report; only config paths and env key names are listed.\n"
    )
    report.detail_path.write_text(content, encoding="utf-8")
