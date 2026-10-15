#!/usr/bin/env python3
"""Validate YAML-backed agent catalogs and bundles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_env_file(path: str | None) -> None:
    if not path:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(path)


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a YAML mapping: {path}")
    return raw


def _load_config_context(config_path: str | None) -> None:
    from assistant_gateway.runtime_config import is_instance_config_document
    from config_loader import reload_config, set_config

    if not config_path:
        reload_config(None, search_default_paths=False)
        return

    path = Path(config_path).expanduser().resolve()
    raw = _load_yaml_mapping(path)
    if is_instance_config_document(raw):
        from assistant_gateway.config import GatewayInstanceConfig
        from assistant_gateway.runtime_config import build_runtime_config_from_instance_config

        set_config(build_runtime_config_from_instance_config(GatewayInstanceConfig.load_for_process(path)))
        return
    reload_config(str(path), search_default_paths=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate startup-loaded agent catalog files or bundle directories.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Additional catalog files, catalog directories, or bundle directories to validate.",
    )
    parser.add_argument(
        "--config",
        help="Optional generic config.yaml or instance_config.yaml whose agent catalog settings should be loaded.",
    )
    parser.add_argument(
        "--env",
        help="Optional env file loaded before config resolution.",
    )
    parser.add_argument(
        "--no-configured",
        action="store_true",
        help="Ignore configured and workspace-default catalog paths.",
    )
    parser.add_argument(
        "--no-default-bundles",
        action="store_true",
        help="Ignore repo-shipped default agent bundles.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable validation output.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        _load_env_file(args.env)
        _load_config_context(args.config)

        from custom_agents.agent_catalog import load_agent_catalog

        catalog = load_agent_catalog(
            args.paths,
            include_configured=not args.no_configured,
            include_default_bundles=not args.no_default_bundles,
        )
        agent_types = sorted(catalog.agent_types)
        team_templates = sorted(catalog.team_templates)
        source_paths = [str(path) for path in catalog.source_paths]
        payload = {
            "valid": True,
            "agent_types": agent_types,
            "team_templates": team_templates,
            "source_paths": source_paths,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("Agent catalog valid.")
            print(f"Agent types: {', '.join(agent_types)}")
            print(f"Team templates: {', '.join(team_templates) or '(none)'}")
            print("Sources:")
            for source_path in source_paths:
                print(f"  - {source_path}")
        return 0
    except Exception as exc:
        payload = {"valid": False, "error": str(exc)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Agent catalog invalid: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
