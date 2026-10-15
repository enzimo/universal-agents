"""Security configuration and contracts."""

from __future__ import annotations
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from pydantic import ValidationInfo, BaseModel, Field, field_validator, model_validator
from runtime_settings.code_sandbox_contract import CODE_SANDBOX_MODES, CODE_SANDBOX_REQUIRED
from default_values import SECURITY_LAYER_DEFAULTS
from runtime_settings.common import _normalized_optional_string


class CodeExecutionSecuritySettings(BaseModel):
    """Sandbox for agent-run shell and Python commands outside OpenShell.

    ``required`` confines each command to the workspace, a private scratch
    directory, read-only operating-system files, and the extra paths below; a
    command is refused when the sandbox cannot be applied. ``off`` runs agent
    code with the runtime's full filesystem access and is logged as a warning.
    """

    sandbox: str = CODE_SANDBOX_REQUIRED
    extra_read_paths: list[str] = Field(default_factory=list)
    extra_write_paths: list[str] = Field(default_factory=list)

    @field_validator("sandbox", mode="before")
    @classmethod
    def _validate_sandbox(cls, value: Any) -> str:
        normalized = str(value or CODE_SANDBOX_REQUIRED).strip().lower()
        if normalized not in CODE_SANDBOX_MODES:
            raise ValueError(
                "security_layer.code_execution.sandbox must be one of: "
                + ", ".join(CODE_SANDBOX_MODES)
            )
        return normalized

    @field_validator("extra_read_paths", "extra_write_paths", mode="before")
    @classmethod
    def _validate_paths(cls, value: Any, info: ValidationInfo) -> list[str]:
        if value in (None, ""):
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"security_layer.code_execution.{info.field_name} must be a list.")
        paths: list[str] = []
        for item in value:
            rendered = str(item or "").strip()
            if not rendered or not Path(rendered).is_absolute() or ".." in Path(rendered).parts:
                raise ValueError(
                    f"security_layer.code_execution.{info.field_name} entries must be absolute "
                    f"paths without '..': {item!r}"
                )
            paths.append(rendered)
        return paths


class OpenShellSecuritySettings(BaseModel):
    sandbox_name: Optional[str] = None
    sandbox_image: Optional[str] = None
    policy_file: Optional[str] = None
    workspace_mount: str = "/workspace"
    manage_gateway: bool = True
    gateway_name: Optional[str] = None
    gateway_bind_host: str = "127.0.0.1"
    gateway_bind_port: int = 18080
    gateway_endpoint: Optional[str] = None
    gateway_driver: str = "docker"
    gateway_state_dir: Optional[str] = None
    gateway_db_url: Optional[str] = None
    gateway_binary: Optional[str] = None
    gateway_ready_timeout_seconds: int = 60
    gateway_extra_args: list[str] = Field(default_factory=list)
    create_if_missing: bool = True
    delete_on_shutdown: bool = False
    upload_workspace: bool = True
    no_git_ignore: bool = False
    provision_timeout_seconds: int = 300
    readiness_timeout_seconds: int = 30

    @field_validator(
        "sandbox_name",
        "sandbox_image",
        "policy_file",
        "gateway_name",
        "gateway_endpoint",
        "gateway_state_dir",
        "gateway_db_url",
        "gateway_binary",
        mode="before",
    )
    @classmethod
    def _normalize_optional_strings(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)

    @field_validator("gateway_driver", mode="before")
    @classmethod
    def _normalize_gateway_driver(cls, value: Any) -> str:
        return str(value or "docker").strip() or "docker"

    @field_validator("workspace_mount", mode="before")
    @classmethod
    def _normalize_workspace_mount(cls, value: Any) -> str:
        rendered = str(value or "/workspace").strip()
        if not rendered.startswith("/"):
            raise ValueError(
                "security_layer.openshell.workspace_mount must be an absolute sandbox path."
            )
        return rendered.rstrip("/") or "/"


class CredentialStateBackendSettings(BaseModel):
    mode: str = SECURITY_LAYER_DEFAULTS.credential_state_backend.value["mode"]
    source: Optional[str] = SECURITY_LAYER_DEFAULTS.credential_state_backend.value[
        "source"
    ]
    namespace: str = SECURITY_LAYER_DEFAULTS.credential_state_backend.value["namespace"]
    fallback_to_files: bool = SECURITY_LAYER_DEFAULTS.credential_state_backend.value[
        "fallback_to_files"
    ]

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, value: Any) -> str:
        normalized = str(value or "files").strip().lower()
        if normalized not in {"files", "credential_source"}:
            raise ValueError(
                "security_layer.credential_state_backend.mode must be 'files' or 'credential_source'."
            )
        return normalized

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_source(cls, value: Any) -> Optional[str]:
        normalized = _normalized_optional_string(value)
        return normalized.lower() if normalized else None

    @field_validator("namespace", mode="before")
    @classmethod
    def _normalize_namespace(cls, value: Any) -> str:
        normalized = (
            str(
                value
                or SECURITY_LAYER_DEFAULTS.credential_state_backend.value["namespace"]
            )
            .strip()
            .strip("/")
        )
        if not normalized:
            raise ValueError(
                "security_layer.credential_state_backend.namespace must not be empty."
            )
        if ".." in normalized.split("/"):
            raise ValueError(
                "security_layer.credential_state_backend.namespace must not contain '..' path segments."
            )
        return normalized

    @model_validator(mode="after")
    def _validate_source_for_mode(self) -> "CredentialStateBackendSettings":
        if self.mode == "credential_source" and not self.source:
            raise ValueError(
                "security_layer.credential_state_backend.source is required when mode is 'credential_source'."
            )
        return self


# Settings of the one-time import of an old credential_bindings.yaml catalog
# into credential_catalog.db, and of the person who received its rows. The
# import is gone: the SQLite catalog is the only credential catalog, and every
# catalog read and write names its owner (a person or the team).
REMOVED_CREDENTIAL_CATALOG_KEYS = ("path", "legacy_owner_canonical_user_id")
REMOVED_CREDENTIAL_CATALOG_ENV_VARS = (
    "SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_PATH",
    "SECURE_TOOL_BROKER_LEGACY_OWNER_CANONICAL_USER_ID",
)
REMOVED_CREDENTIAL_CATALOG_SETTINGS_GUIDANCE = (
    "has been removed: it belonged to the one-time import of an old credential_bindings.yaml "
    "catalog, which no longer exists. Delete it. The credential catalog is "
    "security_layer.credential_catalog.database_path."
)


def reject_removed_credential_catalog_env_vars(env: Mapping[str, str] | None = None) -> None:
    """Refuse to start while a removed credential-catalog environment variable is set."""

    present = sorted(
        key for key in REMOVED_CREDENTIAL_CATALOG_ENV_VARS if key in (os.environ if env is None else env)
    )
    if present:
        raise ValueError(
            f"Removed environment variable(s) are set: {', '.join(present)}. Unset them. They belonged "
            "to the one-time import of an old credential_bindings.yaml catalog, which no longer exists; "
            "the credential catalog is security_layer.credential_catalog.database_path."
        )


class CredentialCatalogSettings(BaseModel):
    enabled: bool = SECURITY_LAYER_DEFAULTS.credential_catalog_enabled.value
    database_path: str = SECURITY_LAYER_DEFAULTS.credential_catalog_database_path.value
    template_overrides_path: str = (
        SECURITY_LAYER_DEFAULTS.credential_template_overrides_path.value
    )
    default_write_source: str = (
        SECURITY_LAYER_DEFAULTS.credential_catalog_default_write_source.value
    )
    openshell_binding_ttl_seconds: int = (
        SECURITY_LAYER_DEFAULTS.credential_catalog_openshell_binding_ttl_seconds.value
    )
    collection_session_ttl_seconds: int = (
        SECURITY_LAYER_DEFAULTS.credential_catalog_collection_session_ttl_seconds.value
    )
    secret_cache_ttl_seconds: int = (
        SECURITY_LAYER_DEFAULTS.credential_catalog_secret_cache_ttl_seconds.value
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_yaml_import_fields(cls, value: Any) -> Any:
        # These models ignore unknown keys, so a removed setting left in the
        # file would look as if it still had an effect; refuse it by name.
        if not isinstance(value, dict):
            return value
        removed = sorted(key for key in REMOVED_CREDENTIAL_CATALOG_KEYS if key in value)
        if removed:
            names = ", ".join(f"security_layer.credential_catalog.{key}" for key in removed)
            raise ValueError(f"{names} {REMOVED_CREDENTIAL_CATALOG_SETTINGS_GUIDANCE}")
        return value

    @field_validator("database_path", "template_overrides_path", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any, info: ValidationInfo) -> str:
        defaults = {
            "database_path": SECURITY_LAYER_DEFAULTS.credential_catalog_database_path.value,
            "template_overrides_path": SECURITY_LAYER_DEFAULTS.credential_template_overrides_path.value,
        }
        default = defaults[str(info.field_name)]
        rendered = str(value or default).strip()
        if not rendered:
            raise ValueError(
                f"security_layer.credential_catalog.{info.field_name} must not be empty."
            )
        return rendered

    @field_validator("default_write_source", mode="before")
    @classmethod
    def _normalize_default_write_source(cls, value: Any) -> str:
        rendered = (
            str(
                value
                or SECURITY_LAYER_DEFAULTS.credential_catalog_default_write_source.value
            )
            .strip()
            .lower()
        )
        if rendered not in {"sealbox", "bitwarden", "vault", "onepassword", "keepass"}:
            raise ValueError(
                "security_layer.credential_catalog.default_write_source must be sealbox, bitwarden, vault, onepassword, or keepass."
            )
        return rendered

    @field_validator(
        "openshell_binding_ttl_seconds", "collection_session_ttl_seconds", mode="before"
    )
    @classmethod
    def _validate_positive_ttl(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("credential catalog TTL values must be integers.") from exc
        if parsed < 1:
            raise ValueError("credential catalog TTL values must be positive.")
        return parsed

    @field_validator("secret_cache_ttl_seconds", mode="before")
    @classmethod
    def _validate_non_negative_secret_cache_ttl(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "credential catalog secret cache TTL must be an integer."
            ) from exc
        if parsed < 0:
            raise ValueError(
                "credential catalog secret cache TTL must be non-negative."
            )
        return parsed


# Settings of the single-tenant Sealbox setup and of the one-time takeover of
# its data by the bootstrap owner. Sealbox no longer has that API (v1), so
# nothing reads them; each person and team has its own Sealbox tenant.
REMOVED_SEALBOX_KEYS = (
    "token_file",
    "auth_token_file",
    "public_key_file",
    "private_key_file",
    "legacy_public_key_file",
    "legacy_private_key_file",
)
REMOVED_SEALBOX_ENV_VARS = (
    "SECURE_TOOL_BROKER_SEALBOX_LEGACY_PUBLIC_KEY_FILE",
    "SECURE_TOOL_BROKER_SEALBOX_LEGACY_PRIVATE_KEY_FILE",
)
REMOVED_SEALBOX_SETTINGS_GUIDANCE = (
    "has been removed: it belonged to the single-tenant Sealbox setup, which Sealbox no longer "
    "supports. Delete it. Each person and team now has its own Sealbox tenant "
    "(security_layer.sealbox.root_token_file and tenant_material_dir)."
)


def reject_removed_sealbox_env_vars(env: Mapping[str, str] | None = None) -> None:
    """Refuse to start while a removed Sealbox environment variable is set."""

    present = sorted(key for key in REMOVED_SEALBOX_ENV_VARS if key in (os.environ if env is None else env))
    if present:
        raise ValueError(
            f"Removed environment variable(s) are set: {', '.join(present)}. Unset them. They pointed at "
            "the key files of the single-tenant Sealbox setup, which Sealbox no longer supports."
        )


# Tool credentials that used to come from the environment. GitHub and Google
# now run under each person's own sign-in, and Hugging Face and X under account
# links (the person's own, else the team's), all kept in the credential store.
# A token left in the environment would be one team-wide credential for every
# person, so startup refuses it and names what replaces it. Each value is the
# replacement; the NAME_FILE form is refused the same way.
_GITHUB_SIGN_IN = "each person signs in with /link cli:github web (or /link cli:github to paste a token)"
_GOOGLE_SIGN_IN = "each person signs in with /link cli:google"
_GOOGLE_CLIENT = "an administrator stores the team's Google sign-in app with /auth google team_client"
REMOVED_TOOL_CREDENTIAL_ENV_VARS: dict[str, str] = {
    "GH_TOKEN": _GITHUB_SIGN_IN,
    "GITHUB_TOKEN": _GITHUB_SIGN_IN,
    "GH_ENTERPRISE_TOKEN": _GITHUB_SIGN_IN,
    "GITHUB_ENTERPRISE_TOKEN": _GITHUB_SIGN_IN,
    "GOG_CLIENT_SECRET": _GOOGLE_CLIENT,
    "GOG_CLIENT_ID": _GOOGLE_CLIENT,
    "GOG_OAUTH_CLIENT_CREDENTIALS_FILE": _GOOGLE_CLIENT,
    "GOG_REFRESH_TOKEN": _GOOGLE_SIGN_IN,
    "GOOGLE_OAUTH_ACCESS_TOKEN": _GOOGLE_SIGN_IN,
    "GOOGLE_OAUTH_REFRESH_TOKEN": _GOOGLE_SIGN_IN,
    "GOG_TOKEN_FILE": _GOOGLE_SIGN_IN,
    "GOG_TOKEN_IMPORT_FILE": _GOOGLE_SIGN_IN,
    "GOG_SESSION_FILE": _GOOGLE_SIGN_IN,
    "GOOGLE_SESSION_FILE": _GOOGLE_SIGN_IN,
    # A service-account key file for Google tools: one Google identity for the
    # whole team. Google tools now run as the person who asked.
    "GOOGLE_APPLICATION_CREDENTIALS": (
        "Google tools run with each person's own Google sign-in; each person signs in with /link cli:google"
    ),
    "HF_TOKEN": (
        "link a Hugging Face token with /link cli:huggingface team (administrators) "
        "or /link cli:huggingface for your own"
    ),
}


def removed_tool_credential_env_problems(env: Mapping[str, str] | None = None) -> list[str]:
    """One plain message per removed tool-credential variable (or its _FILE form) that is set."""

    source = os.environ if env is None else env
    problems: list[str] = []
    for name, replacement in REMOVED_TOOL_CREDENTIAL_ENV_VARS.items():
        for key in (name, f"{name}_FILE"):
            if str(source.get(key) or "").strip():
                problems.append(f"{key} is set, but the broker no longer reads it: {replacement}. Unset {key}.")
    return problems


def reject_removed_tool_credential_env_vars(env: Mapping[str, str] | None = None) -> None:
    """Refuse to start while a removed tool-credential variable is set."""

    problems = removed_tool_credential_env_problems(env)
    if problems:
        raise ValueError("Removed tool credential variable(s) are set. " + " ".join(problems))


class SealboxSecuritySettings(BaseModel):
    enabled: bool = SECURITY_LAYER_DEFAULTS.sealbox_enabled.value
    manage_service: bool = SECURITY_LAYER_DEFAULTS.sealbox_manage_service.value
    auto_initialize: bool = SECURITY_LAYER_DEFAULTS.sealbox_auto_initialize.value
    bind_host: str = SECURITY_LAYER_DEFAULTS.sealbox_bind_host.value
    bind_port: int = SECURITY_LAYER_DEFAULTS.sealbox_bind_port.value
    service_url: Optional[str] = SECURITY_LAYER_DEFAULTS.sealbox_service_url.value
    store_path: str = SECURITY_LAYER_DEFAULTS.sealbox_store_path.value
    root_token_file: str = SECURITY_LAYER_DEFAULTS.sealbox_root_token_file.value
    tenant_material_dir: str = SECURITY_LAYER_DEFAULTS.sealbox_tenant_material_dir.value
    api_version: str = SECURITY_LAYER_DEFAULTS.sealbox_api_version.value
    ready_timeout_seconds: int = (
        SECURITY_LAYER_DEFAULTS.sealbox_ready_timeout_seconds.value
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_single_tenant_fields(cls, value: Any) -> Any:
        # These models ignore unknown keys, so a removed setting left in the
        # file would look as if it still had an effect; refuse it by name.
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        removed = sorted(key for key in REMOVED_SEALBOX_KEYS if key in payload)
        if removed:
            names = ", ".join(f"security_layer.sealbox.{key}" for key in removed)
            raise ValueError(f"{names} {REMOVED_SEALBOX_SETTINGS_GUIDANCE}")
        return payload

    @field_validator("bind_host", mode="before")
    @classmethod
    def _normalize_bind_host(cls, value: Any) -> str:
        rendered = str(value or SECURITY_LAYER_DEFAULTS.sealbox_bind_host.value).strip()
        if not rendered:
            raise ValueError("security_layer.sealbox.bind_host must not be empty.")
        return rendered

    @field_validator("bind_port", mode="before")
    @classmethod
    def _normalize_bind_port(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "security_layer.sealbox.bind_port must be an integer."
            ) from exc
        if parsed < 1 or parsed > 65_535:
            raise ValueError(
                "security_layer.sealbox.bind_port must be between 1 and 65535."
            )
        return parsed

    @field_validator(
        "store_path", "root_token_file", "tenant_material_dir", mode="before"
    )
    @classmethod
    def _normalize_paths(cls, value: Any, info: ValidationInfo) -> str:
        defaults = {
            "store_path": SECURITY_LAYER_DEFAULTS.sealbox_store_path.value,
            "root_token_file": SECURITY_LAYER_DEFAULTS.sealbox_root_token_file.value,
            "tenant_material_dir": SECURITY_LAYER_DEFAULTS.sealbox_tenant_material_dir.value,
        }
        rendered = str(value or defaults[str(info.field_name)]).strip()
        if not rendered:
            raise ValueError(
                f"security_layer.sealbox.{info.field_name} must not be empty."
            )
        if ".." in Path(rendered).parts:
            raise ValueError(
                f"security_layer.sealbox.{info.field_name} must not contain '..' path segments."
            )
        return rendered

    @field_validator("api_version", mode="before")
    @classmethod
    def _normalize_api_version(cls, value: Any) -> str:
        rendered = str(value or "v2").strip().lower()
        if rendered not in {"v2", "2"}:
            raise ValueError(
                "security_layer.sealbox.api_version must be v2 for owner-scoped credentials."
            )
        return "v2"

    @field_validator("ready_timeout_seconds", mode="before")
    @classmethod
    def _normalize_ready_timeout(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "security_layer.sealbox.ready_timeout_seconds must be an integer."
            ) from exc
        if parsed < 1:
            raise ValueError(
                "security_layer.sealbox.ready_timeout_seconds must be positive."
            )
        return parsed


# Security modes. The secure tool broker always runs: every credentialed tool
# call (web research, GitHub, Google, Hugging Face, X and MCP servers) goes
# through it, so no mode or switch turns it off. ``openshell`` also runs agent
# commands in an OpenShell sandbox.
SECURITY_LAYER_MODE_BROKER = "broker"
SECURITY_LAYER_MODE_OPENSHELL = "openshell"
SECURITY_LAYER_MODES = (SECURITY_LAYER_MODE_BROKER, SECURITY_LAYER_MODE_OPENSHELL)
# The removed ways of turning the broker off, refused by name at startup.
REMOVED_SECURITY_LAYER_MODE = "disabled"
REMOVED_SECURE_CLI_SETTING = "secure_cli_enabled"
REMOVED_SECURE_CLI_ENV_VAR = "SECURE_CLI_ENABLED"
_BROKER_ALWAYS_RUNS = (
    "the secure tool broker now always runs, because every credentialed tool call "
    "(web research, GitHub, Google, Hugging Face, X and MCP servers) goes through it"
)


def removed_security_layer_setting_problems(security_layer: Mapping[str, Any]) -> list[str]:
    """One plain message per removed broker switch in a ``security_layer`` mapping."""

    problems: list[str] = []
    if REMOVED_SECURE_CLI_SETTING in security_layer:
        problems.append(
            f"security_layer.{REMOVED_SECURE_CLI_SETTING} has been removed: {_BROKER_ALWAYS_RUNS}. "
            "Delete the setting."
        )
    mode = str(security_layer.get("mode") or "").strip().lower()
    if mode == REMOVED_SECURITY_LAYER_MODE:
        problems.append(
            f"security_layer.mode '{REMOVED_SECURITY_LAYER_MODE}' (also set by SECURITY_LAYER_MODE) "
            f"has been removed: {_BROKER_ALWAYS_RUNS}. Use '{SECURITY_LAYER_MODE_BROKER}', or "
            f"'{SECURITY_LAYER_MODE_OPENSHELL}' to also run agent commands in an OpenShell sandbox."
        )
    return problems


def reject_removed_security_layer_settings(security_layer: Mapping[str, Any]) -> None:
    """Refuse a ``security_layer`` mapping that still turns the broker off."""

    problems = removed_security_layer_setting_problems(security_layer)
    if problems:
        raise ValueError(" ".join(problems))


def normalize_security_layer_mode(value: Any) -> str:
    """``security_layer.mode`` in lower case (``broker`` when empty); refuses removed and unknown modes.

    The one check for the settings model and for onboarding, which assigns the
    mode after the model is built.
    """

    normalized = str(value or SECURITY_LAYER_DEFAULTS.mode.value).strip().lower()
    reject_removed_security_layer_settings({"mode": normalized})
    if normalized not in SECURITY_LAYER_MODES:
        raise ValueError(
            f"security_layer.mode must be '{SECURITY_LAYER_MODE_BROKER}' or "
            f"'{SECURITY_LAYER_MODE_OPENSHELL}'."
        )
    return normalized


def reject_removed_secure_cli_env_var(env: Mapping[str, str] | None = None) -> None:
    """Refuse to start while the removed SECURE_CLI_ENABLED variable is set."""

    if REMOVED_SECURE_CLI_ENV_VAR in (os.environ if env is None else env):
        raise ValueError(
            f"Removed environment variable(s) are set: {REMOVED_SECURE_CLI_ENV_VAR}. Unset it: "
            f"{_BROKER_ALWAYS_RUNS}, and the secure_cli tool is offered like any other tool, "
            "by the agent's grants."
        )


class SecurityLayerSettings(BaseModel):
    mode: str = SECURITY_LAYER_DEFAULTS.mode.value
    broker_internal_base_url: Optional[str] = (
        SECURITY_LAYER_DEFAULTS.broker_internal_base_url.value
    )
    broker_public_port: Optional[int] = SECURITY_LAYER_DEFAULTS.broker_public_port.value
    broker_bind_host: Optional[str] = SECURITY_LAYER_DEFAULTS.broker_bind_host.value
    broker_bind_port: Optional[int] = SECURITY_LAYER_DEFAULTS.broker_bind_port.value
    broker_credentials_dir: Optional[str] = (
        SECURITY_LAYER_DEFAULTS.broker_credentials_dir.value
    )
    credential_state_backend: CredentialStateBackendSettings = Field(
        default_factory=CredentialStateBackendSettings
    )
    credential_catalog: CredentialCatalogSettings = Field(
        default_factory=CredentialCatalogSettings
    )
    bitwarden_server_url: Optional[str] = (
        SECURITY_LAYER_DEFAULTS.bitwarden_server_url.value
    )
    sealbox: SealboxSecuritySettings = Field(default_factory=SealboxSecuritySettings)
    openshell: OpenShellSecuritySettings = Field(
        default_factory=OpenShellSecuritySettings
    )
    code_execution: CodeExecutionSecuritySettings = Field(
        default_factory=CodeExecutionSecuritySettings
    )
    # The broker's periodic re-check of ready tool setups
    # (secure_tool_broker.tool_setup_recheck); 0 turns it off.
    tool_setup_recheck_days: int = Field(
        default=SECURITY_LAYER_DEFAULTS.tool_setup_recheck_days.value, ge=0
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_broker_base_url(cls, value: Any) -> Any:
        if isinstance(value, dict):
            reject_removed_security_layer_settings(value)
        if isinstance(value, dict) and "broker_base_url" in value:
            raise ValueError(
                "security_layer.broker_base_url has been removed. Use "
                "security_layer.broker_internal_base_url for broker API calls and "
                "security_layer.broker_public_port with network.public_origin for user-facing broker links."
            )
        if isinstance(value, dict) and "broker_public_base_url" in value:
            raise ValueError(
                "security_layer.broker_public_base_url has been removed. Use "
                "security_layer.broker_public_port with network.public_origin instead."
            )
        return value

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, value: Any) -> str:
        return normalize_security_layer_mode(value)

    @field_validator("broker_internal_base_url", mode="before")
    @classmethod
    def _normalize_broker_urls(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)

    @field_validator("broker_public_port", mode="before")
    @classmethod
    def _normalize_broker_public_port(cls, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "security_layer.broker_public_port must be an integer."
            ) from exc
        if parsed < 1 or parsed > 65_535:
            raise ValueError(
                "security_layer.broker_public_port must be between 1 and 65535."
            )
        return parsed

    @field_validator("broker_bind_host", mode="before")
    @classmethod
    def _normalize_broker_bind_host(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)

    @field_validator("broker_bind_port", mode="before")
    @classmethod
    def _normalize_broker_bind_port(cls, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "security_layer.broker_bind_port must be an integer."
            ) from exc
        if parsed < 1 or parsed > 65_535:
            raise ValueError(
                "security_layer.broker_bind_port must be between 1 and 65535."
            )
        return parsed

    @field_validator("broker_credentials_dir", mode="before")
    @classmethod
    def _normalize_broker_credentials_dir(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)

    @field_validator("bitwarden_server_url", mode="before")
    @classmethod
    def _normalize_bitwarden_server_url(cls, value: Any) -> Optional[str]:
        normalized = _normalized_optional_string(value)
        if normalized is None:
            return None
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "security_layer.bitwarden_server_url must be an absolute http(s) URL."
            )
        return normalized.rstrip("/")
