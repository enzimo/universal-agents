#!/usr/bin/env python3
"""Exercise a clean runtime-only installation without test or documentation tools."""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
from pathlib import Path
import runpy
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logger = logging.getLogger(__name__)


def main() -> None:
    installed = {dist.metadata["Name"].lower() for dist in importlib.metadata.distributions()}
    development_tools = {
        "coverage", "complexipy", "mkdocs", "mkdocs-material", "mypy",
        "pip-audit", "pytest", "pytest-asyncio", "ruff", "vulture", "uv",
    }
    unexpected = installed & development_tools
    if unexpected:
        raise SystemExit(f"Expected a runtime-only environment; found {sorted(unexpected)}")

    # Import the actual entry points and service owners, including integrations
    # whose undeclared imports can be masked by a developer's environment.
    for module in (
        "strands_main", "assistant_cli", "assistant_gateway.gateway",
        "assistant_gateway.interactive_apps.discord",
        "assistant_gateway.interactive_apps.telegram",
        "assistant_gateway.interactive_apps.whatsapp",
        "secure_tool_broker.service", "activity_collector.service",
        "custom_agents.installed_permission_registry",
        "user_docs.service",
    ):
        importlib.import_module(module)
    runpy.run_path(str(ROOT / "mcp" / "skills_mcp.py"), run_name="runtime_install_smoke")

    from config_loader import load_config
    from custom_agents.conversation_store import ConversationStore
    from custom_agents.memory_plugins.config import normalize_memory_config

    config = load_config(None, search_default_paths=False)
    normalize_memory_config(config.get("agent.memory", {}))
    with tempfile.TemporaryDirectory(prefix="ua-runtime-install-") as directory:
        store = ConversationStore(db_path=str(Path(directory) / "conversation.db"), artifacts_dir=Path(directory) / "conversation_artifacts")
        store.ensure_thread(
            conversation_id="install-smoke", team_id="install-smoke",
            owner_agent_id="manager", owner_agent_name="manager",
            requester_name="user", requester_id="user", root_task_id="task",
            subject_preview="Runtime installation check",
        )
        assert store.get_thread("install-smoke") is not None
    logger.info("Runtime-only imports, configuration, and SQLite persistence passed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
