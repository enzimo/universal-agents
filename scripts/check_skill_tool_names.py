#!/usr/bin/env python3
"""Report skills that name tools or tool groups this runtime does not have.

Why: a skill's text tells the agent which functions to call, and its
``metadata: {ua-tools: "..."}`` frontmatter names the catalog tool groups that
load when the skill is activated (``custom_agents.tool_loading``). A function
renamed in the catalog, or a group id with a typo, leaves the skill sending the
agent to a call that fails; the runtime only logs unknown ``ua-tools`` ids.
This finds both before an agent does.

Checks, for every skill folder (a folder with ``SKILL.md``) under each
argument:

- ``ua-tools`` is a space-separated string of catalog tool ids, at most
  ``MAX_DECLARED_GROUPS`` of them (a declaration names only the few groups the
  skill's main procedure needs; lead ruling of the tool index round two).
- Every backticked name written as a call, such as ```name(...)``` or
  ```name()```, in ``SKILL.md`` and ``references/*.md`` outside fenced code
  blocks, is a catalog function, a catalog dispatcher or another agent tool.
  Dotted names (``json.loads()``) are not checked.

Usable on any skill folder, including the sibling ua-skills repository.
Prints ``path:line: kind: message`` per finding; exits 0 when clean, 1 with
findings, 2 on bad arguments. ``tests/test_check_skill_tool_names.py`` runs it
over ``custom_agents/skill_templates``.

Run: ``.venv/bin/python scripts/check_skill_tool_names.py SKILL_DIR [SKILL_DIR ...]``
"""

from __future__ import annotations

import difflib
import logging
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from access_catalog.catalog import ToolCatalog, load_catalog
from custom_agents.tool_loading import SKILL_TOOLS_METADATA_KEY
from custom_tools.provider_contract import tool_name

SKILL_FILE = "SKILL.md"
MAX_DECLARED_GROUPS = 6
# A whole backtick span that is one plain name followed by an argument list: `name(...)`.
_CALL = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)\([^`]*\)`")
_FENCE = re.compile(r"^\s*(```|~~~)")

KIND_INVALID = "invalid-ua-tools"
KIND_UNKNOWN_ID = "unknown-tool-id"
KIND_TOO_MANY = "too-many-groups"
KIND_UNKNOWN_FUNCTION = "unknown-function"


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.message}"


def known_function_names(catalog: ToolCatalog) -> set[str]:
    """Every name an agent may be told to call: catalog functions, dispatchers, provider and turn tools."""

    from custom_agents.runtime_guardrails import STRUCTURED_OUTPUT_TOOL_NAMES
    from custom_tools.registry import ALL_PROVIDER_TOOLS

    return (
        set(catalog.functions)
        | set(catalog.function_dispatchers)
        | {tool_name(tool) for tool in ALL_PROVIDER_TOOLS}
        | set(STRUCTURED_OUTPUT_TOOL_NAMES)
    )


def skill_folders(roots: Iterable[Path]) -> list[Path]:
    """The skill folders under these paths (a path may itself be a skill folder or a SKILL.md)."""

    found: set[Path] = set()
    for root in roots:
        if root.is_file() and root.name == SKILL_FILE:
            found.add(root.parent)
        elif root.is_dir():
            found.update(path.parent for path in root.rglob(SKILL_FILE))
    return sorted(found)


def _frontmatter(lines: list[str]) -> dict[str, Any]:
    """The SKILL.md frontmatter mapping, or {} without one."""

    if not lines or lines[0].strip() != "---":
        return {}
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            loaded = yaml.safe_load("\n".join(lines[1:index])) or {}
            return loaded if isinstance(loaded, dict) else {}
    return {}


def _declaration_line(lines: list[str]) -> int:
    return next((number for number, text in enumerate(lines, 1) if SKILL_TOOLS_METADATA_KEY in text), 1)


def check_declaration(path: Path, lines: list[str], catalog: ToolCatalog) -> list[Finding]:
    """Findings for the skill's ``ua-tools`` frontmatter."""

    try:
        metadata = _frontmatter(lines).get("metadata")
    except yaml.YAMLError as exc:
        return [Finding(path, 1, KIND_INVALID, f"the frontmatter is not valid YAML ({type(exc).__name__})")]
    if not isinstance(metadata, dict) or SKILL_TOOLS_METADATA_KEY not in metadata:
        return []
    line = _declaration_line(lines)
    value = metadata[SKILL_TOOLS_METADATA_KEY]
    if not isinstance(value, str):
        return [Finding(path, line, KIND_INVALID, f"{SKILL_TOOLS_METADATA_KEY} must be one space-separated string of tool ids")]
    declared = list(dict.fromkeys(value.split()))
    findings = [
        Finding(path, line, KIND_UNKNOWN_ID, f"{tool_id} is not a catalog tool id{_closest(tool_id, set(catalog.tools))}")
        for tool_id in declared
        if tool_id not in catalog.tools
    ]
    if len(declared) > MAX_DECLARED_GROUPS:
        findings.append(
            Finding(path, line, KIND_TOO_MANY, f"{len(declared)} groups declared; name at most {MAX_DECLARED_GROUPS}, the ones the main procedure needs")
        )
    return findings


def _prose_lines(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Numbered lines outside fenced code blocks."""

    fenced = False
    for number, text in enumerate(lines, 1):
        if _FENCE.match(text):
            fenced = not fenced
            continue
        if not fenced:
            yield number, text


def check_calls(path: Path, lines: list[str], known: set[str]) -> list[Finding]:
    """Findings for backticked calls to names that are no agent tool."""

    return [
        Finding(path, number, KIND_UNKNOWN_FUNCTION, f"{name}() is not a tool function{_closest(name, known)}")
        for number, text in _prose_lines(lines)
        for name in _CALL.findall(text)
        if name not in known
    ]


def _closest(name: str, known: set[str]) -> str:
    matches = difflib.get_close_matches(name, sorted(known), n=3, cutoff=0.6)
    return f" (closest: {', '.join(matches)})" if matches else ""


def check_skill(folder: Path, catalog: ToolCatalog, known: set[str]) -> list[Finding]:
    skill_file = folder / SKILL_FILE
    lines = skill_file.read_text(encoding="utf-8").splitlines()
    findings = check_declaration(skill_file, lines, catalog) + check_calls(skill_file, lines, known)
    for reference in sorted((folder / "references").glob("*.md")):
        findings += check_calls(reference, reference.read_text(encoding="utf-8").splitlines(), known)
    return findings


def check(roots: Iterable[Path]) -> list[Finding]:
    catalog = load_catalog(None)
    known = known_function_names(catalog)
    return [finding for folder in skill_folders(roots) for finding in check_skill(folder, catalog, known)]


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.WARNING)
    roots = [Path(arg) for arg in argv]
    if not roots or any(not root.exists() for root in roots):
        print("usage: check_skill_tool_names.py SKILL_DIR [SKILL_DIR ...] (every path must exist)", file=sys.stderr)
        return 2
    findings = check(roots)
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
