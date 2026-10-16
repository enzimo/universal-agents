"""Compact, artifact-backed tools for the Crawl4AI and SearXNG services.

Agents receive only bounded summaries plus workspace artifact paths, never
tokens, base64 captures, or unbounded service responses.

- ``capture_web_screenshot`` takes an exact-viewport screenshot through
  Crawl4AI, because the upstream screenshot MCP schema does not accept viewport
  dimensions. Its request goes through the secure tool broker, which adds the
  token of the account linked to ``http:crawl4ai`` (the person's own, else the
  team's), so the token never reaches the agent process
  (``custom_tools.broker_web_requests``). Crawling and page reading use the
  Crawl4AI MCP connection (``mcp:crawl4ai``).
- ``searxng`` searches the instance's SearXNG server, which needs no key, so
  it calls the server directly.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse, urlsplit

import requests
from strands import tool

from config_loader import get_config
from custom_tools import file_access
from custom_tools.broker_web_requests import (
    BROKER_MAX_TIMEOUT_SECONDS,
    REASON_ACCESS_DENIED,
    BrokerWebRequestError,
    is_missing_key,
    send_web_request,
)
from custom_tools.research_identity import ResearchIdentity, research_identity, result_folder_name
from runtime_settings.services import CRAWL4AI_URL_SETTING, crawl4ai_base_url


logger = logging.getLogger(__name__)

SEARXNG_OPERATIONS = {"capabilities", "search", "suggest"}
REDACTED = "[REDACTED]"
DEFAULT_INLINE_CHARS = 4000
DEFAULT_SEARCH_RESULTS = 8
HARD_SEARCH_RESULTS = 30
MIN_SCREENSHOT_VIEWPORT_PX = 1
MAX_SCREENSHOT_VIEWPORT_PX = 4000
SEARXNG_SEARCH_OPTION_KEYS = {
    "disabled_engines",
    "disabled_plugins",
    "enabled_engines",
    "enabled_plugins",
    "image_proxy",
    "results_on_new_tab",
    "theme",
}


def _json_result(status: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": status,
        "content": [
            {"text": json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)}
        ],
    }


def _safe_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _safe_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bool_value(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _create_artifact_dir(
    service: str, operation: str, label: str
) -> tuple[Path, str, ResearchIdentity]:
    """A new folder for one call's files, named by ``result_folder_name`` so no other call shares it."""

    identity = research_identity()
    artifact_id = result_folder_name(_now_stamp(), service, operation, label)
    artifact_dir = file_access.make_directories(identity.task_folder / artifact_id).path
    return artifact_dir, artifact_id, identity


# Every artifact is written through the agent file library, which never
# follows a link (custom_tools.file_access).
def _write_json(path: Path, value: Any) -> int:
    return _write_text(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def _write_text(path: Path, value: str) -> int:
    return _write_bytes(path, str(value or "").encode("utf-8", errors="replace"))


def _write_bytes(path: Path, value: bytes) -> int:
    file_access.write_bytes(path, value)
    return len(value)


def _bounded_text(value: Any, max_chars: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + f"\n...<truncated {len(text) - max_chars} chars>", True


def _base_manifest(
    *,
    artifact_id: str,
    identity: ResearchIdentity,
    service: str,
    operation: str,
    request: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **identity.manifest_fields(),
        "service": service,
        "operation": operation,
        "request": request,
        "files": [],
        "summary": {},
        "failure": None,
    }


def _file_size(path: Path) -> int:
    _location, entry = file_access.status(path)
    return entry.st_size if entry else 0


def _record_file(manifest: Dict[str, Any], path: Path, role: str) -> None:
    manifest["files"].append({"path": str(path), "role": role, "bytes": _file_size(path)})


def _finish_manifest(artifact_dir: Path, manifest: Dict[str, Any]) -> str:
    path = artifact_dir / "manifest.json"
    _write_json(path, manifest)
    return str(path)


def _failure(
    *,
    artifact_dir: Path,
    artifact_id: str,
    manifest: Dict[str, Any],
    stage: str,
    reason: str,
    message: str,
    retryable: bool,
    hints: Iterable[str],
    status_code: Optional[int] = None,
) -> Dict[str, Any]:
    payload = {
        "status": "failed",
        "artifact_id": artifact_id,
        "artifact_dir": str(artifact_dir),
        "failure_stage": stage,
        "failure_reason": reason,
        "message": message,
        "retryable": retryable,
        "status_code": status_code,
        "mitigation_hints": list(hints),
    }
    manifest["failure"] = payload
    _finish_manifest(artifact_dir, manifest)
    logger.warning(
        "web_service_tool_failed service=%s operation=%s artifact_id=%s stage=%s reason=%s status_code=%s retryable=%s",
        manifest.get("service"),
        manifest.get("operation"),
        artifact_id,
        stage,
        reason,
        status_code,
        retryable,
    )
    return _json_result("error", payload)


def _request_failure_reason(
    exc: requests.RequestException,
) -> tuple[str, Optional[int]]:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 401:
        return "authentication_required", status_code
    if status_code == 403:
        return "operation_forbidden", status_code
    if status_code == 429:
        return "rate_limited", status_code
    if status_code:
        return f"http_{status_code}", status_code
    if isinstance(exc, requests.Timeout):
        return "request_timeout", None
    if isinstance(exc, requests.ConnectionError):
        return "connection_error", None
    return "request_exception", None


def _exception_message(exc: Exception) -> str:
    if not isinstance(exc, requests.RequestException):
        return str(exc)
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail")
            if detail is None:
                detail = body
        else:
            detail = body
        rendered = json.dumps(detail, ensure_ascii=False, default=str)
    except ValueError:
        rendered = str(getattr(response, "text", "") or "")
    rendered = _bounded_text(rendered, 2000)[0]
    return f"{exc}: {rendered}" if rendered else str(exc)


def _config_mapping(path: str) -> Dict[str, Any]:
    value = get_config().get(path, {})
    return dict(value) if isinstance(value, dict) else {}


def _crawl4ai_settings() -> Dict[str, Any]:
    """Where the Crawl4AI server is; the broker adds its token (http:crawl4ai)."""

    config = _config_mapping("web_services.crawl4ai")
    return {
        "base_url": crawl4ai_base_url(config.get("base_url")),
        "timeout_seconds": _safe_float(
            os.getenv("CRAWL4AI_TIMEOUT_SECONDS", config.get("timeout_seconds")),
            default=120.0,
            minimum=1.0,
            maximum=float(BROKER_MAX_TIMEOUT_SECONDS),
        ),
    }


def _searxng_settings() -> Dict[str, Any]:
    config = _config_mapping("web_services.searxng")
    return {
        "base_url": str(
            os.getenv("SEARXNG_BASE_URL")
            or config.get("base_url")
            or "http://127.0.0.1:8080"
        ).rstrip("/"),
        "verify_tls": _bool_value(
            os.getenv("SEARXNG_VERIFY_TLS"),
            default=_bool_value(config.get("verify_tls"), default=True),
        ),
        "timeout_seconds": _safe_float(
            os.getenv("SEARXNG_TIMEOUT_SECONDS", config.get("timeout_seconds")),
            default=30.0,
            minimum=1.0,
            maximum=300.0,
        ),
        "default_num_results": _safe_int(
            os.getenv("SEARXNG_DEFAULT_NUM_RESULTS", config.get("default_num_results")),
            default=DEFAULT_SEARCH_RESULTS,
            minimum=1,
            maximum=HARD_SEARCH_RESULTS,
        ),
    }


def _typed_config(value: Optional[Dict[str, Any]], type_name: str) -> Dict[str, Any]:
    raw = dict(value or {})
    if raw.get("type") and isinstance(raw.get("params"), dict):
        return raw
    return {"type": type_name, "params": raw}


def _service_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        preview, _ = _bounded_text(response.text, 1000)
        raise ValueError(f"service returned non-JSON content: {preview}") from exc


def _decode_binary(value: Any) -> Optional[bytes]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _strict_viewport_dimension(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer number of pixels")
    if not MIN_SCREENSHOT_VIEWPORT_PX <= value <= MAX_SCREENSHOT_VIEWPORT_PX:
        raise ValueError(
            f"{field_name} must be between {MIN_SCREENSHOT_VIEWPORT_PX} and "
            f"{MAX_SCREENSHOT_VIEWPORT_PX} pixels"
        )
    return value


def _png_dimensions(value: bytes) -> tuple[int, int]:
    if (
        len(value) < 24
        or value[:8] != b"\x89PNG\r\n\x1a\n"
        or value[12:16] != b"IHDR"
    ):
        raise ValueError("Crawl4AI screenshot response was not a valid PNG")
    width, height = struct.unpack(">II", value[16:24])
    if width < 1 or height < 1:
        raise ValueError("Crawl4AI screenshot PNG reported invalid dimensions")
    return width, height


CRAWL4AI_TOOL_ID = "http:crawl4ai"
# A screenshot comes back as base64 inside the JSON answer.
CRAWL4AI_MAX_RESPONSE_BYTES = 16_000_000
_CRAWL4AI_HEADERS = {"Accept": "application/json", "User-Agent": "universal-agents-crawl4ai/1.0"}
_CRAWL4AI_SERVICE_HINTS = (
    "verify the configured Crawl4AI 0.9.2 service is healthy",
    "inspect Crawl4AI logs using the returned correlation id when a 5xx response supplies one",
)


class _ServiceAnswerError(ValueError):
    """The service answered with an HTTP error status."""

    def __init__(
        self, message: str, *, reason: str, status_code: int, hints: tuple[str, ...] = _CRAWL4AI_SERVICE_HINTS
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code
        self.hints = hints


def _redirect_error(base_url: str, response: Any) -> _ServiceAnswerError:
    """A 3xx answer: name where it pointed and the setting that fixes it.

    The broker never follows a redirect, so the linked token reaches only the
    configured address; a redirect (for example http to https) means that
    address is not the service's own.
    """

    location = response.redirect_location
    target = urlsplit(location)
    origin = f"{target.scheme}://{target.netloc}" if target.scheme and target.netloc else ""
    return _ServiceAnswerError(
        f"Crawl4AI at {base_url} answered HTTP {response.http_status}, a redirect to "
        f"{location or 'another address'}. The broker does not follow redirects, so the linked token "
        "reaches only the configured address.",
        reason="redirected",
        status_code=response.http_status,
        hints=(
            f"an administrator sets {CRAWL4AI_URL_SETTING} in instance_config.yaml to "
            f"{origin or 'the address Crawl4AI redirects to'} and removes any CRAWL4AI_BASE_URL override, "
            "then restarts",
        ),
    )


def _status_reason(status_code: int) -> str:
    named = {401: "authentication_required", 403: "operation_forbidden", 429: "rate_limited"}
    return named.get(status_code, f"http_{status_code}")


def _crawl4ai_post(path: str, payload: Dict[str, Any], settings: Dict[str, Any]) -> Any:
    """POST to the Crawl4AI server through the broker, which adds the linked token; the JSON answer."""

    response = send_web_request(
        "POST",
        settings["base_url"] + path,
        json_body=payload,
        headers=_CRAWL4AI_HEADERS,
        reason="Crawl4AI screenshot",
        timeout_seconds=settings["timeout_seconds"],
        max_response_bytes=CRAWL4AI_MAX_RESPONSE_BYTES,
    )
    if 300 <= response.http_status < 400:
        raise _redirect_error(settings["base_url"], response)
    preview = _bounded_text(response.text or json.dumps(response.json, default=str), 1000)[0]
    if not response.ok:
        raise _ServiceAnswerError(
            f"Crawl4AI answered HTTP {response.http_status}: {preview}",
            reason=_status_reason(response.http_status),
            status_code=response.http_status,
        )
    if response.truncated:
        raise ValueError("Crawl4AI's answer was larger than the broker returns; capture a smaller viewport.")
    if response.json is None:
        raise ValueError(f"service returned non-JSON content: {preview}")
    return response.json


def _crawl4ai_broker_failure(exc: BrokerWebRequestError) -> Dict[str, Any]:
    """Failure fields for a Crawl4AI request the broker refused or could not send."""

    if is_missing_key(exc):
        stage, hints = "authentication", [
            f"a team administrator links the team's Crawl4AI token with /link {CRAWL4AI_TOOL_ID} team",
            f"the person links their own with /link {CRAWL4AI_TOOL_ID}",
        ]
    elif exc.reason_code == REASON_ACCESS_DENIED:
        stage, hints = "authentication", [f"ask the person, or an administrator, for access to {CRAWL4AI_TOOL_ID}"]
    else:
        stage, hints = "service_request", list(_CRAWL4AI_SERVICE_HINTS)
    return {
        "stage": stage,
        "reason": exc.reason_code,
        "message": exc.message,
        "retryable": exc.retryable,
        "hints": hints,
    }


@tool
def capture_web_screenshot(
    url: str,
    width_px: int = 1280,
    height_px: int = 720,
    wait_seconds: float = 2.0,
    wait_for_images: bool = False,
) -> Dict[str, Any]:
    """Capture an exact-size PNG screenshot through Crawl4AI.

    ``width_px`` and ``height_px`` are the output viewport dimensions and must
    each be between 1 and 4000 pixels. The PNG is saved under the active task's
    workspace artifacts; pass the returned ``path`` to ``notify_user`` when the
    requester asked to receive the image. This tool captures the visible
    viewport, not a variable-height full-page image. Anti-bot blocks are
    reported as failures and are not bypassed.

    Args:
        url: Absolute HTTP or HTTPS page URL to capture.
        width_px: Exact output width in pixels, from 1 through 4000.
        height_px: Exact output height in pixels, from 1 through 4000.
        wait_seconds: Additional render delay from 0 through 60 seconds.
        wait_for_images: Whether Crawl4AI should wait for page images to load.

    Returns:
        A structured success result with the verified local PNG path, or a
        structured failure with diagnostics and mitigation hints.
    """
    target_url = str(url or "").strip()
    parsed_url = urlparse(target_url)
    has_embedded_credentials = (
        parsed_url.username is not None or parsed_url.password is not None
    )
    label = parsed_url.hostname or "invalid-url"
    artifact_dir, artifact_id, identity = _create_artifact_dir(
        "crawl4ai", "exact_screenshot", label
    )
    manifest = _base_manifest(
        artifact_id=artifact_id,
        identity=identity,
        service="crawl4ai",
        operation="exact_screenshot",
        request={
            "url": REDACTED if has_embedded_credentials else target_url,
            "width_px": width_px,
            "height_px": height_px,
            "wait_seconds": wait_seconds,
            "wait_for_images": wait_for_images,
        },
    )

    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or has_embedded_credentials
    ):
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            stage="input_validation",
            reason="invalid_url",
            message="url must be an absolute http/https URL without embedded credentials",
            retryable=False,
            hints=["provide a URL such as https://example.com/"],
        )

    try:
        requested_width = _strict_viewport_dimension(
            width_px, field_name="width_px"
        )
        requested_height = _strict_viewport_dimension(
            height_px, field_name="height_px"
        )
    except ValueError as exc:
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            stage="input_validation",
            reason="invalid_viewport_dimensions",
            message=str(exc),
            retryable=False,
            hints=[
                f"choose integer dimensions from {MIN_SCREENSHOT_VIEWPORT_PX} through {MAX_SCREENSHOT_VIEWPORT_PX} pixels"
            ],
        )

    if isinstance(wait_seconds, bool):
        parsed_wait_seconds = math.nan
    else:
        try:
            parsed_wait_seconds = float(wait_seconds)
        except (TypeError, ValueError):
            parsed_wait_seconds = math.nan
    if (
        not math.isfinite(parsed_wait_seconds)
        or not 0.0 <= parsed_wait_seconds <= 60.0
    ):
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            stage="input_validation",
            reason="invalid_wait_seconds",
            message="wait_seconds must be a finite number between 0 and 60",
            retryable=False,
            hints=["use a short render delay such as 2 seconds"],
        )

    if not isinstance(wait_for_images, bool):
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            stage="input_validation",
            reason="invalid_wait_for_images",
            message="wait_for_images must be a boolean",
            retryable=False,
            hints=["use true to wait for images or false to capture after the render delay"],
        )

    settings = _crawl4ai_settings()
    request_payload = {
        "urls": [target_url],
        "browser_config": _typed_config(
            {
                "headless": True,
                "viewport_width": requested_width,
                "viewport_height": requested_height,
                "text_mode": False,
            },
            "BrowserConfig",
        ),
        "crawler_config": _typed_config(
            {
                "cache_mode": "bypass",
                "force_viewport_screenshot": True,
                "screenshot": True,
                "screenshot_wait_for": parsed_wait_seconds,
                "wait_for_images": wait_for_images,
            },
            "CrawlerRunConfig",
        ),
    }
    request_path = artifact_dir / "crawl4ai_request.json"
    _write_json(request_path, request_payload)
    _record_file(manifest, request_path, "service_request")

    try:
        data = _crawl4ai_post("/crawl", request_payload, settings)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or len(results) != 1:
            raise ValueError("Crawl4AI screenshot response did not contain one result")
        result = results[0]
        if not isinstance(result, dict):
            raise ValueError("Crawl4AI screenshot result had an invalid shape")
        if result.get("success") is not True:
            message = str(result.get("error_message") or "Crawl4AI capture failed")
            return _failure(
                artifact_dir=artifact_dir,
                artifact_id=artifact_id,
                manifest=manifest,
                stage="page_render",
                reason="capture_failed",
                message=message,
                retryable=False,
                hints=[
                    "inspect Crawl4AI service logs for an anti-bot block or browser failure",
                    "do not treat a CAPTCHA or access-denied page as the requested content",
                ],
                status_code=result.get("status_code")
                if isinstance(result.get("status_code"), int)
                else None,
            )

        png = _decode_binary(result.get("screenshot"))
        if png is None:
            raise ValueError("Crawl4AI screenshot result contained no PNG data")
        actual_width, actual_height = _png_dimensions(png)
        destination = artifact_dir / (
            f"screenshot_{requested_width}x{requested_height}.png"
        )
        _write_bytes(destination, png)
        _record_file(manifest, destination, "screenshot")
        if (actual_width, actual_height) != (requested_width, requested_height):
            return _failure(
                artifact_dir=artifact_dir,
                artifact_id=artifact_id,
                manifest=manifest,
                stage="artifact_validation",
                reason="screenshot_dimension_mismatch",
                message=(
                    f"Crawl4AI returned {actual_width}x{actual_height}, expected "
                    f"{requested_width}x{requested_height}"
                ),
                retryable=False,
                hints=[
                    "inspect the saved diagnostic screenshot and Crawl4AI browser configuration"
                ],
            )

        response_path = artifact_dir / "crawl4ai_response.json"
        _write_json(
            response_path,
            {
                "success": data.get("success"),
                "server_processing_time_s": data.get("server_processing_time_s"),
                "result": {
                    "url": result.get("url"),
                    "redirected_url": result.get("redirected_url"),
                    "success": result.get("success"),
                    "status_code": result.get("status_code"),
                    "error_message": result.get("error_message"),
                    "screenshot": {
                        "artifact_path": str(destination),
                        "bytes": _file_size(destination),
                        "width_px": actual_width,
                        "height_px": actual_height,
                    },
                },
            },
        )
        _record_file(manifest, response_path, "service_response")
        manifest["summary"] = {
            "status": "success",
            "operation": "exact_screenshot",
            "width_px": actual_width,
            "height_px": actual_height,
            "file_count": len(manifest["files"]),
        }
        payload = {
            "status": "success",
            "artifact_id": artifact_id,
            "artifact_dir": str(artifact_dir),
            "path": str(destination),
            "filename": destination.name,
            "content_type": "image/png",
            "bytes": _file_size(destination),
            "requested_dimensions": {
                "width_px": requested_width,
                "height_px": requested_height,
            },
            "actual_dimensions": {
                "width_px": actual_width,
                "height_px": actual_height,
            },
            "delivery_hint": "Use notify_user with this path when the requester asked to receive the screenshot.",
        }
        payload["manifest"] = _finish_manifest(artifact_dir, manifest)
        return _json_result("success", payload)
    except BrokerWebRequestError as exc:
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            **_crawl4ai_broker_failure(exc),
        )
    except _ServiceAnswerError as exc:
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            stage="service_request",
            reason=exc.reason,
            message=str(exc),
            retryable=exc.reason == "rate_limited" or (exc.status_code or 0) >= 500,
            status_code=exc.status_code,
            hints=exc.hints,
        )
    except (OSError, ValueError) as exc:
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            stage="artifact_processing",
            reason=type(exc).__name__.lower(),
            message=str(exc),
            retryable=False,
            hints=_CRAWL4AI_SERVICE_HINTS,
        )


def _compact_nested(value: Any, *, max_chars: int = 600) -> Any:
    if isinstance(value, str):
        return _bounded_text(value, max_chars)[0]
    if isinstance(value, list):
        return [_compact_nested(item, max_chars=max_chars) for item in value[:10]]
    if isinstance(value, dict):
        return {
            str(key): _compact_nested(item, max_chars=max_chars)
            for key, item in list(value.items())[:20]
        }
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _bounded_text(value, max_chars)[0]


def _normalize_searxng_results(
    data: Dict[str, Any], max_results: int
) -> list[Dict[str, Any]]:
    normalized = []
    core_fields = {
        "url",
        "title",
        "content",
        "engine",
        "engines",
        "score",
        "category",
        "publishedDate",
        "published_date",
        "template",
    }
    for rank, item in enumerate(list(data.get("results") or [])[:max_results], start=1):
        if not isinstance(item, dict):
            continue
        details = {
            key: _compact_nested(value)
            for key, value in item.items()
            if key not in core_fields
            and key != "parsed_url"
            and value not in (None, "", [], {})
        }
        snippet, snippet_truncated = _bounded_text(item.get("content"), 600)
        normalized.append(
            {
                "rank": rank,
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": snippet,
                "snippet_truncated": snippet_truncated,
                "engine": item.get("engine") or item.get("engines") or "",
                "score": item.get("score"),
                "category": item.get("category"),
                "result_type": item.get("template")
                or item.get("category")
                or "general",
                "published_date": item.get("publishedDate")
                or item.get("published_date"),
                "details": details,
            }
        )
    return normalized


def _normalize_searxng_suggestions(data: Any) -> list[Any]:
    if isinstance(data, dict):
        value = data.get("suggestions") or data.get("results") or []
        return list(value) if isinstance(value, list) else [value]
    if (
        isinstance(data, list)
        and len(data) >= 2
        and isinstance(data[0], str)
        and isinstance(data[1], list)
    ):
        return list(data[1])
    return list(data) if isinstance(data, list) else ([data] if data else [])


@tool
def searxng(
    operation: str,
    query: str = "",
    categories: str = "general",
    engines: str = "",
    language: str = "en-US",
    page: int = 1,
    time_range: str = "",
    safesearch: int = 1,
    output_format: str = "json",
    max_results: int = 0,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Use the configured SearXNG service through one compact tool.

    Operations: capabilities, search, and suggest.  Search supports every live
    engine/category exposed by the instance, language, pagination, day/month/year
    ranges, SafeSearch, internal bang syntax, and JSON/CSV/RSS artifacts.  JSON
    returns preserve answers, corrections, suggestions, infoboxes, engine
    failures, and type-specific media/map/scientific/torrent fields.
    """
    operation = str(operation or "").strip().lower()
    options = dict(options or {})
    settings = _searxng_settings()
    max_results = _safe_int(
        max_results or settings["default_num_results"],
        default=settings["default_num_results"],
        minimum=1,
        maximum=HARD_SEARCH_RESULTS,
    )
    page = _safe_int(page, default=1, minimum=1, maximum=100)
    safesearch = _safe_int(safesearch, default=1, minimum=0, maximum=2)
    output_format = str(output_format or "json").strip().lower()
    artifact_dir, artifact_id, identity = _create_artifact_dir(
        "searxng", operation or "invalid", query or "service"
    )
    request_summary = {
        "operation": operation,
        "query": query,
        "categories": categories,
        "engines": engines,
        "language": language,
        "page": page,
        "time_range": time_range,
        "safesearch": safesearch,
        "output_format": output_format,
        "max_results": max_results,
        "options": options,
    }
    manifest = _base_manifest(
        artifact_id=artifact_id,
        identity=identity,
        service="searxng",
        operation=operation,
        request=request_summary,
    )
    if operation not in SEARXNG_OPERATIONS:
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            stage="input_validation",
            reason="unsupported_operation",
            message=f"operation must be one of: {', '.join(sorted(SEARXNG_OPERATIONS))}",
            retryable=False,
            hints=["use operation='capabilities' to discover engines and categories"],
        )
    if operation in {"search", "suggest"} and not query.strip():
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            stage="input_validation",
            reason="query_required",
            message=f"operation='{operation}' requires query.",
            retryable=False,
            hints=["provide a non-empty search query"],
        )
    if time_range and time_range not in {"day", "month", "year"}:
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            stage="input_validation",
            reason="invalid_time_range",
            message="time_range must be empty, day, month, or year.",
            retryable=False,
            hints=["remove time_range when the selected engines do not support it"],
        )
    if output_format not in {"json", "csv", "rss"}:
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            stage="input_validation",
            reason="invalid_output_format",
            message="output_format must be json, csv, or rss.",
            retryable=False,
            hints=["use json for agent-readable results"],
        )

    headers = {
        "Accept": "application/json" if output_format == "json" else "*/*",
        "User-Agent": "universal-agents-searxng/1.0",
    }
    try:
        if operation == "capabilities":
            aggregate: Dict[str, Any] = {}
            for name, path in (
                ("config", "/config"),
                ("engine_descriptions", "/engine_descriptions.json"),
                ("engine_errors", "/stats/errors"),
            ):
                response = requests.get(
                    settings["base_url"] + path,
                    headers=headers,
                    timeout=settings["timeout_seconds"],
                    verify=settings["verify_tls"],
                )
                if name != "config" and response.status_code >= 400:
                    aggregate[name] = {
                        "available": False,
                        "status_code": response.status_code,
                    }
                    continue
                response.raise_for_status()
                try:
                    aggregate[name] = _service_json(response)
                except ValueError:
                    if name == "config":
                        raise
                    aggregate[name] = {
                        "available": True,
                        "text_preview": _bounded_text(
                            response.text, DEFAULT_INLINE_CHARS
                        )[0],
                    }
            response_path = artifact_dir / "searxng_capabilities.json"
            _write_json(response_path, aggregate)
            _record_file(manifest, response_path, "capabilities")
            config = (
                aggregate.get("config")
                if isinstance(aggregate.get("config"), dict)
                else {}
            )
            engines_data = list(config.get("engines") or [])
            max_engines = _safe_int(
                options.get("max_engines"), default=100, minimum=1, maximum=500
            )
            payload = {
                "status": "success",
                "artifact_id": artifact_id,
                "artifact_dir": str(artifact_dir),
                "version": config.get("version"),
                "instance_name": config.get("instance_name"),
                "categories": config.get("categories") or [],
                "engine_count": len(engines_data),
                "engines_truncated": len(engines_data) > max_engines,
                "engines": [
                    {
                        "name": item.get("name"),
                        "shortcut": item.get("shortcut"),
                        "categories": item.get("categories") or [],
                        "enabled": item.get("enabled"),
                        "paging": item.get("paging"),
                        "safesearch": item.get("safesearch"),
                        "time_range_support": item.get("time_range_support"),
                    }
                    for item in engines_data[:max_engines]
                    if isinstance(item, dict)
                ],
                "locales": config.get("locales") or {},
                "plugins": config.get("plugins") or [],
                "engine_error_count": len(aggregate.get("engine_errors") or {}),
            }
        elif operation == "suggest":
            response = requests.post(
                settings["base_url"] + "/autocompleter",
                data={"q": query},
                headers=headers,
                timeout=settings["timeout_seconds"],
                verify=settings["verify_tls"],
            )
            response.raise_for_status()
            try:
                raw_suggestions = response.json()
            except ValueError:
                raw_suggestions = [
                    line for line in response.text.splitlines() if line.strip()
                ]
            suggestions = _normalize_searxng_suggestions(raw_suggestions)
            response_path = artifact_dir / "searxng_suggestions.json"
            _write_json(
                response_path,
                {"raw": raw_suggestions, "normalized": suggestions},
            )
            _record_file(manifest, response_path, "suggestions")
            payload = {
                "status": "success",
                "artifact_id": artifact_id,
                "artifact_dir": str(artifact_dir),
                "query": query,
                "suggestions": _compact_nested(suggestions),
            }
        else:
            params: Dict[str, Any] = {
                "q": query,
                "format": output_format,
                "categories": categories or "general",
                "language": language or "en-US",
                "pageno": page,
                "safesearch": safesearch,
            }
            if engines:
                params["engines"] = engines
            if time_range:
                params["time_range"] = time_range
            for option_name in SEARXNG_SEARCH_OPTION_KEYS:
                if option_name in options and options[option_name] is not None:
                    params[option_name] = options[option_name]
            response = requests.post(
                settings["base_url"] + "/search",
                data=params,
                headers=headers,
                timeout=settings["timeout_seconds"],
                verify=settings["verify_tls"],
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                redirect_target = response.headers.get("location")
                redirect_path = artifact_dir / "searxng_redirect.json"
                _write_json(
                    redirect_path, {"query": query, "location": redirect_target}
                )
                _record_file(manifest, redirect_path, "external_bang_redirect")
                payload = {
                    "status": "redirect",
                    "artifact_id": artifact_id,
                    "artifact_dir": str(artifact_dir),
                    "location": redirect_target,
                    "privacy_warning": "SearXNG external-bang redirects are not followed automatically.",
                }
            else:
                response.raise_for_status()
                suffix = {"json": "json", "csv": "csv", "rss": "xml"}[output_format]
                raw_path = artifact_dir / f"searxng_results.{suffix}"
                if output_format == "json":
                    data = _service_json(response)
                    _write_json(raw_path, data)
                    normalized = _normalize_searxng_results(data, max_results)
                    normalized_path = artifact_dir / "searxng_results.normalized.json"
                    _write_json(normalized_path, normalized)
                    _record_file(manifest, normalized_path, "normalized_results")
                    payload = {
                        "status": "success",
                        "artifact_id": artifact_id,
                        "artifact_dir": str(artifact_dir),
                        "query": data.get("query") or query,
                        "result_count": len(normalized),
                        "available_result_count": len(data.get("results") or []),
                        "service_reported_result_count": data.get("number_of_results"),
                        "results": normalized,
                        "answers": _compact_nested(data.get("answers") or []),
                        "corrections": _compact_nested(data.get("corrections") or []),
                        "suggestions": _compact_nested(data.get("suggestions") or []),
                        "infoboxes": _compact_nested(data.get("infoboxes") or []),
                        "unresponsive_engines": _compact_nested(
                            data.get("unresponsive_engines") or []
                        ),
                        "raw_file": str(raw_path),
                        "normalized_file": str(normalized_path),
                    }
                else:
                    _write_text(raw_path, response.text)
                    preview, truncated = _bounded_text(
                        response.text, DEFAULT_INLINE_CHARS
                    )
                    payload = {
                        "status": "success",
                        "artifact_id": artifact_id,
                        "artifact_dir": str(artifact_dir),
                        "query": query,
                        "output_format": output_format,
                        "path": str(raw_path),
                        "preview": preview,
                        "truncated": truncated,
                    }
                _record_file(manifest, raw_path, f"raw_{output_format}")

        manifest["summary"] = {
            "status": payload.get("status"),
            "operation": operation,
            "file_count": len(manifest["files"]),
            "result_count": payload.get("result_count"),
        }
        payload["manifest"] = _finish_manifest(artifact_dir, manifest)
        return _json_result("success", payload)
    except (OSError, ValueError, requests.RequestException) as exc:
        if isinstance(exc, requests.RequestException):
            reason, status_code = _request_failure_reason(exc)
        else:
            reason, status_code = type(exc).__name__.lower(), None
        hints = [
            "use operation='capabilities' to inspect live engines and categories",
            "ensure SearXNG search.formats enables json, csv, and rss",
            "remove engines or time_range to broaden the query",
        ]
        if status_code == 403:
            hints.insert(
                0, f"enable output format '{output_format}' in SearXNG settings.yml"
            )
        return _failure(
            artifact_dir=artifact_dir,
            artifact_id=artifact_id,
            manifest=manifest,
            stage="service_request",
            reason=reason,
            message=_exception_message(exc),
            retryable=reason in {"request_timeout", "connection_error", "rate_limited"}
            or bool(status_code and status_code >= 500),
            status_code=status_code,
            hints=hints,
        )
