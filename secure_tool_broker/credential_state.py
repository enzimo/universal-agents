"""Broker-owned credential state paths and persistence helpers."""

from __future__ import annotations

import os
import hashlib
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Mapping, MutableMapping

from access_control import safe_open
from secure_tool_broker.defaults import (
    DEFAULT_CREDENTIALS_DIR_NAME,
    DEFAULT_CREDENTIAL_STATE_FALLBACK_TO_FILES,
    DEFAULT_CREDENTIAL_STATE_MODE,
    DEFAULT_CREDENTIAL_STATE_NAMESPACE,
    DEFAULT_CREDENTIAL_STATE_SOURCE,
)

logger = logging.getLogger(__name__)

# Each owner's private folder below the credentials folder:
# ``users/owner-<sha>`` for a person, ``teams/owner-<sha>`` for a team.
USERS_DIRNAME = "users"
TEAMS_DIRNAME = "teams"
_CREDENTIALS_DIR_ENV = "SECURE_TOOL_BROKER_CREDENTIALS_DIR"

_ACTIVE_OWNER_ID: ContextVar[str | None] = ContextVar(
    "secure_tool_broker_owner_id", default=None
)
_ACTIVE_OWNER_SCOPE_KIND: ContextVar[str] = ContextVar(
    "secure_tool_broker_owner_scope_kind", default="user"
)


def resolve_broker_credentials_dir(
    raw_path: str | None, *, instance_root: str | Path | None = None
) -> Path:
    base = (
        Path(instance_root).expanduser().resolve()
        if instance_root
        else Path.cwd().resolve()
    )
    raw = str(raw_path or DEFAULT_CREDENTIALS_DIR_NAME).strip()
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = base / target
    return target.resolve()


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _existing_file(path: Path) -> str | None:
    return str(path) if path.is_file() else None


def _owner_material_id(owner_id: str) -> str:
    rendered = str(owner_id or "").strip()
    if not rendered:
        raise ValueError("A credential owner id is required.")
    return "owner-" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:32]


def _owner_scope_dirname() -> str:
    return TEAMS_DIRNAME if active_credential_owner_scope_kind() == "team" else USERS_DIRNAME


def broker_credentials_root(env: Mapping[str, str] | None = None) -> Path | None:
    """The broker's credentials folder (``SECURE_TOOL_BROKER_CREDENTIALS_DIR``), without creating it."""

    raw_root = str((env if env is not None else os.environ).get(_CREDENTIALS_DIR_ENV) or "").strip()
    return Path(raw_root).expanduser().resolve() if raw_root else None


def person_credential_parts(person_id: str, *parts: str) -> tuple[str, ...]:
    """Path parts of ``parts`` inside a person's private folder, relative to the credentials folder."""

    return (USERS_DIRNAME, _owner_material_id(person_id), *parts)


def remove_person_credentials_folder(person_id: str, *, env: Mapping[str, str] | None = None) -> bool:
    """Delete a removed person's private credentials folder; False when they had none.

    The folder (``users/owner-<sha>``) holds the files of the person's own
    sign-ins and tools: the file-based credential state, the GitHub CLI
    configuration, private ``gog`` homes and password-manager sessions.
    Nothing in it is followed: a link inside is deleted as a link, and a link
    in place of the folder is refused (``safe_open.UnsafePath``). Only the
    person's own folder is named, so the team's folder is never touched.
    """

    root = broker_credentials_root(env)
    if root is None or not root.is_dir():
        return False
    removed = safe_open.remove_directory(root, person_credential_parts(person_id))
    if removed:
        logger.info(
            "Deleted a removed person's credentials folder. event=person_credentials_folder_deleted person_id=%s",
            person_id,
        )
    return removed


def active_credential_owner_id() -> str | None:
    return _ACTIVE_OWNER_ID.get()


def active_credential_owner_scope_kind() -> str:
    return _ACTIVE_OWNER_SCOPE_KIND.get()


@contextmanager
def credential_owner_context(owner_id: str, *, scope_kind: str = "user") -> Iterator[None]:
    if scope_kind not in {"user", "team"}:
        raise ValueError("Provider state requires a user or team owner scope.")
    rendered = str(owner_id or "").strip()
    if not rendered:
        raise ValueError("A credential owner id is required.")
    token = _ACTIVE_OWNER_ID.set(rendered)
    scope_token = _ACTIVE_OWNER_SCOPE_KIND.set(scope_kind)
    try:
        yield
    finally:
        _ACTIVE_OWNER_ID.reset(token)
        _ACTIVE_OWNER_SCOPE_KIND.reset(scope_token)


def credential_owner_environment(
    owner_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    rendered_owner = str(owner_id or active_credential_owner_id() or "").strip()
    result = dict(env or os.environ)
    if not rendered_owner:
        return result
    raw_root = str(result.get(_CREDENTIALS_DIR_ENV) or "").strip()
    if not raw_root:
        raise ValueError(
            "SECURE_TOOL_BROKER_CREDENTIALS_DIR is required for owner-scoped provider state."
        )
    root = _ensure_private_dir(
        Path(raw_root).expanduser().resolve()
        / _owner_scope_dirname()
        / _owner_material_id(rendered_owner)
    )
    github = _ensure_private_dir(root / "gh")
    bitwarden = _ensure_private_dir(root / "bitwarden")
    onepassword = _ensure_private_dir(root / "onepassword")
    keepass = _ensure_private_dir(root / "keepass")
    vault = _ensure_private_dir(root / "vault")
    result["GH_CONFIG_DIR"] = str(github)
    result["BITWARDENCLI_APPDATA_DIR"] = str(_ensure_private_dir(bitwarden / "appdata"))
    result["OP_CONFIG_DIR"] = str(_ensure_private_dir(onepassword / "config"))

    # Ambient process credentials must never become a fallback for another user.
    # Startup refuses the GitHub token variables (runtime_settings.security), so
    # removing them here only guards a broker started some other way. gog runs
    # with its own allowlisted environment (google_workspace), not this one.
    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "BW_SESSION",
        "BW_CLIENTID",
        "BW_CLIENTSECRET",
        "BW_PASSWORD",
        "VAULT_TOKEN",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "OP_CONNECT_TOKEN",
        "KEEPASSXC_DATABASE",
        "KEEPASS_DATABASE",
        "KEEPASSXC_PASSWORD",
        "KEEPASS_PASSWORD",
        "KEEPASSXC_KEY_FILE",
        "KEEPASS_KEY_FILE",
        "SEALBOX_TOKEN",
        "SEALBOX_TOKEN_FILE",
        "SEALBOX_ROOT_TOKEN_FILE",
        "SEALBOX_AUTH_TOKEN_FILE",
        "SEALBOX_PUBLIC_KEY",
        "SEALBOX_PUBLIC_KEY_FILE",
        "SEALBOX_PRIVATE_KEY",
        "SEALBOX_PRIVATE_KEY_FILE",
    ):
        result.pop(key, None)
    for key in tuple(result):
        if key.startswith("OP_SESSION_"):
            result.pop(key, None)
    file_defaults = {
        "BW_SESSION_FILE": bitwarden / "session",
        "OP_SERVICE_ACCOUNT_TOKEN_FILE": onepassword / "service_account_token",
        "KEEPASSXC_PASSWORD_FILE": keepass / "password",
        "KEEPASS_PASSWORD_FILE": keepass / "password",
        "VAULT_TOKEN_FILE": vault / "token",
    }
    for key, path in file_defaults.items():
        if path.is_file():
            result[key] = str(path)
        else:
            result.pop(key, None)
    bw_session = bitwarden / "session"
    if bw_session.is_file():
        value = bw_session.read_text(encoding="utf-8").strip()
        if value:
            result["BW_SESSION"] = value
    op_token = onepassword / "service_account_token"
    if op_token.is_file():
        value = op_token.read_text(encoding="utf-8").strip()
        if value:
            result["OP_SERVICE_ACCOUNT_TOKEN"] = value
    vault_token = vault / "token"
    if vault_token.is_file():
        value = vault_token.read_text(encoding="utf-8").strip()
        if value:
            result["VAULT_TOKEN"] = value
    keepass_password = keepass / "password"
    if keepass_password.is_file():
        value = keepass_password.read_text(encoding="utf-8").strip()
        if value:
            result["KEEPASSXC_PASSWORD"] = value
            result["KEEPASS_PASSWORD"] = value
    keepass_database = keepass / "database.kdbx"
    if keepass_database.is_file():
        result["KEEPASSXC_DATABASE"] = str(keepass_database)
        result["KEEPASS_DATABASE"] = str(keepass_database)
    keepass_key_file = keepass / "keyfile"
    if keepass_key_file.is_file():
        result["KEEPASSXC_KEY_FILE"] = str(keepass_key_file)
        result["KEEPASS_KEY_FILE"] = str(keepass_key_file)
    return result


def configure_broker_credential_environment(
    credentials_dir: str | Path,
    *,
    credential_state_backend: Mapping[str, object] | None = None,
    env: MutableMapping[str, str] | None = None,
) -> dict[str, Path]:
    """Set broker-only credential state defaults without overriding operator env."""

    env_mapping = env if env is not None else os.environ
    root = _ensure_private_dir(Path(credentials_dir).expanduser().resolve())
    paths = {
        "root": root,
        "github": _ensure_private_dir(root / "gh"),
        "bitwarden": _ensure_private_dir(root / "bitwarden"),
        "onepassword": _ensure_private_dir(root / "onepassword"),
        "keepass": _ensure_private_dir(root / "keepass"),
        "vault": _ensure_private_dir(root / "vault"),
    }

    env_mapping.setdefault("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(root))
    env_mapping.setdefault("GH_CONFIG_DIR", str(paths["github"]))
    env_mapping.setdefault(
        "BITWARDENCLI_APPDATA_DIR",
        str(_ensure_private_dir(paths["bitwarden"] / "appdata")),
    )
    env_mapping.setdefault(
        "OP_CONFIG_DIR", str(_ensure_private_dir(paths["onepassword"] / "config"))
    )
    if credential_state_backend:
        mode = (
            str(credential_state_backend.get("mode") or DEFAULT_CREDENTIAL_STATE_MODE)
            .strip()
            .lower()
        )
        source = (
            str(
                credential_state_backend.get("source")
                or DEFAULT_CREDENTIAL_STATE_SOURCE
            )
            .strip()
            .lower()
        )
        namespace = (
            str(
                credential_state_backend.get("namespace")
                or DEFAULT_CREDENTIAL_STATE_NAMESPACE
            )
            .strip()
            .strip("/")
        )
        fallback = str(
            bool(
                credential_state_backend.get(
                    "fallback_to_files",
                    DEFAULT_CREDENTIAL_STATE_FALLBACK_TO_FILES,
                )
            )
        ).lower()
        env_mapping.setdefault("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", mode)
        if source:
            env_mapping.setdefault(
                "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE", source
            )
        env_mapping.setdefault(
            "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_NAMESPACE",
            namespace or DEFAULT_CREDENTIAL_STATE_NAMESPACE,
        )
        env_mapping.setdefault(
            "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES", fallback
        )

    optional_file_defaults = {
        "BW_SESSION_FILE": paths["bitwarden"] / "session",
        "OP_SERVICE_ACCOUNT_TOKEN_FILE": paths["onepassword"] / "service_account_token",
        "KEEPASSXC_PASSWORD_FILE": paths["keepass"] / "password",
        "KEEPASS_PASSWORD_FILE": paths["keepass"] / "password",
        "VAULT_TOKEN_FILE": paths["vault"] / "token",
    }
    for key, file_path in optional_file_defaults.items():
        existing_file = _existing_file(file_path)
        if existing_file:
            env_mapping.setdefault(key, existing_file)

    return paths


def broker_credential_file(*parts: str, owner_id: str | None = None) -> Path | None:
    raw_root = str(os.getenv(_CREDENTIALS_DIR_ENV) or "").strip()
    if not raw_root:
        return None
    root = _ensure_private_dir(Path(raw_root).expanduser().resolve())
    resolved_owner = str(owner_id or active_credential_owner_id() or "").strip()
    owner_parts: tuple[str, ...] = ()
    if resolved_owner:
        owner_parts = (_owner_scope_dirname(), _owner_material_id(resolved_owner))
    target = root.joinpath(*owner_parts, *parts).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    _ensure_private_dir(target.parent)
    return target


def write_private_broker_file(path: Path, content: str, *, exclusive: bool = False) -> None:
    from secure_tool_broker.private_files import write_private_file

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = content if content.endswith("\n") else f"{content}\n"
    write_private_file(path.parent, (path.name,), value.encode("utf-8"), exclusive=exclusive)
