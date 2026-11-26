"""find_tools finds the right catalog tool for plain-language requests (custom_tools.tool_search).

No model is involved: every request in ``tests/data/tool_finding_queries.yaml``
is ranked against the whole catalog the way ``find_tools`` ranks it, and the
expected tool (or one named in its ``also`` list) must come near the top.
Baseline before the ranking changed (2026-11-26): 78% in the top five, 87% in
the top ten, four requests found nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from access_catalog.catalog import load_catalog
from access_catalog.tool_functions import functions_of_tools, mcp_function_names
from custom_tools.providers.tool_loading import _best_matches
from custom_tools.tool_search import normalize, query_words

QUERIES = Path(__file__).parent / "data" / "tool_finding_queries.yaml"
TOP_FIVE_SHARE = 0.97
TOP = 10


def _catalog_and_queries():
    catalog = load_catalog()
    tools = yaml.safe_load(QUERIES.read_text(encoding="utf-8"))["tools"]
    candidates = [
        (tool, sorted(functions_of_tools(catalog, {tool.tool_id}) | mcp_function_names(catalog, tool.tool_id)))
        for tool in sorted(catalog.tools.values(), key=lambda item: item.tool_id)
    ]
    return catalog, tools, candidates


def _rank(candidates, query: str, expected: set[str]) -> int | None:
    found = [tool.tool_id for tool, _functions in _best_matches(candidates, query)]
    return next((index + 1 for index, tool_id in enumerate(found) if tool_id in expected), None)


def test_every_catalog_tool_has_at_least_three_requests() -> None:
    catalog, tools, _candidates = _catalog_and_queries()
    missing = sorted(tool_id for tool_id in catalog.tools if len((tools.get(tool_id) or {}).get("queries") or ()) < 3)
    unknown = sorted(set(tools) - set(catalog.tools))
    assert not missing, f"add three plain-language requests for: {missing}"
    assert not unknown, f"requests name tools the catalog no longer has: {unknown}"


def test_plain_language_requests_find_their_tool_near_the_top() -> None:
    _catalog, tools, candidates = _catalog_and_queries()
    ranks: list[tuple[str, str, int | None]] = []
    for tool_id, entry in tools.items():
        expected = {tool_id, *(entry.get("also") or ())}
        for query in entry["queries"]:
            ranks.append((tool_id, query, _rank(candidates, query, expected)))
    beyond_top = [(tool_id, query, rank) for tool_id, query, rank in ranks if rank is None or rank > TOP]
    top_five = sum(1 for _tool, _query, rank in ranks if rank is not None and rank <= 5) / len(ranks)
    assert not beyond_top, f"not in the top {TOP}: {beyond_top}"
    assert top_five >= TOP_FIVE_SHARE, f"only {top_five:.1%} of requests found their tool in the top five"


def test_a_query_naming_a_function_puts_its_tool_first() -> None:
    _catalog, _tools, candidates = _catalog_and_queries()
    assert _rank(candidates, "knowledge_search", {"fn:knowledge"}) == 1
    assert _rank(candidates, "use list_scheduled_jobs", {"fn:schedule_runs"}) == 1


def test_words_compare_without_plural_endings_and_filler() -> None:
    assert normalize("schedules") == normalize("schedule") == normalize("scheduled")[:6]
    assert normalize("entries") == "entry"
    assert normalize("access") == "access", "a double s is not a plural"
    assert query_words("the a of my") == set()
    assert "memory" in query_words("remember this"), "everyday words map to the catalog's words"


@pytest.mark.parametrize("query", ["", "   "])
def test_an_empty_query_lists_every_candidate(query: str) -> None:
    _catalog, _tools, candidates = _catalog_and_queries()
    assert len(_best_matches(candidates, query)) == len(candidates)
