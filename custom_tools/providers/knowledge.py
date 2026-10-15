"""Knowledge base tools (catalog tool ``fn:knowledge``, every agent).

Pages live in the team space (``team/<page-name>``) or the personal space of
the person the task works for (``personal/<page-name>``). The rules (whose
space, pending contributions, sensitive content, write limits) live in
``knowledge_base.service``; these tools build who is acting from the task's
verified identity only and format results for the model.

Results never name people: a change shows the agent type that made it, or
"a person" for gateway edits.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from strands import tool

from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import active_task_context, get_agent_ref
from knowledge_base.records import CODE_DISABLED, KnowledgeActor, KnowledgeError, KnowledgePage, SearchHit
from knowledge_base.service import DraftInput, KnowledgeService, PageView, WriteResult
from knowledge_base.vocabulary import ActorKind
from runtime_settings.knowledge import knowledge_settings

_PENDING_MESSAGE = (
    "Team pages change only after review, because this work is for one person: the knowledge curator "
    "(or the person you work for) will publish it. Until then it is visible only in this person's work. "
    "Do not save it again."
)


def _error(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error_code": code, "message": message, "error": message})


def knowledge_service(agent: Any) -> KnowledgeService:
    """The knowledge service for an agent's store and settings.

    One builder for the agent tools, the curator's tools, after-action report
    lessons and the manager's Knowledge page commands, so all of them apply
    the same settings and the person's memories to the overlap check.
    """

    settings = knowledge_settings(getattr(agent, "knowledge_config", None))
    repository = getattr(agent, "_knowledge_repository", None)
    if not settings.enabled:
        raise KnowledgeError(CODE_DISABLED, "The knowledge base is turned off (agent.knowledge.enabled).")
    if repository is None:
        raise KnowledgeError("knowledge_unavailable", "The knowledge base is not available to this agent.")
    pinned = getattr(agent, "_pinned_memory_repository", None)

    def personal_texts(person_id: str) -> list[str]:
        if pinned is None:
            return []
        return [fact.fact_text for fact in pinned.list_usermem(canonical_user_id=person_id)]

    return KnowledgeService(
        repository,
        settings=settings,
        personal_texts=personal_texts,
        logger=getattr(agent, "_logger", None),
    )


def knowledge_actor(agent: Any) -> KnowledgeActor:
    """The acting agent and whom its task works for, from verified identity only.

    Without a valid signed context or schedule run identity the work counts as
    work for nobody: no personal space, and team changes follow the rules for
    system work.
    """

    from custom_agents.access_runtime import verified_task_context

    task_id, _meta = active_task_context(agent)
    context = verified_task_context(agent)
    return KnowledgeActor(
        kind=ActorKind.AGENT,
        actor_id=str(getattr(agent, "id", "") or ""),
        team_id=str(getattr(agent, "team_id", "") or ""),
        agent_type=str(getattr(agent, "named_agent_type", "") or ""),
        display_name=str(getattr(agent, "name", "") or ""),
        work_person_id=str(getattr(context, "person_id", "") or "") if context is not None else "",
        task_id=str(task_id or ""),
        work_id=str(getattr(context, "context_id", "") or "") if context is not None else "",
    )


def _run(action: Any) -> str:
    agent = get_agent_ref()
    if agent is None:
        return _error("agent_ref_missing", "Agent reference not set")
    try:
        return json.dumps(action(knowledge_service(agent), knowledge_actor(agent)), default=str)
    except KnowledgeError as error:
        return json.dumps(error.payload(), default=str)


def _by(kind: str, agent_type: str) -> str:
    if kind == ActorKind.AGENT.value:
        return agent_type or "an agent"
    return "a person" if kind == ActorKind.PERSON.value else "the system"


def _page_summary(page: KnowledgePage) -> dict[str, Any]:
    return {
        "page": page.reference(),
        "title": page.title,
        "type": page.page_type,
        "summary": page.summary,
        "tags": list(page.tags),
        "revision": page.revision,
        "updated_at": page.updated_at_utc,
    }


def _hit(hit: SearchHit) -> dict[str, Any]:
    return {**_page_summary(hit.page), "snippet": hit.snippet}


def _write_payload(result: WriteResult) -> dict[str, Any]:
    flags = list(result.review_kinds)
    if result.contribution is not None:
        return {
            "ok": True,
            "status": "pending_review",
            "contribution_id": result.contribution.contribution_id,
            "page": f"team/{result.contribution.slug}",
            "change": result.contribution.kind.value,
            "review_flags": flags,
            "message": _PENDING_MESSAGE,
        }
    if result.note is not None and result.page is not None:
        return {"ok": True, "status": "note_added", "page": result.page.reference(), "note_id": result.note.note_id}
    assert result.page is not None
    return {
        "ok": True,
        "status": "saved",
        "page": result.page.reference(),
        "revision": result.page.revision,
        "page_status": result.page.status.value,
        "review_flags": flags,
    }


@tool
def knowledge_search(query: str = "", space: str = "all", page_type: str = "", tag: str = "", limit: int = 8, offset: int = 0) -> str:
    """
    Search the shared knowledge base before researching something the team may already know.

    Finds pages containing all your words (or any of them when that finds too
    little), ranked by relevance. With an empty query, lists the most recently
    changed pages.

    Args:
        query: Plain words to look for, such as "github release workflow".
        space: "all" (default), "team", or "personal" (the person this task works for).
        page_type: Optional filter: concept, how-to, decision, lesson, reference or source.
        tag: Optional tag filter.
        limit: How many results to return.
        offset: Skip this many results, to page through more.
    """

    return _run(
        lambda service, actor: {
            "ok": True,
            "results": [
                _hit(hit)
                for hit in service.search(
                    actor, query=query, space=space, page_type=page_type, tag=tag, limit=limit, offset=offset
                )
            ],
        }
    )


@tool
def knowledge_read(page: str, revision: int = 0) -> str:
    """
    Read one knowledge page with the pages that link to it and its open notes.

    The name of a page that was merged into another leads to that page;
    the result then says which name you used in redirected_from.

    Args:
        page: The page reference, such as "team/github-release-process" or "personal/travel-preferences".
        revision: Optional earlier revision number to read instead of the current text.
    """

    return _run(lambda service, actor: _read_payload(service.read(actor, page, revision=revision)))


def _read_payload(view: PageView) -> dict[str, Any]:
    """A page as the model reads it: text, links to it, open notes, and what this work proposed."""

    shown = view.revision
    payload = {
        "ok": True,
        **_page_summary(view.page),
        "status": view.page.status.value,
        "body": shown.body if shown else view.page.body,
        "linked_from": [linked.reference() for linked in view.backlinks],
        "open_notes": [
            {"note_id": note.note_id, "text": note.body, "by": _by(note.actor_kind, note.actor_agent_type), "at": note.created_at_utc}
            for note in view.open_notes
        ],
    }
    if view.redirected_from:
        # A merged page's name: this is the page it was merged into.
        payload["redirected_from"] = view.redirected_from
    if view.page.frozen_until_utc:
        payload["frozen_until"] = view.page.frozen_until_utc
    if shown is not None:
        payload.update(
            {"viewing_revision": shown.revision, "title": shown.title, "summary": shown.summary, "tags": list(shown.tags)}
        )
    if view.pending_contributions:
        payload["pending_changes_from_this_work"] = [
            {"contribution_id": c.contribution_id, "change": c.kind.value, "proposed_at": c.created_at_utc}
            for c in view.pending_contributions
        ]
    return payload


@tool
def knowledge_write(
    page: str,
    body: str = "",
    title: str = "",
    summary: Optional[str] = None,
    page_type: str = "",
    tags: Optional[list[str]] = None,
    base_revision: int = 0,
    change_summary: str = "",
) -> str:
    """
    Create a knowledge page, or edit one you have read.

    To create, leave base_revision at 0. To edit, read the page first and pass
    its revision as base_revision; send the whole new body. Empty title and body
    keep their current values; omit summary to keep it or send "" to clear it.
    If someone saved first, you get the newer revision
    to merge. Prefer knowledge_add_note for a small observation.

    In work for a person, changes to team pages are proposed for review and
    published later. Never include passwords, keys, tokens or identity
    numbers, and keep the person's private details out of team pages.

    Args:
        page: "team/<page-name>" or "personal/<page-name>", such as "team/github-release-process".
        body: The page text in Markdown. Link other pages with [[page-name]] or [[team/page-name]].
        title: The page title (required to create).
        summary: One sentence saying what the page covers; omit to keep it, or "" to clear it.
        page_type: concept, how-to, decision, lesson, reference (default) or source.
        tags: A few short tags.
        base_revision: 0 to create; the revision you read to edit.
        change_summary: A short description of what you changed and why.
    """

    draft = DraftInput(title=title, body=body, summary=summary, page_type=page_type, tags=tags)
    return _run(
        lambda service, actor: _write_payload(
            service.write(actor, page, draft, base_revision=int(base_revision or 0), change_summary=change_summary)
        )
    )


@tool
def knowledge_add_note(page: str, note: str) -> str:
    """
    Add a short note to a knowledge page without rewriting it.

    Use notes for corrections, new observations and disagreements; the
    curator merges them into the page. In work for a person, notes on team
    pages are reviewed before others see them.

    Args:
        page: The page reference, such as "team/github-release-process".
        note: The observation, in a few sentences.
    """

    return _run(lambda service, actor: _write_payload(service.add_note(actor, page, note)))


@tool
def knowledge_history(page: str, limit: int = 10) -> str:
    """
    List a knowledge page's revisions, newest first.

    Args:
        page: The page reference, such as "team/github-release-process".
        limit: How many revisions to list.
    """

    def action(service: KnowledgeService, actor: KnowledgeActor) -> dict[str, Any]:
        current, revisions = service.history(actor, page, limit=limit)
        return {
            "ok": True,
            "page": current.reference(),
            "current_revision": current.revision,
            "revisions": [
                {
                    "revision": item.revision,
                    "change": item.change_kind.value,
                    "summary": item.change_summary,
                    "by": _by(item.actor_kind, item.actor_agent_type),
                    "at": item.created_at_utc,
                }
                for item in revisions
            ],
        }

    return _run(action)


@tool
def knowledge_archive(page: str, base_revision: int, reason: str) -> str:
    """
    Archive a knowledge page that is wrong or no longer useful. Archived pages
    leave search results but keep every revision, and an edit restores them.

    Args:
        page: The page reference, such as "team/old-deploy-process".
        base_revision: The revision you read.
        reason: Why the page should be archived.
    """

    return _run(
        lambda service, actor: _write_payload(service.archive(actor, page, base_revision=int(base_revision), reason=reason))
    )


PROVIDER = ToolProvider(
    name="knowledge",
    tools=(
        knowledge_search,
        knowledge_read,
        knowledge_write,
        knowledge_add_note,
        knowledge_history,
        knowledge_archive,
    ),
)
