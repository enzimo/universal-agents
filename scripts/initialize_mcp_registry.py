#!/usr/bin/env python3
"""Initialize the instance MCP registry before concurrent agents start."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sys
from typing import Sequence


def _resolve_project_root() -> Path:
    """Find the application root when run in-tree or copied into a container."""

    candidates = [
        Path(os.environ["UA_APP_ROOT"]).expanduser()
        if os.environ.get("UA_APP_ROOT")
        else None,
        Path(__file__).resolve().parents[1],
        Path.cwd(),
        Path("/app"),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = candidate.resolve()
        if (resolved / "assistant_gateway").is_dir() and (
            resolved / "mcp_control"
        ).is_dir():
            return resolved
    raise RuntimeError(
        "Could not locate the Universal Agents application root. "
        "Set UA_APP_ROOT to the directory containing assistant_gateway and mcp_control."
    )


PROJECT_ROOT = _resolve_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from assistant_gateway.config import GatewayInstanceConfig  # noqa: E402
from mcp_control.registry import (  # noqa: E402
    MCPConnectionStore,
    reconcile_instance_mcp_connections,
    resolve_mcp_connection_paths,
)


LOGGER = logging.getLogger("initialize_mcp_registry")
DEFAULT_INSTANCE_CONFIG = Path("/data/instance_config.yaml")


@dataclass(frozen=True)
class MCPRegistryInitializationResult:
    config_path: Path
    baseline_path: Path
    baseline_preexisting: bool
    connection_count: int
    upgrade_mode: str


def initialize_mcp_registry(
    config_path: str | Path,
) -> MCPRegistryInitializationResult:
    """Run the normal secret-free MCP reconciliation path once and verify it."""

    resolved_config_path = Path(config_path).expanduser().resolve()
    if not resolved_config_path.is_file():
        raise FileNotFoundError(
            f"Instance configuration was not found: {resolved_config_path}"
        )

    config = GatewayInstanceConfig.load_for_process(resolved_config_path)
    paths = resolve_mcp_connection_paths(config.instance_root_path)
    baseline_preexisting = paths.baseline.is_file()

    records = reconcile_instance_mcp_connections(config)
    baseline = MCPConnectionStore(paths).baseline()
    if baseline is None:
        raise RuntimeError(
            f"MCP reconciliation completed without creating a baseline: {paths.baseline}"
        )

    return MCPRegistryInitializationResult(
        config_path=resolved_config_path,
        baseline_path=paths.baseline,
        baseline_preexisting=baseline_preexisting,
        connection_count=len(records),
        upgrade_mode=baseline.upgrade_mode,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(DEFAULT_INSTANCE_CONFIG),
        help="Onboarded instance_config.yaml path (default: /data/instance_config.yaml).",
    )
    parser.add_argument(
        "--confirm-exclusive",
        action="store_true",
        help=(
            "Confirm that every Universal Agents process using this instance root "
            "is stopped. Required before reconciliation."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.confirm_exclusive:
        parser.error(
            "Pass --confirm-exclusive after stopping every Universal Agents "
            "process that uses this instance root."
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        result = initialize_mcp_registry(args.config)
    except Exception:
        LOGGER.exception(
            "MCP registry initialization failed. "
            "event=mcp_registry_initialization_failed config_path=%s",
            Path(args.config).expanduser(),
        )
        return 1

    LOGGER.info(
        "MCP registry initialization complete. "
        "event=mcp_registry_initialization_complete config_path=%s "
        "baseline_path=%s baseline_preexisting=%s connection_count=%s "
        "upgrade_mode=%s",
        result.config_path,
        result.baseline_path,
        result.baseline_preexisting,
        result.connection_count,
        result.upgrade_mode,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
