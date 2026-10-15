#!/usr/bin/env python3
"""Generate ``ua_definitions/access/cli/gog.yaml`` from a ``gog schema --json`` dump.

Run it whenever the pinned gogcli version changes (``GOGCLI_VERSION`` in
``Dockerfile.bundle``):

    gog schema --json > /tmp/gog-schema.json
    .venv/bin/python scripts/generate_gog_command_table.py /tmp/gog-schema.json

Every leaf command gets a level. The rules below apply in this order:
``OVERRIDES`` (exact dotted paths), ``GROUP_RULES`` (command prefixes), then
the last command word (``READ_WORDS`` / ``WRITE_WORDS`` / ``FULL_WORDS``).
Anything that matches nothing is ``full``. Review the diff of the generated
file before committing: the table decides what a read or write grant allows.

The file also records command and flag aliases, root flags, which flags and
positional arguments name local files (the broker checks those against the
task's folders), and the read commands that cannot run under ``--readonly``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "ua_definitions" / "access" / "cli" / "gog.yaml"

# Never run through passthrough: they manage the broker's own credentials or
# configuration, run local programs or servers, or read/write local repositories.
BLOCKED_COMMANDS = {
    "auth": "manages the broker's Google credentials",
    "login": "manages the broker's Google credentials",
    "logout": "manages the broker's Google credentials",
    "config": "changes the broker's gog configuration",
    "mcp": "runs a local MCP server with the broker's credentials",
    "backup": "reads and writes local backup repositories and keys",
    "completion": "prints shell completion scripts (not useful through the broker)",
    "drive.changes.serve": "runs a local web server and shell hooks",
    "drive.changes.poll": "runs a local shell command for each change",
    "docs.comments.poll": "runs a local shell command for each comment",
    "gmail.settings.watch.serve": "runs a local web server",
    "gmail.track": "manages a local tracking worker and keys",
    "zoom.auth": "manages the broker's Zoom credentials",
    "update": "replaces or checks the broker's own gog program",
    "drive.sync": "syncs whole local folders; the broker copies only named files",
    "appscript.pull": "writes a whole local folder of script files (use appscript content to read them)",
}

# Flags that are never accepted, wherever they appear: they replace the
# broker's credentials, point gog at other local state, change the broker's
# command restrictions, run local programs, or log sensitive data.
BLOCKED_FLAGS = {
    "access-token", "access-token-file", "access-token-stdin", "refresh-token-file",
    "refresh-token-stdin", "home", "enable-commands", "enable-commands-exact",
    "disable-commands", "verbose", "on-change", "on-new", "mmdc", "service-account",
    "key", "key-env", "key-stdin", "cert", "identity", "repo", "config", "worker-dir",
    "admin-key", "tracking-key", "channel-token-file", "state-file", "filter-file",
    "redirect-host", "redirect-uri", "client-secret", "password", "impersonate", "token",
    "quota-project",  # bills API use to another Google Cloud project
}
# Credential flags from other gog versions; always refused, even if absent here.
EXTRA_BLOCKED_SPELLINGS = {
    "--credentials", "--credential", "--oauth", "--refresh-token", "--config-file",
    "--config-path", "--keyring", "--keyring-password", "--token-file",
}

# Flags whose value is a local file or directory. '-' (standard input or
# output) is accepted; any other value must be inside the task's folders.
LOCAL_PATH_FLAGS = {
    "attach", "body-file", "body-html-file", "content-file", "file", "from-file",
    "note-file", "notes-file", "signature-file", "out", "out-dir", "raw-file",
}
# Flags that accept '@path' as a file reference (inline JSON otherwise). The
# 'request' flag also accepts a plain path, so it is treated as a path flag.
FILE_REFERENCE_FLAGS = {
    "columns-json", "data-json", "format-json", "spec-json", "request", "gradient-rule-json",
}
# Positional arguments that name local files, by name everywhere, and by name
# for single commands where the name ('file') means a Drive file elsewhere.
LOCAL_PATH_POSITIONALS = {"localPath", "image"}
COMMAND_PATH_POSITIONALS = {"gmail.import": {"file"}}

# Read-level commands get gog's --readonly, which blocks every request that
# is not a GET or one of gog's known read-only POSTs. These read commands use
# another read-only POST (checked in the gog source), so they run without it.
READONLY_EXEMPT = {
    "maps.places.search": "Places text search is a POST to places:searchText",
}

GROUP_RULES: list[tuple[str, str]] = [
    ("admin", "full"),  # domain administration
    ("api", "full"),  # generic Discovery API calls
    ("appscript.run", "full"),  # runs arbitrary Apps Script
    ("batch", "write"),
    ("gmail.settings", "full"),  # filters, forwarding, delegates and auto-replies redirect mail
    ("gmail.autoreply", "full"),
    ("groups.members.add", "full"),
    ("groups.members.remove", "full"),
]

OVERRIDES = {
    "send": "write",
    "ls": "read",
    "search": "read",
    "open": "read",
    "download": "read",
    "upload": "write",
    "me": "read",
    "whoami": "read",
    "status": "read",
    "version": "read",
    "schema": "read",
    "time": "read",
    "gmail.settings.filters.list": "read",
    "gmail.settings.forwarding.list": "read",
    "gmail.settings.delegates.list": "read",
    "gmail.settings.sendas.list": "read",
    "gmail.settings.vacation.get": "read",
    "gmail.settings.autoforward.get": "read",
    "gmail.settings.filters.export": "read",
    "gmail.settings.filters.get": "read",
    "gmail.settings.forwarding.get": "read",
    "gmail.settings.delegates.get": "read",
    "gmail.settings.sendas.get": "read",
    "gmail.settings.watch.status": "read",
    "batch.list": "read",
    "batch.show": "read",
    # The generic API group needs full; these only describe the discovery documents.
    "api.list": "read",
    "api.describe": "read",
    # Watches deliver notifications to a destination the caller chooses.
    "drive.changes.watch": "full",
    "forms.watch.create": "full",
    "drive.share": "full",
    "drive.unshare": "full",
    "drive.permissions": "full",
    "calendar.acl": "full",
    # Reviewed individually (no rule matched their wording).
    "analytics.accounts": "read",
    "appscript.content": "read",
    "calendar.alias.unset": "write",
    "calendar.calendars": "read",
    "calendar.colors": "read",
    "calendar.create-calendar": "write",
    "calendar.event": "read",
    "calendar.propose-time": "read",
    "calendar.team": "read",
    "calendar.time": "read",
    "calendar.users": "read",
    "chat.dm.space": "write",
    "chat.messages.react": "write",
    "classroom.announcements.assignees": "write",
    "classroom.courses.leave": "full",
    "classroom.coursework.assignees": "write",
    "classroom.roster": "read",
    "classroom.submissions.grade": "write",
    "classroom.submissions.reclaim": "write",
    "classroom.submissions.return": "write",
    "classroom.submissions.turn-in": "write",
    "docs.add-tab": "write",
    "docs.cell-style": "write",
    "docs.comments.locate": "read",
    "docs.delete-tab": "full",
    "docs.find-range": "read",
    "docs.insert-date-chip": "write",
    "docs.insert-file-chip": "write",
    "docs.insert-page-break": "write",
    "docs.insert-person": "write",
    "docs.list-tabs": "read",
    "docs.page-layout": "write",
    "docs.rename-tab": "write",
    "docs.table-column-width": "write",
    "docs.table-merge": "write",
    "docs.table-unmerge": "write",
    "drive.audit.sharing": "read",
    "drive.audit.user": "read",
    "drive.bulk.remove-public": "full",
    "drive.bulk.update-role": "full",
    "drive.changes.start-token": "read",
    "drive.drives": "read",
    "drive.mkdir": "write",
    "forms.add-question": "write",
    "forms.delete-question": "full",
    "forms.move-question": "write",
    "forms.watch.renew": "write",
    "gmail.batch.modify": "write",
    "gmail.labels.modify": "write",
    "gmail.labels.style": "write",
    "gmail.mark-read": "write",
    "gmail.messages.modify": "write",
    "gmail.thread.modify": "write",
    "gmail.unread": "write",
    "maps.places.details": "read",
    "maps.reverse-geocode": "read",
    "meet.participants": "read",
    "people.relations": "read",
    "photos.picker.wait": "read",
    "sheets.add-tab": "write",
    "sheets.copy-paste": "write",
    "sheets.delete-dimension": "full",
    "sheets.delete-tab": "full",
    "sheets.freeze": "write",
    "sheets.notes": "read",
    "sheets.number-format": "write",
    "sheets.read-format": "read",
    "sheets.rename-tab": "write",
    "sheets.reorder-tab": "write",
    "sheets.resize-columns": "write",
    "sheets.resize-rows": "write",
    "sheets.unmerge": "write",
    "slides.create-from-template": "write",
    "slides.delete-slide": "full",
    "slides.insert-text": "write",
    "slides.list-slides": "read",
    "slides.read-slide": "read",
    "slides.replace-text": "write",
    "tasks.done": "write",
    "tasks.undo": "write",
    "time.now": "read",
    # New in gog 0.41, reviewed individually.
    "adsense.accounts.children": "read",
    "adsense.adclients.adcode": "read",
    "adsense.adunits.adcode": "read",
    "adsense.adunits.linkedcustomchannels": "read",
    "adsense.customchannels.linkedadunits": "read",
    "appscript.deployments": "read",
    "appscript.versions": "read",
    "calendar.changed": "read",
    "calendar.delete-calendar": "full",
    "docs.insert-footnote": "write",
    "docs.insert-horizontal-rule": "write",
    "docs.insert-section-break": "write",
    "docs.replace-image": "write",
    "docs.section-columns": "write",
    "docs.table-row.pin-header": "write",
    "docs.table-row.style": "write",
    "gmail.drafts.reply-all": "write",
    "gmail.reply-all": "write",
    "searchconsole.inspect": "read",
    # Runs the data source's BigQuery query, which the project is billed for.
    "sheets.datasource.refresh": "full",
    "sheets.duplicate-tab": "write",
    "slides.bullets": "write",
    "slides.duplicate-slide": "write",
    "slides.element.alt-text": "write",
    "slides.element.create-line": "write",
    "slides.element.create-shape": "write",
    "slides.element.group": "write",
    "slides.element.style": "write",
    "slides.element.transform": "write",
    "slides.element.ungroup": "write",
    "slides.element.z-order": "write",
    "slides.link": "write",
    "slides.locate": "read",
    "slides.move-slide": "write",
    "slides.new-slide": "write",
    "slides.paragraph-style": "write",
    "slides.skip-slide": "write",
    "slides.style-text": "write",
    "slides.table.border.style": "write",
    "slides.table.cell.style": "write",
    "slides.table.column.size": "write",
    "slides.table.row.size": "write",
    "slides.table.unmerge": "write",
    "slides.unskip-slide": "write",
}

READ_WORDS = {
    "list", "ls", "get", "search", "find", "show", "view", "cat", "read", "info",
    "status", "describe", "history", "du", "tree", "inventory", "audit", "query",
    "report", "whoami", "me", "freebusy", "conflicts", "url", "urls", "open", "export",
    "download", "dl", "thumbnail", "diff", "check", "count", "lookup", "directions",
    "distance", "geocode", "preview", "usage", "events", "agenda", "attachments",
    "attachment", "raw", "structure", "outline", "text", "values", "metadata", "picker",
    "stats", "summary", "suggest", "resolve-time", "availability", "whatis", "labels",
    "members", "responses", "permissions-list", "revisions", "changes", "activity",
}
WRITE_WORDS = {
    "create", "add", "update", "edit", "set", "send", "reply", "forward", "draft",
    "drafts", "upload", "up", "put", "move", "mv", "copy", "cp", "rename", "mark",
    "star", "unstar", "label", "unlabel", "archive", "unarchive", "append", "write",
    "insert", "format", "import", "apply", "subscribe", "unsubscribe", "accept",
    "decline", "respond", "rsvp", "invite", "comment", "reopen", "pin", "unpin",
    "snooze", "sort", "fill", "replace", "sed", "find-replace", "batch-update",
    "submit", "publish", "restore", "untrash", "begin", "end", "abort", "complete",
    "uncomplete", "resolve", "unresolve", "merge", "dedupe", "clone", "duplicate",
    "resize", "insert-image", "insert-table", "table-row", "cell-update",
    "create-from-markdown", "add-slide", "replace-slide", "update-notes",
    "update-note", "mute", "unmute", "join", "focus-time", "out-of-office",
    "working-location", "shortcut", "watch", "start", "stop",
}
FULL_WORDS = {
    "delete", "del", "rm", "remove", "trash", "purge", "empty", "destroy", "revoke",
    "transfer", "wipe", "reset", "suspend", "unsuspend", "deprovision", "share",
    "unshare", "clear", "prune", "run", "deploy", "undeploy", "rotate",
}

# Some commands reuse a path flag name for a Drive ID ('--file <file ID>').
_NOT_A_PATH = re.compile(r"\bfile id\b", re.IGNORECASE)
_ALIAS_SEGMENT = re.compile(r"^(?P<name>[^\s(]+)(?:\s*\((?P<aliases>[^)]*)\))?$")


def _command_aliases(node: dict[str, Any]) -> list[str]:
    """Aliases of a node's own name, from its schema ``path`` ('gog drive (drv) ls')."""

    parts = re.findall(r"[^\s(]+(?:\s*\([^)]*\))?", str(node.get("path", "")))
    if not parts:
        return []
    match = _ALIAS_SEGMENT.match(parts[-1].strip())
    if not match or not match.group("aliases"):
        return []
    return [alias.strip() for alias in match.group("aliases").split(",") if alias.strip()]


def classify(path: str) -> tuple[str, str]:
    """(level, rule) for one dotted command path."""

    if path in OVERRIDES:
        return OVERRIDES[path], "override"
    for prefix, level in GROUP_RULES:
        if path == prefix or path.startswith(prefix + "."):
            return level, f"group:{prefix}"
    last = path.rsplit(".", 1)[-1]
    if last in FULL_WORDS:
        return "full", f"word:{last}"
    if last in WRITE_WORDS:
        return "write", f"word:{last}"
    if last in READ_WORDS:
        return "read", f"word:{last}"
    return "full", "unclassified"


def _blocked(path: str) -> bool:
    return any(path == blocked or path.startswith(blocked + ".") for blocked in BLOCKED_COMMANDS)


def _spellings(flag: dict[str, Any]) -> list[str]:
    """Every way to write a flag: --name, --alias, -s."""

    names = [f"--{flag['name']}", *(f"--{alias}" for alias in flag.get("aliases") or [])]
    if flag.get("short"):
        names.append(f"-{flag['short']}")
    return names


def _takes_value(flag: dict[str, Any]) -> bool:
    return str(flag.get("type", "")) not in {"bool", "kong.helpFlag", "kong.VersionFlag"}


def build_table(schema: dict[str, Any]) -> dict[str, Any]:
    root = schema["command"]
    commands: dict[str, str] = {}
    command_aliases: dict[str, list[str]] = {}
    blocked_spellings: set[str] = set()
    path_flags: dict[str, list[str]] = {}
    reference_flags: dict[str, list[str]] = {}
    path_positionals: dict[str, list[int]] = {}
    value_flags: dict[str, list[str]] = {}
    unclassified: list[str] = []

    def walk(node: dict[str, Any], path: list[str]) -> None:
        dotted = ".".join(path)
        for flag in node.get("flags") or []:
            if flag["name"] in BLOCKED_FLAGS:
                blocked_spellings.update(_spellings(flag))
        if path:
            aliases = _command_aliases(node)
            if aliases:
                command_aliases[dotted] = aliases
        children = [child for child in node.get("subcommands") or [] if isinstance(child, dict)]
        if path and not children and not _blocked(dotted):
            level, rule = classify(dotted)
            commands[dotted] = level
            if rule == "unclassified":
                unclassified.append(dotted)
            flags = node.get("flags") or []
            local = sorted(
                spelling
                for flag in flags
                if flag["name"] in LOCAL_PATH_FLAGS and not _NOT_A_PATH.search(flag.get("help", ""))
                for spelling in _spellings(flag)
            )
            if local:
                path_flags[dotted] = local
            references = sorted(
                spelling
                for flag in flags
                if flag["name"] in FILE_REFERENCE_FLAGS
                for spelling in _spellings(flag)
            )
            if references:
                reference_flags[dotted] = references
            path_names = LOCAL_PATH_POSITIONALS | COMMAND_PATH_POSITIONALS.get(dotted, set())
            positions = [
                index
                for index, positional in enumerate(node.get("positionals") or [])
                if positional.get("name") in path_names
            ]
            if positions:
                path_positionals[dotted] = positions
                # To find positional arguments the broker must skip flag values.
                value_flags[dotted] = sorted(
                    spelling for flag in flags if _takes_value(flag) for spelling in _spellings(flag)
                )
        for child in children:
            walk(child, [*path, child["name"]])

    walk(root, [])
    root_flags = [flag for flag in root.get("flags") or [] if flag["name"] not in BLOCKED_FLAGS]
    return {
        "cli": "gog",
        "generated_from": schema.get("build", "unknown"),
        "commands": dict(sorted(commands.items())),
        "command_aliases": dict(sorted(command_aliases.items())),
        "blocked_commands": dict(sorted(BLOCKED_COMMANDS.items())),
        "global_flags_with_value": sorted(
            spelling for flag in root_flags if _takes_value(flag) for spelling in _spellings(flag)
        ),
        "global_flags": sorted(
            spelling for flag in root_flags if not _takes_value(flag) for spelling in _spellings(flag)
        ),
        "blocked_flags": sorted(blocked_spellings | EXTRA_BLOCKED_SPELLINGS),
        "local_path_flags": dict(sorted(path_flags.items())),
        "file_reference_flags": dict(sorted(reference_flags.items())),
        "local_path_positionals": dict(sorted(path_positionals.items())),
        "positional_value_flags": dict(sorted(value_flags.items())),
        "readonly_exempt": dict(sorted(READONLY_EXEMPT.items())),
        "_unclassified": unclassified,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("schema", type=Path, help="Output of 'gog schema --json'.")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    table = build_table(json.loads(args.schema.read_text(encoding="utf-8")))
    unclassified = table.pop("_unclassified")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Access levels for gog passthrough commands (cli:google).\n"
        "# Generated by scripts/generate_gog_command_table.py from the pinned gogcli schema;\n"
        "# commands not listed require full access. Edit the script, not this file.\n"
    )
    args.output.write_text(header + yaml.safe_dump(table, sort_keys=False, width=100), encoding="utf-8")
    print(f"Wrote {len(table['commands'])} commands to {args.output}")
    print(f"{len(unclassified)} commands matched no rule and require full:")
    for path in unclassified:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
