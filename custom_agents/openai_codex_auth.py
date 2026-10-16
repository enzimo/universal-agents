"""OpenAI Codex OAuth credential resolution for the openai-codex provider."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode

from env_secrets import get_env_with_file_fallback
from file_locking import FileLockUnavailable, exclusive_file_lock
from runtime_settings.instance_layout import InstanceLayout

logger = logging.getLogger(__name__)

OPENAI_CODEX_PROVIDER = "openai-codex"
AUTH_STORE_LOCK_PURPOSE = "OpenAI Codex sign-in store"
DEFAULT_OPENAI_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_OPENAI_CODEX_REFRESH_SKEW_SECONDS = 120
DEFAULT_OPENAI_CODEX_REFRESH_TIMEOUT_SECONDS = 20.0
CODEX_BACKEND_USER_AGENT = "codex_cli_rs/0.0.0 (Universal Agents)"


class OpenAICodexAuthError(RuntimeError):
    """Raised when openai-codex credentials cannot be resolved or refreshed."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "openai_codex_auth_error",
        relogin_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.relogin_required = relogin_required


@dataclass(frozen=True)
class OpenAICodexSettings:
    base_url: str = DEFAULT_OPENAI_CODEX_BASE_URL
    auth_store_path: Optional[str] = None
    shared_codex_home: Optional[str] = None
    allow_shared_codex_auth_read: bool = True
    refresh_skew_seconds: int = DEFAULT_OPENAI_CODEX_REFRESH_SKEW_SECONDS
    refresh_timeout_seconds: float = DEFAULT_OPENAI_CODEX_REFRESH_TIMEOUT_SECONDS


@dataclass(frozen=True)
class OpenAICodexCredentials:
    provider: str
    base_url: str
    api_key: str
    source: str
    auth_mode: str
    default_headers: dict[str, str]
    auth_store_path: Optional[str] = None
    last_refresh: Optional[str] = None


def default_openai_codex_config() -> dict[str, Any]:
    return {
        "base_url": DEFAULT_OPENAI_CODEX_BASE_URL,
        "auth_store_path": None,
        "shared_codex_home": None,
        "allow_shared_codex_auth_read": True,
        "refresh_skew_seconds": DEFAULT_OPENAI_CODEX_REFRESH_SKEW_SECONDS,
        "refresh_timeout_seconds": DEFAULT_OPENAI_CODEX_REFRESH_TIMEOUT_SECONDS,
        "model_discovery": {
            "enabled": True,
            "cache_path": None,
            "cache_ttl_seconds": 3600,
            "client_version": "1.0.0",
        },
    }


def parse_bool_env_value(name: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean environment value for {name}: {value!r}")


def normalize_openai_codex_provider(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    if normalized in {
        "codex",
        "openai-codex",
        "openai codex",
        "openai codex oauth",
        "openai subscription",
        "openai-subscription",
        "openai subscription service",
        "chatgpt",
        "chatgpt codex",
    }:
        return OPENAI_CODEX_PROVIDER
    return normalized


def is_openai_codex_provider(value: Any) -> bool:
    return normalize_openai_codex_provider(value) == OPENAI_CODEX_PROVIDER


def resolve_openai_codex_settings(
    raw_settings: Mapping[str, Any] | None = None,
    *,
    instance_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> OpenAICodexSettings:
    env_mapping = env if env is not None else os.environ
    merged = default_openai_codex_config()
    if isinstance(raw_settings, Mapping):
        merged.update({k: v for k, v in raw_settings.items() if k != "model_discovery"})

    base_url = str(env_mapping.get("OPENAI_CODEX_BASE_URL") or merged["base_url"]).strip().rstrip("/")
    auth_store_path = env_mapping.get("OPENAI_CODEX_AUTH_STORE_PATH") or merged.get("auth_store_path")
    shared_codex_home = env_mapping.get("OPENAI_CODEX_SHARED_HOME") or merged.get("shared_codex_home")

    allow_shared = bool(merged.get("allow_shared_codex_auth_read", True))
    if env_mapping.get("OPENAI_CODEX_ALLOW_SHARED_AUTH_READ"):
        allow_shared = parse_bool_env_value(
            "OPENAI_CODEX_ALLOW_SHARED_AUTH_READ",
            env_mapping.get("OPENAI_CODEX_ALLOW_SHARED_AUTH_READ", ""),
        )

    refresh_skew = int(
        env_mapping.get("OPENAI_CODEX_REFRESH_SKEW_SECONDS")
        or merged.get("refresh_skew_seconds")
        or DEFAULT_OPENAI_CODEX_REFRESH_SKEW_SECONDS
    )
    refresh_timeout = float(
        env_mapping.get("OPENAI_CODEX_REFRESH_TIMEOUT_SECONDS")
        or merged.get("refresh_timeout_seconds")
        or DEFAULT_OPENAI_CODEX_REFRESH_TIMEOUT_SECONDS
    )
    resolved_auth_store_path = resolve_openai_codex_auth_store_path(
        auth_store_path,
        instance_root=instance_root,
    )

    return OpenAICodexSettings(
        base_url=base_url or DEFAULT_OPENAI_CODEX_BASE_URL,
        auth_store_path=str(resolved_auth_store_path),
        shared_codex_home=str(Path(shared_codex_home).expanduser().resolve()) if shared_codex_home else None,
        allow_shared_codex_auth_read=allow_shared,
        refresh_skew_seconds=max(0, refresh_skew),
        refresh_timeout_seconds=max(5.0, refresh_timeout),
    )


def resolve_openai_codex_auth_store_path(
    configured_path: Any,
    *,
    instance_root: str | Path | None = None,
) -> Path:
    raw = str(configured_path or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path.resolve()
        if instance_root:
            return (Path(instance_root).expanduser().resolve() / path).resolve()
        return path.resolve()
    if instance_root:
        return InstanceLayout.at(instance_root).openai_codex_auth_path
    state_home = os.getenv("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return (base / "team_nats_agents" / "openai_codex_auth.json").resolve()


def codex_backend_headers(access_token: str) -> dict[str, str]:
    headers = {
        "User-Agent": CODEX_BACKEND_USER_AGENT,
        "originator": "codex_cli_rs",
    }
    account_id = _chatgpt_account_id_from_token(access_token)
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def resolve_openai_codex_credentials(
    raw_settings: Mapping[str, Any] | OpenAICodexSettings | None = None,
    *,
    instance_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
) -> OpenAICodexCredentials:
    env_mapping = env if env is not None else os.environ
    settings = (
        raw_settings
        if isinstance(raw_settings, OpenAICodexSettings)
        else resolve_openai_codex_settings(
            raw_settings,
            instance_root=instance_root,
            env=env_mapping,
        )
    )
    auth_store_path = settings.auth_store_path or str(
        resolve_openai_codex_auth_store_path(
            None,
            instance_root=instance_root,
        )
    )

    env_token = get_env_with_file_fallback("OPENAI_CODEX_ACCESS_TOKEN", env=env_mapping)
    env_source = "OPENAI_CODEX_ACCESS_TOKEN"
    if not env_token:
        env_token = get_env_with_file_fallback("CODEX_ACCESS_TOKEN", env=env_mapping)
        env_source = "CODEX_ACCESS_TOKEN"
    if env_token:
        _raise_if_token_expired(env_token, source=env_source)
        return OpenAICodexCredentials(
            provider=OPENAI_CODEX_PROVIDER,
            base_url=settings.base_url,
            api_key=env_token,
            source=env_source,
            auth_mode="chatgpt",
            default_headers=codex_backend_headers(env_token),
        )

    owned = _read_owned_auth_store(Path(auth_store_path))
    if owned:
        tokens = dict(owned.get("tokens") or {})
        access_token = str(tokens.get("access_token") or "").strip()
        if access_token:
            should_refresh = force_refresh or (
                refresh_if_expiring and access_token_is_expiring(access_token, settings.refresh_skew_seconds)
            )
            if should_refresh:
                with _auth_store_lock(Path(auth_store_path)):
                    reread = _read_owned_auth_store(Path(auth_store_path))
                    if reread:
                        tokens = dict(reread.get("tokens") or tokens)
                        access_token = str(tokens.get("access_token") or access_token).strip()
                    if force_refresh or (
                        refresh_if_expiring and access_token_is_expiring(access_token, settings.refresh_skew_seconds)
                    ):
                        tokens = refresh_openai_codex_tokens(
                            tokens,
                            timeout_seconds=settings.refresh_timeout_seconds,
                        )
                        _write_owned_auth_store(Path(auth_store_path), tokens)
                        owned = _read_owned_auth_store(Path(auth_store_path)) or owned
                        access_token = str(tokens.get("access_token") or "").strip()
            _raise_if_token_expired(access_token, source="openai-codex auth store")
            return OpenAICodexCredentials(
                provider=OPENAI_CODEX_PROVIDER,
                base_url=settings.base_url,
                api_key=access_token,
                source="auth_store",
                auth_mode=str(owned.get("auth_mode") or "chatgpt"),
                default_headers=codex_backend_headers(access_token),
                auth_store_path=auth_store_path,
                last_refresh=str(owned.get("last_refresh") or "") or None,
            )

    if settings.allow_shared_codex_auth_read:
        shared_tokens, shared_path = read_shared_codex_tokens(
            configured_codex_home=settings.shared_codex_home,
            env=env_mapping,
        )
        if shared_tokens:
            access_token = str(shared_tokens.get("access_token") or "").strip()
            if access_token and not access_token_is_expiring(access_token, 0):
                return OpenAICodexCredentials(
                    provider=OPENAI_CODEX_PROVIDER,
                    base_url=settings.base_url,
                    api_key=access_token,
                    source=f"shared_codex_auth:{shared_path}",
                    auth_mode="chatgpt",
                    default_headers=codex_backend_headers(access_token),
                )

    raise OpenAICodexAuthError(
        "OpenAI Codex OAuth credentials are not available. Run `codex login` or "
        "`codex login --device-auth`, then use `assistant_cli.py openai-codex-import` "
        "for long-running agents, or set OPENAI_CODEX_ACCESS_TOKEN.",
        code="openai_codex_credentials_unavailable",
        relogin_required=True,
    )


def import_shared_codex_auth(
    *,
    codex_home: str | Path,
    raw_settings: Mapping[str, Any] | OpenAICodexSettings | None = None,
    instance_root: str | Path | None = None,
    target_auth_store_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    settings = (
        raw_settings
        if isinstance(raw_settings, OpenAICodexSettings)
        else resolve_openai_codex_settings(
            raw_settings,
            instance_root=instance_root,
            env=env,
        )
    )
    auth_store_path = settings.auth_store_path or str(
        resolve_openai_codex_auth_store_path(
            None,
            instance_root=instance_root,
        )
    )
    tokens, source_path = read_shared_codex_tokens(configured_codex_home=str(codex_home), env=env)
    if not tokens:
        raise OpenAICodexAuthError(
            f"No file-based Codex OAuth tokens found at {Path(codex_home).expanduser() / 'auth.json'}. "
            "Run Codex CLI login with file credential storage first.",
            code="openai_codex_import_missing_tokens",
            relogin_required=True,
        )
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise OpenAICodexAuthError(
            f"Codex auth file {source_path} must contain both access_token and refresh_token.",
            code="openai_codex_import_incomplete_tokens",
            relogin_required=True,
        )
    _raise_if_token_expired(access_token, source=str(source_path))
    target = (
        Path(target_auth_store_path).expanduser().resolve()
        if target_auth_store_path is not None
        else Path(auth_store_path)
    )
    with _auth_store_lock(target):
        _write_owned_auth_store(target, tokens, source=f"codex_cli_import:{source_path}")
    return target


def read_shared_codex_tokens(
    *,
    configured_codex_home: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Optional[dict[str, str]], Optional[Path]]:
    env_mapping = env if env is not None else os.environ
    homes: list[Path] = []
    if configured_codex_home:
        homes.append(Path(configured_codex_home).expanduser())
    if env_mapping.get("CODEX_HOME"):
        homes.append(Path(env_mapping["CODEX_HOME"]).expanduser())
    homes.append(Path.home() / ".codex")
    seen: set[Path] = set()
    for home in homes:
        path = (home / "auth.json").resolve()
        if path in seen:
            continue
        seen.add(path)
        tokens = _read_codex_auth_json(path)
        if tokens:
            return tokens, path
    return None, None


def refresh_openai_codex_tokens(tokens: Mapping[str, Any], *, timeout_seconds: float = 20.0) -> dict[str, str]:
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not refresh_token:
        raise OpenAICodexAuthError(
            "OpenAI Codex auth store is missing refresh_token. Re-import a fresh Codex CLI login.",
            code="openai_codex_missing_refresh_token",
            relogin_required=True,
        )
    data = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CODEX_OAUTH_CLIENT_ID,
        }
    ).encode("utf-8")
    request = urllib_request.Request(
        CODEX_OAUTH_TOKEN_URL,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=max(5.0, float(timeout_seconds))) as response:
            status = response.status
            body = response.read(128 * 1024).decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        status = exc.code
        body = exc.read(128 * 1024).decode("utf-8", errors="replace")
        _raise_refresh_http_error(status, body)
    except urllib_error.URLError as exc:
        raise OpenAICodexAuthError(
            f"OpenAI Codex token refresh failed: {exc.reason}",
            code="openai_codex_refresh_network_error",
        ) from exc
    if status != 200:
        _raise_refresh_http_error(status, body)
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        raise OpenAICodexAuthError(
            "OpenAI Codex token refresh returned invalid JSON.",
            code="openai_codex_refresh_invalid_json",
            relogin_required=True,
        ) from exc
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise OpenAICodexAuthError(
            "OpenAI Codex token refresh response was missing access_token.",
            code="openai_codex_refresh_missing_access_token",
            relogin_required=True,
        )
    return {
        "access_token": access_token,
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
    }


def access_token_is_expiring(access_token: str, skew_seconds: int) -> bool:
    claims = decode_jwt_claims(access_token)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return float(exp) <= time.time() + max(0, int(skew_seconds))


def decode_jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = str(token).split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _chatgpt_account_id_from_token(access_token: str) -> Optional[str]:
    claims = decode_jwt_claims(access_token)
    auth_claims = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claims, Mapping):
        account_id = auth_claims.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id.strip():
            return account_id.strip()
    return None


def _raise_if_token_expired(access_token: str, *, source: str) -> None:
    if access_token_is_expiring(access_token, 0):
        raise OpenAICodexAuthError(
            f"OpenAI Codex access token from {source} is expired. Refresh with Codex CLI and re-import.",
            code="openai_codex_access_token_expired",
            relogin_required=True,
        )


def _read_codex_auth_json(path: Path) -> Optional[dict[str, str]]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Failed to read Codex auth file at %s", path, exc_info=True)
        return None
    tokens = payload.get("tokens")
    if not isinstance(tokens, Mapping):
        return None
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return None
    result = {"access_token": access_token.strip()}
    if isinstance(refresh_token, str) and refresh_token.strip():
        result["refresh_token"] = refresh_token.strip()
    return result


def _read_owned_auth_store(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("OpenAI Codex auth store at %s is unreadable.", path)
        return None
    if not isinstance(payload, Mapping):
        return None
    tokens = payload.get("tokens")
    if not isinstance(tokens, Mapping):
        return None
    return dict(payload)


def _write_owned_auth_store(path: Path, tokens: Mapping[str, Any], *, source: str = "refresh") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {
        "version": 1,
        "provider": OPENAI_CODEX_PROVIDER,
        "auth_mode": "chatgpt",
        "source": source,
        "last_refresh": now,
        "tokens": {
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
        },
    }
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        with contextlib.suppress(OSError):
            tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


@contextlib.contextmanager
def _auth_store_lock(path: Path) -> Iterator[None]:
    """Hold the sign-in store's lock while its tokens are refreshed or replaced.

    Agent processes, the onboarding server and ``assistant_cli.py`` share this
    store, and a Codex refresh token works only once, so two unlocked refreshes
    can sign the whole team out. A lock failure therefore stops the work
    (``file_locking``) and is reported as an ``OpenAICodexAuthError`` that does
    not ask for a new sign-in, since signing in again cannot fix the folder.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(exclusive_file_lock(path, purpose=AUTH_STORE_LOCK_PURPOSE))
        except FileLockUnavailable as exc:
            raise OpenAICodexAuthError(
                f"{exc} Signing in again will not help until this is fixed.",
                code="openai_codex_auth_store_lock_unavailable",
                relogin_required=False,
            ) from exc
        yield


def _raise_refresh_http_error(status: int, body: str) -> None:
    code = "openai_codex_refresh_failed"
    message = f"OpenAI Codex token refresh failed with HTTP {status}."
    relogin_required = status in {400, 401, 403}
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError:
        payload = {}
    error_payload = payload.get("error") if isinstance(payload, Mapping) else None
    if isinstance(error_payload, Mapping):
        nested_code = error_payload.get("code") or error_payload.get("type")
        nested_message = error_payload.get("message")
        if isinstance(nested_code, str) and nested_code.strip():
            code = nested_code.strip()
        if isinstance(nested_message, str) and nested_message.strip():
            message = f"OpenAI Codex token refresh failed: {nested_message.strip()}"
    elif isinstance(error_payload, str) and error_payload.strip():
        code = error_payload.strip()
        description = payload.get("error_description") or payload.get("message")
        if isinstance(description, str) and description.strip():
            message = f"OpenAI Codex token refresh failed: {description.strip()}"
    if status == 429:
        raise OpenAICodexAuthError(
            "OpenAI Codex token refresh was rate limited. Credentials may still be valid; retry later.",
            code="openai_codex_refresh_rate_limited",
            relogin_required=False,
        )
    if code in {"invalid_grant", "invalid_token", "invalid_request", "refresh_token_reused"}:
        relogin_required = True
    if code == "refresh_token_reused":
        message = (
            "OpenAI Codex refresh token was already consumed by another client. "
            "Run Codex CLI login again with a dedicated CODEX_HOME, then re-import."
        )
    raise OpenAICodexAuthError(message, code=code, relogin_required=relogin_required)
