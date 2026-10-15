"""Model discovery for the OpenAI Codex OAuth provider."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None  # type: ignore[assignment]

from custom_agents.openai_codex_auth import (
    DEFAULT_OPENAI_CODEX_BASE_URL,
    OpenAICodexSettings,
    codex_backend_headers,
    default_openai_codex_config,
    resolve_openai_codex_auth_store_path,
)

logger = logging.getLogger(__name__)

DEFAULT_CODEX_MODELS = [
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
]


@dataclass(frozen=True)
class OpenAICodexModelDiscoverySettings:
    enabled: bool = True
    base_url: str = DEFAULT_OPENAI_CODEX_BASE_URL
    cache_path: Optional[str] = None
    cache_ttl_seconds: int = 3600
    client_version: str = "1.0.0"
    shared_codex_home: Optional[str] = None


def resolve_openai_codex_model_discovery_settings(
    raw_settings: Mapping[str, Any] | None = None,
    *,
    auth_settings: OpenAICodexSettings | None = None,
    instance_root: str | Path | None = None,
) -> OpenAICodexModelDiscoverySettings:
    defaults = default_openai_codex_config()
    provider_settings = dict(raw_settings or {})
    discovery = dict(defaults["model_discovery"])
    if isinstance(provider_settings.get("model_discovery"), Mapping):
        discovery.update(provider_settings["model_discovery"])
    base_url = str(
        (auth_settings.base_url if auth_settings else None)
        or provider_settings.get("base_url")
        or DEFAULT_OPENAI_CODEX_BASE_URL
    ).strip().rstrip("/")
    cache_path = discovery.get("cache_path")
    if cache_path:
        resolved_cache_path = _resolve_path(cache_path, instance_root=instance_root)
    else:
        auth_path = resolve_openai_codex_auth_store_path(
            provider_settings.get("auth_store_path") if auth_settings is None else auth_settings.auth_store_path,
            instance_root=instance_root,
        )
        resolved_cache_path = auth_path.with_name("openai_codex_models_cache.json")
    return OpenAICodexModelDiscoverySettings(
        enabled=bool(discovery.get("enabled", True)),
        base_url=base_url or DEFAULT_OPENAI_CODEX_BASE_URL,
        cache_path=str(resolved_cache_path),
        cache_ttl_seconds=max(0, int(discovery.get("cache_ttl_seconds") or 0)),
        client_version=str(discovery.get("client_version") or "1.0.0"),
        shared_codex_home=(
            auth_settings.shared_codex_home
            if auth_settings is not None
            else provider_settings.get("shared_codex_home")
        ),
    )


def get_codex_model_ids(
    settings: OpenAICodexModelDiscoverySettings | Mapping[str, Any] | None = None,
    *,
    access_token: str | None = None,
    force_refresh: bool = False,
) -> list[str]:
    resolved = (
        settings
        if isinstance(settings, OpenAICodexModelDiscoverySettings)
        else resolve_openai_codex_model_discovery_settings(settings)
    )
    cache_path = (
        Path(resolved.cache_path).expanduser().resolve()
        if resolved.cache_path
        else resolve_openai_codex_auth_store_path(None).with_name(
            "openai_codex_models_cache.json"
        )
    )
    if resolved.enabled and access_token:
        try:
            models = _fetch_live_models(resolved, access_token=access_token)
            if models:
                _write_model_cache(cache_path, models)
                return models
        except Exception as exc:
            logger.warning("OpenAI Codex live model discovery failed; falling back to cache/defaults: %s", exc)
    if not force_refresh:
        cached = _read_model_cache(
            cache_path,
            ttl_seconds=resolved.cache_ttl_seconds,
        )
        if cached:
            return cached
    stale_cached = _read_model_cache(cache_path, ttl_seconds=None)
    if stale_cached:
        return stale_cached
    cli_models = _read_codex_cli_models(resolved.shared_codex_home)
    if cli_models:
        return cli_models
    return list(DEFAULT_CODEX_MODELS)


def _fetch_live_models(settings: OpenAICodexModelDiscoverySettings, *, access_token: str) -> list[str]:
    query = urllib_parse.urlencode({"client_version": settings.client_version})
    url = f"{settings.base_url.rstrip('/')}/models?{query}"
    request = urllib_request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            **codex_backend_headers(access_token),
        },
        method="GET",
    )
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            body = response.read(512 * 1024).decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}") from exc
    payload = json.loads(body or "{}")
    return _extract_model_ids(payload)


def _extract_model_ids(payload: Mapping[str, Any]) -> list[str]:
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    visible: list[tuple[int, int, str]] = []
    for index, model in enumerate(models):
        if isinstance(model, str) and model.strip():
            visible.append((10_000 + index, index, model.strip()))
            continue
        if not isinstance(model, Mapping):
            continue
        slug = model.get("slug") or model.get("id") or model.get("model")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = str(model.get("visibility") or "").strip().lower()
        if visibility in {"hidden", "hide"}:
            continue
        priority = model.get("priority")
        try:
            parsed_priority = int(priority)
        except (TypeError, ValueError):
            parsed_priority = 10_000 + index
        visible.append((parsed_priority, index, slug.strip()))
    visible.sort(key=lambda item: (item[0], item[1], item[2]))
    return _dedupe([item[2] for item in visible])


def _write_model_cache(path: Path, models: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at_epoch_seconds": time.time(),
        "models": _dedupe(models),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_model_cache(path: Path, *, ttl_seconds: int | None) -> list[str]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if ttl_seconds is not None and ttl_seconds > 0:
        fetched_at = payload.get("fetched_at_epoch_seconds")
        if isinstance(fetched_at, (int, float)) and fetched_at + ttl_seconds < time.time():
            return []
    models = payload.get("models")
    if isinstance(models, list):
        return _dedupe(str(model).strip() for model in models if str(model).strip())
    return _extract_model_ids(payload) if isinstance(payload, Mapping) else []


def _read_codex_cli_models(configured_codex_home: str | Path | None) -> list[str]:
    homes: list[Path] = []
    if configured_codex_home:
        homes.append(Path(configured_codex_home).expanduser())
    if os.getenv("CODEX_HOME"):
        homes.append(Path(os.getenv("CODEX_HOME", "")).expanduser())
    homes.append(Path.home() / ".codex")
    discovered: list[str] = []
    seen_homes: set[Path] = set()
    for home in homes:
        resolved_home = home.resolve()
        if resolved_home in seen_homes:
            continue
        seen_homes.add(resolved_home)
        discovered.extend(_read_codex_cli_model_cache(resolved_home / "models_cache.json"))
        config_default = _read_codex_cli_default_model(resolved_home / "config.toml")
        if config_default:
            discovered.append(config_default)
    return _dedupe(discovered)


def _read_codex_cli_model_cache(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(payload, Mapping):
        if isinstance(payload.get("models"), list):
            return _extract_model_ids(payload)
        if all(isinstance(key, str) for key in payload.keys()):
            return _dedupe(str(key) for key in payload.keys())
    if isinstance(payload, list):
        return _extract_model_ids({"models": payload})
    return []


def _read_codex_cli_default_model(path: Path) -> Optional[str]:
    if not path.is_file() or tomllib is None:
        return None
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    model = payload.get("model") if isinstance(payload, Mapping) else None
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _resolve_path(
    path_value: Any,
    *,
    instance_root: str | Path | None = None,
) -> Path:
    path = Path(str(path_value or "")).expanduser()
    if path.is_absolute():
        return path.resolve()
    if instance_root:
        return (Path(instance_root).expanduser().resolve() / path).resolve()
    return path.resolve()


def _dedupe(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
