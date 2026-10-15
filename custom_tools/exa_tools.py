"""Exa web search tools that send their requests through the secure tool broker.

These replace the Strands community ``exa_search`` and ``exa_get_contents``
tools, which read ``EXA_API_KEY`` from the agent's environment and called Exa
directly. The names and useful parameters stay the same. Each call goes to the
broker's ``secure_requests`` action for the web API tool ``http:exa``; the
broker adds the Exa key of the linked account (the person's own, else the
team's), so no key reaches the agent process. See
``custom_tools.broker_web_requests``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional, Union

from strands import tool

from custom_tools.broker_web_requests import BrokerWebRequestError, send_web_request

logger = logging.getLogger(__name__)

EXA_API_BASE_URL = "https://api.exa.ai"
EXA_TIMEOUT_SECONDS = 60
# Exa may return full page text for many results; allow larger answers than
# the broker's 1 MB default.
EXA_MAX_RESPONSE_BYTES = 8_000_000

SearchType = Literal["auto", "instant", "fast", "deep"]
Category = Literal[
    "company",
    "research paper",
    "news",
    "pdf",
    "github",
    "personal site",
    "linkedin profile",
    "people",
    "financial report",
]
Livecrawl = Literal["never", "fallback", "always", "preferred"]
ContentOption = Union[bool, Dict[str, Any]]


def _result(status: str, text: str) -> Dict[str, Any]:
    return {"status": status, "content": [{"text": text}]}


def _without_empty(values: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _call_exa(endpoint: str, payload: Dict[str, Any], *, reason: str) -> Dict[str, Any]:
    """POST one Exa request through the broker and return the tool result."""

    try:
        response = send_web_request(
            "POST",
            f"{EXA_API_BASE_URL}{endpoint}",
            json_body=payload,
            reason=reason,
            timeout_seconds=EXA_TIMEOUT_SECONDS,
            max_response_bytes=EXA_MAX_RESPONSE_BYTES,
        )
    except BrokerWebRequestError as exc:
        return _result("error", f"Exa request not sent: {exc.message}")
    if not response.ok:
        detail = json.dumps(response.json)[:1000] if response.json is not None else response.text[:1000]
        logger.warning(
            "Exa answered with an error. event=exa_request_failed endpoint=%s http_status=%s",
            endpoint,
            response.http_status,
        )
        return _result("error", f"Exa answered HTTP {response.http_status}: {detail}")
    if response.json is None:
        return _result("error", f"Exa did not answer with JSON: {response.text[:1000]}")
    text = json.dumps(response.json, ensure_ascii=False)
    if response.truncated:
        text += "\n(The answer was cut off at the size limit; ask for fewer results or shorter text.)"
    return _result("success", text)


def _contents_options(**options: Any) -> Dict[str, Any]:
    """Exa's content options under their API names, leaving out the ones not given."""

    names = {
        "text": "text",
        "highlights": "highlights",
        "summary": "summary",
        "livecrawl": "livecrawl",
        "livecrawl_timeout": "livecrawlTimeout",
        "max_age_hours": "maxAgeHours",
        "subpages": "subpages",
        "subpage_target": "subpageTarget",
        "extras": "extras",
    }
    return _without_empty({names[key]: value for key, value in options.items()})


@tool
def exa_search(
    query: str,
    type: Optional[SearchType] = "auto",
    category: Optional[Category] = None,
    user_location: Optional[str] = None,
    num_results: Optional[int] = None,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    start_crawl_date: Optional[str] = None,
    end_crawl_date: Optional[str] = None,
    start_published_date: Optional[str] = None,
    end_published_date: Optional[str] = None,
    include_text: Optional[List[str]] = None,
    exclude_text: Optional[List[str]] = None,
    context: Optional[ContentOption] = None,
    moderation: Optional[bool] = None,
    text: Optional[ContentOption] = None,
    highlights: Optional[ContentOption] = None,
    summary: Optional[Dict[str, Any]] = None,
    livecrawl: Optional[Livecrawl] = None,
    livecrawl_timeout: Optional[int] = None,
    max_age_hours: Optional[int] = None,
    subpages: Optional[int] = None,
    subpage_target: Optional[Union[str, List[str]]] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Search the web with Exa, a search engine made for AI agents.

    "auto" (the default) picks the best search approach; "deep" is thorough,
    "fast" and "instant" are quicker. Use a category only when it clearly
    fits; general search usually works best. Ask for page content with
    ``text`` (``{"maxCharacters": 2000}`` keeps it short) or ``highlights``.

    Args:
        query: What to search for, for example "Best project management tools".
        type: "auto" (recommended), "instant", "fast" or "deep".
        category: Optional kind of source, for example "news", "research paper", "company" or "github".
        user_location: Two-letter country code for local results, for example "US".
        num_results: Number of results, 1 to 100 (default 10).
        include_domains: Only results from these domains.
        exclude_domains: No results from these domains.
        start_crawl_date: Only links crawled after this date (ISO 8601).
        end_crawl_date: Only links crawled before this date (ISO 8601).
        start_published_date: Only links published after this date (ISO 8601).
        end_published_date: Only links published before this date (ISO 8601).
        include_text: One phrase (up to 5 words) the page text must contain.
        exclude_text: One phrase (up to 5 words) the page text must not contain.
        context: True, or {"maxCharacters": n}, to get results formatted as one text for the model.
        moderation: Filter unsafe content.
        text: True, or {"maxCharacters": n, "includeHtmlTags": bool}, to include page text.
        highlights: True, or {"maxCharacters": n, "query": "..."}, for short relevant excerpts.
        summary: {"query": "...", "schema": {...}} for a summary of each page.
        livecrawl: "never", "fallback", "always" or "preferred".
        livecrawl_timeout: Live crawl timeout in milliseconds (default 10000).
        max_age_hours: Oldest cached copy to accept; 0 always crawls, -1 never does.
        subpages: Number of subpages to read from each result.
        subpage_target: Words that pick which subpages to read.
        extras: {"links": n, "imageLinks": n} for links and image links on each page.

    Returns:
        Exa's JSON answer: each result's title, URL, date and the content asked for.
    """

    if not str(query or "").strip():
        return _result("error", "query must not be empty.")
    if num_results is not None and not 1 <= num_results <= 100:
        return _result("error", "num_results must be between 1 and 100.")
    payload = _without_empty(
        {
            "query": query,
            "type": type or "auto",
            "category": category,
            "userLocation": user_location,
            "numResults": num_results,
            "includeDomains": include_domains,
            "excludeDomains": exclude_domains,
            "startCrawlDate": start_crawl_date,
            "endCrawlDate": end_crawl_date,
            "startPublishedDate": start_published_date,
            "endPublishedDate": end_published_date,
            "includeText": include_text,
            "excludeText": exclude_text,
            "context": context,
            "moderation": moderation,
        }
    )
    contents = _contents_options(
        text=text,
        highlights=highlights,
        summary=summary,
        livecrawl=livecrawl,
        livecrawl_timeout=livecrawl_timeout,
        max_age_hours=max_age_hours,
        subpages=subpages,
        subpage_target=subpage_target,
        extras=extras,
    )
    if contents:
        payload["contents"] = contents
    return _call_exa("/search", payload, reason="Exa web search")


@tool
def exa_get_contents(
    urls: List[str],
    text: Optional[ContentOption] = None,
    highlights: Optional[ContentOption] = None,
    summary: Optional[Dict[str, Any]] = None,
    livecrawl: Optional[Livecrawl] = None,
    livecrawl_timeout: Optional[int] = None,
    max_age_hours: Optional[int] = None,
    subpages: Optional[int] = None,
    subpage_target: Optional[Union[str, List[str]]] = None,
    extras: Optional[Dict[str, Any]] = None,
    context: Optional[ContentOption] = None,
) -> Dict[str, Any]:
    """Get the text, summaries and details of known web pages through Exa.

    Exa answers from its cache and crawls pages it has not seen. Use this for
    URLs you already have; use ``exa_search`` to find pages.

    Args:
        urls: The page URLs to read.
        text: True, or {"maxCharacters": n, "includeHtmlTags": bool}, for the page text.
        highlights: True, or {"maxCharacters": n, "query": "..."}, for short relevant excerpts.
        summary: {"query": "...", "schema": {...}} for a summary of each page.
        livecrawl: "never", "fallback" (default), "always" or "preferred".
        livecrawl_timeout: Live crawl timeout in milliseconds (default 10000).
        max_age_hours: Oldest cached copy to accept; 0 always crawls, -1 never does.
        subpages: Number of subpages to read from each URL.
        subpage_target: Words that pick which subpages to read.
        extras: {"links": n, "imageLinks": n} for links and image links on each page.
        context: True, or {"maxCharacters": n}, to get the pages formatted as one text for the model.

    Returns:
        Exa's JSON answer with each page's content and a status per URL.
    """

    cleaned = [str(url).strip() for url in urls or [] if str(url).strip()]
    if not cleaned:
        return _result("error", "Give at least one URL.")
    payload = {
        "urls": cleaned,
        **_contents_options(
            text=text,
            highlights=highlights,
            summary=summary,
            livecrawl=livecrawl,
            livecrawl_timeout=livecrawl_timeout,
            max_age_hours=max_age_hours,
            subpages=subpages,
            subpage_target=subpage_target,
            extras=extras,
        ),
    }
    if context is not None:
        payload["context"] = context
    return _call_exa("/contents", payload, reason="Exa page contents")


__all__ = ["exa_get_contents", "exa_search"]
