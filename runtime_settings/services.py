"""Services configuration and contracts."""

from __future__ import annotations
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse
from pydantic import BaseModel, Field, field_validator, model_validator
from default_values import WEB_SERVICES_DEFAULTS


def _normalize_internal_service_base_url(
    value: Any, *, field_name: str, default: str
) -> str:
    normalized = str(value or default).strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field_name} must be an absolute http(s) URL.")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not contain embedded credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must not contain a query or fragment.")
    return normalized


CRAWL4AI_BASE_URL_ENV = "CRAWL4AI_BASE_URL"
# The setting a built-in web API tool takes its address from
# (``service_url_setting`` in ua_definitions/access/tools/broker.yaml).
CRAWL4AI_URL_SETTING = "web_services.crawl4ai.base_url"
# Crawl4AI settings removed when its requests moved to the secure tool broker
# (the broker adds the linked token and always verifies TLS certificates) and
# the unused custom crawl tool was deleted; the Crawl4AI MCP connection is the
# crawl tool. Saved instance files hold them with their former defaults, which
# are dropped (as runtime_settings.legacy_layout does for removed locations);
# any other value is refused by name, because it would silently stop working.
REMOVED_CRAWL4AI_DEFAULTS: dict[str, Any] = {
    "verify_tls": True,
    "expected_version": "0.9.2",
    "allow_execute_js": False,
    "job_poll_seconds": 2.0,
    "job_timeout_seconds": 300.0,
}
REMOVED_CRAWL4AI_GUIDANCE = (
    "Crawl4AI requests go through the secure tool broker, which always verifies TLS "
    "certificates, and crawling uses the Crawl4AI MCP connection. Delete the key."
)


def _holds_former_default(value: Any, default: Any) -> bool:
    if isinstance(default, bool):
        return str(value).strip().lower() == str(default).lower()
    if isinstance(default, float):
        try:
            return float(value) == default
        except (TypeError, ValueError):
            return False
    return str(value).strip() == str(default)


# Keys of web search and crawling services are linked accounts in the
# credential store now (http:exa, http:brave-search, http:crawl4ai); the broker
# adds them to each request, so no process reads them from the environment.
# Each removed variable -> the /link commands that replace it.
REMOVED_LINKED_KEY_ENV_VARS: dict[str, str] = {
    "EXA_API_KEY": "/link http:exa team",
    "BRAVE_SEARCH_API_KEY": "/link http:brave-search team",
    "CRAWL4AI_API_TOKEN": "/link http:crawl4ai team and /link mcp:crawl4ai team",
}
# Crawl4AI switches removed with REMOVED_CRAWL4AI_KEYS.
REMOVED_CRAWL4AI_ENV_VARS = ("CRAWL4AI_VERIFY_TLS", "CRAWL4AI_EXECUTE_JS_ENABLED")


def reject_removed_web_service_env_vars(env: Mapping[str, str] | None = None) -> None:
    """Refuse to start while a removed web service key or switch is in the environment.

    A key left in the environment (or in team.env, or as a ``*_FILE`` secret)
    would otherwise be ignored while the operator believes it is used.
    """

    source = os.environ if env is None else env
    keys = [name for name in REMOVED_LINKED_KEY_ENV_VARS if name in source or f"{name}_FILE" in source]
    switches = [name for name in REMOVED_CRAWL4AI_ENV_VARS if name in source]
    present = [name for key in keys for name in (key, f"{key}_FILE") if name in source]
    if not present and not switches:
        return
    parts = [f"Removed environment variable(s) are set: {', '.join([*present, *switches])}. Unset them."]
    if keys:
        steps = "; ".join(f"{REMOVED_LINKED_KEY_ENV_VARS[name]} for {name}" for name in keys)
        parts.append(
            "Web search and crawling keys are kept in the credential store now, and the broker adds them "
            f"to each request. A team administrator links each key in chat ({steps}); onboarding also "
            "stores the Exa and Brave keys typed there."
        )
    if switches:
        parts.append(
            "The Crawl4AI switches have no effect any more: the secure tool broker sends Crawl4AI requests "
            "and always verifies TLS certificates, and crawling uses the Crawl4AI MCP connection."
        )
    raise ValueError(" ".join(parts))


def removed_key_references(entry: Mapping[str, Any]) -> list[str]:
    """``<header or variable>: ${NAME}`` for each removed key an MCP server entry still references.

    The shipped Crawl4AI connection used to send ``Bearer ${CRAWL4AI_API_TOKEN}``;
    that variable is gone, so the connection declares a credential slot
    (``credential_injections``) and gets the token from its account link.
    """

    found: list[str] = []
    for section in ("headers", "env"):
        values = entry.get(section)
        for target, value in (values.items() if isinstance(values, Mapping) else ()):
            found += [
                f"{target}: ${{{name}}}"
                for name in REMOVED_LINKED_KEY_ENV_VARS
                if f"${{{name}}}" in str(value)
            ]
    return found


class Crawl4AIServiceSettings(BaseModel):
    """Where this instance's Crawl4AI server is; its token is a linked account (http:crawl4ai)."""

    base_url: str = WEB_SERVICES_DEFAULTS.crawl4ai.value["base_url"]
    timeout_seconds: float = WEB_SERVICES_DEFAULTS.crawl4ai.value["timeout_seconds"]

    @model_validator(mode="before")
    @classmethod
    def _drop_or_refuse_removed_keys(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        present = [key for key in REMOVED_CRAWL4AI_DEFAULTS if key in value]
        customized = [key for key in present if not _holds_former_default(value[key], REMOVED_CRAWL4AI_DEFAULTS[key])]
        if customized:
            names = ", ".join(f"web_services.crawl4ai.{key}={value[key]}" for key in customized)
            raise ValueError(f"{names}: this setting has been removed. {REMOVED_CRAWL4AI_GUIDANCE}")
        return {key: item for key, item in value.items() if key not in present}

    @field_validator("base_url", mode="before")
    @classmethod
    def _normalize_base_url(cls, value: Any) -> str:
        return _normalize_internal_service_base_url(
            value,
            field_name=CRAWL4AI_URL_SETTING,
            default=WEB_SERVICES_DEFAULTS.crawl4ai.value["base_url"],
        )

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _normalize_positive_seconds(cls, value: Any) -> float:
        parsed = float(value)
        if parsed <= 0:
            raise ValueError("web_services.crawl4ai.timeout_seconds must be > 0.")
        return parsed


def crawl4ai_base_url(configured: Any, env: Mapping[str, str] | None = None) -> str:
    """The Crawl4AI server address: ``CRAWL4AI_BASE_URL`` when set, else the configured one.

    The agents' Crawl4AI tool and the broker's tool catalog both use this, so
    the address the tool calls is the one the broker matches to http:crawl4ai.
    """

    override = str((os.environ if env is None else env).get(CRAWL4AI_BASE_URL_ENV) or "").strip()
    return _normalize_internal_service_base_url(
        override or configured,
        field_name=CRAWL4AI_URL_SETTING,
        default=WEB_SERVICES_DEFAULTS.crawl4ai.value["base_url"],
    )


class SearXNGServiceSettings(BaseModel):
    base_url: str = WEB_SERVICES_DEFAULTS.searxng.value["base_url"]
    verify_tls: bool = WEB_SERVICES_DEFAULTS.searxng.value["verify_tls"]
    timeout_seconds: float = WEB_SERVICES_DEFAULTS.searxng.value["timeout_seconds"]
    default_num_results: int = WEB_SERVICES_DEFAULTS.searxng.value[
        "default_num_results"
    ]

    @field_validator("base_url", mode="before")
    @classmethod
    def _normalize_base_url(cls, value: Any) -> str:
        return _normalize_internal_service_base_url(
            value,
            field_name="web_services.searxng.base_url",
            default=WEB_SERVICES_DEFAULTS.searxng.value["base_url"],
        )

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _normalize_timeout_seconds(cls, value: Any) -> float:
        parsed = float(value)
        if parsed <= 0:
            raise ValueError("web_services.searxng.timeout_seconds must be > 0.")
        return parsed

    @field_validator("default_num_results", mode="before")
    @classmethod
    def _normalize_default_num_results(cls, value: Any) -> int:
        parsed = int(value)
        if parsed < 1 or parsed > 30:
            raise ValueError(
                "web_services.searxng.default_num_results must be between 1 and 30."
            )
        return parsed


class WebServicesSettings(BaseModel):
    crawl4ai: Crawl4AIServiceSettings = Field(default_factory=Crawl4AIServiceSettings)
    searxng: SearXNGServiceSettings = Field(default_factory=SearXNGServiceSettings)


def web_service_urls(settings: WebServicesSettings, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Configured addresses for the tool catalog's ``service_url_setting`` entries."""

    return {CRAWL4AI_URL_SETTING: crawl4ai_base_url(settings.crawl4ai.base_url, env)}
