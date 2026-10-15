"""Fake Knowledge page answers for the onboarding aesthetic preview.

``scripts/run_onboarding_aesthetic_preview.py`` serves Web Chat without a
gateway or manager; this module answers ``/api/web-chat/knowledge/*`` with
fixed sample pages and proposals in the shapes the real gateway sends
(``assistant_gateway.interactive_apps.website_handlers.knowledge``), so the
page can be reviewed and the browser probe
(``tests/knowledge_page_browser_probe.mjs``) can run. Saves change nothing;
a body with an ``@`` answers with a review flag until Save anyway, to show
that flow.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import Any

KNOWLEDGE_PREFIX = "/api/web-chat/knowledge"

_PAGES: dict[str, dict[str, Any]] = {
    "release-process": {
        "title": "Release process",
        "page_type": "how-to",
        "summary": "How we tag, test and publish a release.",
        "tags": ["release", "ci"],
        "revision": 4,
        "body": (
            "## Steps\n\n1. Update `VERSION` and the changelog.\n2. Run the full test suite.\n"
            "3. Tag `vX.Y.Z` and push the tag.\n\nSee [[ci-pipeline]] for what runs on the tag, "
            "[[old-release-checklist|the old checklist]] for history, and [[rollback-plan]] when a release fails."
        ),
        "updated_at_utc": "2026-10-10T15:42:00Z",
        "last_editor": {"kind": "person", "label": "You"},
    },
    "ci-pipeline": {
        "title": "CI pipeline",
        "page_type": "reference",
        "summary": "Which jobs run on pushes, pull requests and tags.",
        "tags": ["ci"],
        "revision": 2,
        "body": "Pushes run the fast suite. Tags also build the container and the docs.",
        "updated_at_utc": "2026-10-09T09:10:00Z",
        "last_editor": {"kind": "agent", "agent_type": "KnowledgeCuratorAgent", "label": "KnowledgeCuratorAgent"},
    },
    "on-call-handbook": {
        "title": "On-call handbook",
        "page_type": "how-to",
        "summary": "What to do when the pager goes off.",
        "tags": ["operations"],
        "revision": 7,
        "body": "Acknowledge within five minutes, then open an incident thread.",
        "updated_at_utc": "2026-10-08T21:00:00Z",
        "last_editor": {"kind": "person", "label": "Beatrix"},
        "frozen_until_utc": "2026-10-12T09:00:00Z",
    },
    "retired-runbook": {
        "title": "Retired runbook",
        "page_type": "how-to",
        "summary": "An archived page you can restore.",
        "tags": ["operations"],
        "revision": 2,
        "status": "archived",
        "body": "These instructions are retired. Restore this page to edit them again.",
        "updated_at_utc": "2026-10-07T10:00:00Z",
        "last_editor": {"kind": "person", "label": "You"},
    },
}
_PERSONAL: dict[str, dict[str, Any]] = {
    "travel-preferences": {
        "title": "Travel preferences",
        "page_type": "reference",
        "summary": "Seats, hotels and timing I like.",
        "tags": ["travel"],
        "revision": 1,
        "body": "Window seats, morning flights, hotels near the venue.",
        "updated_at_utc": "2026-10-07T08:00:00Z",
        "last_editor": {"kind": "person", "label": "You"},
    },
}
_PROPOSAL: dict[str, Any] = {
    "contribution_id": "kb_contrib_preview",
    "kind": "edit",
    "page": "release-process",
    "title": "Release process",
    "page_type": "how-to",
    "summary": "How we tag, test and publish a release.",
    "tags": ["release", "ci"],
    "body": (
        "## Steps\n\n1. Update `VERSION` and the changelog.\n2. Run the full test suite.\n"
        "3. Run the Chromium probes.\n4. Tag `vX.Y.Z` and push the tag.\n\nSee [[ci-pipeline]] for what runs on the tag."
    ),
    "change_summary": "The release review found the browser checks were skipped once.",
    "base_revision": 4,
    "agent_type": "WebResearchAgent",
    "created_at_utc": "2026-10-11T07:30:00Z",
    "review_flags": [],
}
# A merged page's name leads to the page it was merged into.
_REDIRECTS = {"old-release-checklist": "release-process"}
_LINK_RE = re.compile(r"\[\[([^\[\]|\n]{1,200})(?:\|[^\[\]\n]{0,200})?\]\]")
_EMAIL_FLAG = {"kind": "email_address", "label": "an email address", "field": "body", "line": 1, "column": 1}


def _summary(space: str, name: str, page: dict[str, Any]) -> dict[str, Any]:
    return {
        "space": space, "page": name, "title": page["title"], "page_type": page["page_type"],
        "summary": page["summary"], "tags": page["tags"], "revision": page["revision"],
        "status": page.get("status", "active"),
        "updated_at_utc": page["updated_at_utc"], "snippet": "",
    }


def _links(space: str, body: str) -> list[dict[str, Any]]:
    pages = _PAGES if space == "team" else _PERSONAL
    links = []
    for raw in dict.fromkeys(match.strip() for match in _LINK_RE.findall(body)):
        name = raw.split("/", 1)[-1]
        links.append({"written": raw, "space": space, "page": name, "exists": name in pages or name in _REDIRECTS})
    return links


def _detail(space: str, name: str) -> dict[str, Any] | None:
    redirected_from = ""
    if space == "team" and name in _REDIRECTS:
        redirected_from, name = f"team/{name}", _REDIRECTS[name]
    page = (_PAGES if space == "team" else _PERSONAL).get(name)
    if page is None:
        return None
    backlinks = [
        _summary("team", other, data) for other, data in _PAGES.items() if f"[[{name}]]" in data["body"]
    ]
    notes = [
        {
            "note_id": "kb_note_preview", "body": "Mention the docs build on tags.",
            "created_at_utc": "2026-10-10T16:00:00Z",
            "editor": {"kind": "agent", "agent_type": "KnowledgeCuratorAgent", "label": "KnowledgeCuratorAgent"},
        }
    ] if name == "release-process" else []
    return {
        **_summary(space, name, page), "body": page["body"], "created_at_utc": "2026-09-30T10:00:00Z",
        "frozen_until_utc": page.get("frozen_until_utc", ""), "shown_revision": 0,
        "last_editor": page["last_editor"], "backlinks": backlinks, "open_notes": notes,
        "pending_contributions": 1 if name == _PROPOSAL["page"] else 0,
        "links": _links(space, page["body"]), "redirected_from": redirected_from,
    }


def _list(query: dict[str, list[str]]) -> dict[str, Any]:
    space = (query.get("space") or ["team"])[0]
    status = (query.get("status") or ["active"])[0]
    words = [word for word in (query.get("query") or [""])[0].lower().split() if word]
    pages = _PAGES if space == "team" else _PERSONAL
    found = [
        _summary(space, name, page) for name, page in pages.items()
        if page.get("status", "active") == status
        and all(word in (page["title"] + " " + page["body"]).lower() for word in words)
    ]
    offset = max(0, int((query.get("offset") or ["0"])[0]))
    return {"status": "ok", "action": "list", "pages": found[offset:offset + 20], "total": len(found)}


def _history(space: str, name: str) -> dict[str, Any] | None:
    detail = _detail(space, name)
    if detail is None:
        return None
    editors = [
        {"kind": "person", "label": "You"},
        {"kind": "agent", "agent_type": "KnowledgeCuratorAgent", "label": "KnowledgeCuratorAgent"},
        {"kind": "person", "label": "Beatrix"},
        {"kind": "person", "label": "You"},
    ]
    kinds = ["edit", "edit", "edit", "create"]
    revisions = [
        {
            "revision": detail["revision"] - index, "title": detail["title"], "status": "active",
            "change_kind": kinds[index % 4], "change_summary": "Edit published after review" if index == 1 else "",
            "created_at_utc": f"2026-10-{10 - index:02d}T12:00:00Z", "editor": editors[index % 4],
        }
        for index in range(detail["revision"])
    ]
    return {"status": "ok", "action": "history", "pages": [detail], "revisions": revisions, "total": detail["revision"]}


def _contribution(full: bool) -> dict[str, Any]:
    proposal = dict(_PROPOSAL)
    if not full:
        return {**proposal, "body": "", "current": None}
    return {**proposal, "current": _detail("team", proposal["page"])}


def answer_get(route: str, query: dict[str, list[str]]) -> dict[str, Any] | None:
    """The JSON for a Knowledge GET, or None when nothing matches."""

    parts = route.removeprefix(KNOWLEDGE_PREFIX).strip("/").split("/")
    if parts == ["access"]:
        return {"status": "ok", "enabled": True}
    if parts == ["pages"]:
        return _list(query)
    if parts[:1] == ["pages"] and len(parts) == 3:
        detail = _detail(parts[1], parts[2])
        return {"status": "ok", "action": "read", "page": detail} if detail else None
    if parts[:1] == ["pages"] and len(parts) == 4 and parts[3] == "history":
        return _history(parts[1], parts[2])
    if parts == ["counts"]:
        return {"status": "ok", "pending_proposals": 1}
    if parts == ["contributions"]:
        return {"status": "ok", "action": "list_contributions", "contributions": [_contribution(False)], "total": 1}
    if parts[:1] == ["contributions"] and len(parts) == 2:
        return {"status": "ok", "action": "read_contribution", "contribution": _contribution(True)}
    return None


def answer_post(route: str, body: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]] | None:
    """The status and JSON for a Knowledge change, or None when nothing matches."""

    parts = route.removeprefix(KNOWLEDGE_PREFIX).strip("/").split("/")
    if parts[:1] == ["contributions"] and len(parts) == 3:
        return HTTPStatus.OK, {"status": "ok", "page": _detail("team", _PROPOSAL["page"])}
    if parts[:1] != ["pages"] or len(parts) not in (2, 4):
        return None
    text = str(body.get("body") or body.get("note") or "")
    if "@" in text and body.get("confirm_review") is not True:
        return HTTPStatus.CONFLICT, {
            "status": "refused",
            "error": {
                "code": "review_required",
                "message": "This team page change contains email addresses. Everyone on the team will see it. "
                "Confirm to save it anyway, or remove those details.",
            },
            "review_flags": [_EMAIL_FLAG],
        }
    space, name = parts[1], parts[2] if len(parts) == 4 else "release-process"
    detail = _detail(space, name) or _detail("team", "release-process")
    if len(parts) == 4 and parts[3] == "save" and detail is not None:
        # Preview writes remain stateless, but the viewer shows a restored page.
        detail = {**detail, "status": "active"}
    return HTTPStatus.OK, {"status": "ok", "page": detail, "review_flags": []}


__all__ = ["KNOWLEDGE_PREFIX", "answer_get", "answer_post"]
