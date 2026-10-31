"""Artifact-backed web research tools for Strands agents.

The tools in this module keep raw web-search and retrieval payloads out of the
active LLM tool transcript. Full service responses are written in the
task's research folder, while tool returns contain compact manifests, bounded
excerpts, and actionable failure details. Every artifact is written and read
through the agent file library (``custom_tools.file_access``), which never
follows a link.

Result folders are deleted after the team's retention period
(``tool_policy.output_retention_days``, ``custom_agents.output_retention``).
Their names start with the UTC time they were written, so inspecting a file of
a result whose folder is gone and was written before the period answers that
it was deleted under the retention policy, with ``retryable: false``.
"""

import json
import os
import re
import stat as stat_module
from html import unescape
from html.parser import HTMLParser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from strands import tool

from custom_agents.output_retention import (
    DELETED_OUTPUT_REASON,
    deleted_by_retention,
    deleted_output_note,
    retention_days_of,
    written_at_from_name,
)
from custom_tools import file_access
from custom_tools.broker_web_requests import (
    REASON_ACCESS_DENIED,
    BrokerWebRequestError,
    is_missing_key,
    send_web_request,
)
from custom_tools.path_policy import PathPolicyError
from custom_tools.research_identity import ResearchIdentity, research_identity, result_folder_name
from custom_tools.runtime_context import get_agent_ref

# Requests go through the secure tool broker, which adds the Brave key of the
# account linked to http:brave-search (custom_tools.broker_web_requests).
BRAVE_SEARCH_BASE_URL = "https://api.search.brave.com/res/v1"
BRAVE_TOOL_ID = "http:brave-search"

SEARCH_DEFAULT_MAX_RESULTS = 8
SEARCH_HARD_MAX_RESULTS = 20
BRAVE_LLM_CONTEXT_DEFAULT_COUNT = 20
BRAVE_LLM_CONTEXT_HARD_COUNT = 50
BRAVE_LLM_CONTEXT_DEFAULT_MAX_URLS = 8
BRAVE_LLM_CONTEXT_HARD_MAX_URLS = 50
BRAVE_LLM_CONTEXT_DEFAULT_MAX_TOKENS = 8192
BRAVE_LLM_CONTEXT_HARD_MAX_TOKENS = 32768
INSPECT_DEFAULT_MAX_CHARS = 6000
INSPECT_HARD_MAX_CHARS = 12000
INSPECT_DEFAULT_MAX_LINES = 80
INSPECT_HARD_MAX_LINES = 200
INSPECT_DEFAULT_MAX_ITEMS = 40
INSPECT_HARD_MAX_ITEMS = 200
# Inspection reads a saved artifact once, whole; larger files are refused.
INSPECT_MAX_FILE_BYTES = 32 * 1024 * 1024


def _json_result(status: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": status,
        "content": [{"text": json.dumps(payload, indent=2, sort_keys=True)}],
    }


def _safe_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _create_artifact_dir(kind: str, label: str) -> tuple[Path, str, ResearchIdentity]:
    """A new folder for one call's files, named by ``result_folder_name`` so no other call shares it."""

    identity = research_identity()
    artifact_id = result_folder_name(_now_stamp(), kind, label)
    artifact_dir = file_access.make_directories(identity.task_folder / artifact_id).path
    return artifact_dir, artifact_id, identity


def _write_json(path: Path, payload: Any) -> int:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    return _write_text(path, text + "\n")


def _write_text(path: Path, text: str) -> int:
    data = text.encode("utf-8", errors="replace")
    file_access.write_bytes(path, data)
    return len(data)


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + f"\n...<truncated {len(text) - max_chars} chars>", True


def _excerpt_for(text: str, query: str, max_chars: int) -> tuple[str, bool]:
    if not query:
        return _truncate_text(text, max_chars)
    terms = [term.lower() for term in re.findall(r"[a-zA-Z0-9]{3,}", query)]
    lower = text.lower()
    best_index = -1
    for term in terms:
        index = lower.find(term)
        if index != -1 and (best_index == -1 or index < best_index):
            best_index = index
    if best_index == -1:
        return _truncate_text(text, max_chars)
    start = max(0, best_index - max_chars // 3)
    end = min(len(text), start + max_chars)
    excerpt = text[start:end]
    if start > 0:
        excerpt = "...<preceding text omitted>\n" + excerpt
    if end < len(text):
        excerpt += f"\n...<truncated {len(text) - end} trailing chars>"
    return excerpt, start > 0 or end < len(text)


def _failure_payload(
    *,
    artifact_id: str,
    artifact_dir: Path,
    failure_stage: str,
    failure_reason: str,
    message: str,
    retryable: bool,
    attempted: Dict[str, Any],
    mitigation_hints: List[str],
) -> Dict[str, Any]:
    return {
        "status": "failed",
        "artifact_id": artifact_id,
        "artifact_dir": str(artifact_dir),
        "failure_stage": failure_stage,
        "failure_reason": failure_reason,
        "message": message,
        "retryable": retryable,
        "attempted": attempted,
        "mitigation_hints": mitigation_hints,
    }


def _base_manifest(
    *,
    artifact_id: str,
    identity: ResearchIdentity,
    service: str,
    request: Dict[str, Any],
    budgets: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **identity.manifest_fields(),
        "service": service,
        "request": request,
        "budgets": budgets,
        "files": [],
        "summary": {},
        "failure": None,
    }


def _record_manifest(path: Path, manifest: Dict[str, Any]) -> None:
    _write_json(path / "manifest.json", manifest)


def _brave_failure(
    *,
    artifact_id: str,
    artifact_dir: Path,
    failure_stage: str,
    failure_reason: str,
    message: str,
    retryable: bool,
    attempted: Dict[str, Any],
    mitigation_hints: List[str],
    manifest: Dict[str, Any],
) -> Dict[str, Any]:
    failure = _failure_payload(
        artifact_id=artifact_id,
        artifact_dir=artifact_dir,
        failure_stage=failure_stage,
        failure_reason=failure_reason,
        message=message,
        retryable=retryable,
        attempted=attempted,
        mitigation_hints=mitigation_hints,
    )
    manifest["failure"] = failure
    _record_manifest(artifact_dir, manifest)
    return _json_result("error", failure)


# No key here: the broker adds X-Subscription-Token from the linked account.
# No Accept-Encoding either: the broker does not unpack compressed answers.
_BRAVE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "team-nats-agents-brave-search-artifact/1.0",
}
_MISSING_KEY_HINTS = [
    f"a team administrator links the team's Brave key with /link {BRAVE_TOOL_ID} team",
    f"the person links their own Brave key with /link {BRAVE_TOOL_ID}",
    "use exa_search or searxng until a key is linked",
]


def _broker_failure_fields(exc: BrokerWebRequestError, *, stage: str) -> Dict[str, Any]:
    """Failure fields for a request the broker refused or could not send."""

    if is_missing_key(exc):
        hints = _MISSING_KEY_HINTS
    elif exc.reason_code == REASON_ACCESS_DENIED:
        hints = [f"ask the person, or an administrator, for access to {BRAVE_TOOL_ID}", "use searxng instead"]
    else:
        hints = ["retry shortly", "try a different search provider"]
    failure_stage = "authentication" if is_missing_key(exc) or exc.reason_code == REASON_ACCESS_DENIED else stage
    return {
        "failure_stage": failure_stage,
        "failure_reason": exc.reason_code,
        "message": exc.message,
        "retryable": exc.retryable,
        "mitigation_hints": hints,
    }


def _http_failure_reason(status: int) -> str:
    return "http_429_rate_limited" if status == 429 else f"http_{status}"


def _brave_fetch(
    path: str,
    params: Dict[str, Any],
    *,
    timeout: int,
    stage: str,
    service: str,
    hints: List[str],
    failure: Any,
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """GET one Brave endpoint through the broker: (JSON answer, None) or (None, failure result).

    ``failure`` is ``_brave_failure`` with the artifact, manifest and attempted
    request already bound, taking the remaining failure fields.
    """

    try:
        response = send_web_request(
            "GET", f"{BRAVE_SEARCH_BASE_URL}{path}", params=params, headers=_BRAVE_HEADERS,
            reason=f"Brave {service.replace('_', ' ')}", timeout_seconds=timeout,
        )
    except BrokerWebRequestError as exc:
        return None, failure(**_broker_failure_fields(exc, stage=stage))
    if not response.ok:
        preview = response.text[:500] or str(response.json)[:500]
        return None, failure(
            failure_stage=stage,
            failure_reason=_http_failure_reason(response.http_status),
            message=f"Brave answered HTTP {response.http_status}: {preview}",
            retryable=response.http_status == 429 or response.http_status >= 500,
            mitigation_hints=hints,
        )
    if not isinstance(response.json, dict):
        return None, failure(
            failure_stage="service_response",
            failure_reason="invalid_json_response",
            message=f"Brave did not return JSON: {response.text[:500]}",
            retryable=True,
            mitigation_hints=["retry the same query later"],
        )
    return response.json, None


def _normalize_brave_web_results(data: Dict[str, Any], max_results: int) -> List[Dict[str, Any]]:
    raw_results = ((data.get("web") or {}).get("results") or []) if isinstance(data, dict) else []
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(list(raw_results)[:max_results], start=1):
        if not isinstance(item, dict):
            continue
        snippet, snippet_truncated = _truncate_text(str(item.get("description") or ""), 500)
        extra_snippets = []
        for extra in list(item.get("extra_snippets") or [])[:5]:
            extra_text, extra_truncated = _truncate_text(str(extra), 350)
            extra_snippets.append({"text": extra_text, "truncated": extra_truncated})
        profile = item.get("profile") if isinstance(item.get("profile"), dict) else {}
        meta_url = item.get("meta_url") if isinstance(item.get("meta_url"), dict) else {}
        normalized.append(
            {
                "rank": index,
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": snippet,
                "snippet_truncated": snippet_truncated,
                "age": item.get("age"),
                "page_age": item.get("page_age"),
                "language": item.get("language"),
                "source": profile.get("name") or meta_url.get("hostname") or "",
                "family_friendly": (data.get("web") or {}).get("family_friendly"),
                "extra_snippets": extra_snippets,
            }
        )
    return normalized


def _brave_search_markdown(query: str, results: List[Dict[str, Any]]) -> str:
    lines = ["# Brave Web Search Results", "", f"Query: {query}", ""]
    for item in results:
        lines.extend(
            [
                f"## {item['rank']}. {item.get('title') or '(untitled)'}",
                "",
                f"- URL: {item.get('url')}",
                f"- Source: {item.get('source') or 'unknown'}",
                f"- Age: {item.get('age') or item.get('page_age') or 'unknown'}",
                "",
                item.get("snippet") or "",
                "",
            ]
        )
        for extra in item.get("extra_snippets") or []:
            lines.extend(["Additional snippet:", extra.get("text") or "", ""])
    return "\n".join(lines)


def _collect_context_strings(value: Any, output: List[str], *, max_items: int, max_chars_each: int) -> None:
    if len(output) >= max_items:
        return
    if isinstance(value, dict):
        for key in ("text", "snippet", "content", "markdown", "summary", "title"):
            if key in value:
                _collect_context_strings(value[key], output, max_items=max_items, max_chars_each=max_chars_each)
                if len(output) >= max_items:
                    return
        for nested in value.values():
            _collect_context_strings(nested, output, max_items=max_items, max_chars_each=max_chars_each)
            if len(output) >= max_items:
                return
    elif isinstance(value, list):
        for item in value:
            _collect_context_strings(item, output, max_items=max_items, max_chars_each=max_chars_each)
            if len(output) >= max_items:
                return
    elif isinstance(value, str):
        text = value.strip()
        if len(text) >= 40 and text not in output:
            clipped, _ = _truncate_text(text, max_chars_each)
            output.append(clipped)


def _normalize_brave_sources(data: Dict[str, Any], *, max_sources: int) -> List[Dict[str, Any]]:
    raw_sources = data.get("sources") if isinstance(data, dict) else {}
    if not isinstance(raw_sources, dict):
        return []
    normalized: List[Dict[str, Any]] = []
    for index, (url, source) in enumerate(list(raw_sources.items())[:max_sources], start=1):
        if isinstance(source, dict):
            normalized.append(
                {
                    "rank": index,
                    "url": url,
                    "title": source.get("title") or source.get("name") or source.get("site_name") or "",
                    "site_name": source.get("site_name") or source.get("hostname") or "",
                    "source_type": source.get("type") or "",
                }
            )
        else:
            normalized.append({"rank": index, "url": url, "title": "", "site_name": "", "source_type": ""})
    return normalized


def _brave_context_markdown(query: str, sources: List[Dict[str, Any]], snippets: List[str]) -> str:
    lines = ["# Brave LLM Context", "", f"Query: {query}", "", "## Sources", ""]
    for source in sources:
        lines.append(f"- {source.get('title') or source.get('url')} ({source.get('url')})")
    lines.extend(["", "## Context Preview", ""])
    for index, snippet in enumerate(snippets, start=1):
        lines.extend([f"### Snippet {index}", "", snippet, ""])
    return "\n".join(lines)


@tool
def brave_web_search_artifact(
    query: str,
    country: str = "US",
    search_lang: str = "en",
    ui_lang: str = "en-US",
    count: int = SEARCH_DEFAULT_MAX_RESULTS,
    offset: int = 0,
    safesearch: str = "moderate",
    freshness: str = "",
    result_filter: str = "web",
    extra_snippets: bool = False,
    spellcheck: bool = True,
    text_decorations: bool = False,
    goggles: str = "",
    include_raw_response: bool = True,
) -> Dict[str, Any]:
    """
    Search Brave's independent web index and write the full response to disk.

    Use this for broad or current web search, especially when the user asks to
    prefer Brave. Returns compact result metadata and artifact paths; fetch or
    inspect selected pages separately when more source detail is needed.
    """
    query = str(query or "").strip()
    count = _safe_int(count, default=SEARCH_DEFAULT_MAX_RESULTS, minimum=1, maximum=SEARCH_HARD_MAX_RESULTS)
    offset = _safe_int(offset, default=0, minimum=0, maximum=9)
    artifact_dir, artifact_id, identity = _create_artifact_dir("brave-web-search", query or "empty-query")
    request_payload = {
        "query": query,
        "country": country,
        "search_lang": search_lang,
        "ui_lang": ui_lang,
        "count": count,
        "offset": offset,
        "safesearch": safesearch,
        "freshness": freshness,
        "result_filter": result_filter,
        "extra_snippets": extra_snippets,
        "spellcheck": spellcheck,
        "text_decorations": text_decorations,
        "goggles": "[provided]" if goggles else "",
    }
    manifest = _base_manifest(
        artifact_id=artifact_id,
        identity=identity,
        service="brave_web_search",
        request=request_payload,
        budgets={"count": count, "hard_max_results": SEARCH_HARD_MAX_RESULTS, "offset": offset},
    )
    if not query:
        return _brave_failure(
            artifact_id=artifact_id,
            artifact_dir=artifact_dir,
            failure_stage="input_validation",
            failure_reason="empty_query",
            message="query must not be empty.",
            retryable=False,
            attempted={"service": "brave_web_search", **request_payload},
            mitigation_hints=["provide a non-empty search query"],
            manifest=manifest,
        )
    if safesearch not in {"off", "moderate", "strict"}:
        return _brave_failure(
            artifact_id=artifact_id,
            artifact_dir=artifact_dir,
            failure_stage="input_validation",
            failure_reason="invalid_safesearch",
            message="safesearch must be one of off, moderate, or strict.",
            retryable=False,
            attempted={"service": "brave_web_search", **request_payload},
            mitigation_hints=["retry with safesearch='moderate'"],
            manifest=manifest,
        )
    params: Dict[str, Any] = {
        "q": query,
        "country": country,
        "search_lang": search_lang,
        "ui_lang": ui_lang,
        "count": count,
        "offset": offset,
        "safesearch": safesearch,
        "spellcheck": spellcheck,
        "text_decorations": text_decorations,
    }
    if freshness:
        params["freshness"] = freshness
    if result_filter:
        params["result_filter"] = result_filter
    if extra_snippets:
        params["extra_snippets"] = True
    if goggles:
        params["goggles"] = goggles

    data, failed = _brave_fetch(
        "/web/search",
        params,
        timeout=30,
        stage="search_request",
        service="brave_web_search",
        hints=[
            "retry with count=5",
            "remove freshness or custom goggles",
            "try brave_llm_context_artifact if the agent needs extracted page context",
            "try a different search provider if Brave is rate-limited",
        ],
        failure=lambda **fields: _brave_failure(
            artifact_id=artifact_id,
            artifact_dir=artifact_dir,
            attempted={"service": "brave_web_search", **request_payload},
            manifest=manifest,
            **fields,
        ),
    )
    if data is None:
        return failed or {}

    raw_path = artifact_dir / "brave_web_search.raw.json"
    if include_raw_response:
        raw_bytes = _write_json(raw_path, data)
        manifest["files"].append({"path": str(raw_path), "role": "raw_json", "bytes": raw_bytes})

    normalized = _normalize_brave_web_results(data, count)
    normalized_path = artifact_dir / "brave_web_search.normalized.json"
    markdown_path = artifact_dir / "brave_web_search.md"
    normalized_bytes = _write_json(normalized_path, normalized)
    markdown_bytes = _write_text(markdown_path, _brave_search_markdown(query, normalized))
    manifest["files"].extend(
        [
            {"path": str(normalized_path), "role": "normalized_json", "bytes": normalized_bytes},
            {"path": str(markdown_path), "role": "markdown_summary", "bytes": markdown_bytes},
        ]
    )
    available_count = len(((data.get("web") or {}).get("results") or []))
    query_meta = data.get("query") if isinstance(data.get("query"), dict) else {}
    manifest["summary"] = {
        "result_count": len(normalized),
        "available_result_count": available_count,
        "more_results_available": query_meta.get("more_results_available"),
        "truncated_inline_return": available_count > len(normalized),
    }
    _record_manifest(artifact_dir, manifest)
    payload = {
        "status": "success",
        "artifact_id": artifact_id,
        "artifact_dir": str(artifact_dir),
        "query": query,
        "provider": "brave",
        "result_count": len(normalized),
        "available_result_count": available_count,
        "more_results_available": query_meta.get("more_results_available"),
        "truncated": available_count > len(normalized),
        "files": {
            "manifest": str(artifact_dir / "manifest.json"),
            "normalized_json": str(normalized_path),
            "raw_json": str(raw_path) if include_raw_response else None,
            "markdown": str(markdown_path),
        },
        "top_results": normalized,
        "next_step_hint": "Use the crawl4ai_md or crawl4ai_crawl MCP tool on selected URLs, or compare with another search provider when claims are important or contested.",
    }
    return _json_result("success", payload)


@tool
def brave_llm_context_artifact(
    query: str,
    country: str = "US",
    search_lang: str = "en",
    count: int = BRAVE_LLM_CONTEXT_DEFAULT_COUNT,
    maximum_number_of_urls: int = BRAVE_LLM_CONTEXT_DEFAULT_MAX_URLS,
    maximum_number_of_tokens: int = BRAVE_LLM_CONTEXT_DEFAULT_MAX_TOKENS,
    maximum_number_of_snippets: int = 50,
    context_threshold_mode: str = "balanced",
    freshness: str = "",
    goggles: str = "",
    enable_source_metadata: bool = True,
    include_raw_response: bool = True,
) -> Dict[str, Any]:
    """
    Retrieve Brave's pre-extracted LLM grounding context and save it as artifacts.

    Use this when the agent needs source-grounding content directly rather than
    only ranked links and snippets. Returns a bounded context preview, sources,
    and file paths to the full saved API response.
    """
    query = str(query or "").strip()
    count = _safe_int(count, default=BRAVE_LLM_CONTEXT_DEFAULT_COUNT, minimum=1, maximum=BRAVE_LLM_CONTEXT_HARD_COUNT)
    maximum_number_of_urls = _safe_int(
        maximum_number_of_urls,
        default=BRAVE_LLM_CONTEXT_DEFAULT_MAX_URLS,
        minimum=1,
        maximum=BRAVE_LLM_CONTEXT_HARD_MAX_URLS,
    )
    maximum_number_of_tokens = _safe_int(
        maximum_number_of_tokens,
        default=BRAVE_LLM_CONTEXT_DEFAULT_MAX_TOKENS,
        minimum=1024,
        maximum=BRAVE_LLM_CONTEXT_HARD_MAX_TOKENS,
    )
    maximum_number_of_snippets = _safe_int(maximum_number_of_snippets, default=50, minimum=1, maximum=256)
    artifact_dir, artifact_id, identity = _create_artifact_dir("brave-llm-context", query or "empty-query")
    request_payload = {
        "query": query,
        "country": country,
        "search_lang": search_lang,
        "count": count,
        "maximum_number_of_urls": maximum_number_of_urls,
        "maximum_number_of_tokens": maximum_number_of_tokens,
        "maximum_number_of_snippets": maximum_number_of_snippets,
        "context_threshold_mode": context_threshold_mode,
        "freshness": freshness,
        "goggles": "[provided]" if goggles else "",
        "enable_source_metadata": enable_source_metadata,
    }
    manifest = _base_manifest(
        artifact_id=artifact_id,
        identity=identity,
        service="brave_llm_context",
        request=request_payload,
        budgets={
            "count": count,
            "maximum_number_of_urls": maximum_number_of_urls,
            "maximum_number_of_tokens": maximum_number_of_tokens,
            "maximum_number_of_snippets": maximum_number_of_snippets,
        },
    )
    if not query:
        return _brave_failure(
            artifact_id=artifact_id,
            artifact_dir=artifact_dir,
            failure_stage="input_validation",
            failure_reason="empty_query",
            message="query must not be empty.",
            retryable=False,
            attempted={"service": "brave_llm_context", **request_payload},
            mitigation_hints=["provide a non-empty search query"],
            manifest=manifest,
        )
    if context_threshold_mode not in {"disabled", "strict", "balanced", "lenient"}:
        return _brave_failure(
            artifact_id=artifact_id,
            artifact_dir=artifact_dir,
            failure_stage="input_validation",
            failure_reason="invalid_context_threshold_mode",
            message="context_threshold_mode must be disabled, strict, balanced, or lenient.",
            retryable=False,
            attempted={"service": "brave_llm_context", **request_payload},
            mitigation_hints=["retry with context_threshold_mode='balanced'"],
            manifest=manifest,
        )
    params: Dict[str, Any] = {
        "q": query,
        "country": country,
        "search_lang": search_lang,
        "count": count,
        "maximum_number_of_urls": maximum_number_of_urls,
        "maximum_number_of_tokens": maximum_number_of_tokens,
        "maximum_number_of_snippets": maximum_number_of_snippets,
        "context_threshold_mode": context_threshold_mode,
        "enable_source_metadata": enable_source_metadata,
    }
    if freshness:
        params["freshness"] = freshness
    if goggles:
        params["goggles"] = goggles

    data, failed = _brave_fetch(
        "/llm/context",
        params,
        timeout=45,
        stage="context_request",
        service="brave_llm_context",
        hints=[
            "retry with count=5 and maximum_number_of_tokens=2048",
            "try context_threshold_mode='strict'",
            "remove freshness or custom goggles",
            "try brave_web_search_artifact or another search provider if Brave is rate-limited",
        ],
        failure=lambda **fields: _brave_failure(
            artifact_id=artifact_id,
            artifact_dir=artifact_dir,
            attempted={"service": "brave_llm_context", **request_payload},
            manifest=manifest,
            **fields,
        ),
    )
    if data is None:
        return failed or {}

    raw_path = artifact_dir / "brave_llm_context.raw.json"
    if include_raw_response:
        raw_bytes = _write_json(raw_path, data)
        manifest["files"].append({"path": str(raw_path), "role": "raw_json", "bytes": raw_bytes})

    sources = _normalize_brave_sources(data, max_sources=maximum_number_of_urls)
    context_snippets: List[str] = []
    _collect_context_strings(data.get("grounding"), context_snippets, max_items=10, max_chars_each=900)
    normalized = {
        "sources": sources,
        "source_count": len(data.get("sources") or {}) if isinstance(data.get("sources"), dict) else len(sources),
        "grounding_keys": sorted((data.get("grounding") or {}).keys()) if isinstance(data.get("grounding"), dict) else [],
        "context_preview": context_snippets,
    }
    normalized_path = artifact_dir / "brave_llm_context.normalized.json"
    markdown_path = artifact_dir / "brave_llm_context.md"
    normalized_bytes = _write_json(normalized_path, normalized)
    markdown_bytes = _write_text(markdown_path, _brave_context_markdown(query, sources, context_snippets))
    manifest["files"].extend(
        [
            {"path": str(normalized_path), "role": "normalized_json", "bytes": normalized_bytes},
            {"path": str(markdown_path), "role": "markdown_summary", "bytes": markdown_bytes},
        ]
    )
    manifest["summary"] = {
        "source_count": normalized["source_count"],
        "returned_source_count": len(sources),
        "context_preview_count": len(context_snippets),
        "grounding_keys": normalized["grounding_keys"],
    }
    _record_manifest(artifact_dir, manifest)
    payload = {
        "status": "success",
        "artifact_id": artifact_id,
        "artifact_dir": str(artifact_dir),
        "query": query,
        "provider": "brave",
        "source_count": normalized["source_count"],
        "returned_source_count": len(sources),
        "grounding_keys": normalized["grounding_keys"],
        "files": {
            "manifest": str(artifact_dir / "manifest.json"),
            "normalized_json": str(normalized_path),
            "raw_json": str(raw_path) if include_raw_response else None,
            "markdown": str(markdown_path),
        },
        "sources": sources,
        "context_preview": context_snippets,
        "next_step_hint": "Use the saved artifacts for audit, cite sources from the response, and compare with another provider when accuracy is important.",
    }
    return _json_result("success", payload)


def _resolve_artifact_path(path: str) -> tuple[Optional[file_access.FileLocation], Optional[str]]:
    """Check a path for inspection: inside this task's research folder, opened without links.

    The research folder is compared by name below the person's real folder, not
    resolved itself, so a link put in its place cannot move the allowed area.
    """

    identity = research_identity()
    research_root = Path(os.path.realpath(identity.research_root.parent)) / identity.research_root.name
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = identity.research_root.parent / candidate
    try:
        location = file_access.locate(candidate)
    except PathPolicyError as exc:
        return None, f"Path is outside this task's research folder ({research_root}): {exc}"
    if not (location.path == research_root or research_root in location.path.parents):
        return None, (
            f"Path is outside this task's research folder ({research_root}): {location.path}"
        )
    return location, None


def _read_located_text(location: file_access.FileLocation) -> str:
    _location, text = file_access.read_text(
        location.path, max_bytes=INSPECT_MAX_FILE_BYTES, errors="replace"
    )
    return text


def _inspection_failure(stage: str, reason: str, message: str, *, retryable: bool) -> Dict[str, Any]:
    return _json_result(
        "error",
        {"status": "failed", "failure_stage": stage, "failure_reason": reason, "message": message, "retryable": retryable},
    )


def _inspection_target(path: str) -> tuple[Path, Dict[str, Any], str] | Dict[str, Any]:
    """The checked path, its stat fields and its text (read once), or the failure result."""

    location, error = _resolve_artifact_path(path)
    if error or location is None:
        return _inspection_failure("input_validation", "path_outside_artifacts", str(error), retryable=False)
    try:
        _location, entry = file_access.status(location.path)
    except PathPolicyError as exc:
        return _inspection_failure("input_validation", "path_outside_artifacts", str(exc), retryable=False)
    if entry is None:
        if _deleted_under_retention(location.path):
            days = retention_days_of(get_agent_ref())
            return _inspection_failure("artifact_read", DELETED_OUTPUT_REASON, deleted_output_note(days), retryable=False)
        return _inspection_failure("input_validation", "file_not_found", f"File not found: {location.path}", retryable=True)
    stat = {
        "path": str(location.path),
        "file_bytes": entry.st_size,
        "is_file": stat_module.S_ISREG(entry.st_mode),
        "is_dir": stat_module.S_ISDIR(entry.st_mode),
    }
    text = ""
    if stat["is_file"]:
        try:
            text = _read_located_text(location)
        except (OSError, PathPolicyError) as exc:
            return _inspection_failure("read", getattr(exc, "reason_code", "read_failed"), str(exc), retryable=False)
        stat["line_count"] = _line_count(text)
    return location.path, stat, text


def _deleted_under_retention(path: Path) -> bool:
    """Whether a missing path was in a result folder the cleanup deleted.

    ``research/<task folder>/<result folder>/...``: the result folder's name
    starts with the time it was written (``result_folder_name``), and the
    cleanup deletes a result folder as a whole, so the folder must be past
    the period and gone. A file missing from a folder that still exists is an
    ordinary "not found".
    """

    identity = research_identity()
    research_root = Path(os.path.realpath(identity.research_root.parent)) / identity.research_root.name
    try:
        parts = path.relative_to(research_root).parts
    except ValueError:
        return False
    if len(parts) < 2 or os.path.lexists(research_root / parts[0] / parts[1]):
        return False
    return deleted_by_retention(written_at_from_name(parts[1]), retention_days_of(get_agent_ref()))


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _bounded_lines(text: str, *, start_line: int, max_lines: int, max_chars: int) -> Dict[str, Any]:
    lines: List[str] = []
    returned_chars = 0
    end_line = start_line + max_lines - 1
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        if number < start_line:
            continue
        if number > end_line:
            break
        if returned_chars + len(line) > max_chars:
            remaining = max(0, max_chars - returned_chars)
            if remaining:
                lines.append(line[:remaining])
                returned_chars += remaining
            return {"text": "".join(lines), "returned_chars": returned_chars, "truncated": True}
        lines.append(line)
        returned_chars += len(line)
    return {"text": "".join(lines), "returned_chars": returned_chars, "truncated": False}


def _grep(text: str, *, query: str, max_lines: int, max_chars: int, context_lines: int) -> Dict[str, Any]:
    terms = [term for term in re.findall(r"[^\s]+", query) if term]
    if not terms:
        return {"matches": [], "returned_chars": 0, "truncated": False}
    pattern = re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE)
    all_lines = text.splitlines()
    matches: List[Dict[str, Any]] = []
    seen: set[int] = set()
    returned_chars = 0
    for index, line in enumerate(all_lines, start=1):
        if not pattern.search(line):
            continue
        start = max(1, index - context_lines)
        end = min(len(all_lines), index + context_lines)
        for line_no in range(start, end + 1):
            if line_no in seen:
                continue
            line_text = all_lines[line_no - 1]
            if returned_chars + len(line_text) > max_chars or len(matches) >= max_lines:
                return {"matches": matches, "returned_chars": returned_chars, "truncated": True}
            seen.add(line_no)
            matches.append({"line": line_no, "text": line_text})
            returned_chars += len(line_text)
    return {"matches": matches, "returned_chars": returned_chars, "truncated": False}


def _extract_relevant(text: str, *, query: str, max_chars: int) -> Dict[str, Any]:
    excerpt, truncated = _excerpt_for(text, query, max_chars)
    return {"text": excerpt, "returned_chars": len(excerpt), "truncated": truncated}


def _metadata_url_for(path: Path) -> str:
    candidates: List[Path] = []
    for suffix in (".fit.md", ".raw.md", ".html", ".txt", ".md"):
        if path.name.endswith(suffix):
            candidates.append(path.with_name(path.name[: -len(suffix)] + ".metadata.json"))
    candidates.append(path.with_suffix(".metadata.json"))

    for candidate in candidates:
        try:
            _location, raw = file_access.read_text(candidate, max_bytes=INSPECT_MAX_FILE_BYTES)
            data = json.loads(raw)
        except (OSError, ValueError):
            continue
        url = data.get("url") if isinstance(data, dict) else None
        if isinstance(url, str) and url:
            return url
    return ""


def _normalized_terms(*values: Any) -> List[str]:
    terms: List[str] = []
    for value in values:
        if isinstance(value, list):
            items = value
        elif isinstance(value, tuple):
            items = list(value)
        elif value is None:
            items = []
        else:
            items = [str(value)]
        for item in items:
            terms.extend(term.lower() for term in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}", str(item)))
    seen: set[str] = set()
    unique_terms: List[str] = []
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        unique_terms.append(term)
    return unique_terms


def _matches_terms(record: Dict[str, Any], terms: List[str]) -> bool:
    if not terms:
        return True
    haystack = " ".join(str(value) for value in record.values() if value is not None).lower()
    return any(term in haystack for term in terms)


def _bounded_records(records: List[Dict[str, Any]], *, max_items: int) -> Dict[str, Any]:
    return {
        "items": records[:max_items],
        "item_count": len(records),
        "returned_item_count": min(len(records), max_items),
        "truncated": len(records) > max_items,
    }


def _clean_html_text(text: str, *, max_chars: int) -> Dict[str, Any]:
    without_scripts = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", text)
    without_tags = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
    cleaned = re.sub(r"\s+", " ", unescape(without_tags)).strip()
    excerpt, truncated = _truncate_text(cleaned, max_chars)
    return {"text": excerpt, "returned_chars": len(excerpt), "truncated": truncated}


class _MarkdownExtractor(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: List[str] = []
        self.skip_stack: List[str] = []
        self.link_stack: List[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attr_map = {name.lower(): value or "" for name, value in attrs}
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_stack.append(tag)
            return
        if self.skip_stack:
            return
        if tag in {"p", "div", "section", "article", "header", "footer", "nav", "table", "ul", "ol"}:
            self.parts.append("\n\n")
        elif tag in {"br", "tr"}:
            self.parts.append("\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(tag[1])
            self.parts.append("\n\n" + ("#" * level) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")
        elif tag == "a":
            href = attr_map.get("href", "").strip()
            self.link_stack.append(urljoin(self.base_url, href) if self.base_url and href else href)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_stack:
            if self.skip_stack[-1] == tag:
                self.skip_stack.pop()
            return
        if tag == "a" and self.link_stack:
            href = self.link_stack.pop()
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                self.parts.append(f" ({href})")
        elif tag in {"p", "div", "section", "article", "header", "footer", "nav", "table", "tr", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_stack:
            return
        value = re.sub(r"\s+", " ", data)
        if value.strip():
            self.parts.append(value)

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        lines = [line.strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _html_to_markdown(text: str, *, base_url: str, max_chars: int) -> Dict[str, Any]:
    parser = _MarkdownExtractor(base_url=base_url)
    parser.feed(text)
    markdown = parser.markdown()
    excerpt, truncated = _truncate_text(markdown, max_chars)
    return {"text": excerpt, "returned_chars": len(excerpt), "truncated": truncated, "base_url": base_url}


def _extract_tag_text(text: str, tag: str) -> str:
    match = re.search(rf"(?is)<{tag}\b[^>]*>(.*?)</{tag}>", text)
    return _strip_tags(match.group(1)) if match else ""


def _extract_meta_content(text: str, names: set[str]) -> str:
    for match in re.finditer(r"(?is)<meta\b(?P<attrs>[^>]*)>", text):
        attrs = {attr.group("name").lower(): unescape(attr.group("value")).strip() for attr in _ATTR_RE.finditer(match.group("attrs"))}
        meta_name = (attrs.get("name") or attrs.get("property") or "").lower()
        if meta_name in names and attrs.get("content"):
            return attrs["content"]
    return ""


def _extract_page_preview(text: str, *, base_url: str, max_items: int, max_chars: int) -> Dict[str, Any]:
    title = _extract_tag_text(text, "title")
    description = _extract_meta_content(text, {"description", "og:description", "twitter:description"})
    headings: List[Dict[str, Any]] = []
    for match in re.finditer(r"(?is)<h([1-6])\b[^>]*>(.*?)</h\1>", text):
        heading = _strip_tags(match.group(2))
        if heading:
            headings.append({"level": int(match.group(1)), "text": heading})
        if len(headings) >= max_items:
            break
    cleaned = _clean_html_text(text, max_chars=max_chars)
    links = _extract_links_from_html(text, base_url=base_url, query="", max_items=max_items)
    return {
        "title": title,
        "description": description,
        "headings": headings,
        "links_preview": links["items"],
        "text_preview": cleaned["text"],
        "returned_chars": len(cleaned["text"]),
        "truncated": cleaned["truncated"] or links["truncated"],
        "base_url": base_url,
    }


def _extract_urls_from_text(text: str, *, base_url: str, query: str, max_items: int) -> Dict[str, Any]:
    terms = _normalized_terms(query)
    seen: set[str] = set()
    records: List[Dict[str, Any]] = []
    for match in re.finditer(r"https?://[^\s\"'<>)}\]]+", text):
        url = unescape(match.group(0)).rstrip(".,;:")
        if url in seen:
            continue
        seen.add(url)
        record = {"url": url, "absolute_url": urljoin(base_url, url) if base_url else url}
        if _matches_terms(record, terms):
            records.append(record)
    return _bounded_records(records, max_items=max_items)


_LINK_RE = re.compile(r"(?is)<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>")
_ATTR_RE = re.compile(r"(?is)\b(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)")


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"(?s)<[^>]+>", " ", text))).strip()


def _extract_links_from_html(text: str, *, base_url: str, query: str, max_items: int) -> Dict[str, Any]:
    terms = _normalized_terms(query)
    seen: set[tuple[str, str]] = set()
    seen_urls: set[str] = set()
    records: List[Dict[str, Any]] = []
    for match in _LINK_RE.finditer(text):
        attrs = {attr.group("name").lower(): unescape(attr.group("value")).strip() for attr in _ATTR_RE.finditer(match.group("attrs"))}
        href = attrs.get("href") or ""
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        text_value = _strip_tags(match.group("body"))
        absolute_url = urljoin(base_url, href) if base_url else href
        key = (absolute_url, text_value)
        if key in seen:
            continue
        seen.add(key)
        seen_urls.add(absolute_url)
        record = {
            "url": href,
            "absolute_url": absolute_url,
            "text": text_value,
            "title": attrs.get("title") or attrs.get("aria-label") or "",
            "rel": attrs.get("rel") or "",
        }
        if _matches_terms(record, terms):
            records.append(record)

    for attr in re.finditer(r"(?is)\b(?:href|src)\s*=\s*([\"'])(?P<url>.*?)(?:\1)", text):
        url = unescape(attr.group("url")).strip()
        if not url or url.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute_url = urljoin(base_url, url) if base_url else url
        if absolute_url in seen_urls:
            continue
        key = (absolute_url, "")
        if key in seen:
            continue
        seen.add(key)
        seen_urls.add(absolute_url)
        record = {"url": url, "absolute_url": absolute_url, "text": "", "title": "", "rel": ""}
        if _matches_terms(record, terms):
            records.append(record)
    return _bounded_records(records, max_items=max_items)


def _extract_sitemap_urls_from_text(text: str, *, query: str, max_items: int) -> Dict[str, Any]:
    terms = _normalized_terms(query)
    records: List[Dict[str, Any]] = []
    for block in re.findall(r"(?is)<url\b.*?</url>", text):
        loc_match = re.search(r"(?is)<loc>\s*(.*?)\s*</loc>", block)
        if not loc_match:
            continue
        record = {
            "url": unescape(_strip_tags(loc_match.group(1))),
            "lastmod": "",
            "changefreq": "",
            "priority": "",
        }
        for field in ("lastmod", "changefreq", "priority"):
            field_match = re.search(rf"(?is)<{field}>\s*(.*?)\s*</{field}>", block)
            if field_match:
                record[field] = unescape(_strip_tags(field_match.group(1)))
        if _matches_terms(record, terms):
            records.append(record)

    if not records:
        seen: set[str] = set()
        for loc in re.findall(r"(?is)<loc>\s*(.*?)\s*</loc>", text):
            url = unescape(_strip_tags(loc))
            if not url or url in seen:
                continue
            seen.add(url)
            record = {"url": url, "lastmod": "", "changefreq": "", "priority": ""}
            if _matches_terms(record, terms):
                records.append(record)
    return _bounded_records(records, max_items=max_items)


def _score_candidate(record: Dict[str, Any], terms: List[str]) -> int:
    score = 0
    weighted_fields = {
        "text": 4,
        "title": 4,
        "description": 3,
        "url": 2,
        "absolute_url": 2,
        "lastmod": 1,
    }
    for term in terms:
        if not term:
            continue
        for field, weight in weighted_fields.items():
            score += str(record.get(field) or "").lower().count(term) * weight
    return score


def _extract_content_candidates(
    text: str,
    *,
    base_url: str,
    query: str,
    content_keywords: Optional[list[str]],
    max_items: int,
) -> Dict[str, Any]:
    candidate_terms = _normalized_terms(query, content_keywords)
    links = _extract_links_from_html(text, base_url=base_url, query="", max_items=INSPECT_HARD_MAX_ITEMS)["items"]
    urls = _extract_urls_from_text(text, base_url=base_url, query="", max_items=INSPECT_HARD_MAX_ITEMS)["items"]
    sitemap_urls = _extract_sitemap_urls_from_text(text, query="", max_items=INSPECT_HARD_MAX_ITEMS)["items"]

    merged: Dict[str, Dict[str, Any]] = {}
    for record in [*links, *urls, *sitemap_urls]:
        url = str(record.get("absolute_url") or record.get("url") or "")
        if not url:
            continue
        merged.setdefault(url, {**record, "url": url})

    scored: List[Dict[str, Any]] = []
    for record in merged.values():
        if candidate_terms and not _matches_terms(record, candidate_terms):
            continue
        score = _score_candidate(record, candidate_terms)
        scored.append({"score": score, **record})
    scored.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("url") or "")))
    return _bounded_records(scored, max_items=max_items)


@tool
def web_artifact_inspect(
    path: str,
    action: str = "stat",
    start_line: int = 1,
    max_lines: int = INSPECT_DEFAULT_MAX_LINES,
    max_chars: int = INSPECT_DEFAULT_MAX_CHARS,
    query: str = "",
    context_lines: int = 3,
    max_items: int = INSPECT_DEFAULT_MAX_ITEMS,
    base_url: str = "",
    content_keywords: Optional[list[str]] = None,
) -> Dict[str, Any]:
    """
    Safely inspect a saved web research artifact without reading an unbounded
    file into model context.

    Actions include bounded text reads plus structured extraction modes:
    `preview`, `extract_markdown`, `extract_links`, `extract_urls`,
    `extract_sitemap_urls`, `extract_text`, and `extract_content_candidates`.
    Saved results are deleted after the team's retention period; for one
    deleted that way the answer says so (failure_reason
    "deleted_by_retention", not retryable): run the research tool again if
    you still need it.
    """
    target = _inspection_target(path)
    if isinstance(target, dict):
        return target
    resolved, stat, text = target
    is_file, is_dir = stat["is_file"], stat["is_dir"]
    action = (action or "stat").strip().lower()
    max_lines = _safe_int(max_lines, default=INSPECT_DEFAULT_MAX_LINES, minimum=1, maximum=INSPECT_HARD_MAX_LINES)
    max_chars = _safe_int(max_chars, default=INSPECT_DEFAULT_MAX_CHARS, minimum=200, maximum=INSPECT_HARD_MAX_CHARS)
    start_line = _safe_int(start_line, default=1, minimum=1, maximum=10_000_000)
    context_lines = _safe_int(context_lines, default=3, minimum=0, maximum=10)
    max_items = _safe_int(max_items, default=INSPECT_DEFAULT_MAX_ITEMS, minimum=1, maximum=INSPECT_HARD_MAX_ITEMS)

    if action == "stat":
        return _json_result("success", {"status": "success", "action": action, **stat, "truncated": False})
    if action == "manifest":
        manifest_path = resolved / "manifest.json" if is_dir else resolved
        data = json.loads(text if not is_dir else file_access.read_text(manifest_path, max_bytes=INSPECT_MAX_FILE_BYTES)[1])
        compact = {
            "artifact_id": data.get("artifact_id"),
            "service": data.get("service"),
            "task_id": data.get("task_id"),
            "summary": data.get("summary"),
            "failure": data.get("failure"),
            "files": [
                {"path": item.get("path"), "role": item.get("role"), "url": item.get("url"), "bytes": item.get("bytes")}
                for item in list(data.get("files") or [])[:80]
            ],
            "truncated_file_list": len(data.get("files") or []) > 80,
        }
        return _json_result("success", {"status": "success", "action": action, "path": str(manifest_path), **compact})
    if not is_file:
        return _json_result("error", {"status": "failed", "failure_stage": "input_validation", "failure_reason": "action_requires_file", "message": "Use action='manifest' or provide a file path.", "retryable": True})
    if action == "head":
        content = _bounded_lines(text, start_line=1, max_lines=max_lines, max_chars=max_chars)
    elif action == "tail":
        line_count = int(stat.get("line_count") or 0)
        content = _bounded_lines(text, start_line=max(1, line_count - max_lines + 1), max_lines=max_lines, max_chars=max_chars)
    elif action == "read_range":
        content = _bounded_lines(text, start_line=start_line, max_lines=max_lines, max_chars=max_chars)
    elif action == "grep":
        content = _grep(text, query=query, max_lines=max_lines, max_chars=max_chars, context_lines=context_lines)
    elif action == "extract_relevant":
        content = _extract_relevant(text, query=query, max_chars=max_chars)
    elif action == "extract_text":
        content = _clean_html_text(text, max_chars=max_chars)
    elif action == "preview":
        effective_base_url = base_url.strip() or _metadata_url_for(resolved)
        content = _extract_page_preview(text, base_url=effective_base_url, max_items=max_items, max_chars=max_chars)
    elif action == "extract_markdown":
        effective_base_url = base_url.strip() or _metadata_url_for(resolved)
        content = _html_to_markdown(text, base_url=effective_base_url, max_chars=max_chars)
    elif action == "extract_urls":
        effective_base_url = base_url.strip() or _metadata_url_for(resolved)
        content = _extract_urls_from_text(text, base_url=effective_base_url, query=query, max_items=max_items)
        content["base_url"] = effective_base_url
    elif action == "extract_links":
        effective_base_url = base_url.strip() or _metadata_url_for(resolved)
        content = _extract_links_from_html(text, base_url=effective_base_url, query=query, max_items=max_items)
        content["base_url"] = effective_base_url
    elif action == "extract_sitemap_urls":
        content = _extract_sitemap_urls_from_text(text, query=query, max_items=max_items)
    elif action == "extract_content_candidates":
        effective_base_url = base_url.strip() or _metadata_url_for(resolved)
        content = _extract_content_candidates(
            text,
            base_url=effective_base_url,
            query=query,
            content_keywords=content_keywords,
            max_items=max_items,
        )
        content["base_url"] = effective_base_url
    else:
        return _json_result(
            "error",
            {
                "status": "failed",
                "failure_stage": "input_validation",
                "failure_reason": "unsupported_action",
                "message": (
                    "action must be one of: stat, manifest, head, tail, read_range, grep, "
                    "extract_relevant, extract_text, preview, extract_markdown, extract_urls, "
                    "extract_links, extract_sitemap_urls, extract_content_candidates"
                ),
                "retryable": True,
            },
        )
    payload = {"status": "success", "action": action, **stat, **content}
    return _json_result("success", payload)
