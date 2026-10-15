#!/usr/bin/env python3
"""Regenerate the secret variable tables in user_docs_source/docker.md.

The tables (which process receives each secret, its Docker secret file form,
and what it is) come from docker/secret_env_vars.yaml, the only list of secret
environment variables. They replace the text between the two marker comments
in the Docker guide. Run this after changing the list; ``--check`` reports a
stale guide without writing, and tests/test_env_secrets.py runs the same
comparison in the fast test profile.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from env_secrets import (  # noqa: E402
    FILE_SUFFIX,
    FORM_NONE,
    FORM_VALUE,
    SECRET_ENV_LIST,
    SecretEnvEntry,
    SecretEnvList,
)


DOC_PATH = REPO_ROOT / "user_docs_source" / "docker.md"
BEGIN_MARKER = "<!-- BEGIN GENERATED: secret variables (scripts/export_secret_env_docs.py) -->"
END_MARKER = "<!-- END GENERATED: secret variables -->"
REGENERATE_COMMAND = ".venv/bin/python scripts/export_secret_env_docs.py"

# What each ``file`` value means, as shown in the docs' "Secret file" column.
FORM_LABELS = {
    FORM_VALUE: "contents",
    FORM_NONE: "none",
}


def _cell(text: str) -> str:
    """Text safe inside a Markdown table cell."""

    return text.replace("|", "\\|").strip()


def _processes_table(secret_env_list: SecretEnvList) -> list[str]:
    lines = ["| Process | What it is |", "| --- | --- |"]
    lines.extend(f"| `{name}` | {_cell(about)} |" for name, about in secret_env_list.processes.items())
    return lines


def _variable_row(name: str, entry: SecretEnvEntry) -> str:
    receivers = ", ".join(f"`{process}`" for process in entry.receivers)
    return f"| `{name}` | {receivers} | {FORM_LABELS[entry.file]} | {_cell(entry.about)} |"


def render_secret_env_section(secret_env_list: SecretEnvList = SECRET_ENV_LIST) -> str:
    """The Markdown between the markers, without the markers themselves."""

    lines = [
        "This part is generated from `docker/secret_env_vars.yaml`; edit that file and",
        f"run `{REGENERATE_COMMAND}` instead of editing it here.",
        "",
        "The processes named in the tables:",
        "",
        *_processes_table(secret_env_list),
        "",
        f"The *Secret file* column says how `<NAME>{FILE_SUFFIX}` works: *contents* means",
        "the file's contents become the value, and *none* means there is no file",
        "form; the entry only limits which processes receive the variable. A name",
        "ending in `*` covers every variable that starts with the text before it.",
    ]
    for group in secret_env_list.groups:
        lines.extend(
            [
                "",
                f"#### {group.name}",
                "",
                "| Variable | Processes that receive it | Secret file | What it is |",
                "| --- | --- | --- | --- |",
            ]
        )
        lines.extend(_variable_row(name, entry) for name, entry in group.variables.items())
    return "\n".join(lines)


def rendered_doc(current: str, secret_env_list: SecretEnvList = SECRET_ENV_LIST) -> str:
    """``current`` with the generated section replaced; the markers must appear once each."""

    if current.count(BEGIN_MARKER) != 1 or current.count(END_MARKER) != 1:
        raise ValueError(f"{DOC_PATH} must contain each generated-section marker exactly once")
    before, rest = current.split(BEGIN_MARKER)
    _old, after = rest.split(END_MARKER)
    return f"{before}{BEGIN_MARKER}\n{render_secret_env_section(secret_env_list)}\n{END_MARKER}{after}"


def secret_env_doc_is_stale(path: Path = DOC_PATH) -> bool:
    """Whether the Docker guide's tables differ from the list."""

    current = path.read_text(encoding="utf-8")
    return rendered_doc(current) != current


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report a stale guide without writing; exit non-zero if it drifted.",
    )
    args = parser.parse_args()
    if args.check:
        if secret_env_doc_is_stale():
            parser.error(f"Stale secret variable tables in {DOC_PATH.relative_to(REPO_ROOT)}; run {REGENERATE_COMMAND}")
        return
    DOC_PATH.write_text(rendered_doc(DOC_PATH.read_text(encoding="utf-8")), encoding="utf-8")


if __name__ == "__main__":
    main()
