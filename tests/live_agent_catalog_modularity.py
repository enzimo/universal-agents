#!/usr/bin/env python3
"""Live filesystem/config-path validation for modular agent catalogs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_env_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(path)


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_workspace_bundle(workspace_dir: Path) -> Path:
    bundle_dir = workspace_dir / "agent_bundles" / "custom_domain"
    prompt_dir = bundle_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "domain.md").write_text(
        "# {{domain}} Specialist\n\nReturn practical {{domain}} guidance.",
        encoding="utf-8",
    )
    _write_yaml(
        bundle_dir / "agent.yaml",
        {
            "schema_version": 1,
            "agent_templates": {
                "DomainResearchTemplate": {
                    "parameters": {
                        "domain": {"type": "string"},
                        "visible": {"type": "boolean", "default": False},
                    },
                    "definition": {
                        "display_name": "{{domain}} Research Agent",
                        "role": "{{domain}} research assistant",
                        "description": "Researches practical {{domain}} decisions.",
                        "prompt": "# {{domain}} Research\n\nReturn practical {{domain}} guidance.",
                        "default_tools": ["fn:publishing", "fn:schedules", "code:python"],
                        "onboarding": {
                            "visible": "{{visible}}",
                            "default_enabled": False,
                            "default_count": 1,
                            "summary": "{{domain}} research",
                            "description": "Researches {{domain}} decisions.",
                        },
                    },
                }
            },
            "agent_types": {
                "AquariumResearchAgent": {
                    "extends": "DomainResearchTemplate",
                    "params": {"domain": "Aquarium", "visible": True},
                }
            },
        },
    )
    return bundle_dir


def _run_validator(*, config_path: Path, env_path: Path | None) -> dict:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "validate_agent_catalog.py"),
        "--config",
        str(config_path),
        "--json",
    ]
    if env_path is not None:
        command.extend(["--env", str(env_path)])
    validator = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if validator.returncode != 0:
        raise RuntimeError(
            "validate_agent_catalog.py failed: "
            f"stdout={validator.stdout!r} stderr={validator.stderr!r}"
        )
    return json.loads(validator.stdout)


def run_live_check(output_dir: Path, *, config_path: Path, env_path: Path | None) -> dict:
    from assistant_gateway.config import GatewayInstanceConfig, StartupAgentSettings
    from config_loader import CONFIG_SCHEMA_VERSION, reload_config
    from custom_agents.agent_catalog import load_agent_catalog, load_agent_type_map
    from custom_agents.team_composition import (
        TeamCompositionConfig,
        ephemeral_idle_ttl_for_agent_type,
        save_team_composition,
        set_ephemeral_agent_policy,
        set_startup_agent,
        write_generated_agent_bundle,
    )
    from docker.launch_headless import build_startup_agent_commands

    if not config_path.exists():
        raise FileNotFoundError(f"Config file does not exist: {config_path}")
    if env_path is not None and not env_path.exists():
        raise FileNotFoundError(f"Env file does not exist: {env_path}")

    _load_env_file(env_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    reload_config(str(config_path), search_default_paths=False)
    real_config_catalog = load_agent_catalog(include_configured=True)
    real_validator_payload = _run_validator(config_path=config_path, env_path=env_path)
    real_config_expected_agents = {
        "TeamArchitect",
        "InternalImprovementAgent",
        "MemReviewAgent",
        "WebResearchAgent",
        "PersonalAdminAgent",
        "ResearchComparisonAgent",
        "HomeErrandsAgent",
        "LearningCoachingAgent",
        "CodeWritingAgent",
    }
    real_missing = sorted(real_config_expected_agents - set(real_config_catalog.agent_types))
    if real_missing:
        raise AssertionError(f"Real config catalog missing expected agents: {real_missing}")

    with tempfile.TemporaryDirectory(prefix="ua-agent-catalog-live-") as tmp:
        root = Path(tmp)
        workspace_dir = root / "workspace"
        workspace_dir.mkdir()
        bundle_dir = _write_workspace_bundle(workspace_dir)
        composition_path = workspace_dir / "TEAM_AGENTS.yaml"
        composition = TeamCompositionConfig()
        write_generated_agent_bundle(
            workspace_dir=workspace_dir,
            config=composition,
            agent_type="PlanningCriticAgent",
            template_name="TaskSpecialistTemplate",
            params={
                "display_name": "Planning Critic Agent",
                "role": "Planning review specialist",
                "description": "Reviews implementation plans for gaps and risks.",
                "prompt": "# Planning Critic\n\nReview plans for missing risks.",
            },
        )
        composition = set_startup_agent(
            composition,
            agent_type="WebResearchAgent",
            count=1,
            enabled=False,
        )
        composition = set_startup_agent(
            composition,
            agent_type="PlanningCriticAgent",
            count=2,
            enabled=True,
        )
        composition = set_ephemeral_agent_policy(
            composition,
            agent_type="PlanningCriticAgent",
            idle_ttl_seconds=30,
        )
        save_team_composition(composition_path, composition)
        workspace_config_path = root / "config.yaml"
        _write_yaml(
            workspace_config_path,
            {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "agent": {
                    "workspace_dir": str(workspace_dir),
                },
            },
        )

        reload_config(str(workspace_config_path), search_default_paths=False)
        full_catalog = load_agent_catalog(include_configured=True)
        core_map = load_agent_type_map(
            include_configured=False,
            include_default_bundles=False,
        )

        validator_payload = _run_validator(config_path=workspace_config_path, env_path=env_path)

        expected_default_agents = {
            "TeamArchitect",
            "InternalImprovementAgent",
            "MemReviewAgent",
            "WebResearchAgent",
            "PersonalAdminAgent",
            "ResearchComparisonAgent",
            "HomeErrandsAgent",
            "LearningCoachingAgent",
            "CodeWritingAgent",
            "AquariumResearchAgent",
            "PlanningCriticAgent",
        }
        observed_agents = set(full_catalog.agent_types)
        missing = sorted(expected_default_agents - observed_agents)
        if missing:
            raise AssertionError(f"Missing expected agents: {missing}")
        if "PersonalAdminAgent" in core_map or "CodeWritingAgent" in core_map:
            raise AssertionError("Core-only catalog unexpectedly includes bundled agents.")
        if "AquariumResearchAgent" not in validator_payload["agent_types"]:
            raise AssertionError("Validator did not see the workspace default bundle.")
        if "PlanningCriticAgent" not in validator_payload["agent_types"]:
            raise AssertionError("Validator did not see the generated composition bundle.")
        if ephemeral_idle_ttl_for_agent_type(composition, "PlanningCriticAgent") != 30:
            raise AssertionError("Team composition TTL policy did not resolve for generated agent.")

        instance_config = GatewayInstanceConfig.create(
            instance_name="livecomposition",
            instance_root=str(root / "instance"),
            team_id="team_live",
            timezone="UTC",
        )
        instance_config.gateway.startup_agents = [
            StartupAgentSettings(named_agent_type="WebResearchAgent", count=1),
        ]
        startup_commands = build_startup_agent_commands(instance_config, python_executable=sys.executable)
        startup_labels = [label for label, _command in startup_commands]
        if startup_labels != ["PlanningCriticAgent#1", "PlanningCriticAgent#2", "MemReviewAgent"]:
            raise AssertionError(f"Unexpected composition startup command labels: {startup_labels}")

        report = {
            "real_config_path": str(config_path),
            "env_path": str(env_path) if env_path is not None else None,
            "real_config_agent_types": sorted(real_config_catalog.agent_types),
            "real_config_team_templates": sorted(real_config_catalog.team_templates),
            "real_config_source_paths": [str(path) for path in real_config_catalog.source_paths],
            "real_config_validator": real_validator_payload,
            "workspace_config_path": str(workspace_config_path),
            "workspace_bundle": str(bundle_dir),
            "team_composition_path": str(composition_path),
            "team_composition": composition.model_dump(mode="json", exclude_none=True),
            "startup_command_labels": startup_labels,
            "agent_types": sorted(full_catalog.agent_types),
            "core_agent_types": sorted(core_map),
            "team_templates": sorted(full_catalog.team_templates),
            "source_paths": [str(path) for path in full_catalog.source_paths],
            "validator": validator_payload,
        }
        report_path = output_dir / "agent_catalog_modularity_report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a live config/filesystem check for modular agent catalogs.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "test_results" / "live_agent_catalog_modularity"),
        help="Directory where the JSON report should be written.",
    )
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "config.yaml"),
        help="Repo config file used for the real startup-path catalog validation.",
    )
    parser.add_argument(
        "--env",
        default=str(PROJECT_ROOT / "debug.env"),
        help="Env file loaded before real config validation. Use an empty value to skip.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    env_path = Path(args.env).expanduser().resolve() if str(args.env or "").strip() else None
    output_dir = Path(args.output_dir).expanduser().resolve()
    report = run_live_check(
        output_dir,
        config_path=Path(args.config).expanduser().resolve(),
        env_path=env_path,
    )
    print(
        json.dumps(
            {
                "valid": True,
                "report_path": str(output_dir / "agent_catalog_modularity_report.json"),
                "real_config_agent_count": len(report["real_config_agent_types"]),
                "workspace_agent_count": len(report["agent_types"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
