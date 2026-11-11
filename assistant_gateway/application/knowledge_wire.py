"""The Knowledge page's requests as the contract's ``knowledge.*`` bodies, and replies as its views.

The browser keeps its request and result models (``KnowledgeCommandPayload``,
``KnowledgeCommandResultPayload``); the team services speak the contract. This module
converts between them, with no I/O:

- archive and revert reasons are the fixed texts below, written by this code and never
  typed in the browser, because they enter shared history;
- a page version is its revision number as text (``seen_version``);
- text travels inline, at most ``INLINE_TEXT_MAX`` characters: the contract's object
  store for longer text does not exist yet, so a longer text is refused before sending;
  an empty note is refused too, as the service would;
- tags are normalized as the knowledge service stores them (``slugify``: lowercase words
  joined by hyphens, at most 80 characters, empty ones dropped), as the contract asks of
  senders;
- editors arrive as an agent type, a person reference or the system; ``label`` names them.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from custom_messages.contracts.control_v1 import (
    KnowledgeCommandAction,
    KnowledgeCommandPayload,
    KnowledgeCommandResultPayload,
    KnowledgeContributionPayload,
    KnowledgeEditorPayload,
    KnowledgeLinkPayload,
    KnowledgeNotePayload,
    KnowledgePageDetailPayload,
    KnowledgePageSummaryPayload,
    KnowledgeReviewFlagPayload,
    KnowledgeRevisionPayload,
)
from knowledge_base.records import INLINE_TEXT_MAX
from knowledge_base.text import slugify

# Fixed history texts for changes that need a reason (module docstring).
ARCHIVE_REASON = "Archived on the Web Chat Knowledge page"
REVERT_REASON = "Reverted on the Web Chat Knowledge page"
HISTORY_SHOWN = 50
TOO_LONG_TEXT = "The text is {length} characters long; at most {limit} can be sent. Shorten it."
EMPTY_NOTE_TEXT = "A note needs text. Write the note, then add it."

# The contract's refusal outcomes, as the Knowledge page's codes (website_handlers.knowledge).
REFUSAL_CODES = {
    "not_allowed": "authorization_denied",
    "feature_off": "knowledge_off",
    "changed_since_read": "conflict",
    "contribution_not_pending": "conflict",
    "page_exists": "page_exists",
    "merged": "merged",
    "review_required": "review_required",
    "sensitive_content": "sensitive_content",
    "not_found": "not_found",
}
_DEFAULT_REFUSAL_CODE = "invalid_request"
_RUNTIME_ERROR = "runtime_error"

EditorLabel = Callable[[dict[str, Any]], str]


class KnowledgeRequestNotSent(ValueError):
    """A request the gateway refuses before sending it (module docstring); its message says why."""


class KnowledgeTextTooLong(KnowledgeRequestNotSent):
    """Text the contract cannot carry inline (module docstring)."""


def knowledge_request(payload: KnowledgeCommandPayload) -> tuple[str, dict[str, Any]]:
    """The contract's kind and body (without ``request_id``) for one Knowledge page request."""

    return f"knowledge.{payload.action.value}", _BODIES[payload.action](payload)


def _page_of(payload: KnowledgeCommandPayload) -> dict[str, Any]:
    return {"space": payload.space, "page": payload.page}


def _list_body(payload: KnowledgeCommandPayload) -> dict[str, Any]:
    body: dict[str, Any] = {"space": payload.space, "status": payload.status, "offset": payload.offset,
                            "limit": payload.limit}
    if payload.query:
        body["query"] = payload.query
    return body


def _read_body(payload: KnowledgeCommandPayload) -> dict[str, Any]:
    return {**_page_of(payload), **({"revision": payload.revision} if payload.revision else {})}


def _history_body(payload: KnowledgeCommandPayload) -> dict[str, Any]:
    return {**_page_of(payload), "offset": 0, "limit": HISTORY_SHOWN}


def _write_body(payload: KnowledgeCommandPayload) -> dict[str, Any]:
    body = {"space": payload.space, "confirm_review": payload.confirm_review, **_draft_fields(payload)}
    if payload.page:
        body["page"] = payload.page
    if payload.base_revision:
        body["seen_version"] = str(payload.base_revision)
    return body


def _add_note_body(payload: KnowledgeCommandPayload) -> dict[str, Any]:
    if not payload.note.strip():
        raise KnowledgeRequestNotSent(EMPTY_NOTE_TEXT)
    return {**_page_of(payload), "note": _inline(payload.note), "confirm_review": payload.confirm_review}


def _archive_body(payload: KnowledgeCommandPayload) -> dict[str, Any]:
    return {**_page_of(payload), "seen_version": str(payload.base_revision), "reason": ARCHIVE_REASON}


def _revert_body(payload: KnowledgeCommandPayload) -> dict[str, Any]:
    return {**_page_of(payload), "to_revision": payload.revision, "seen_version": str(payload.base_revision),
            "reason": REVERT_REASON}


def _publish_body(payload: KnowledgeCommandPayload) -> dict[str, Any]:
    body: dict[str, Any] = {"contribution_id": payload.contribution_id, "edited": payload.edited}
    if payload.base_revision:
        body["seen_version"] = str(payload.base_revision)
    if payload.edited:
        # A page proposal publishes the edited fields; a note proposal publishes the text as the note.
        body.update(_draft_fields(payload))
        if payload.body:
            body["note"] = _inline(payload.body)
    return body


_BODIES: dict[KnowledgeCommandAction, Callable[[KnowledgeCommandPayload], dict[str, Any]]] = {
    KnowledgeCommandAction.LIST: _list_body,
    KnowledgeCommandAction.READ: _read_body,
    KnowledgeCommandAction.HISTORY: _history_body,
    KnowledgeCommandAction.WRITE: _write_body,
    KnowledgeCommandAction.ADD_NOTE: _add_note_body,
    KnowledgeCommandAction.ARCHIVE: _archive_body,
    KnowledgeCommandAction.REVERT: _revert_body,
    KnowledgeCommandAction.LIST_CONTRIBUTIONS: lambda payload: {"offset": payload.offset, "limit": payload.limit},
    KnowledgeCommandAction.COUNT_CONTRIBUTIONS: lambda payload: {},
    KnowledgeCommandAction.READ_CONTRIBUTION: lambda payload: {"contribution_id": payload.contribution_id},
    KnowledgeCommandAction.PUBLISH_CONTRIBUTION: _publish_body,
    KnowledgeCommandAction.WITHDRAW_CONTRIBUTION: lambda payload: {"contribution_id": payload.contribution_id},
}


def _draft_fields(payload: KnowledgeCommandPayload) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if payload.title:
        fields["title"] = payload.title
    if payload.summary is not None:
        fields["summary"] = payload.summary
    if payload.tags is not None:
        fields["tags"] = normalized_tags(payload.tags)
    if payload.body:
        fields["content"] = _inline(payload.body)
    return fields


def normalized_tags(tags: Iterable[str]) -> list[str]:
    """Tags as the knowledge service stores them (module docstring), in their first order."""

    return list(dict.fromkeys(tag for tag in (slugify(str(raw)) for raw in tags) if tag))


def _inline(text: str) -> dict[str, Any]:
    if len(text) > INLINE_TEXT_MAX:
        raise KnowledgeTextTooLong(TOO_LONG_TEXT.format(length=len(text), limit=INLINE_TEXT_MAX))
    return {"type": "inline", "text": text}


def refusal_code(outcome: str) -> str:
    if outcome == _RUNTIME_ERROR:
        return _RUNTIME_ERROR
    return REFUSAL_CODES.get(outcome, _DEFAULT_REFUSAL_CODE)


def review_flags(reply: dict[str, Any]) -> tuple[KnowledgeReviewFlagPayload, ...]:
    return tuple(KnowledgeReviewFlagPayload(**flag) for flag in reply.get("review_flags") or ())


def current_revision(reply: dict[str, Any]) -> int:
    version = str(reply.get("current_version") or "0")
    return int(version) if version.isdigit() else 0


def knowledge_view(action: KnowledgeCommandAction, reply: dict[str, Any], *,
                   label: EditorLabel) -> KnowledgeCommandResultPayload:
    """The browser's result for an ``ok`` reply, with every editor named by ``label``."""

    view = _Views(label)
    values: dict[str, Any] = {"action": action, "review_flags": list(review_flags(reply))}
    if "pages" in reply:
        values["pages"] = [view.summary(page) for page in reply["pages"]]
    if action is KnowledgeCommandAction.HISTORY:
        values["pages"] = [view.summary(reply["page"])]
        values["revisions"] = [view.revision(revision) for revision in reply["revisions"]]
    elif "page" in reply:
        values["page"] = view.page(reply["page"])
    if "contributions" in reply:
        values["contributions"] = [view.contribution(item) for item in reply["contributions"]]
    if "contribution" in reply:
        values["contribution"] = view.contribution(reply["contribution"])
    if "total" in reply:
        values["total"] = reply["total"]
    return KnowledgeCommandResultPayload(**values)


class _Views:
    def __init__(self, label: EditorLabel) -> None:
        self._label = label

    def editor(self, editor: dict[str, Any]) -> KnowledgeEditorPayload:
        return KnowledgeEditorPayload(kind=editor["kind"], agent_type=editor.get("agent_type", ""),
                                      label=self._label(editor))

    def summary(self, page: dict[str, Any]) -> KnowledgePageSummaryPayload:
        snippet = page.get("snippet") or {}
        return KnowledgePageSummaryPayload(
            space=page["space"], page=page["page"], title=page["title"], page_type=page["page_type"],
            summary=page.get("summary", ""), tags=list(page.get("tags") or ()), revision=int(page["seen_version"]),
            status=page["status"], updated_at_utc=page["updated_at"], snippet=str(snippet.get("text", "")),
        )

    def page(self, page: dict[str, Any]) -> KnowledgePageDetailPayload:
        shown_by = page.get("shown_by") or page["updated_by"]
        return KnowledgePageDetailPayload(
            **self.summary(page).model_dump(),
            body=page["content"]["text"],
            created_at_utc=page["created_at"],
            frozen_until_utc=page.get("frozen_until", ""),
            shown_revision=int(page.get("shown_revision", 0)),
            last_editor=self.editor(shown_by),
            backlinks=[self.summary(linked) for linked in page["backlinks"]],
            open_notes=[self.note(note) for note in page["open_notes"]],
            pending_contributions=len(page["pending_contributions"]),
            links=[KnowledgeLinkPayload(**link) for link in page["links"]],
            redirected_from=page.get("redirected_from", ""),
        )

    def note(self, note: dict[str, Any]) -> KnowledgeNotePayload:
        return KnowledgeNotePayload(note_id=note["note_id"], body=note["content"]["text"],
                                    created_at_utc=note["created_at"], editor=self.editor(note["created_by"]))

    def revision(self, revision: dict[str, Any]) -> KnowledgeRevisionPayload:
        return KnowledgeRevisionPayload(
            revision=revision["revision"], title=revision["title"], status=revision["status"],
            change_kind=revision["change_kind"], change_summary=revision.get("change_summary", ""),
            created_at_utc=revision["updated_at"], editor=self.editor(revision["updated_by"]),
        )

    def contribution(self, contribution: dict[str, Any]) -> KnowledgeContributionPayload:
        content = contribution.get("content") or {}
        current = contribution.get("current")
        return KnowledgeContributionPayload(
            contribution_id=contribution["contribution_id"], kind=contribution["kind"], page=contribution["page"],
            title=contribution["title"], page_type=contribution["page_type"],
            summary=contribution.get("summary", ""), tags=list(contribution.get("tags") or ()),
            body=str(content.get("text", "")), change_summary=contribution.get("change_summary", ""),
            base_revision=contribution["base_revision"], agent_type=contribution["author"]["agent_type"],
            created_at_utc=contribution["created_at"],
            review_flags=[KnowledgeReviewFlagPayload(**flag) for flag in contribution.get("review_flags") or ()],
            current=self.page(current) if current else None,
        )
