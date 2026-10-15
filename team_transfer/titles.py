"""Readable names for the records a review lists.

Record keys (``team_transfer.selection.record_key``) are stable identities,
such as ``page/pg_3f2a`` or ``usr_7c1d``, which mean little to the person
choosing what to import. The review pages show these titles instead, grouped
under each section's title. The key stays the identity every selection,
receipt and plan uses; a title is display text only, built from the record's
own fields and the archive's people, and never decides anything. A record
whose fields do not give a better name is shown by its key.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Final, Mapping

SECTION_TITLES: Final[dict[str, str]] = {
    "settings": "Settings",
    "operator_secrets": "Secrets from team.env",
    "control": "Team files and agent types",
    "skills": "Skills",
    "team_tools": "Added tools",
    "mcp": "MCP connections",
    "procedures": "Installed procedures",
    "people": "People",
    "access": "Access",
    "objectives": "Objectives",
    "schedules": "Schedules",
    "pinned_memory": "Pinned memories",
    "knowledge": "Knowledge pages",
    "broker_credentials": "Stored sign-ins and keys",
}

_SCHEDULE_KINDS: Final[dict[str, str]] = {
    "session": "Chat session",
    "scheduled_target": "Delivery target",
    "heartbeat_source": "Heartbeat",
    "heartbeat_item": "Heartbeat item",
    "worker_binding": "Worker setup",
    "pool_limit": "Worker pool limit",
    "execution_job": "Automation job",
    "managed_service": "Managed service",
}
_SCHEDULE_NAME = re.compile(r"^schedule_name:\s*(.+?)\s*$", re.MULTILINE)
_MAX_TITLE_CHARS: Final = 120
Archive = Mapping[str, Mapping[str, Mapping[str, Any]]]


def section_title(section: str) -> str:
    return SECTION_TITLES.get(section, section)


def record_title(section: str, key: str, raw: Mapping[str, Any], archive: Archive) -> str:
    """The name a person recognizes for one record; the key when nothing better exists."""

    try:
        title = _TITLES.get(section, _no_title)(key, raw, archive)
    except (KeyError, TypeError, ValueError, AttributeError):
        # Display only: a record shaped differently than expected keeps its key.
        title = ""
    return _short(title) or key


def _no_title(_key: str, _raw: Mapping[str, Any], _archive: Archive) -> str:
    return ""


def _person(archive: Archive, person_id: Any) -> str:
    if not person_id:
        return ""
    person = archive.get("people", {}).get(str(person_id))
    return str(person["display_name"]) if person else str(person_id)


def _short(text: str) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= _MAX_TITLE_CHARS else line[: _MAX_TITLE_CHARS - 1] + "…"


def _people(_key: str, raw: Mapping[str, Any], _archive: Archive) -> str:
    details = [str(raw.get("role") or "")]
    if raw.get("status") and raw["status"] != "active":
        details.append(str(raw["status"]))
    return f"{raw['display_name']} ({', '.join(d for d in details if d)})"


def _pinned_memory(_key: str, raw: Mapping[str, Any], archive: Archive) -> str:
    fact = raw["document"]
    owner = _person(archive, fact.get("person_id"))
    return f"{fact['fact_text']}" + (f" · for {owner}" if owner else "")


def _knowledge(_key: str, raw: Mapping[str, Any], archive: Archive) -> str:
    row = raw["row"]
    table = raw["table"]
    pages = {
        str(page["row"].get("page_id")): page["row"]
        for page in archive.get("knowledge", {}).values()
        if page.get("table") == "page"
    }

    def page_title(page_id: Any) -> str:
        page = pages.get(str(page_id))
        return str(page.get("title")) if page else str(page_id)

    if table == "page":
        owner = _person(archive, row.get("owner_person_id"))
        return f"{row['title']}" + (f" · {owner}'s page" if owner else "")
    if table == "revision":
        return f"Revision {row.get('revision', '')} of {page_title(row['page_id'])}"
    if table == "note":
        return f"Note on {page_title(row['page_id'])}"
    if table == "link":
        return f"Link from {page_title(row['page_id'])} to {row.get('target_slug', '')}"
    if table == "redirect":
        return f"{row.get('from_slug', '')} now leads to {page_title(row.get('to_page_id'))}"
    if table == "contribution":
        if row.get("page_id"):
            return f"Proposed change to {page_title(row['page_id'])}"
        return f"Proposed new page {row.get('title') or ''}".strip()
    return ""


def _schedules(_key: str, raw: Mapping[str, Any], _archive: Archive) -> str:
    kind = str(raw["kind"])
    if kind == "schedule":
        found = _SCHEDULE_NAME.search(raw["schedule"]["markdown"])
        name = found.group(1).strip("'\"") if found else ""
        return name if name and name != "null" else ""
    label = _SCHEDULE_KINDS.get(kind)
    return f"{label} {raw['record_id']}" if label else ""


def _access(_key: str, raw: Mapping[str, Any], archive: Archive) -> str:
    kind = str(raw["kind"])
    document = raw["document"]
    if kind == "grant":
        subject = document["subject"]
        who = _person(archive, subject["id"]) if subject["kind"] == "person" else subject["id"]
        target = document["target"]["id"]
        if document.get("kind") == "block":
            return f"Block on {target} for {who}"
        return f"{str(document.get('level', '')).capitalize()} access to {target} for {who}".strip()
    if kind == "account_link":
        owner = _person(archive, document["owner_id"]) if document["owner_kind"] == "person" else "the team"
        return f"{document['tool_id']} account of {owner}"
    if kind == "person_settings":
        return f"Settings of {_person(archive, document.get('person_id'))}"
    return ""


def _objectives(_key: str, raw: Mapping[str, Any], _archive: Archive) -> str:
    document = raw["document"]
    return str(document.get("title") or document.get("name") or "")


def _broker_credentials(key: str, raw: Mapping[str, Any], archive: Archive) -> str:
    owner = raw.get("owner") or {}
    if owner.get("kind") == "person":
        return f"{key} · {_person(archive, owner.get('owner_id'))}"
    return f"{key} · the team"


def _operator_secrets(_key: str, raw: Mapping[str, Any], _archive: Archive) -> str:
    if raw.get("kind") == "auth_file":
        return f"Model sign-in {raw['name']}"
    return str(raw["name"])


_TITLES: dict[str, Callable[[str, Mapping[str, Any], Archive], str]] = {
    "people": _people,
    "pinned_memory": _pinned_memory,
    "knowledge": _knowledge,
    "schedules": _schedules,
    "access": _access,
    "objectives": _objectives,
    "broker_credentials": _broker_credentials,
    "operator_secrets": _operator_secrets,
}

__all__ = ["SECTION_TITLES", "record_title", "section_title"]
