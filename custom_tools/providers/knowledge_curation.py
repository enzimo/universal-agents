"""Knowledge curation tools (catalog tool ``fn:knowledge_curation``).

The tool set ``knowledge_review`` holds them: the curator on the agent side
and people on the person side, never the team lead or workers (owner's
decision 2026-10-10: only the curator, working for a person, or that person
publishes the team changes proposed from their work). KnowledgeCuratorAgent
uses them in two kinds of run:

- **for one person** (its per-person schedule, signed for that person):
  review that person's pending contributions, publish (optionally edited to
  remove personal details) or reject them, and tidy their personal pages;
- **for nobody** (the team schedule): merge open notes into team pages, fix
  broken links, merge duplicate pages, review the activity report, freeze,
  unfreeze or revert pages that agents fight over.

The rules stay in ``knowledge_base.service``: a contribution is reachable only
in work for its own person, and blocked content is refused again on publish.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from strands import tool

from custom_tools.provider_contract import ToolProvider
from custom_tools.providers.knowledge import _by, _error, knowledge_service, _page_summary, knowledge_actor
from custom_tools.runtime_context import get_agent_ref
from knowledge_base.records import KnowledgeActor, KnowledgeContribution, KnowledgeError
from knowledge_base.service import DraftInput, KnowledgeService


def _run(action: Any) -> str:
    agent = get_agent_ref()
    if agent is None:
        return _error("agent_ref_missing", "Agent reference not set")
    try:
        return json.dumps(action(knowledge_service(agent), knowledge_actor(agent)), default=str)
    except KnowledgeError as error:
        return json.dumps(error.payload(), default=str)


def _contribution(contribution: KnowledgeContribution, *, with_text: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contribution_id": contribution.contribution_id,
        "change": contribution.kind.value,
        "page": f"team/{contribution.slug}",
        "proposed_by": contribution.actor_agent_type or "an agent",
        "proposed_at": contribution.created_at_utc,
        "review_flags": list(contribution.review_kinds),
        "change_summary": contribution.change_summary,
    }
    if with_text:
        payload.update(
            {
                "base_revision": contribution.base_revision,
                "title": contribution.title,
                "summary": contribution.summary,
                "tags": list(contribution.tags),
                "page_type": contribution.page_type,
                "text": contribution.body,
            }
        )
    return payload


@tool
def list_pending_contributions(limit: int = 20) -> str:
    """
    List the team-page changes proposed from the work of the person this run works for, oldest last.

    Args:
        limit: How many to list.
    """

    return _run(
        lambda service, actor: {
            "ok": True,
            "contributions": [_contribution(c, with_text=False) for c in service.list_contributions(actor, limit=limit)],
        }
    )


@tool
def read_contribution(contribution_id: str) -> str:
    """
    Read one pending contribution in full, with the current text of the team page it changes.

    Args:
        contribution_id: The id from list_pending_contributions.
    """

    def action(service: KnowledgeService, actor: KnowledgeActor) -> dict[str, Any]:
        contribution = service.get_contribution(actor, contribution_id)
        payload = {"ok": True, **_contribution(contribution, with_text=True)}
        try:
            current = service.read(actor, f"team/{contribution.slug}").page
            payload["current_page"] = {**_page_summary(current), "status": current.status.value, "body": current.body}
        except KnowledgeError:
            payload["current_page"] = None
        return payload

    return _run(action)


@tool
def publish_contribution(
    contribution_id: str,
    body: str = "",
    title: str = "",
    summary: Optional[str] = None,
    tags: Optional[list[str]] = None,
    base_revision: int = 0,
    note: Optional[str] = None,
    page: str = "",
) -> str:
    """
    Publish a pending contribution to the team space, as proposed or as your edited version.

    Edit out anything private to the person first (health, finances, family,
    preferences, contact details, anything that identifies them) and keep only
    what helps the whole team. The team page's history records only that a
    reviewed change was published; nothing you write here describes the review. If the page changed since the proposal, merge
    the change into the current text and pass the current revision as
    base_revision. An archive preserves the current page text and details;
    send its current revision without edited fields. For a note, pass the
    edited text as note; an empty edited note is refused.

    Args:
        contribution_id: The id from list_pending_contributions.
        body: Your edited page text; empty publishes the proposed text.
        title: Edited title; empty keeps the proposed one.
        summary: Edited one-sentence summary; omit to keep the proposed one, or "" to clear it.
        tags: Edited tags; omit to keep the proposed ones.
        base_revision: The current page revision you merged into, when the page changed meanwhile.
        note: The edited note text, for a proposed note; omit to publish the proposed text.
        page: The name for a proposed new page (for example "release-tagging"); without it the page is named from its title.
    """

    draft = (
        DraftInput(title=title, body=body, summary=summary, tags=tags)
        if (body or title or summary is not None or tags is not None)
        else None
    )

    def action(service: KnowledgeService, actor: KnowledgeActor) -> dict[str, Any]:
        result = service.publish_contribution(
            actor,
            contribution_id,
            draft=draft,
            base_revision=int(base_revision or 0),
            note=note,
            page=page,
        )
        assert result.page is not None
        return {"ok": True, "status": "published", "page": result.page.reference(), "revision": result.page.revision}

    return _run(action)


@tool
def reject_contribution(contribution_id: str, reason: str) -> str:
    """
    Reject a pending contribution, for example when it is private to the person, wrong, or duplicates a page.

    Args:
        contribution_id: The id from list_pending_contributions.
        reason: Why, in one or two sentences.
    """

    return _run(
        lambda service, actor: {
            "ok": True,
            "status": service.reject_contribution(actor, contribution_id, reason=reason).status.value,
        }
    )


@tool
def knowledge_activity_report(hours: int = 24) -> str:
    """
    Knowledge writes per agent type, refusals by reason, the busiest team pages and frozen pages.

    Use it to find agents that write too much, keep getting refused, or fight over a page.

    Args:
        hours: How far back to look.
    """

    def action(service: KnowledgeService, actor: KnowledgeActor) -> dict[str, Any]:
        report = service.activity(actor, hours=hours)
        return {
            "ok": True,
            "since": report.since_utc,
            "by_agent_type": [
                {"agent_type": item.agent_type, "saved": item.applied, "proposed": item.pending, "refused": item.refused}
                for item in report.by_agent_type
            ],
            "busiest_pages": [{"page": page, "edits": edits} for page, edits in report.busiest_pages],
            "frozen_pages": [{"page": page, "frozen_until": until} for page, until in report.frozen_pages],
        }

    return _run(action)


@tool
def knowledge_problems(stale_days: int = 180) -> str:
    """
    Check team pages for broken links, stale pages, open notes and exact duplicates.

    Args:
        stale_days: Pages not updated for this many days are listed as stale.
    """

    def action(service: KnowledgeService, actor: KnowledgeActor) -> dict[str, Any]:
        problems = service.problems(actor, stale_days=stale_days)
        return {
            "ok": True,
            "broken_links": [{"page": page, "links_to": target} for page, target in problems.broken_links],
            "stale_pages": [{"page": page, "updated_at": at} for page, at in problems.stale_pages],
            "pages_with_open_notes": [{"page": page, "open_notes": count} for page, count in problems.pages_with_open_notes],
            "duplicate_pages": [{"page": page, "duplicate": duplicate} for page, duplicate in problems.duplicate_pages],
        }

    return _run(action)


@tool
def list_open_knowledge_notes(space: str = "team", limit: int = 20) -> str:
    """
    List open notes waiting to be merged into their pages, oldest first.

    Args:
        space: "team", or "personal" in a run for a person.
        limit: How many to list.
    """

    def action(service: KnowledgeService, actor: KnowledgeActor) -> dict[str, Any]:
        notes = service.open_notes(actor, space=space, limit=limit)
        pages = service.page_references(note.page_id for note in notes)
        return {
            "ok": True,
            "notes": [
                {
                    "note_id": note.note_id,
                    "page": pages[note.page_id],
                    "text": note.body,
                    "by": _by(note.actor_kind, note.actor_agent_type),
                    "at": note.created_at_utc,
                }
                for note in notes
            ],
        }

    return _run(action)


@tool
def resolve_knowledge_note(note_id: str, merged: bool) -> str:
    """
    Close an open note: merged=true after you worked it into the page, false to dismiss it.

    Args:
        note_id: The id from list_open_knowledge_notes.
        merged: Whether the page now contains what the note said.
    """

    def action(service: KnowledgeService, actor: KnowledgeActor) -> dict[str, Any]:
        service.resolve_note(actor, note_id, merged=bool(merged))
        return {"ok": True, "status": "resolved" if merged else "dismissed"}

    return _run(action)


@tool
def freeze_knowledge_page(page: str, minutes: int) -> str:
    """
    Stop agents from editing a page for a while; they add notes instead. Lift it early with unfreeze_knowledge_page.

    Args:
        page: The page reference, such as "team/release-process".
        minutes: How long, from 1 minute to a week (10080).
    """

    def action(service: KnowledgeService, actor: KnowledgeActor) -> dict[str, Any]:
        frozen = service.freeze(actor, page, minutes=int(minutes))
        return {"ok": True, "page": frozen.reference(), "frozen_until": frozen.frozen_until_utc}

    return _run(action)


@tool
def unfreeze_knowledge_page(page: str) -> str:
    """
    Let agents edit a frozen page again, for example once you merged its notes and the disagreement is settled.

    Args:
        page: The page reference, such as "team/release-process".
    """

    def action(service: KnowledgeService, actor: KnowledgeActor) -> dict[str, Any]:
        thawed = service.unfreeze(actor, page)
        return {"ok": True, "page": thawed.reference(), "frozen_until": None}

    return _run(action)


@tool
def merge_knowledge_pages(
    source: str,
    target: str,
    body: str,
    base_revision: int,
    source_revision: int,
    title: str = "",
    summary: Optional[str] = None,
) -> str:
    """
    Merge two pages about the same topic into one: the target keeps the merged text, the source is archived.

    Read both pages first and write the merged text of both as body. The
    source's name then leads to the target, so links and references to it
    keep working; nobody can create a new page under that name. The source's
    open notes move to the target. Both pages must be in the same space; in a
    run for one person, only that person's own pages can be merged. Frozen
    pages cannot be merged until the freeze ends or you lift it. The pages'
    history records the merge with fixed text naming the other page.

    Args:
        source: The page to merge away, such as "team/release-steps".
        target: The page to keep, such as "team/release-process".
        body: The merged page text in Markdown.
        base_revision: The target's revision you read.
        source_revision: The source's revision you read.
        title: The merged page's title; empty keeps the target's.
        summary: The merged page's one-sentence summary; omit to keep the target's, or "" to clear it.
    """

    draft = DraftInput(title=title, body=body, summary=summary)

    def action(service: KnowledgeService, actor: KnowledgeActor) -> dict[str, Any]:
        result = service.merge(
            actor,
            source,
            target,
            draft,
            base_revision=int(base_revision),
            source_revision=int(source_revision),
        )
        return {
            "ok": True,
            "status": "merged",
            "page": result.target.reference(),
            "revision": result.target.revision,
            "archived": result.source.reference(),
            "redirects_to": result.target.reference(),
        }

    return _run(action)


@tool
def revert_knowledge_page(page: str, to_revision: int, base_revision: int, reason: str) -> str:
    """
    Make an earlier revision of a page current again (as a new revision), for example after a bad or looping edit.

    Direct team reverts belong to the team run for nobody. In a person's run,
    propose team changes with knowledge_write; their own personal pages may
    still be reverted. Team history records only the restored revision.

    Args:
        page: The page reference.
        to_revision: The earlier revision to bring back.
        base_revision: The page's current revision.
        reason: Why; kept in personal history only, never shared team history.
    """

    def action(service: KnowledgeService, actor: KnowledgeActor) -> dict[str, Any]:
        result = service.revert(actor, page, to_revision=int(to_revision), base_revision=int(base_revision), reason=reason)
        assert result.page is not None
        return {"ok": True, "page": result.page.reference(), "revision": result.page.revision}

    return _run(action)


PROVIDER = ToolProvider(
    name="knowledge_curation",
    tools=(
        list_pending_contributions,
        read_contribution,
        publish_contribution,
        reject_contribution,
        knowledge_activity_report,
        knowledge_problems,
        list_open_knowledge_notes,
        resolve_knowledge_note,
        freeze_knowledge_page,
        unfreeze_knowledge_page,
        merge_knowledge_pages,
        revert_knowledge_page,
    ),
)
