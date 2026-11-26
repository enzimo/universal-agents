"""What each role's first request carries in tool definitions, per tool presentation (scripts/measure_tool_context.py).

The tool index experiment merges only if it saves context; these checks keep
the measured savings from eroding as tools are added. Numbers on 2026-11-26
with compact index signatures and ``call_tool``:
worker 11,665 -> 4,561 / 4,919 tokens, team lead 18,641 -> 8,748 / 9,229,
system 15,135 -> 4,960 / 5,353.
"""

from __future__ import annotations

import pytest

from runtime_settings.tool_presentation_contract import (
    TOOL_PRESENTATION_FULL_DEFINITIONS,
    TOOL_PRESENTATION_INDEX,
    TOOL_PRESENTATION_INDEX_AND_SCRIPTS,
)
from scripts.measure_tool_context import measure

# The index setups must cost at most this share of full definitions.
MAX_SHARE_OF_FULL = 0.5
# The core every role keeps in full stays small.
MAX_CORE_TOOLS = {"worker": 22, "system": 22, "team_lead": 34}


@pytest.fixture(scope="module")
def rows():
    return {(row.role, row.presentation): row for row in measure()}


@pytest.mark.parametrize("role", ["worker", "team_lead", "system"])
@pytest.mark.parametrize("presentation", [TOOL_PRESENTATION_INDEX, TOOL_PRESENTATION_INDEX_AND_SCRIPTS])
def test_an_index_costs_at_most_half_of_full_definitions(rows, role: str, presentation: str) -> None:
    full = rows[(role, TOOL_PRESENTATION_FULL_DEFINITIONS)]
    index = rows[(role, presentation)]
    assert index.total_tokens <= full.total_tokens * MAX_SHARE_OF_FULL, (role, presentation, index, full)
    assert index.shown <= MAX_CORE_TOOLS[role], (role, index.shown)


@pytest.mark.parametrize("role", ["worker", "team_lead", "system"])
def test_the_index_lists_every_held_tool_not_shown(rows, role: str) -> None:
    full = rows[(role, TOOL_PRESENTATION_FULL_DEFINITIONS)]
    index = rows[(role, TOOL_PRESENTATION_INDEX)]
    # Full definitions hide the catalog's on_request groups without listing them per function,
    # so the index setups must account for at least everything full definitions showed.
    assert index.shown + index.indexed >= full.shown
