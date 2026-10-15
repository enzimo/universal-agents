"""Strands tool for publishing generated single-page websites.

The page is rendered here, in trusted code, and written into the folder of
the person the task works for (``users/<person>/sites/<site_id>/``; the
person comes only from the task's signed context), with a trusted record in
``state/published_sites/<site_id>.json`` holding each file's SHA-256. The site
host serves a page only through that record and only while the digests match
(``user_docs.published_sites``), so agents' code cannot change what the Web
Chat origin serves.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from strands import tool

from access_control.safe_open import UnsafePath
from custom_tools.runtime_context import get_agent_ref
from custom_tools.workspace_scope import current_workspace_scope, runtime_layout
from runtime_settings.network import compose_public_base_url
from default_values import INTERACTIVE_APP_DEFAULTS, NETWORK_DEFAULTS
from user_docs.published_sites import (
    SITE_CHAT_SIGN_IN_PATH,
    SITE_ID_PATTERN,
    SITE_ID_RANDOM_HEX_CHARS,
    SITE_INDEX_FILENAME,
    PublishedSiteRecordV1,
    PublishedSiteStore,
    site_folder_parts,
)


LOGGER = logging.getLogger(__name__)
_SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9._-]+")
# Room left in a site id for the stamp and the random part (SITE_ID_PATTERN allows 128).
_TRACKING_SLUG_MAX = 40
_PAGE_SLUG_MAX = 48
SiteKind = Literal["article", "comparison", "how_to", "app"]
_SITE_KINDS: tuple[str, ...] = ("article", "comparison", "how_to", "app")
_SITE_KIND_PRESENTATION: dict[str, dict[str, str]] = {
    "article": {
        "default_title": "Generated Article",
        "eyebrow": "Generated article",
        "chat_copy": "Continue the conversation about this page.",
    },
    "comparison": {
        "default_title": "Generated Comparison",
        "eyebrow": "Generated comparison",
        "chat_copy": "Continue the conversation about this comparison.",
    },
    "how_to": {
        "default_title": "Generated How-To",
        "eyebrow": "Generated how-to",
        "chat_copy": "Continue the conversation about this how-to.",
    },
    "app": {
        "default_title": "Interactive App",
        "eyebrow": "Interactive app",
        "chat_copy": "Continue the conversation about this app.",
    },
}


def _script_json(value: Any) -> str:
    """JSON that is safe inside a ``<script>`` block.

    The page carries agent-supplied values (for example ``session_key`` from
    the site spec); ``json.dumps`` leaves ``</script>`` as is, which would let
    such a value end the script and run its own on the Web Chat origin.
    """

    rendered = json.dumps(value, sort_keys=True)
    for character, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"), (" ", "\\u2029")):
        rendered = rendered.replace(character, escaped)
    return rendered


def _json_result(status: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": status,
        "content": [{"text": json.dumps(payload, indent=2, sort_keys=True)}],
    }


def _safe_slug(value: str, *, fallback: str = "site", max_len: int = 64) -> str:
    slug = _SLUG_PATTERN.sub("-", str(value or "").strip().lower()).strip("-._")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug or fallback)[:max_len].strip("-._") or fallback


def _validate_site_id(site_id: str) -> str:
    value = _text(site_id)
    if not value or not SITE_ID_PATTERN.match(value):
        raise ValueError("site_id must contain only letters, numbers, dots, underscores, or hyphens.")
    return value


def _normalize_site_kind(value: Any) -> str:
    normalized = _text(value).lower().replace("-", "_").replace(" ", "_") or "article"
    if normalized not in _SITE_KINDS:
        choices = ", ".join(_SITE_KINDS)
        raise ValueError(f"site_kind must be one of: {choices}.")
    return normalized


def _site_id_from_url(value: Any) -> str:
    url = _text(value)
    if not url:
        return ""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[-3:-1] == ["docs", "sites"]:
        return parts[-1]
    return ""


def _requested_update_site_id(spec: dict[str, Any]) -> str:
    for key in ("update_site_id", "existing_site_id", "regenerate_site_id"):
        value = _text(spec.get(key))
        if value:
            return value
    for key in ("update_site_url", "existing_site_url", "regenerate_site_url"):
        value = _site_id_from_url(spec.get(key))
        if value:
            return value
    if bool(spec.get("update_existing") or spec.get("regenerate_existing")):
        return _text(spec.get("site_id")) or _site_id_from_url(spec.get("site_url"))
    return ""


def _site_store(agent: Any) -> PublishedSiteStore:
    """Records in ``state/published_sites``, pages below ``workspaces/``.

    The workspaces folder is the task's own (``current_workspace_scope``), so
    the page lands in the folder of the person the task works for.
    """

    return PublishedSiteStore(
        records_dir=runtime_layout(agent).published_sites_dir,
        workspaces_dir=current_workspace_scope(agent).workspaces_dir,
    )


def _failure(stage: str, reason: str, message: str) -> dict[str, Any]:
    return _json_result(
        "error",
        {
            "status": "failed",
            "failure_stage": stage,
            "failure_reason": reason,
            "message": message,
            "retryable": False,
        },
    )


def _new_site_id(
    store: PublishedSiteStore,
    *,
    requested_slug: str,
    tracking_slug: str,
    now: datetime,
) -> str:
    """A new, unused site id ending in a random part, so page links are hard to guess."""

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    page_slug = _safe_slug(requested_slug, max_len=_PAGE_SLUG_MAX)
    base = "_".join(part for part in (tracking_slug, f"{stamp}-{page_slug}") if part)
    while True:
        site_id = f"{base}-{secrets.token_hex(SITE_ID_RANDOM_HEX_CHARS // 2)}"
        if not store.exists(site_id):
            return site_id


def _record_to_update(
    store: PublishedSiteStore, update_site_id: str, person_id: str | None
) -> PublishedSiteRecordV1 | dict[str, Any]:
    """The record of the page to update, or the failure to return.

    Only a page made for the same person (or for no person, by work for no
    person) can be updated; any other page is reported as not found.
    """

    try:
        site_id = _validate_site_id(update_site_id)
    except ValueError as exc:
        return _failure("input_validation", "invalid_site_id", str(exc))
    record = store.record(site_id)
    if record is None or record.owner_person_id != person_id:
        if record is not None:
            LOGGER.warning(
                "An update of a page made for another person was refused. event=generated_site_update_refused "
                "site_id=%s owner_person_id=%s task_person_id=%s",
                site_id,
                record.owner_person_id or "-",
                person_id or "-",
            )
        return _failure("input_validation", "site_not_found", f"Generated site does not exist: {site_id}")
    return record


def website_base_url() -> str:
    """The internal website's address people open (workflow pages, generated sites)."""

    return (
        os.getenv("INTERNAL_WEBSITE_BASE_URL")
        or compose_public_base_url(
            os.getenv("NETWORK_PUBLIC_ORIGIN") or NETWORK_DEFAULTS.public_origin.value,
            int(
                os.getenv("INTERNAL_WEBSITE_PUBLIC_PORT")
                or INTERACTIVE_APP_DEFAULTS.internal_website_public_port.value
                or INTERACTIVE_APP_DEFAULTS.internal_website_bind_port.value
            ),
        )
    ).rstrip("/")


def _current_task_context() -> dict[str, Any]:
    agent = get_agent_ref()
    task_id = "no_active_task"
    conversation_id = None
    session_key = None
    channel_name = None
    chat_id = None
    thread_id = None
    gateway_context: Any = None
    if agent is not None and getattr(agent, "task_manager", None) is not None:
        task_id = (agent.task_manager.current_task_id if agent.task_manager is not None else None) or task_id
        task_info = agent.task_manager.get_task_info(task_id) if task_id != "no_active_task" else None
        meta = getattr(task_info, "meta", None)
        conversation_id = getattr(meta, "conversation_id", None)
        gateway_context = getattr(meta, "gateway_context", None)
        session_key = getattr(gateway_context, "session_key", None) if gateway_context else None
        channel_name = getattr(gateway_context, "channel_name", None) if gateway_context else None
        chat_id = getattr(gateway_context, "chat_id", None) if gateway_context else None
        thread_id = getattr(gateway_context, "thread_id", None) if gateway_context else None
    return {
        "task_id": task_id,
        "conversation_id": conversation_id,
        "session_key": session_key,
        "channel_name": channel_name,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "team_id": getattr(agent, "team_id", None),
        "agent_id": getattr(agent, "id", None),
        "named_agent_type": getattr(agent, "named_agent_type", None),
    }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _escape(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _markdown_attr(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _markdown_block(value: Any, *, class_name: str = "") -> str:
    text = _text(value)
    if not text:
        return ""
    classes = "ua-markdown" + (f" {class_name}" if class_name else "")
    return f"<div class=\"{classes}\" data-markdown=\"{_markdown_attr(text)}\"></div>"


def _markdown_paragraph(value: Any, *, class_name: str = "") -> str:
    text = _text(value)
    if not text:
        return ""
    classes = "ua-markdown" + (f" {class_name}" if class_name else "")
    return f"<div class=\"{classes}\" data-markdown=\"{_markdown_attr(text)}\"></div>"


def _safe_href(value: Any) -> str:
    url = _text(value)
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        LOGGER.warning("Omitting unsafe generated-site resource URL scheme: %s", parsed.scheme or "<empty>")
        return ""
    return url


def _tracking_slug(spec: dict[str, Any], context: dict[str, Any]) -> str:
    explicit = _text(spec.get("url_tracking_id") or spec.get("tracking_id"))
    if explicit:
        return _safe_slug(explicit, fallback="track", max_len=_TRACKING_SLUG_MAX)
    thread_id = _text(spec.get("thread_id") or context.get("thread_id"))
    if thread_id:
        return "thread-" + _safe_slug(thread_id, fallback="thread", max_len=_TRACKING_SLUG_MAX - 7)
    conversation_id = _text(spec.get("conversation_id") or context.get("conversation_id"))
    if conversation_id:
        return "conv-" + _safe_slug(conversation_id, fallback="conversation", max_len=_TRACKING_SLUG_MAX - 5)
    return ""


def _has_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_meaningful_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_meaningful_value(item) for item in value)
    return True


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, Any]:
    first_present: tuple[str, Any] | None = None
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            value = mapping.get(key)
            first_present = first_present or (key, value)
            if _has_meaningful_value(value):
                return key, value
    if first_present is not None:
        return first_present
    return keys[0], None


def _section_heading(
    spec: dict[str, Any],
    key: str,
    default: str,
    *,
    aliases: tuple[str, ...] = (),
) -> str:
    lookup_keys: list[str] = []
    for lookup_key in (key, *aliases):
        if lookup_key not in lookup_keys:
            lookup_keys.append(lookup_key)
    headings = spec.get("section_headings") or spec.get("headings")
    if isinstance(headings, dict):
        for lookup_key in lookup_keys:
            heading = _text(headings.get(lookup_key))
            if heading:
                return html.escape(heading)
    for lookup_key in lookup_keys:
        for suffix in ("heading", "title", "label"):
            heading = _text(spec.get(f"{lookup_key}_{suffix}"))
            if heading:
                return html.escape(heading)
    return html.escape(default)


def _render_panel(
    spec: dict[str, Any],
    key: str,
    default_heading: str,
    body_html: str,
    *,
    aliases: tuple[str, ...] = (),
) -> str:
    if not body_html.strip():
        return ""
    heading = _section_heading(spec, key, default_heading, aliases=aliases)
    return (
        "      <section class=\"panel\">\n"
        f"        <h2>{heading}</h2>\n"
        f"        {body_html}\n"
        "      </section>"
    )


def _simple_list_values(items: Any) -> list[str]:
    return [_text(item) for item in _as_list(items) if _text(item)]


def _render_simple_list(items: Any, *, empty_text: str = "") -> str:
    values = _simple_list_values(items)
    if not values:
        return f"<p class=\"muted\">{html.escape(empty_text)}</p>" if empty_text else ""
    return "<ul>" + "".join(
        f"<li><div class=\"ua-markdown inline\" data-markdown=\"{_markdown_attr(item)}\"></div></li>"
        for item in values
    ) + "</ul>"


def _render_checklist(items: Any, *, group: str, empty_text: str = "") -> str:
    rows: list[str] = []
    for index, raw_item in enumerate(_as_list(items)):
        if isinstance(raw_item, dict):
            name = _text(raw_item.get("name") or raw_item.get("title") or raw_item.get("item"))
            quantity = _text(raw_item.get("quantity"))
            notes = _text(raw_item.get("notes") or raw_item.get("detail") or raw_item.get("description"))
            required = raw_item.get("required")
        else:
            name = _text(raw_item)
            quantity = ""
            notes = ""
            required = None
        if not name:
            continue
        secondary_parts = []
        if quantity:
            secondary_parts.append(f"Qty: {html.escape(quantity)}")
        if required is not None:
            secondary_parts.append("Required" if bool(required) else "Optional")
        secondary = " · ".join(secondary_parts)
        note_html = (
            f"<div class=\"item-note ua-markdown inline\" data-markdown=\"{_markdown_attr(notes)}\"></div>"
            if notes
            else ""
        )
        secondary_html = f"<span class=\"item-meta\">{secondary}</span>" if secondary else ""
        rows.append(
            "<label class=\"check-row\">"
            f"<input type=\"checkbox\" data-check-key=\"{group}:{index}\">"
            "<span>"
            f"<strong>{html.escape(name)}</strong>"
            f"{secondary_html}{note_html}"
            "</span>"
            "</label>"
        )
    if not rows:
        return f"<p class=\"muted\">{html.escape(empty_text)}</p>" if empty_text else ""
    return "<div class=\"checklist\">" + "\n".join(rows) + "</div>"


def _render_steps(steps: Any) -> str:
    rows: list[str] = []
    for source_index, raw_step in enumerate(_as_list(steps), start=1):
        if isinstance(raw_step, dict):
            raw_title = _text(raw_step.get("title") or raw_step.get("name"))
            details = _text(raw_step.get("details") or raw_step.get("description") or raw_step.get("instruction"))
            warning = _text(raw_step.get("warning") or raw_step.get("caution"))
            verification = _text(raw_step.get("verification") or raw_step.get("check"))
        else:
            raw_title = ""
            details = _text(raw_step)
            warning = ""
            verification = ""
        if not raw_title and not details and not warning and not verification:
            continue
        display_number = len(rows) + 1
        title = raw_title or f"Step {display_number}"
        warning_html = _markdown_block(warning, class_name="warning") if warning else ""
        verification_html = _markdown_block("Check: " + verification, class_name="verify") if verification else ""
        rows.append(
            "<article class=\"step\">"
            "<label class=\"step-heading\">"
            f"<input type=\"checkbox\" data-check-key=\"step:{source_index}\">"
            f"<span><span class=\"step-number\">{display_number}</span>{html.escape(title)}</span>"
            "</label>"
            f"{_markdown_paragraph(details)}"
            f"{warning_html}{verification_html}"
            "</article>"
        )
    if not rows:
        return ""
    return "\n".join(rows)


def _render_plans(plans: Any) -> str:
    rows: list[str] = []
    for raw_plan in _as_list(plans):
        if isinstance(raw_plan, dict):
            raw_title = _text(raw_plan.get("title") or raw_plan.get("name"))
            description = _text(raw_plan.get("description") or raw_plan.get("summary"))
            items = raw_plan.get("items") or raw_plan.get("steps") or []
        else:
            raw_title = ""
            description = _text(raw_plan)
            items = []
        if not raw_title and not description and not _simple_list_values(items):
            continue
        title = raw_title or "Plan"
        rows.append(
            "<article class=\"plan\">"
            f"<h3>{html.escape(title)}</h3>"
            f"{_markdown_paragraph(description)}"
            f"{_render_simple_list(items)}"
            "</article>"
        )
    if not rows:
        return ""
    return "\n".join(rows)


def _render_extra_sections(sections: Any) -> str:
    rendered: list[str] = []
    for raw_section in _as_list(sections):
        if not isinstance(raw_section, dict):
            continue
        title = _text(raw_section.get("title") or raw_section.get("name"))
        body = _text(raw_section.get("body") or raw_section.get("content"))
        bullets = raw_section.get("items") or raw_section.get("bullets") or []
        bullets_html = _render_simple_list(bullets)
        if not title and not body and not bullets_html:
            continue
        rendered.append(
            "<section class=\"panel\">"
            f"<h2>{html.escape(title or 'Notes')}</h2>"
            f"{_markdown_paragraph(body) if body else ''}"
            f"{bullets_html}"
            "</section>"
        )
    return "\n".join(rendered)


def _render_resources(resources: Any) -> str:
    rows: list[str] = []
    for raw_resource in _as_list(resources):
        if isinstance(raw_resource, dict):
            label = _text(raw_resource.get("label") or raw_resource.get("title") or raw_resource.get("url"))
            url = _safe_href(raw_resource.get("url"))
            notes = _text(raw_resource.get("notes") or raw_resource.get("description"))
        else:
            label = _text(raw_resource)
            url = _safe_href(raw_resource)
            notes = ""
        if not label:
            continue
        if url:
            label_html = f"<a href=\"{html.escape(url, quote=True)}\" rel=\"noopener noreferrer\">{html.escape(label)}</a>"
        else:
            label_html = html.escape(label)
        note_html = _markdown_paragraph(notes) if notes else ""
        rows.append(f"<li>{label_html}{note_html}</li>")
    if not rows:
        return ""
    return "<ul>" + "".join(rows) + "</ul>"


def _render_content_cards(items: Any) -> str:
    cards: list[str] = []
    for raw_item in _as_list(items):
        if isinstance(raw_item, dict):
            title = _text(raw_item.get("title") or raw_item.get("name") or raw_item.get("label"))
            body = _text(
                raw_item.get("body")
                or raw_item.get("content")
                or raw_item.get("description")
                or raw_item.get("summary")
            )
            value = _text(raw_item.get("value") or raw_item.get("metric"))
        else:
            title = ""
            body = _text(raw_item)
            value = ""
        if not title and not body and not value:
            continue
        title_html = f"<h3>{html.escape(title)}</h3>" if title else ""
        value_html = f"<p class=\"card-value\">{html.escape(value)}</p>" if value else ""
        cards.append(
            "<article class=\"content-card\">"
            f"{title_html}{value_html}{_markdown_paragraph(body) if body else ''}"
            "</article>"
        )
    if not cards:
        return ""
    return "<div class=\"card-grid\">" + "".join(cards) + "</div>"


def _render_option_cards(options: Any) -> str:
    cards: list[str] = []
    for raw_option in _as_list(options):
        if isinstance(raw_option, dict):
            name = _text(raw_option.get("name") or raw_option.get("title") or raw_option.get("label"))
            summary = _text(raw_option.get("summary") or raw_option.get("description"))
            best_for = _text(raw_option.get("best_for") or raw_option.get("bestFit"))
            pros = raw_option.get("pros") or raw_option.get("advantages")
            cons = raw_option.get("cons") or raw_option.get("disadvantages")
        else:
            name = _text(raw_option)
            summary = ""
            best_for = ""
            pros = None
            cons = None
        if not name:
            continue
        best_for_html = (
            f"<p class=\"best-for\"><strong>Best for:</strong> {html.escape(best_for)}</p>"
            if best_for
            else ""
        )
        pros_html = _render_simple_list(pros)
        cons_html = _render_simple_list(cons)
        cards.append(
            "<article class=\"option-card\">"
            f"<h3>{html.escape(name)}</h3>"
            f"{_markdown_paragraph(summary) if summary else ''}"
            f"{best_for_html}"
            f"{'<h4>Pros</h4>' + pros_html if pros_html else ''}"
            f"{'<h4>Cons</h4>' + cons_html if cons_html else ''}"
            "</article>"
        )
    if not cards:
        return ""
    return "<div class=\"option-grid\">" + "".join(cards) + "</div>"


def _comparison_option_names(options: Any, rows: Any) -> list[str]:
    names: list[str] = []
    for raw_option in _as_list(options):
        if isinstance(raw_option, dict):
            name = _text(raw_option.get("name") or raw_option.get("title") or raw_option.get("label"))
        else:
            name = _text(raw_option)
        if name and name not in names:
            names.append(name)
    if names:
        return names
    for raw_row in _as_list(rows):
        if not isinstance(raw_row, dict) or not isinstance(raw_row.get("values"), dict):
            continue
        for name in raw_row["values"]:
            normalized = _text(name)
            if normalized and normalized not in names:
                names.append(normalized)
    return names


def _render_comparison_table(options: Any, rows: Any) -> str:
    option_names = _comparison_option_names(options, rows)
    if not option_names:
        return ""
    body_rows: list[str] = []
    for raw_row in _as_list(rows):
        if not isinstance(raw_row, dict):
            continue
        criterion = _text(raw_row.get("criterion") or raw_row.get("name") or raw_row.get("label"))
        values = raw_row.get("values")
        notes = _text(raw_row.get("notes") or raw_row.get("detail"))
        if not criterion or not isinstance(values, (dict, list, tuple)):
            continue
        if isinstance(values, dict):
            cells = [_text(values.get(option_name)) for option_name in option_names]
        else:
            raw_values = list(values)
            cells = [_text(raw_values[index]) if index < len(raw_values) else "" for index in range(len(option_names))]
        notes_html = f"<p class=\"table-note\">{html.escape(notes)}</p>" if notes else ""
        body_rows.append(
            "<tr>"
            f"<th scope=\"row\">{html.escape(criterion)}{notes_html}</th>"
            + "".join(f"<td>{_markdown_paragraph(cell) if cell else ''}</td>" for cell in cells)
            + "</tr>"
        )
    if not body_rows:
        return ""
    header_cells = "".join(f"<th scope=\"col\">{html.escape(name)}</th>" for name in option_names)
    return (
        "<div class=\"comparison-table-wrap\"><table class=\"comparison-table\">"
        f"<thead><tr><th scope=\"col\">Criterion</th>{header_cells}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>"
    )


def _render_recommendation(value: Any) -> str:
    if isinstance(value, dict):
        title = _text(value.get("title") or value.get("choice") or value.get("name"))
        body = _text(value.get("body") or value.get("summary") or value.get("reason"))
        reasons = value.get("reasons") or value.get("items")
    else:
        title = ""
        body = _text(value)
        reasons = None
    if not title and not body and not _simple_list_values(reasons):
        return ""
    return (
        "<div class=\"recommendation\">"
        f"{'<h3>' + html.escape(title) + '</h3>' if title else ''}"
        f"{_markdown_paragraph(body) if body else ''}"
        f"{_render_simple_list(reasons)}"
        "</div>"
    )


def _render_stats(stats: Any) -> str:
    normalized: list[Any]
    if isinstance(stats, dict):
        normalized = [{"label": key, "value": value} for key, value in stats.items()]
    else:
        normalized = _as_list(stats)
    cards: list[str] = []
    for raw_stat in normalized:
        if isinstance(raw_stat, dict):
            label = _text(raw_stat.get("label") or raw_stat.get("name") or raw_stat.get("title"))
            value = _text(raw_stat.get("value") or raw_stat.get("current"))
            detail = _text(raw_stat.get("detail") or raw_stat.get("description") or raw_stat.get("unit"))
        else:
            label = ""
            value = _text(raw_stat)
            detail = ""
        if not label and not value:
            continue
        cards.append(
            "<article class=\"stat-card\">"
            f"{'<span>' + html.escape(label) + '</span>' if label else ''}"
            f"<strong>{html.escape(value)}</strong>"
            f"{'<small>' + html.escape(detail) + '</small>' if detail else ''}"
            "</article>"
        )
    if not cards:
        return ""
    return "<div class=\"stat-grid\">" + "".join(cards) + "</div>"


def _number(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _render_progress_trackers(trackers: Any) -> str:
    cards: list[str] = []
    for index, raw_tracker in enumerate(_as_list(trackers), start=1):
        if not isinstance(raw_tracker, dict):
            continue
        label = _text(raw_tracker.get("label") or raw_tracker.get("name") or raw_tracker.get("title"))
        if not label:
            continue
        description = _text(raw_tracker.get("description") or raw_tracker.get("summary"))
        target = max(0.000001, _number(raw_tracker.get("target") or raw_tracker.get("goal"), 1.0))
        current = max(0.0, _number(raw_tracker.get("current") or raw_tracker.get("initial"), 0.0))
        step = max(0.000001, _number(raw_tracker.get("step") or raw_tracker.get("increment"), 1.0))
        unit = _text(raw_tracker.get("unit"))
        tracker_key = f"{index}-{_safe_slug(raw_tracker.get('id') or label, fallback=f'tracker-{index}')}"
        cards.append(
            "<article class=\"progress-card\" "
            f"data-progress-key=\"{html.escape(tracker_key, quote=True)}\" "
            f"data-progress-initial=\"{_format_number(current)}\" "
            f"data-progress-target=\"{_format_number(target)}\" "
            f"data-progress-unit=\"{html.escape(unit, quote=True)}\">"
            f"<h3>{html.escape(label)}</h3>"
            f"{_markdown_paragraph(description) if description else ''}"
            "<div class=\"progress-readout\">"
            "<progress data-progress-bar></progress>"
            "<output data-progress-output></output>"
            "</div>"
            "<div class=\"progress-controls\">"
            f"<button type=\"button\" data-progress-delta=\"-{_format_number(step)}\" aria-label=\"Decrease {html.escape(label, quote=True)}\">−</button>"
            f"<input type=\"number\" min=\"0\" step=\"{_format_number(step)}\" data-progress-input aria-label=\"{html.escape(label, quote=True)} value\">"
            f"<button type=\"button\" data-progress-delta=\"{_format_number(step)}\" aria-label=\"Increase {html.escape(label, quote=True)}\">+</button>"
            "</div>"
            "</article>"
        )
    if not cards:
        return ""
    return "<div class=\"progress-grid\">" + "".join(cards) + "</div>"


def _site_has_browser_state(spec: dict[str, Any], site_kind: str) -> bool:
    if site_kind == "how_to":
        return any(
            _has_meaningful_value(spec.get(key))
            for key in (
                "materials",
                "supplies",
                "items_to_bring",
                "what_to_bring",
                "packing_list",
                "tools",
                "steps",
            )
        )
    if site_kind == "app":
        return any(
            _has_meaningful_value(spec.get(key))
            for key in ("progress_trackers", "trackers", "checklist_items", "tasks", "habits")
        )
    return False


def _format_generated_at(value: datetime) -> str:
    utc_value = value.astimezone(timezone.utc)
    hour = utc_value.hour % 12 or 12
    minute = f"{utc_value.minute:02d}"
    am_pm = "AM" if utc_value.hour < 12 else "PM"
    return f"{utc_value.strftime('%B')} {utc_value.day}, {utc_value.year} at {hour}:{minute} {am_pm} UTC"


def _render_html(
    spec: dict[str, Any],
    *,
    site_kind: str,
    site_id: str,
    site_url: str,
    context: dict[str, Any],
) -> str:
    presentation = _SITE_KIND_PRESENTATION[site_kind]
    title = _text(spec.get("title")) or presentation["default_title"]
    subtitle = _text(spec.get("subtitle") or spec.get("summary"))
    generated_at_dt = datetime.now(timezone.utc)
    generated_at = generated_at_dt.isoformat()
    generated_at_display = _format_generated_at(generated_at_dt)
    chat_config = {
        "siteId": site_id,
        "siteUrl": site_url,
        "siteKind": site_kind,
        "apiPath": f"/api/sites/{site_id}/chat",
        "signInUrl": SITE_CHAT_SIGN_IN_PATH,
        "sessionKey": spec.get("session_key") or context.get("session_key"),
        "conversationId": spec.get("conversation_id") or context.get("conversation_id"),
        "mirrorToOrigin": bool(spec.get("mirror_to_origin", True)),
    }
    subtitle_html = _markdown_block(subtitle, class_name="subtitle") if subtitle else ""
    safety_key, safety_items = _first_present(spec, ("safety_notes", "safety"))
    materials_key, materials_items = _first_present(
        spec,
        ("materials", "supplies", "items_to_bring", "what_to_bring", "packing_list"),
    )
    materials_default_headings = {
        "supplies": "Supplies",
        "items_to_bring": "What To Bring",
        "what_to_bring": "What To Bring",
        "packing_list": "Packing List",
    }
    assumptions_key, assumptions_items = _first_present(spec, ("assumptions", "limitations", "constraints"))
    resources_key, resources_items = _first_present(spec, ("resources", "references", "links"))
    extra_sections_html = _render_extra_sections(spec.get("sections"))
    resources_html = _render_resources(resources_items)
    kind_sections: tuple[str, ...]
    if site_kind == "how_to":
        kind_sections = (
            _render_panel(
                spec,
                safety_key,
                "Safety Notes",
                _render_simple_list(safety_items),
                aliases=("safety", "safety_notes"),
            ),
            _render_panel(
                spec,
                materials_key,
                materials_default_headings.get(materials_key, "Materials"),
                _render_checklist(materials_items, group="material"),
                aliases=("materials", "supplies", "items_to_bring", "what_to_bring", "packing_list"),
            ),
            _render_panel(spec, "tools", "Tools", _render_checklist(spec.get("tools"), group="tool")),
            _render_panel(spec, "plans", "Plans", _render_plans(spec.get("plans"))),
            _render_panel(
                spec,
                "steps",
                "Ordered Steps",
                _render_steps(spec.get("steps")),
                aliases=("ordered_steps",),
            ),
            _render_panel(
                spec,
                assumptions_key,
                "Assumptions And Limits",
                _render_simple_list(assumptions_items),
                aliases=("assumptions", "limitations", "constraints", "assumptions_and_limits"),
            ),
        )
    elif site_kind == "comparison":
        comparison_rows = spec.get("comparison_rows") or spec.get("criteria")
        kind_sections = (
            _render_panel(spec, "options", "Options", _render_option_cards(spec.get("options"))),
            _render_panel(
                spec,
                "comparison",
                "Side-By-Side Comparison",
                _render_comparison_table(spec.get("options"), comparison_rows),
                aliases=("comparison_rows", "criteria"),
            ),
            _render_panel(
                spec,
                "recommendation",
                "Recommendation",
                _render_recommendation(spec.get("recommendation")),
            ),
        )
    elif site_kind == "app":
        app_checklist_key, app_checklist_items = _first_present(
            spec,
            ("checklist_items", "tasks", "habits"),
        )
        progress_key, progress_items = _first_present(spec, ("progress_trackers", "trackers"))
        kind_sections = (
            _render_panel(spec, "stats", "At A Glance", _render_stats(spec.get("stats"))),
            _render_panel(
                spec,
                progress_key,
                "Progress",
                _render_progress_trackers(progress_items),
                aliases=("progress_trackers", "trackers"),
            ),
            _render_panel(
                spec,
                app_checklist_key,
                "Trackable Items",
                _render_checklist(app_checklist_items, group="app"),
                aliases=("checklist_items", "tasks", "habits"),
            ),
            _render_panel(spec, "actions", "Actions And Links", _render_resources(spec.get("actions"))),
        )
    else:
        kind_sections = (
            _render_panel(
                spec,
                "highlights",
                "Highlights",
                _render_content_cards(spec.get("highlights")),
            ),
            _render_panel(
                spec,
                "key_points",
                "Key Points",
                _render_simple_list(spec.get("key_points")),
                aliases=("key_points", "takeaways"),
            ),
            _render_panel(
                spec,
                "callouts",
                "Worth Noting",
                _render_content_cards(spec.get("callouts")),
            ),
        )
    standard_sections_html = "\n".join(section for section in kind_sections if section)
    resources_section_html = _render_panel(
        spec,
        resources_key,
        "References",
        resources_html,
        aliases=("resources", "references", "links"),
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="/docs/assets/ua-runtime-theme.css">
  <script src="/docs/assets/appearance.js" defer></script>
  <script src="/docs/assets/static-mesh.js" defer></script>
  <script src="/docs/assets/metallic_mesh.js" defer></script>
  <script src="/docs/assets/operational_ids.js"></script>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #5f6c72;
      --line: rgba(28, 69, 60, 0.16);
      --paper: #f8faf7;
      --panel: rgba(255, 255, 255, 0.70);
      --panel-soft: rgba(255, 255, 255, 0.56);
      --panel-strong: rgba(247, 255, 252, 0.78);
      --accent: #0f766e;
      --accent-strong: #075985;
      --accent-mid: #0284c7;
      --warn: #8a4b08;
      --warn-bg: rgba(255, 245, 219, 0.76);
      --verify-bg: rgba(233, 247, 239, 0.76);
      --shadow: 0 34px 120px rgba(3, 28, 42, 0.22);
    }}
    :root[data-color-mode="dark"] {{
      color-scheme: dark;
      --ink: #edf3f0;
      --muted: #a9b8b3;
      --line: rgba(222, 235, 229, 0.16);
      --paper: #030202;
      --panel: rgba(12, 15, 16, 0.84);
      --panel-soft: rgba(15, 18, 19, 0.72);
      --panel-strong: rgba(8, 10, 11, 0.88);
      --accent: #8bc8d7;
      --accent-strong: #bce8f0;
      --accent-mid: #59b9d1;
      --warn: #e6b86e;
      --warn-bg: rgba(86, 64, 27, 0.42);
    }}
    * {{ box-sizing: border-box; }}
    html {{
      min-height: 100%;
      background:
        radial-gradient(circle at 16% 14%, color-mix(in srgb, var(--ua-doc-light-blue, #082f73) 46%, transparent) 0, transparent 36%),
        radial-gradient(circle at 84% 14%, color-mix(in srgb, var(--ua-doc-light-yellow, #ffd60a) 48%, transparent) 0, transparent 38%),
        radial-gradient(circle at 18% 86%, color-mix(in srgb, var(--ua-doc-light-green, #20f05a) 38%, transparent) 0, transparent 40%),
        radial-gradient(circle at 86% 86%, color-mix(in srgb, var(--ua-doc-light-red, #ff3030) 26%, transparent) 0, transparent 42%),
        var(--paper);
    }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: transparent;
      color: var(--ink);
      line-height: 1.5;
    }}
    body.chat-popped {{
      padding-bottom: calc(max(300px, 33vh) + 36px);
    }}
    .ua-doc-static-mesh {{
      position: fixed;
      inset: 0;
      z-index: 0;
      width: 100vw;
      height: 100vh;
      pointer-events: none;
    }}
    header {{
      position: relative;
      z-index: 1;
      max-width: 1440px;
      margin: 18px auto 0;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel-strong);
      color: var(--ink);
      padding: 32px clamp(18px, 5vw, 56px);
      box-shadow: var(--shadow);
      backdrop-filter: blur(24px) saturate(140%);
      -webkit-backdrop-filter: blur(24px) saturate(140%);
    }}
    .eyebrow {{
      margin: 0 0 8px;
      color: var(--accent-strong);
      font-size: 0.8rem;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    h1 {{ margin: 0; max-width: 980px; font-size: clamp(2rem, 5vw, 4.4rem); line-height: 1.02; }}
    header p {{ max-width: 860px; margin: 16px 0 0; color: var(--muted); font-size: 1.05rem; }}
    main {{
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(310px, 390px);
      gap: 18px;
      padding: 18px;
      max-width: 1440px;
      margin: 0 auto;
    }}
    .content {{ display: grid; gap: 18px; }}
    .panel, .chat {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 18px;
      box-shadow: 0 18px 70px rgba(3, 28, 42, 0.14);
      backdrop-filter: blur(24px) saturate(140%);
      -webkit-backdrop-filter: blur(24px) saturate(140%);
    }}
    h2 {{ margin: 0 0 12px; font-size: 1.25rem; }}
    h3 {{ margin: 0 0 8px; font-size: 1rem; }}
    h4 {{ margin: 14px 0 6px; font-size: .9rem; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }}
    .pill {{ border: 1px solid var(--line); color: var(--ink); background: var(--panel-soft); border-radius: 999px; padding: 5px 10px; font-size: .82rem; }}
    .card-grid, .option-grid, .stat-grid, .progress-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 12px;
    }}
    .content-card, .option-card, .stat-card, .progress-card {{
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: var(--panel-soft);
    }}
    .card-value {{ margin: 0 0 8px; color: var(--accent-strong); font-size: 1.45rem; font-weight: 800; }}
    .best-for {{ color: var(--accent-strong); }}
    .comparison-table-wrap {{ width: 100%; overflow-x: auto; }}
    .comparison-table {{ width: 100%; min-width: 620px; border-collapse: collapse; }}
    .comparison-table th, .comparison-table td {{
      border: 1px solid var(--line);
      padding: 11px;
      text-align: left;
      vertical-align: top;
    }}
    .comparison-table thead th {{ background: rgba(8, 145, 178, 0.13); color: var(--ink); }}
    .comparison-table tbody th {{ min-width: 150px; background: var(--panel-soft); }}
    .table-note {{ margin: 4px 0 0; color: var(--muted); font-size: .82rem; font-weight: 400; }}
    .recommendation {{
      border-left: 4px solid var(--accent-mid);
      border-radius: 7px;
      padding: 14px;
      background: rgba(8, 145, 178, 0.10);
    }}
    .stat-card {{ display: grid; gap: 4px; }}
    .stat-card span, .stat-card small {{ color: var(--muted); }}
    .stat-card strong {{ color: var(--accent-strong); font-size: 1.7rem; line-height: 1.1; }}
    .progress-card {{ display: grid; gap: 12px; }}
    .progress-readout {{ display: grid; gap: 5px; }}
    .progress-readout progress {{ width: 100%; height: 14px; accent-color: var(--accent-mid); }}
    .progress-readout output {{ color: var(--muted); font-size: .88rem; }}
    .progress-controls {{
      display: grid;
      grid-template-columns: 42px minmax(84px, 1fr) 42px;
      gap: 8px;
      align-items: center;
    }}
    .progress-controls button {{ height: 38px; padding: 0; font-size: 1.15rem; }}
    .progress-controls input {{
      width: 100%;
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: rgba(255, 255, 255, 0.72);
      color: var(--ink);
      font: inherit;
    }}
    .checklist {{ display: grid; gap: 8px; }}
    .check-row, .step-heading {{
      display: grid;
      grid-template-columns: 22px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
    }}
    input[type="checkbox"] {{ width: 18px; height: 18px; accent-color: var(--accent); }}
    .item-meta, .item-note {{ display: block; margin-top: 2px; color: var(--muted); font-size: .9rem; }}
    .step {{ display: grid; gap: 8px; border-top: 1px solid var(--line); padding-top: 14px; }}
    .step:first-child {{ border-top: 0; padding-top: 0; }}
    .step p {{ margin: 0; }}
    .step-number {{
      display: inline-grid;
      place-items: center;
      width: 24px;
      height: 24px;
      margin-right: 8px;
      border-radius: 50%;
      background: linear-gradient(135deg, var(--accent-strong), var(--accent-mid));
      color: white;
      font-size: .82rem;
    }}
    .warning, .verify {{
      padding: 10px;
      border-radius: 6px;
      border: 1px solid transparent;
    }}
    .warning {{ background: var(--warn-bg); border-color: #efd28a; color: var(--warn); }}
    .verify {{ background: var(--verify-bg); border-color: #bde5c8; color: #1f5f35; }}
    .plan {{ border-top: 1px solid var(--line); padding-top: 12px; }}
    .plan:first-child {{ border-top: 0; padding-top: 0; }}
    .muted {{ color: var(--muted); }}
    .chat {{
      position: sticky;
      top: 16px;
      align-self: start;
      display: grid;
      grid-template-rows: auto minmax(220px, 1fr) auto auto;
      gap: 12px;
    }}
    .chat-header {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
    }}
    .chat-popout-button {{
      display: inline-grid;
      place-items: center;
      width: 38px;
      height: 38px;
      padding: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--accent-strong);
      background: var(--panel-soft);
      box-shadow: none;
    }}
    .chat-popout-button svg {{ width: 19px; height: 19px; stroke: currentColor; }}
    .chat-popout-button:hover,
    .chat-popout-button:focus-visible {{ color: white; background: linear-gradient(135deg, var(--accent-strong), var(--accent-mid)); outline: none; }}
    .chat.is-popped {{
      position: fixed;
      left: 18px;
      right: 18px;
      bottom: 18px;
      top: auto;
      z-index: 20;
      height: max(300px, 33vh);
      max-height: calc(100vh - 36px);
      grid-template-columns: minmax(0, 1fr) minmax(300px, 0.42fr);
      grid-template-rows: auto minmax(0, 1fr) auto;
      align-items: stretch;
    }}
    .chat.is-popped .chat-header {{ grid-column: 2; grid-row: 1; align-content: start; }}
    .chat.is-popped .chat-copy {{ display: none; }}
    .chat.is-popped .messages {{ grid-column: 1; grid-row: 1 / span 3; min-height: 0; max-height: none; }}
    .chat.is-popped form {{ grid-column: 2; grid-row: 2; align-self: stretch; grid-template-rows: minmax(0, 1fr) auto auto; }}
    .chat.is-popped textarea {{ min-height: 0; height: 100%; resize: none; }}
    .chat.is-popped #chat-status {{ grid-column: 2; grid-row: 3; margin: 0; }}
    .messages {{ min-height: 260px; max-height: 52vh; overflow: auto; display: grid; align-content: start; gap: 10px; padding-right: 4px; }}
    .message {{
      width: fit-content;
      max-width: min(760px, 86%);
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: var(--panel-soft);
    }}
    .message.user {{
      justify-self: end;
      border-color: rgba(8, 145, 178, 0.34);
      background: rgba(8, 145, 178, 0.16);
    }}
    .message.assistant {{
      justify-self: start;
      border-color: #d2d8dc;
      background: rgba(255, 255, 255, 0.48);
    }}
    .ua-markdown > :first-child {{ margin-top: 0; }}
    .ua-markdown > :last-child {{ margin-bottom: 0; }}
    .ua-markdown p {{ margin: 0 0 0.7rem; }}
    .ua-markdown ul,
    .ua-markdown ol {{ margin: 0.35rem 0 0.8rem; padding-left: 1.35rem; }}
    .ua-markdown blockquote {{ margin: 0.6rem 0; padding-left: 0.8rem; border-left: 3px solid var(--line-strong); color: var(--muted); }}
    .ua-markdown pre {{ margin: 0.65rem 0; overflow: auto; border: 1px solid var(--line); border-radius: 8px; padding: 0.75rem; background: rgba(255, 255, 255, 0.54); }}
    .ua-markdown code {{ border-radius: 5px; padding: 0.1rem 0.28rem; background: rgba(255, 255, 255, 0.58); font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; font-size: 0.9em; }}
    .ua-markdown pre code {{ padding: 0; background: transparent; }}
    .message-table-wrap {{ overflow-x: auto; margin: 0.65rem 0; }}
    .message-table-wrap table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
    .message-table-wrap th,
    .message-table-wrap td {{ border: 1px solid var(--line); padding: 0.45rem 0.55rem; text-align: left; vertical-align: top; }}
    .message-table-wrap th {{ background: rgba(8, 145, 178, 0.12); color: var(--ink); }}
    .chat form {{ display: grid; gap: 8px; }}
    .mirror-toggle {{
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      gap: 8px;
      align-items: center;
      color: var(--muted);
      font-size: .9rem;
    }}
    textarea {{ width: 100%; min-height: 84px; resize: vertical; border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: rgba(255, 255, 255, 0.72); color: var(--ink); font: inherit; }}
    button {{ border: 0; border-radius: 8px; padding: 10px 12px; background: linear-gradient(135deg, var(--accent-strong), var(--accent-mid)); color: white; font: inherit; font-weight: 700; cursor: pointer; }}
    button:disabled {{ background: #8aa6a2; cursor: wait; }}
    a {{ color: var(--accent-strong); }}
    @media (max-width: 900px) {{
      main {{ grid-template-columns: 1fr; }}
      .chat {{ position: static; }}
      .chat.is-popped {{
        position: fixed;
        left: 10px;
        right: 10px;
        bottom: 10px;
        height: 72vh;
        grid-template-columns: 1fr;
        grid-template-rows: auto minmax(0, 1fr) auto auto;
      }}
      .chat.is-popped .chat-header,
      .chat.is-popped .messages,
      .chat.is-popped form,
      .chat.is-popped #chat-status {{ grid-column: auto; grid-row: auto; }}
      body.chat-popped {{ padding-bottom: calc(72vh + 20px); }}
    }}
  </style>
</head>
<body>
  <header>
    <p class="eyebrow">{html.escape(presentation["eyebrow"])}</p>
    <h1>{html.escape(title)}</h1>
    {subtitle_html}
    <div class="meta">
      <time class="pill" datetime="{html.escape(generated_at, quote=True)}">Generated: {html.escape(generated_at_display)}</time>
    </div>
  </header>
  <main>
    <div class="content">
      {standard_sections_html}
      {extra_sections_html}
      {resources_section_html}
    </div>
    <aside id="site-chat" class="chat" aria-label="Ask follow-up questions">
      <div class="chat-header">
        <div>
          <h2>Ask The Agent</h2>
          <p class="muted chat-copy">{html.escape(presentation["chat_copy"])}</p>
        </div>
        <button id="chat-popout-button" class="chat-popout-button" type="button" aria-label="Pop out chat" aria-pressed="false">
          <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M8 3H3v5"></path>
            <path d="M16 3h5v5"></path>
            <path d="M21 16v5h-5"></path>
            <path d="M3 16v5h5"></path>
            <path d="M3 3l7 7"></path>
            <path d="M21 3l-7 7"></path>
            <path d="M21 21l-7-7"></path>
            <path d="M3 21l7-7"></path>
          </svg>
        </button>
      </div>
      <div id="messages" class="messages" aria-live="polite"></div>
      <form id="chat-form">
        <textarea id="message-input" placeholder="Ask a follow-up question"></textarea>
        <label class="mirror-toggle">
          <input id="mirror-toggle" type="checkbox">
          <span>Mirror this web chat to the original conversation</span>
        </label>
        <button id="send-button" type="submit">Send</button>
      </form>
      <p class="muted" id="chat-status"></p>
    </aside>
  </main>
  <script src="/docs/assets/chat_markdown.js"></script>
  <script>
    const CHECK_PREFIX = "single-page-site:" + {_script_json(site_id)} + ":";
    const CHAT = {_script_json(chat_config)};
    const chatPanel = document.getElementById("site-chat");
    const popoutButton = document.getElementById("chat-popout-button");
    const messages = document.getElementById("messages");
    const form = document.getElementById("chat-form");
    const input = document.getElementById("message-input");
    const button = document.getElementById("send-button");
    const mirrorToggle = document.getElementById("mirror-toggle");
    const statusEl = document.getElementById("chat-status");
    mirrorToggle.checked = Boolean(CHAT.mirrorToOrigin && CHAT.sessionKey);

    function restoreChecks() {{
      document.querySelectorAll("[data-check-key]").forEach((box) => {{
        const key = CHECK_PREFIX + box.dataset.checkKey;
        box.checked = localStorage.getItem(key) === "true";
        box.addEventListener("change", () => localStorage.setItem(key, String(box.checked)));
      }});
    }}

    function restoreProgressTrackers() {{
      document.querySelectorAll("[data-progress-key]").forEach((tracker) => {{
        const key = CHECK_PREFIX + "progress:" + tracker.dataset.progressKey;
        const input = tracker.querySelector("[data-progress-input]");
        const progress = tracker.querySelector("[data-progress-bar]");
        const output = tracker.querySelector("[data-progress-output]");
        const initial = Number(tracker.dataset.progressInitial || "0");
        const target = Math.max(Number(tracker.dataset.progressTarget || "1"), 0.000001);
        const unit = tracker.dataset.progressUnit || "";
        if (!input || !progress || !output) return;
        progress.max = target;

        function update(rawValue, persist) {{
          const parsed = Number(rawValue);
          const value = Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
          input.value = String(value);
          progress.value = Math.min(value, target);
          output.textContent = value + " / " + target + (unit ? " " + unit : "");
          if (persist) localStorage.setItem(key, String(value));
        }}

        const stored = localStorage.getItem(key);
        update(stored === null ? initial : stored, false);
        input.addEventListener("change", () => update(input.value, true));
        tracker.querySelectorAll("[data-progress-delta]").forEach((control) => {{
          control.addEventListener("click", () => {{
            update(Number(input.value || "0") + Number(control.dataset.progressDelta || "0"), true);
          }});
        }});
      }});
    }}

    function clientId() {{
      const key = CHECK_PREFIX + "client-id";
      let value = localStorage.getItem(key);
      if (!value) {{
        value = window.UAOperationalIds.newId("browser");
        localStorage.setItem(key, value);
      }}
      return value;
    }}

    // The chat works only in the signed-in Web Chat session of the person the
    // page was made for; anyone else gets this sign-in link instead.
    function showSignIn(message) {{
      statusEl.textContent = (message || "Sign in to Web Chat to use this chat.") + " ";
      const link = document.createElement("a");
      link.href = CHAT.signInUrl;
      link.textContent = "Sign in to Web Chat";
      statusEl.appendChild(link);
    }}

    function appendMessage(kind, text) {{
      const node = document.createElement("div");
      node.className = "message " + kind;
      renderMarkdownInto(node, text);
      messages.appendChild(node);
      messages.scrollTop = messages.scrollHeight;
    }}

    function markdownRenderer() {{
      return window.UAWebChatMarkdown && typeof window.UAWebChatMarkdown.render === "function"
        ? window.UAWebChatMarkdown
        : null;
    }}

    function renderMarkdownInto(node, text) {{
      const renderer = markdownRenderer();
      if (renderer) {{
        node.innerHTML = renderer.render(text || "");
      }} else {{
        node.textContent = text || "";
      }}
    }}

    function renderStaticMarkdown() {{
      document.querySelectorAll("[data-markdown]").forEach((node) => {{
        renderMarkdownInto(node, node.getAttribute("data-markdown") || "");
      }});
    }}

    function setChatPopped(popped) {{
      if (!chatPanel || !popoutButton) return;
      chatPanel.classList.toggle("is-popped", popped);
      document.body.classList.toggle("chat-popped", popped);
      popoutButton.setAttribute("aria-pressed", String(popped));
      popoutButton.setAttribute("aria-label", popped ? "Dock chat" : "Pop out chat");
      messages.scrollTop = messages.scrollHeight;
    }}

    if (popoutButton) {{
      popoutButton.addEventListener("click", () => {{
        setChatPopped(!chatPanel.classList.contains("is-popped"));
      }});
    }}

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      appendMessage("user", text);
      input.value = "";
      button.disabled = true;
      statusEl.textContent = "Waiting for the agent...";
      try {{
        const response = await fetch(CHAT.apiPath, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            message: text,
            client_id: clientId(),
            session_key: CHAT.sessionKey,
            conversation_id: CHAT.conversationId,
            mirror_to_origin: Boolean(mirrorToggle && mirrorToggle.checked)
          }})
        }});
        const payload = await response.json().catch(() => ({{ reply: "The website host returned a non-JSON response." }}));
        if (response.status === 401 || response.status === 403) {{
          showSignIn(payload.reply);
          return;
        }}
        if (!response.ok) {{
          throw new Error(payload.reply || payload.message || "Chat request failed.");
        }}
        CHAT.sessionKey = payload.session_key || CHAT.sessionKey;
        CHAT.conversationId = payload.conversation_id || CHAT.conversationId;
        appendMessage("assistant", payload.reply || "");
        statusEl.textContent = "";
      }} catch (error) {{
        appendMessage("assistant", "Chat failed: " + error.message);
        statusEl.textContent = "Check that the gateway and website host are running.";
      }} finally {{
        button.disabled = false;
        input.focus();
      }}
    }});

    restoreChecks();
    restoreProgressTrackers();
    renderStaticMarkdown();
  </script>
</body>
</html>
"""


@tool
def publish_single_page_site(
    site_spec_json: str,
    site_slug: str = "",
    site_kind: SiteKind = "article",
) -> dict[str, Any]:
    """
    Publish a structured single-page website with embedded chat.

    Choose site_kind from article, comparison, how_to, or app before composing
    the JSON spec. article is the default for explanations, reports, reference
    pages, collections, and other content whose sections can be read
    independently. comparison is for evaluating alternatives. how_to is only
    for explicitly requested instructions/checklists or genuinely ordered user
    actions where sequence affects success. app is for interactive pages whose
    browser-local state, progress, counters, tasks, or habits are part of the
    experience.

    The site_spec_json argument must be a JSON object. All kinds may use title,
    summary, sections, and resources. Optional useful elements include:
    article: highlights, key_points, and callouts; comparison: options with
    pros/cons/best_for, comparison_rows with criterion and values, and a
    recommendation; how_to: safety_notes, materials or supplies, tools, plans,
    steps with warnings/verification, and assumptions; app: stats,
    progress_trackers with current/target/unit/step, checklist_items or
    tasks/habits, and action links. These are choices, not requirements: include
    only elements supported by the content and omit filler. Empty elements are
    not rendered.

    Optional section headings can be passed with section_headings or top-level
    fields such as materials_heading. To regenerate an existing page in place,
    pass update_site_id, existing_site_id, regenerate_site_id, or
    update_existing=true with site_id or site_url set to the prior returned
    page. update_site_url, existing_site_url, and regenerate_site_url are also
    accepted; only pages made for the same person can be updated. The tool
    writes index.html into sites/<site_id>/ of the person's folder (agents/
    for work for no person) and returns the browser URL. Editing that file
    afterwards does not change the page; it stops the page from being served,
    so publish again to change it. Anyone with the link can open the page, and
    only the person it was made for can use its chat. Return the exact
    returned site_url to the user; do not invent or shorten generated-site
    URLs, and do not wrap bare URLs in Markdown emphasis.
    """
    try:
        spec = json.loads(site_spec_json)
    except json.JSONDecodeError as exc:
        return _json_result(
            "error",
            {
                "status": "failed",
                "failure_stage": "input_validation",
                "failure_reason": "invalid_json",
                "message": str(exc),
                "retryable": False,
            },
        )
    if not isinstance(spec, dict):
        return _json_result(
            "error",
            {
                "status": "failed",
                "failure_stage": "input_validation",
                "failure_reason": "json_not_object",
                "message": "site_spec_json must decode to a JSON object.",
                "retryable": False,
            },
        )
    try:
        resolved_site_kind = _normalize_site_kind(spec.get("site_kind") or site_kind)
    except ValueError as exc:
        return _json_result(
            "error",
            {
                "status": "failed",
                "failure_stage": "input_validation",
                "failure_reason": "invalid_site_kind",
                "message": str(exc),
                "retryable": False,
            },
        )

    return _publish(spec, site_slug=site_slug, site_kind=resolved_site_kind)


def _optional_text(value: Any) -> str | None:
    return _text(value) or None


def _publish(spec: dict[str, Any], *, site_slug: str, site_kind: str) -> dict[str, Any]:
    """Render the page, write it into the person's folder, then record its digests.

    The files are written first and the record last, so the host never
    serves a record whose files are not written yet (it would refuse them).
    """

    agent = get_agent_ref()
    context = _current_task_context()
    store = _site_store(agent)
    person_id = current_workspace_scope(agent).person_id
    title = _text(spec.get("title")) or _SITE_KIND_PRESENTATION[site_kind]["default_title"]
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    previous: PublishedSiteRecordV1 | None = None
    update_site_id = _requested_update_site_id(spec)
    if update_site_id:
        found = _record_to_update(store, update_site_id, person_id)
        if not isinstance(found, PublishedSiteRecordV1):
            return found
        previous = found
        site_id = found.site_id
    else:
        site_id = _new_site_id(
            store,
            requested_slug=_text(site_slug) or _text(spec.get("site_slug")) or title,
            tracking_slug=_tracking_slug(spec, context),
            now=now,
        )
    site_url = f"{website_base_url()}/docs/sites/{site_id}/"
    html_text = _render_html(spec, site_kind=site_kind, site_id=site_id, site_url=site_url, context=context)
    folder_parts = site_folder_parts(person_id, site_id)
    try:
        digests = store.write_files(folder_parts, {SITE_INDEX_FILENAME: html_text.encode("utf-8")})
    except UnsafePath as exc:
        LOGGER.warning(
            "A page could not be written into the person's folder. event=generated_site_write_refused "
            "site_id=%s person_id=%s reason=%s",
            site_id,
            person_id or "-",
            exc.reason,
        )
        return _failure(
            "write",
            "site_folder_unsafe",
            f"The page folder cannot be written safely ({exc}). Remove links or extra hard links from it, or publish a new page.",
        )
    store.save(
        PublishedSiteRecordV1(
            site_id=site_id,
            folder="/".join(folder_parts),
            owner_person_id=person_id,
            title=title,
            site_kind=site_kind,
            created_at=previous.created_at if previous else now_iso,
            updated_at=now_iso,
            update_count=previous.update_count + 1 if previous else 0,
            files=digests,
            conversation_id=_optional_text(spec.get("conversation_id") or context.get("conversation_id")),
            session_key=_optional_text(spec.get("session_key") or context.get("session_key")),
            channel_name=_optional_text(context.get("channel_name")),
            chat_id=_optional_text(context.get("chat_id")),
            thread_id=_optional_text(spec.get("thread_id") or context.get("thread_id")),
            task_id=_optional_text(context.get("task_id")),
            team_id=_optional_text(context.get("team_id")),
            agent_id=_optional_text(context.get("agent_id")),
            named_agent_type=_optional_text(context.get("named_agent_type")),
        )
    )
    operation = "updated" if previous else "created"
    site_dir = store.workspaces_dir.joinpath(*folder_parts)
    return _json_result(
        "success",
        {
            "status": "success",
            "operation": operation,
            "updated_existing": previous is not None,
            "site_kind": site_kind,
            "site_id": site_id,
            "site_url": site_url,
            "site_dir": str(site_dir),
            "index_path": str(site_dir / SITE_INDEX_FILENAME),
            "chat_enabled": True,
            "next_step_hint": (
                "Return the exact site_url to the user. "
                + (
                    "Mention that interactive state is saved in the browser. "
                    if _site_has_browser_state(spec, site_kind)
                    else ""
                )
                + "Do not invent or shorten generated-site URLs, and do not wrap a bare URL in Markdown emphasis. "
                f"For future changes to this same page, pass update_site_id={site_id!r}."
            ),
        },
    )
