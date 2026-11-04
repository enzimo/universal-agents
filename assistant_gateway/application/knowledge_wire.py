"""The Knowledge page's requests as the contract's ``knowledge.*`` bodies, and replies as its views.

The browser keeps its request and result models (``KnowledgeCommandPayload``,
``KnowledgeCommandResultPayload``); the team services speak the contract. This module
converts between them, with no I/O:

- archive and revert reasons are the fixed texts below, written by this code and never
  typed in the browser, because they enter shared history;
- a page version is its revision number as text (``seen_version``);
- text travels inline, at most ``INLINE_TEXT_MAX`` characters: the contract's object
  store for longer text does not exist yet, so a longer text is refused before sending;
- editors arrive as an agent type, a person reference or the system; ``label`` names them.
"""

from __future__ import annotations

from typing import Any, Callable

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

# Fixed history texts for changes that need a reason (module docstring).
ARCHIVE_REASON = "Archived on the Web Chat Knowledge page"
REVERT_REASON = "Reverted on the Web Chat Knowledge page"
INLINE_TEXT_MAX = 32768
HISTORY_SHOWN = 50
TOO_LONG_TEXT = "The text is {length} characters long; at most {limit} can be sent. Shorten it."

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


class KnowledgeTextTooLong(ValueError):
    """Text the contract cannot carry inline (module docstring)."""


def knowledge_request(payload: KnowledgeCommandPayload) -> tuple[str, dict[str, Any]]:
    """The contract's kind and body (without ``request_id``) for one Knowledge page request."""

    action = payload.action
    page = {"space": payload.space, "page": payload.page}
    if action is KnowledgeCommandAction.LIST:
        body: dict[str, Any] = {"space": payload.space, "status": payload.status, "offset": payload.offset,
                                "limit": payload.limit}
        if payload.query:
            body["query"] = payload.query
    elif action is KnowledgeCommandAction.READ:
        body = {**page, **({"revision": payload.revision} if payload.revision else {})}
    elif action is KnowledgeCommandAction.HISTORY:
        body = {**page, "offset": 0, "limit": HISTORY_SHOWN}
    elif action is KnowledgeCommandAction.WRITE:
        body = {"space": payload.space, "confirm_review": payload.confirm_review, **_draft_fields(payload)}
        if payload.page:
            body["page"] = payload.page
        if payload.base_revision:
            body["seen_version"] = str(payload.base_revision)
    elif action is KnowledgeCommandAction.ADD_NOTE:
        body = {**page, "note": _inline(payload.note), "confirm_review": payload.confirm_review}
    elif action is KnowledgeCommandAction.ARCHIVE:
        body = {**page, "seen_version": str(payload.base_revision), "reason": ARCHIVE_REASON}
    elif action is KnowledgeCommandAction.REVERT:
        body = {**page, "to_revision": payload.revision, "seen_version": str(payload.base_revision),
                "reason": REVERT_REASON}
    elif action is KnowledgeCommandAction.LIST_CONTRIBUTIONS:
        body = {"offset": payload.offset, "limit": payload.limit}
    elif action is KnowledgeCommandAction.COUNT_CONTRIBUTIONS:
        body = {}
    elif action is KnowledgeCommandAction.PUBLISH_CONTRIBUTION:
        body = {"contribution_id": payload.contribution_id, "edited": payload.edited}
        if payload.base_revision:
            body["seen_version"] = str(payload.base_revision)
        if payload.edited:
            # A page proposal publishes the edited fields; a note proposal publishes the text as the note.
            body.update(_draft_fields(payload))
            if payload.body:
                body["note"] = _inline(payload.body)
    else:
        body = {"contribution_id": payload.contribution_id}
    return f"knowledge.{action.value}", body


def _draft_fields(payload: KnowledgeCommandPayload) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if payload.title:
        fields["title"] = payload.title
    if payload.summary is not None:
        fields["summary"] = payload.summary
    if payload.tags is not None:
        fields["tags"] = list(payload.tags)
    if payload.body:
        fields["content"] = _inline(payload.body)
    return fields


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
