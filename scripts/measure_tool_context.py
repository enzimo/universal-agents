#!/usr/bin/env python3
"""Measure what each role's first model request carries in tool definitions, per tool presentation.

Why: the tool index experiment (ua-architecture ``plans/tool_index_and_scripts_plan.md``)
must show real savings before it merges. This counts, for each role's standard
tools and each ``agent.tool_presentation``, the functions shown with full
definitions, their tokens (OpenAI's o200k tokenizer, the definitions as a
provider receives them), and the tokens of the one-line index that lists the
rest. It needs no model and no running instance; ``tests/test_tool_presentation_budget.py``
checks the same numbers.

Run: ``.venv/bin/python scripts/measure_tool_context.py``
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from access_catalog.catalog import load_catalog
from access_catalog.tool_functions import functions_of_tool_sets
from access_control.standard_grants import INTERNET_SET, STANDARD_TOOL_SETS_BY_ROLE
from custom_agents.tool_index import index_text
from custom_agents.tool_loading import shown_names
from custom_agents.tool_presentation import withheld_functions
from custom_tools.provider_contract import tool_name
from custom_tools.registry import ALL_PROVIDER_TOOLS
from runtime_settings.tool_presentation_contract import TOOL_PRESENTATION_INDEX_AND_SCRIPTS, TOOL_PRESENTATIONS

ROLES = ("worker", "team_lead", "system")


@dataclass(frozen=True)
class Measurement:
    role: str
    presentation: str
    shown: int
    shown_tokens: int
    indexed: int
    index_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.shown_tokens + self.index_tokens


def _tokens() -> Any:
    import tiktoken

    encoding = tiktoken.get_encoding("o200k_base")
    return lambda text: len(encoding.encode(text))


def definition_text(tool: Any) -> str:
    """One tool definition as a provider receives it (name, description, input schema)."""

    spec = tool.tool_spec
    return json.dumps(
        {"name": spec["name"], "description": spec.get("description", ""), "inputSchema": spec.get("inputSchema", {})},
        separators=(",", ":"),
    )


def measure(roles: tuple[str, ...] = ROLES) -> list[Measurement]:
    count = _tokens()
    catalog = load_catalog()
    tools = {tool_name(tool): tool for tool in ALL_PROVIDER_TOOLS}
    rows: list[Measurement] = []
    for role in roles:
        held = functions_of_tool_sets(catalog, (*STANDARD_TOOL_SETS_BY_ROLE[role], INTERNET_SET)) & set(tools)
        for presentation in TOOL_PRESENTATIONS:
            # A role's agent without definition defaults: what every agent of the role starts with.
            agent = SimpleNamespace(id=f"measure-{role}", named_agent_type="", default_tools=(), tool_filter={"tool_presentation": presentation})
            shown = shown_names(agent, catalog, held, at_start=True)
            indexed = held - shown - withheld_functions(agent, catalog)
            text = (
                index_text(catalog, indexed, tools, scripts=presentation == TOOL_PRESENTATION_INDEX_AND_SCRIPTS)
                if presentation != TOOL_PRESENTATIONS[0]
                else ""
            )
            rows.append(
                Measurement(
                    role=role,
                    presentation=presentation,
                    shown=len(shown),
                    shown_tokens=sum(count(definition_text(tools[name])) for name in shown),
                    indexed=len(indexed) if text else 0,
                    index_tokens=count(text) if text else 0,
                )
            )
    return rows


def main() -> int:
    logging.disable(logging.CRITICAL)
    print(f"{'role':<10} {'presentation':<18} {'shown':>5} {'tokens':>7} {'indexed':>7} {'index':>6} {'total':>7}")
    for row in measure():
        print(
            f"{row.role:<10} {row.presentation:<18} {row.shown:>5} {row.shown_tokens:>7} "
            f"{row.indexed:>7} {row.index_tokens:>6} {row.total_tokens:>7}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
