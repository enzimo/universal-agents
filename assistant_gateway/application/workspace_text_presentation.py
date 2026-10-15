"""Readable, display-only presentation for safe workspace text previews."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound


LOGGER = logging.getLogger(__name__)

WORKSPACE_SYNTAX_HIGHLIGHT_MAX_BYTES = 512 * 1024

WORKSPACE_TEXT_SUFFIXES = frozenset(
    {
        ".c",
        ".cfg",
        ".conf",
        ".cpp",
        ".css",
        ".csv",
        ".env.example",
        ".go",
        ".h",
        ".hpp",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsonl",
        ".jsx",
        ".log",
        ".properties",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsv",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
WORKSPACE_TEXT_FILENAMES = frozenset(
    {
        "agents.md",
        "dockerfile",
        "license",
        "makefile",
        "readme",
        "requirements.txt",
    }
)


@dataclass(frozen=True)
class _SyntaxDefinition:
    label: str
    lexer_alias: str


@dataclass(frozen=True)
class WorkspaceTextPresentation:
    """Optional presentation data that never replaces the raw file content."""

    syntax_language: str | None = None
    highlighted_html: str | None = None
    formatted_content: str | None = None


_SYNTAX_BY_SUFFIX = {
    ".c": _SyntaxDefinition("C", "c"),
    ".cfg": _SyntaxDefinition("Configuration", "ini"),
    ".conf": _SyntaxDefinition("Configuration", "ini"),
    ".cpp": _SyntaxDefinition("C++", "cpp"),
    ".css": _SyntaxDefinition("CSS", "css"),
    ".go": _SyntaxDefinition("Go", "go"),
    ".h": _SyntaxDefinition("C", "c"),
    ".hpp": _SyntaxDefinition("C++", "cpp"),
    ".ini": _SyntaxDefinition("INI", "ini"),
    ".java": _SyntaxDefinition("Java", "java"),
    ".js": _SyntaxDefinition("JavaScript", "javascript"),
    ".json": _SyntaxDefinition("JSON", "json"),
    ".jsonl": _SyntaxDefinition("JSON Lines", "json"),
    ".jsx": _SyntaxDefinition("JSX", "jsx"),
    ".properties": _SyntaxDefinition("Properties", "properties"),
    ".py": _SyntaxDefinition("Python", "python"),
    ".rb": _SyntaxDefinition("Ruby", "ruby"),
    ".rs": _SyntaxDefinition("Rust", "rust"),
    ".sh": _SyntaxDefinition("Shell", "bash"),
    ".sql": _SyntaxDefinition("SQL", "sql"),
    ".toml": _SyntaxDefinition("TOML", "toml"),
    ".ts": _SyntaxDefinition("TypeScript", "typescript"),
    ".tsx": _SyntaxDefinition("TSX", "tsx"),
    ".xml": _SyntaxDefinition("XML", "xml"),
    ".yaml": _SyntaxDefinition("YAML", "yaml"),
    ".yml": _SyntaxDefinition("YAML", "yaml"),
}
_SYNTAX_BY_FILENAME = {
    "dockerfile": _SyntaxDefinition("Dockerfile", "docker"),
    "makefile": _SyntaxDefinition("Makefile", "make"),
}
_HTML_FORMATTER = HtmlFormatter(nowrap=True, classprefix="ua-syntax-")


def is_workspace_text_file(*, name: str, content_type: str | None) -> bool:
    """Return whether a safe file name belongs to the text preview family."""

    lowered_name = name.casefold()
    suffix = Path(lowered_name).suffix
    return (
        suffix in WORKSPACE_TEXT_SUFFIXES
        or lowered_name in WORKSPACE_TEXT_FILENAMES
        or str(content_type or "").startswith("text/")
    )


def present_workspace_text(
    *,
    name: str,
    content: str,
    size_bytes: int,
) -> WorkspaceTextPresentation:
    """Build bounded highlighting and formatting without changing raw content."""

    definition = _syntax_definition(name)
    if definition is None:
        return WorkspaceTextPresentation()

    if size_bytes > WORKSPACE_SYNTAX_HIGHLIGHT_MAX_BYTES:
        return WorkspaceTextPresentation(
            syntax_language=definition.label,
        )

    formatted_content = _format_json_for_display(name=name, content=content)
    displayed_content = formatted_content or content
    try:
        lexer = get_lexer_by_name(
            definition.lexer_alias,
            stripnl=False,
            ensurenl=False,
        )
        highlighted_html = highlight(displayed_content, lexer, _HTML_FORMATTER)
    except (ClassNotFound, RuntimeError, ValueError) as exc:
        LOGGER.warning(
            "Workspace syntax highlighting failed; using escaped plain text",
            extra={
                "workspace_filename": name,
                "syntax_language": definition.label,
                "error_type": type(exc).__name__,
            },
        )
        highlighted_html = None

    return WorkspaceTextPresentation(
        syntax_language=definition.label,
        highlighted_html=highlighted_html,
        formatted_content=formatted_content,
    )


def _syntax_definition(name: str) -> _SyntaxDefinition | None:
    lowered_name = name.casefold()
    return _SYNTAX_BY_FILENAME.get(lowered_name) or _SYNTAX_BY_SUFFIX.get(
        Path(lowered_name).suffix
    )


def _format_json_for_display(*, name: str, content: str) -> str | None:
    if Path(name.casefold()).suffix != ".json":
        return None
    try:
        parsed = json.loads(content, parse_constant=_reject_nonstandard_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None
    formatted = json.dumps(parsed, ensure_ascii=False, indent=2)
    if content.endswith(("\n", "\r")):
        formatted += "\n"
    return formatted


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")
