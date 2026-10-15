#!/usr/bin/env python3
"""
Convenience launcher for starting one configured agent type for manual testing.

Examples:
    python scripts/launch_agent_type.py TeamArchitect --team-id team_303 --env debug.env
    python scripts/launch_agent_type.py CodeWritingAgent --team-id team_303 --env debug.env
    python scripts/launch_agent_type.py --list
"""

# Direct execution bootstraps the repository path before project imports.
# ruff: noqa: E402

import argparse
import os
import sys
from pathlib import Path


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from default_values import AGENT_DEFAULTS
from assistant_gateway.runtime_config import is_instance_config_document
from strands_main import _load_yaml_mapping, build_parser, run_with_args


def _parse_bool_like(value: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError("Expected one of: true, false")


def _build_launcher_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quick launcher for a single configured agent type",
    )
    parser.add_argument(
        "agent_type",
        nargs="?",
        default=AGENT_DEFAULTS.named_agent_type.value,
        help="Named agent type to start (default: TeamArchitect)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available agent types and exit",
    )
    parser.add_argument(
        "--lead",
        default="auto",
        choices=["auto", "true", "false"],
        help="Whether to run as team lead. 'auto' makes TeamArchitect the lead and all others non-lead.",
    )

    shared = build_parser()
    shared_args = {action.dest: action for action in shared._actions if action.dest != "help"}
    for dest in [
        "team_id",
        "model_provider",
        "model_id",
        "config",
        "agent_id",
        "env",
        "task_history_db_path",
        "agent_result_transformer",
    ]:
        action = shared_args[dest]
        option_strings = list(action.option_strings)
        kwargs = {
            "dest": action.dest,
            "default": action.default,
            "help": action.help,
        }
        if getattr(action, "choices", None):
            kwargs["choices"] = action.choices
        if getattr(action, "type", None):
            kwargs["type"] = action.type
        parser.add_argument(*option_strings, **kwargs)

    return parser


def _load_catalog_config_context(args: argparse.Namespace) -> None:
    if args.env:
        try:
            from dotenv import load_dotenv
        except ImportError:
            load_dotenv = None
        if load_dotenv is not None:
            load_dotenv(args.env)

    from config_loader import reload_config, set_config

    if not args.config:
        reload_config(None, search_default_paths=False)
        return

    config_path = Path(args.config).expanduser().resolve()
    raw_config = _load_yaml_mapping(config_path)
    if is_instance_config_document(raw_config):
        from assistant_gateway.config import GatewayInstanceConfig
        from assistant_gateway.runtime_config import build_runtime_config_from_instance_config

        set_config(build_runtime_config_from_instance_config(GatewayInstanceConfig.load_for_process(config_path)))
        return

    reload_config(str(config_path), search_default_paths=False)


def _print_agent_types(args: argparse.Namespace) -> None:
    _load_catalog_config_context(args)
    from custom_agents.strands_team import AGENT_TYPE_MAP, create_agent_description

    print("Available agent types:\n")
    for agent_type in sorted(AGENT_TYPE_MAP.keys()):
        description = create_agent_description(AGENT_TYPE_MAP[agent_type])
        print(f"{agent_type}\n{description}\n")


def main() -> None:
    parser = _build_launcher_parser()
    args = parser.parse_args()

    if args.list:
        _print_agent_types(args)
        return

    if args.lead == "auto":
        is_lead = args.agent_type == AGENT_DEFAULTS.named_agent_type.value
    else:
        is_lead = _parse_bool_like(args.lead)

    main_args = argparse.Namespace(
        agent_type=args.agent_type,
        team_id=args.team_id,
        is_lead=is_lead,
        model_provider=args.model_provider,
        model_id=args.model_id,
        config=args.config,
        agent_id=args.agent_id,
        env=args.env,
        task_history_db_path=args.task_history_db_path,
        agent_result_transformer=args.agent_result_transformer,
    )
    run_with_args(main_args)


if __name__ == "__main__":
    main()
