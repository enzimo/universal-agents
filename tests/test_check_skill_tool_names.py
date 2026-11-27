"""The skill tool-name check (scripts/check_skill_tool_names.py) and the bundled skills' ua-tools declarations.

The check reports declared ua-tools ids that are no catalog tool, declarations
over MAX_DECLARED_GROUPS, and backticked calls to names that are no agent
tool; the bundled skill templates must stay clean. A bound on what the
bundled declarations load keeps a later edit from quietly adding thousands of
tokens to every request after a skill is activated with full definitions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from strands import Skill

from access_catalog.catalog import load_catalog
from access_catalog.tool_functions import functions_of_tools
from custom_agents.tool_loading import declared_skill_tool_ids
from custom_tools.provider_contract import tool_name
from custom_tools.registry import ALL_PROVIDER_TOOLS
from scripts.check_skill_tool_names import (
    KIND_INVALID,
    KIND_NOT_CHECKED,
    KIND_TOO_MANY,
    KIND_UNKNOWN_FUNCTION,
    KIND_UNKNOWN_ID,
    MAX_DECLARED_GROUPS,
    check,
    main,
)
from scripts.measure_tool_context import _tokens, definition_text

TEMPLATES = Path(__file__).resolve().parents[1] / "custom_agents" / "skill_templates"
# The most full-definition tokens (o200k) one bundled skill's declaration may load (largest on 2026-11-26: 2,789).
MAX_DECLARED_TOKENS = 3_000


def write_skill(root: Path, name: str, frontmatter_extra: str, body: str, reference: str = "") -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test skill.\n{frontmatter_extra}---\n\n{body}\n", encoding="utf-8")
    if reference:
        (folder / "references").mkdir()
        (folder / "references" / "guide.md").write_text(reference, encoding="utf-8")
    return folder


def test_a_renamed_function_and_an_unknown_tool_id_are_reported_with_the_closest_names(tmp_path: Path) -> None:
    folder = write_skill(
        tmp_path,
        "renamed",
        "metadata:\n  ua-tools: fn:objectives fn:objectivez\n",
        "Search first with `knowledge_serch(query='x')`, then `knowledge_search(query='x')` and `json.loads()`.\n\n"
        "```\nexample_only_call()\n```",
        reference="Later call `create_objectiv()`.\n",
    )

    findings = check([tmp_path])

    assert [(f.path.name, f.line, f.kind) for f in findings] == [
        ("SKILL.md", 5, KIND_UNKNOWN_ID),
        ("SKILL.md", 8, KIND_UNKNOWN_FUNCTION),
        ("guide.md", 1, KIND_UNKNOWN_FUNCTION),
    ]
    assert "fn:objectivez is not a catalog tool id (closest: fn:objectives" in findings[0].message
    assert "knowledge_serch() is not a tool function (closest: knowledge_search" in findings[1].message
    assert str(findings[2]).startswith(f"{folder / 'references' / 'guide.md'}:1: unknown-function: create_objectiv()")


def test_invalid_and_oversized_declarations_are_reported(tmp_path: Path) -> None:
    write_skill(tmp_path, "listed", "metadata:\n  ua-tools: [fn:objectives]\n", "Body.")
    many = " ".join(sorted(load_catalog(None).on_request)[: MAX_DECLARED_GROUPS + 1])
    write_skill(tmp_path, "many", f"metadata:\n  ua-tools: {many}\n", "Body.")

    write_skill(tmp_path, "broken", "metadata: [unclosed\n", "Body.")

    kinds = {(f.path.parent.name, f.kind) for f in check([tmp_path])}

    assert kinds == {("listed", KIND_INVALID), ("many", KIND_TOO_MANY), ("broken", KIND_INVALID)}


def test_a_declared_mcp_server_is_reported_as_not_checked_without_failing(tmp_path: Path, capsys) -> None:
    write_skill(tmp_path, "tickets", "metadata:\n  ua-tools: mcp:tickets fn:objectives\n", "Body.")

    [finding] = check([tmp_path])

    assert (finding.kind, finding.fails) == (KIND_NOT_CHECKED, False) and "mcp:tickets" in finding.message
    assert main([str(tmp_path)]) == 0
    assert "not-checked: mcp:tickets is an MCP server id" in capsys.readouterr().out


def test_the_command_exits_1_with_findings_0_when_clean_and_2_on_bad_arguments(tmp_path: Path, capsys) -> None:
    write_skill(tmp_path / "bad", "bad", "", "Call `no_such_tool()`.")
    write_skill(tmp_path / "good", "good", "metadata:\n  ua-tools: fn:objectives\n", "Call `create_objective()`.")

    assert main([str(tmp_path / "bad")]) == 1
    assert "unknown-function: no_such_tool()" in capsys.readouterr().out
    assert main([str(tmp_path / "good" / "good" / "SKILL.md")]) == 0
    assert main([]) == 2 and main([str(tmp_path / "missing")]) == 2


def test_the_bundled_skill_templates_name_only_real_tools_and_groups() -> None:
    assert [str(finding) for finding in check([TEMPLATES])] == []


@pytest.mark.parametrize("skill_file", sorted(TEMPLATES.glob("*/SKILL.md")), ids=lambda path: path.parent.name)
def test_each_bundled_declaration_parses_through_strands_and_stays_small(skill_file: Path) -> None:
    catalog = load_catalog(None)
    tools = {tool_name(tool): tool for tool in ALL_PROVIDER_TOOLS}
    count = _tokens()

    declared = declared_skill_tool_ids(Skill.from_file(skill_file).metadata, skill_name=skill_file.parent.name)
    loads = {
        name
        for tool_id in declared
        for name in functions_of_tools(catalog, {tool_id}) - catalog.on_request.get(tool_id, frozenset())
        if name in tools
    }

    assert len(declared) <= MAX_DECLARED_GROUPS
    assert set(declared) <= set(catalog.on_request), "declare only groups that load on request"
    assert sum(count(definition_text(tools[name])) for name in loads) <= MAX_DECLARED_TOKENS
