"""The ``secure_cli`` provider of a CLI the team installed (``cli:<name>``, owner's decision 2026-09-28).

Installed CLIs are catalog data, not code (``custom_messages.contracts.cli_tool_v1``),
so their providers are not registered at start-up: ``installed_cli_provider``
builds one from the current tool catalog when an agent or the broker asks for
the provider name (``secure_tools.registry.set_provider_resolver``). The one
action is ``exec`` with ``params={"argv": [...]}`` (after the program's name)
and optional ``stdin``; the broker checks the list against the approved
command table and runs it (``secure_tool_broker.installed_clis``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from access_catalog.cli_commands import argv_from_params
from secure_tools.providers import ActionSpec, ToolContext, ToolResult, ValidatedAction

INSTALLED_CLI_ACTION = "exec"
MAX_STDIN_CHARS = 1_000_000


@dataclass(frozen=True)
class InstalledCliProvider:
    """One installed CLI as a ``secure_cli`` provider (module docstring)."""

    name: str
    tool: Any  # ToolDefinitionV1 with installed_cli
    policy: Any  # access_catalog.cli_tables.CliPolicy

    @property
    def actions(self) -> Mapping[str, ActionSpec]:
        return {
            INSTALLED_CLI_ACTION: ActionSpec(
                INSTALLED_CLI_ACTION,
                f"Run one {self.tool.display_name} command: params argv (a list, without the program name) "
                "and optional stdin. Each command needs its catalog level; unlisted commands need full.",
            )
        }

    def validate(self, action: str, params: dict[str, Any]) -> ValidatedAction:
        if action != INSTALLED_CLI_ACTION:
            raise ValueError(f"{self.name} has one action, {INSTALLED_CLI_ACTION}.")
        argv = argv_from_params(params, cli=self.name)
        checked: dict[str, Any] = {"argv": argv}
        stdin = params.get("stdin")
        if stdin is not None:
            if not isinstance(stdin, str) or len(stdin) > MAX_STDIN_CHARS:
                raise ValueError(f"stdin is text of at most {MAX_STDIN_CHARS} characters.")
            checked["stdin"] = stdin
        return ValidatedAction(self.name, INSTALLED_CLI_ACTION, checked)

    def execute(self, action: ValidatedAction, context: ToolContext) -> ToolResult:
        from secure_tool_broker.installed_clis import run_installed_cli

        return run_installed_cli(self.tool, self.policy, action, context)


def installed_cli_provider(catalog: Any, name: str) -> InstalledCliProvider | None:
    """The provider of the installed CLI ``name`` in ``catalog``, or None."""

    name = str(name or "").strip().lower()
    tool = catalog.tools.get(f"cli:{name}")
    policy = catalog.cli_policies.get(name)
    if tool is None or tool.installed_cli is None or policy is None:
        return None
    return InstalledCliProvider(name=name, tool=tool, policy=policy)


__all__ = ["INSTALLED_CLI_ACTION", "InstalledCliProvider", "installed_cli_provider"]
