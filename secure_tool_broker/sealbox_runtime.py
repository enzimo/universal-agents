"""Runtime helpers for managed Sealbox service wiring."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from secure_tool_broker.defaults import (
    DEFAULT_SEALBOX_API_VERSION,
    DEFAULT_SEALBOX_AUTO_INITIALIZE,
    DEFAULT_SEALBOX_BIND_HOST,
    DEFAULT_SEALBOX_BIND_PORT,
    DEFAULT_SEALBOX_ENABLED,
    DEFAULT_SEALBOX_MANAGE_SERVICE,
    DEFAULT_SEALBOX_READY_TIMEOUT_SECONDS,
    DEFAULT_SEALBOX_ROOT_TOKEN_FILE,
    DEFAULT_SEALBOX_SERVICE_URL,
    DEFAULT_SEALBOX_STORE_PATH,
    DEFAULT_SEALBOX_TENANT_MATERIAL_DIR,
)
from secure_tool_broker.sealbox_cli import chmod_private_file


@dataclass(frozen=True)
class SealboxRuntimeConfig:
    enabled: bool
    manage_service: bool
    auto_initialize: bool
    bind_host: str
    bind_port: int
    service_url: str | None
    store_path: Path
    root_token_file: Path
    tenant_material_dir: Path
    api_version: str
    ready_timeout_seconds: int

    @property
    def url(self) -> str:
        return self.service_url or f"http://{self.bind_host}:{self.bind_port}"

    @property
    def listen_addr(self) -> str:
        return f"{self.bind_host}:{self.bind_port}"


def _setting(settings: object, name: str, default: object) -> object:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _env_bool(env: Mapping[str, str], key: str) -> bool | None:
    raw = str(env.get(key, "")).strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean environment value for {key}: {env.get(key)!r}")


def _coerce_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid Sealbox boolean setting: {value!r}")


def _env_int(env: Mapping[str, str], key: str) -> int | None:
    raw = str(env.get(key, "")).strip()
    if not raw:
        return None
    return int(raw)


def _env_string(env: Mapping[str, str], key: str) -> str | None:
    raw = str(env.get(key, "")).strip()
    return raw or None


def _resolve_path(raw: object, *, base_dir: Path) -> Path:
    path = Path(str(raw or "")).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def resolve_sealbox_runtime_config(
    settings: object,
    *,
    broker_credentials_dir: str | Path,
    env: Mapping[str, str] | None = None,
) -> SealboxRuntimeConfig:
    base_dir = Path(broker_credentials_dir).expanduser().resolve()
    env_mapping = dict(env or {})
    enabled = _setting(settings, "enabled", DEFAULT_SEALBOX_ENABLED)
    manage_service = _setting(
        settings,
        "manage_service",
        DEFAULT_SEALBOX_MANAGE_SERVICE,
    )
    auto_initialize = _setting(
        settings,
        "auto_initialize",
        DEFAULT_SEALBOX_AUTO_INITIALIZE,
    )
    bind_host = _setting(settings, "bind_host", DEFAULT_SEALBOX_BIND_HOST)
    bind_port = _setting(settings, "bind_port", DEFAULT_SEALBOX_BIND_PORT)
    store_path = _setting(settings, "store_path", DEFAULT_SEALBOX_STORE_PATH)
    service_url = _setting(settings, "service_url", DEFAULT_SEALBOX_SERVICE_URL)
    root_token_file = _setting(
        settings,
        "root_token_file",
        DEFAULT_SEALBOX_ROOT_TOKEN_FILE,
    )
    tenant_material_dir = _setting(
        settings,
        "tenant_material_dir",
        DEFAULT_SEALBOX_TENANT_MATERIAL_DIR,
    )
    api_version = _setting(settings, "api_version", DEFAULT_SEALBOX_API_VERSION)
    ready_timeout_seconds = _setting(
        settings,
        "ready_timeout_seconds",
        DEFAULT_SEALBOX_READY_TIMEOUT_SECONDS,
    )
    if env_mapping:
        env_enabled = _env_bool(env_mapping, "SECURE_TOOL_BROKER_SEALBOX_ENABLED")
        env_manage_service = _env_bool(
            env_mapping, "SECURE_TOOL_BROKER_SEALBOX_MANAGE_SERVICE"
        )
        env_auto_initialize = _env_bool(
            env_mapping, "SECURE_TOOL_BROKER_SEALBOX_AUTO_INITIALIZE"
        )
        env_bind_port = _env_int(env_mapping, "SEALBOX_BIND_PORT")
        env_ready_timeout = _env_int(env_mapping, "SEALBOX_READY_TIMEOUT_SECONDS")
        enabled = env_enabled if env_enabled is not None else enabled
        manage_service = (
            env_manage_service if env_manage_service is not None else manage_service
        )
        auto_initialize = (
            env_auto_initialize if env_auto_initialize is not None else auto_initialize
        )
        bind_host = _env_string(env_mapping, "SEALBOX_BIND_HOST") or bind_host
        bind_port = env_bind_port if env_bind_port is not None else bind_port
        store_path = _env_string(env_mapping, "SEALBOX_STORE_PATH") or store_path
        service_url = _env_string(env_mapping, "SEALBOX_URL") or service_url
        root_token_file = (
            _env_string(env_mapping, "SEALBOX_ROOT_TOKEN_FILE") or root_token_file
        )
        tenant_material_dir = (
            _env_string(env_mapping, "SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR")
            or tenant_material_dir
        )
        api_version = _env_string(env_mapping, "SEALBOX_API_VERSION") or api_version
        ready_timeout_seconds = (
            env_ready_timeout
            if env_ready_timeout is not None
            else ready_timeout_seconds
        )
    return SealboxRuntimeConfig(
        enabled=_coerce_bool(enabled, default=True),
        manage_service=_coerce_bool(manage_service, default=True),
        auto_initialize=_coerce_bool(auto_initialize, default=True),
        bind_host=str(bind_host or DEFAULT_SEALBOX_BIND_HOST).strip(),
        bind_port=int(bind_port),
        service_url=str(service_url or "").strip().rstrip("/") or None,
        store_path=_resolve_path(store_path, base_dir=base_dir),
        root_token_file=_resolve_path(root_token_file, base_dir=base_dir),
        tenant_material_dir=_resolve_path(tenant_material_dir, base_dir=base_dir),
        api_version=str(api_version or DEFAULT_SEALBOX_API_VERSION).strip().lower(),
        ready_timeout_seconds=int(ready_timeout_seconds),
    )


def sealbox_cli_env(
    runtime: SealboxRuntimeConfig,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env_mapping = dict(env or os.environ)
    env_mapping.setdefault("SEALBOX_URL", runtime.url)
    env_mapping["SEALBOX_ROOT_TOKEN_FILE"] = str(runtime.root_token_file)
    env_mapping["SEALBOX_TOKEN_FILE"] = str(runtime.root_token_file)
    env_mapping["SEALBOX_API_VERSION"] = runtime.api_version
    env_mapping.setdefault("SEALBOX_OUTPUT_FORMAT", "json")
    return env_mapping


def sealbox_server_env(
    runtime: SealboxRuntimeConfig,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env_mapping = dict(env or os.environ)
    env_mapping["STORE_PATH"] = str(runtime.store_path)
    env_mapping["AUTH_TOKEN_FILE"] = str(runtime.root_token_file)
    env_mapping["LISTEN_ADDR"] = runtime.listen_addr
    return env_mapping


def apply_sealbox_cli_env(
    runtime: SealboxRuntimeConfig,
    *,
    env: MutableMapping[str, str],
) -> None:
    env.setdefault("SEALBOX_URL", runtime.url)
    env["SEALBOX_ROOT_TOKEN_FILE"] = str(runtime.root_token_file)
    env["SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR"] = str(
        runtime.tenant_material_dir
    )
    env["SEALBOX_API_VERSION"] = runtime.api_version
    env.setdefault("SEALBOX_OUTPUT_FORMAT", "json")


def chmod_public_file(path: Path) -> None:
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def ensure_sealbox_token_file(path: Path, *, rotate: bool = False) -> bool:
    if path.exists() and not rotate:
        chmod_private_file(path)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{secrets.token_urlsafe(32)}\n", encoding="utf-8")
    chmod_private_file(path)
    return True


def ensure_sealbox_key_pair_files(
    *,
    public_key_file: Path,
    private_key_file: Path,
) -> bool:
    public_exists = public_key_file.is_file()
    private_exists = private_key_file.is_file()
    if public_exists and private_exists:
        chmod_public_file(public_key_file)
        chmod_private_file(private_key_file)
        return False
    if public_exists != private_exists:
        raise RuntimeError(
            "Sealbox key initialization found only one key file. Refusing to overwrite partial key material; "
            f"public={public_key_file} private={private_key_file}"
        )
    private_key_file.parent.mkdir(parents=True, exist_ok=True)
    public_key_file.parent.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.PKCS1,
    )
    private_key_file.write_bytes(private_pem)
    public_key_file.write_bytes(public_pem)
    chmod_private_file(private_key_file)
    chmod_public_file(public_key_file)
    return True


def ensure_managed_sealbox_material(
    runtime: SealboxRuntimeConfig,
    *,
    rotate_token: bool = False,
) -> tuple[Path, ...]:
    """Create only the managed server root token.

    Tenant API tokens and key pairs are client material provisioned lazily by
    the broker and are never part of server startup.
    """

    ensure_sealbox_token_file(runtime.root_token_file, rotate=rotate_token)
    return (runtime.root_token_file,)
