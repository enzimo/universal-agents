"""Credential collection template registry for broker-owned credentials."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from secure_tools.providers import ToolResult


TEMPLATE_SCHEMA_VERSION = 1
DEFAULT_TEMPLATE_OVERRIDES_FILENAME = "credential_template_overrides.yaml"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_TEMPLATE_PATH = PROJECT_ROOT / "ua_definitions" / "credential_templates" / "builtins.yaml"
SECRET_FIELD_KINDS = {"password", "token", "api_key", "secret", "totp", "credential_file"}
TEMPLATE_VARIABLE_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
SUGGESTED_GENERIC_TEMPLATE_IDS = [
    "generic_username_password",
    "generic_signup_username_password",
    "generic_api_token",
    "generic_oauth_client_secret",
    "generic_service_account_file",
]


def _string(value: Any) -> str:
    return str(value or "").strip()


def _slug(value: str, *, upper: bool = False) -> str:
    rendered = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    rendered = re.sub(r"_+", "_", rendered)
    rendered = rendered or "service"
    return rendered.upper() if upper else rendered.lower()


def _template_id(value: Any) -> str:
    rendered = _string(value)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", rendered):
        raise ValueError("template_id must contain only letters, numbers, underscore, dot, colon, or dash.")
    return rendered


def _credential_key(value: Any) -> str:
    rendered = _string(value)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", rendered):
        raise ValueError("credential_key must contain only letters, numbers, underscore, dot, colon, or dash.")
    return rendered


def _is_secret_field(raw: Mapping[str, Any]) -> bool:
    kind = _string(raw.get("kind")).lower()
    visibility = _string(raw.get("visibility")).lower()
    return visibility == "secret" or kind in SECRET_FIELD_KINDS or bool(raw.get("secret", False))


def _resolve_instance_path(raw_path: str | os.PathLike[str] | None, *, default_filename: str) -> Path:
    raw = _string(raw_path)
    if raw:
        candidate = Path(raw).expanduser()
    else:
        explicit = _string(os.getenv("SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH"))
        if explicit:
            candidate = Path(explicit).expanduser()
        else:
            instance_root = _string(os.getenv("TEAM_INSTANCE_ROOT"))
            candidate = (Path(instance_root).expanduser() if instance_root else Path.cwd()) / default_filename
    if not candidate.is_absolute():
        instance_root = _string(os.getenv("TEAM_INSTANCE_ROOT"))
        base = Path(instance_root).expanduser() if instance_root else Path.cwd()
        candidate = base / candidate
    return candidate.resolve()


def resolve_template_overrides_path(path: str | os.PathLike[str] | None = None) -> Path:
    return _resolve_instance_path(path, default_filename=DEFAULT_TEMPLATE_OVERRIDES_FILENAME)


class CredentialTemplateFormSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_control: str = "text"
    autocomplete: str | None = None
    accept: list[str] = Field(default_factory=list)
    help_text: str | None = None

    @field_validator("input_control", mode="before")
    @classmethod
    def _normalize_input_control(cls, value: Any) -> str:
        rendered = _string(value or "text").lower()
        if rendered not in {"text", "password", "textarea", "file"}:
            raise ValueError("form.input_control must be text, password, textarea, or file.")
        return rendered


class CredentialTemplateField(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    kind: str = "custom"
    visibility: str = "secret"
    value_source: str = "user_input"
    required: bool = True
    value: str | None = None
    generation_policy: str | None = None
    label: str | None = None
    description: str | None = None
    placeholder: str | None = None
    field_ref: str | None = None
    form: CredentialTemplateFormSpec | None = None
    source_ref: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        rendered = _string(value)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", rendered):
            raise ValueError(f"Invalid credential field name: {value!r}")
        return rendered

    @field_validator("kind", "visibility", "value_source", mode="before")
    @classmethod
    def _normalize_string(cls, value: Any) -> str:
        return _string(value).lower()

    @model_validator(mode="after")
    def _reject_secret_value(self) -> "CredentialTemplateField":
        if _is_secret_field(self.model_dump(mode="json", exclude_none=True)) and _string(self.value):
            raise ValueError(f"Secret field {self.name} must not include a template value.")
        return self


class CredentialTemplate(BaseModel):
    """How to collect one kind of credential: its fields and signup details.

    Templates describe only the credential. Which tool uses it, and where its
    values go (a header, a query value, or an environment variable), is an
    account link (``AccountLinkV1``) made when the account is linked.
    """

    model_config = ConfigDict(extra="allow")

    template_id: str
    display_name: str
    description: str = ""
    credential_kind: str
    aliases: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    extends: str | None = None
    base_template_id: str | None = None
    template_version: int = 1
    default_credential_key_template: str = "{{service_slug}}_credential"
    default_purpose_template: str = "Credential for approved {{service_name}} workflows."
    flow: str = "credential_collection"
    store_target: str = "default"
    signup_url: str | None = None
    fields: list[CredentialTemplateField] = Field(default_factory=list)
    automation_steps: list[str] = Field(default_factory=list)
    save_approved_variant_by_default: bool = False

    @field_validator("template_id", mode="before")
    @classmethod
    def _validate_template_id(cls, value: Any) -> str:
        return _template_id(value)

    @field_validator("credential_kind", "flow", "store_target", mode="before")
    @classmethod
    def _normalize_string(cls, value: Any) -> str:
        return _string(value).lower()

    @model_validator(mode="after")
    def _validate_template(self) -> "CredentialTemplate":
        if self.flow not in {"credential_collection", "signup", "manual_signup", "existing_source_registration"}:
            raise ValueError("Credential template flow is unsupported.")
        if not self.fields:
            raise ValueError("Credential template must define at least one field.")
        return self


class CredentialTemplateRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = TEMPLATE_SCHEMA_VERSION
    templates: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema(cls, value: int) -> int:
        if int(value) != TEMPLATE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported credential template schema_version: {value}")
        return int(value)


@dataclass(frozen=True)
class LoadedCredentialTemplate:
    template: CredentialTemplate
    source: str
    path: Path


def _load_template_file(path: Path, *, required: bool) -> CredentialTemplateRegistry:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Credential template file does not exist: {path}")
        return CredentialTemplateRegistry()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"Credential template file must contain a YAML mapping: {path}")
    registry = CredentialTemplateRegistry.model_validate(raw)
    for template_id, template in registry.templates.items():
        _template_id(template_id)
        _reject_secret_values(template)
    return registry


def _reject_secret_values(raw: Any) -> None:
    if not isinstance(raw, Mapping):
        return
    fields = raw.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if isinstance(field, Mapping) and _is_secret_field(field) and _string(field.get("value")):
                raise ValueError(f"Secret field {field.get('name') or field.get('field_ref')} must not include a value.")
    if isinstance(fields, Mapping):
        for name, field in fields.items():
            if isinstance(field, Mapping) and _is_secret_field(field) and _string(field.get("value")):
                raise ValueError(f"Secret field {name} must not include a value.")


def load_credential_templates(
    *,
    overrides_path: str | os.PathLike[str] | None = None,
) -> dict[str, LoadedCredentialTemplate]:
    builtins = _load_template_file(BUILTIN_TEMPLATE_PATH, required=True)
    resolved_overrides_path = resolve_template_overrides_path(overrides_path)
    overrides = _load_template_file(resolved_overrides_path, required=False)
    raw_templates: dict[str, dict[str, Any]] = {}
    sources: dict[str, tuple[str, Path]] = {}
    for template_id, raw in builtins.templates.items():
        payload = dict(raw)
        payload.setdefault("template_id", template_id)
        raw_templates[template_id] = payload
        sources[template_id] = ("builtin", BUILTIN_TEMPLATE_PATH)
    for template_id, raw in overrides.templates.items():
        payload = dict(raw)
        payload.setdefault("template_id", template_id)
        raw_templates[template_id] = payload
        sources[template_id] = ("approved_variant", resolved_overrides_path)

    resolved: dict[str, LoadedCredentialTemplate] = {}
    resolving: set[str] = set()

    def resolve_one(template_id: str) -> LoadedCredentialTemplate:
        if template_id in resolved:
            return resolved[template_id]
        if template_id in resolving:
            raise ValueError(f"Credential template inheritance cycle includes {template_id}.")
        raw = raw_templates.get(template_id)
        if raw is None:
            raise ValueError(f"Credential template does not exist: {template_id}")
        resolving.add(template_id)
        parent_id = _string(raw.get("extends")) or None
        merged = dict(raw)
        if parent_id:
            parent = resolve_one(parent_id).template.model_dump(mode="json", exclude_none=True)
            merged = _deep_merge(parent, raw)
            merged["base_template_id"] = parent_id
            merged["template_id"] = template_id
        _reject_secret_values(merged)
        template = CredentialTemplate.model_validate(merged)
        source, path = sources[template_id]
        loaded = LoadedCredentialTemplate(template=template, source=source, path=path)
        resolved[template_id] = loaded
        resolving.remove(template_id)
        return loaded

    for template_id in sorted(raw_templates):
        resolve_one(template_id)
    return resolved


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        if key == "extends":
            continue
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def list_credential_templates(params: Mapping[str, Any]) -> ToolResult:
    query = _string(params.get("query")).lower()
    credential_kind = _string(params.get("credential_kind")).lower()
    domain = _normalize_domain(params.get("domain"))
    templates = load_credential_templates()
    summaries = []
    for loaded in templates.values():
        template = loaded.template
        if credential_kind and template.credential_kind != credential_kind:
            continue
        if domain and domain not in {_normalize_domain(item) for item in template.domains}:
            continue
        if query and not _template_matches_query(template, query):
            continue
        summaries.append(_template_summary(loaded))
    return ToolResult(status="success", content={"status": "template_listed", "templates": summaries})


def get_credential_template(params: Mapping[str, Any]) -> ToolResult:
    template_id = _template_id(params.get("template_id") or params.get("id"))
    templates = load_credential_templates()
    loaded = templates.get(template_id)
    if loaded is None:
        return ToolResult(status="error", error=f"Credential template is not registered: {template_id}")
    return ToolResult(
        status="success",
        content={
            "status": "template_found",
            "template": loaded.template.model_dump(mode="json", exclude_none=True),
            "source": loaded.source,
        },
    )


def render_credential_template_request(params: Mapping[str, Any]) -> ToolResult:
    try:
        raw_template_id = _string(params.get("template_id") or params.get("id"))
        if not raw_template_id:
            return ToolResult(
                status="error",
                error=(
                    "template.render_request requires template_id. Use template.list to discover service-specific "
                    "templates; for common cases use generic_username_password, generic_signup_username_password, "
                    "generic_api_token, generic_oauth_client_secret, or generic_service_account_file."
                ),
                content={
                    "error_code": "template_id_required",
                    "suggested_template_ids": SUGGESTED_GENERIC_TEMPLATE_IDS,
                    "form_creation_action": "secure_credential_collection_request",
                },
            )
        template_id = _template_id(raw_template_id)
        templates = load_credential_templates()
        loaded = templates.get(template_id)
        if loaded is None:
            return ToolResult(status="error", error=f"Credential template is not registered: {template_id}")
        template = loaded.template
        service_name = _string(params.get("service_name")) or template.display_name
        domain = _normalize_domain(params.get("domain") or (template.domains[0] if template.domains else service_name))
        context = {
            "service_name": service_name,
            "service_slug": _slug(service_name),
            "service_env_prefix": _slug(service_name, upper=True),
            "domain": domain,
            "username": _string(params.get("desired_username") or params.get("username")),
        }
        credential_key = _credential_key(
            params.get("credential_key") or _render_template_string(template.default_credential_key_template, context)
        )
        context["credential_key"] = credential_key
        signup_url = _render_optional(template.signup_url, context)
        context["signup_url"] = signup_url or ""
        purpose = _string(params.get("purpose")) or _render_template_string(template.default_purpose_template, context)
        request_payload = {
            "service_name": service_name,
            "signup_url": signup_url,
            "credential_key": credential_key,
            "display_name": _string(params.get("display_name")) or service_name,
            "purpose": purpose,
            "store_target": _string(params.get("store_target")) or template.store_target,
            "flow": _string(params.get("flow")) or template.flow,
            "fields": [_render_mapping(field.model_dump(mode="json", exclude_none=True), context) for field in template.fields],
            "automation_steps": [_render_template_string(step, context) for step in template.automation_steps],
            "user_approved": bool(params.get("user_approved", False)),
            "template_reference": {
                "template_id": template.template_id,
                "template_source": loaded.source,
                "base_template_id": template.base_template_id,
                "template_version": template.template_version,
            },
        }
        if context["username"]:
            for field in request_payload["fields"]:
                if _string(field.get("kind")).lower() in {"username", "email", "login"} and not _string(field.get("value")):
                    field["value"] = context["username"]
                    field["value_source"] = "user_or_agent_supplied_metadata"
                    break
        _apply_render_augmentations(request_payload, params.get("augmentations") or {})
        save_variant = bool(
            params.get(
                "save_template_variant_on_approval",
                template.save_approved_variant_by_default or bool(params.get("augmentations")),
            )
        )
        request_payload["save_template_variant_on_approval"] = save_variant
        variant = _build_template_variant(
            base_template=template,
            request_payload=request_payload,
            source_template_id=template.template_id,
        )
        request_payload["template_variant"] = variant if save_variant else None
        return ToolResult(
            status="success",
            content={
                "status": "template_rendered",
                "request": request_payload,
                "template_reference": request_payload["template_reference"],
                "template_variant": variant,
                "validation_warnings": [],
            },
        )
    except Exception as exc:
        return ToolResult(status="error", error=f"Credential template render failed: {exc}")


def save_approved_template_variant(params: Mapping[str, Any]) -> ToolResult:
    try:
        if not bool(params.get("user_approved")):
            return ToolResult(
                status="error",
                error="Approved template variants can be saved only after user approval.",
                content={"error_code": "template_variant_unapproved"},
            )
        variant = dict(params.get("template_variant") or params.get("variant") or {})
        template_payload = dict(variant.get("template") or variant)
        if not template_payload and params.get("request"):
            request_payload = dict(params["request"])
            reference = dict(request_payload.get("template_reference") or {})
            base_template_id = _string(reference.get("template_id")) or "agent_generated"
            template_payload = _template_payload_from_request(
                request_payload=request_payload,
                base_template_id=base_template_id,
            )
        if not template_payload:
            return ToolResult(status="error", error="template_variant or request is required.")
        _reject_secret_values(template_payload)
        template_payload.setdefault("credential_kind", "custom")
        template_payload.setdefault("flow", "credential_collection")
        template_payload.setdefault("default_credential_key_template", template_payload.get("template_id", "approved_credential"))
        template_payload.setdefault("default_purpose_template", template_payload.get("description", "Approved credential workflow."))
        template_id = _string(template_payload.get("template_id"))
        if not template_id:
            template_id = _approved_variant_id(template_payload)
            template_payload["template_id"] = template_id
        template_payload["template_id"] = _template_id(template_payload["template_id"])
        now = params.get("approved_at") or _utc_now()
        template_payload["approved_at"] = now
        template_payload["approved_by"] = _string(params.get("approved_by")) or "user"
        CredentialTemplate.model_validate(template_payload)

        path = resolve_template_overrides_path(params.get("path"))
        registry = _load_template_file(path, required=False)
        registry.templates[template_payload["template_id"]] = template_payload
        _write_template_registry(path, registry)
        return ToolResult(
            status="success",
            content={
                "status": "template_variant_saved",
                "template_id": template_payload["template_id"],
                "path": str(path),
            },
        )
    except Exception as exc:
        return ToolResult(status="error", error=f"Credential template variant save failed: {exc}")


def _write_template_registry(path: Path, registry: CredentialTemplateRegistry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    rendered = yaml.safe_dump(registry.model_dump(mode="json", exclude_none=True), sort_keys=False)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(rendered)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _template_summary(loaded: LoadedCredentialTemplate) -> dict[str, Any]:
    template = loaded.template
    return {
        "template_id": template.template_id,
        "display_name": template.display_name,
        "description": template.description,
        "credential_kind": template.credential_kind,
        "flow": template.flow,
        "aliases": template.aliases,
        "domains": template.domains,
        "source": loaded.source,
        "base_template_id": template.base_template_id,
        "field_names": [field.name for field in template.fields],
    }


def _template_matches_query(template: CredentialTemplate, query: str) -> bool:
    normalized_query = query.lower().strip()
    exact_terms = {
        template.template_id.lower(),
        template.display_name.lower(),
        template.credential_kind.lower(),
        *[alias.lower() for alias in template.aliases],
        *[_normalize_domain(domain) for domain in template.domains],
        *template.template_id.lower().replace("-", "_").split("_"),
    }
    if len(normalized_query) <= 2:
        return normalized_query in exact_terms
    haystack = " ".join(
        [
            template.template_id,
            template.display_name,
            template.description,
            template.credential_kind,
            *template.aliases,
            *template.domains,
        ]
    ).lower()
    return normalized_query in haystack


def _normalize_domain(value: Any) -> str:
    rendered = _string(value).lower()
    if not rendered:
        return ""
    parsed = urlparse(rendered if "://" in rendered else f"https://{rendered}")
    return (parsed.hostname or rendered).removeprefix("www.")


def _render_optional(value: str | None, context: Mapping[str, str]) -> str | None:
    if value is None:
        return None
    rendered = _render_template_string(value, context)
    return rendered or None


def _render_template_string(value: str, context: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        return str(context.get(match.group(1), ""))

    return TEMPLATE_VARIABLE_PATTERN.sub(replace, str(value))


def _render_mapping(value: Any, context: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _render_template_string(value, context)
    if isinstance(value, list):
        return [_render_mapping(item, context) for item in value]
    if isinstance(value, dict):
        return {
            _render_template_string(str(key), context): _render_mapping(item, context)
            for key, item in value.items()
        }
    return value


def _apply_render_augmentations(request_payload: dict[str, Any], augmentations: Any) -> None:
    if not isinstance(augmentations, Mapping):
        return
    for key in ("service_name", "signup_url", "purpose", "store_target", "flow"):
        if key in augmentations and _string(augmentations[key]):
            request_payload[key] = augmentations[key]
    if isinstance(augmentations.get("automation_steps"), list):
        request_payload["automation_steps"] = [str(item) for item in augmentations["automation_steps"]]
    fields_by_name = {str(field.get("name")): field for field in request_payload.get("fields", []) if isinstance(field, dict)}
    raw_fields = augmentations.get("fields")
    if isinstance(raw_fields, Mapping):
        iterable = [dict({"name": name}, **dict(value or {})) for name, value in raw_fields.items() if isinstance(value, Mapping)]
    elif isinstance(raw_fields, list):
        iterable = [dict(item) for item in raw_fields if isinstance(item, Mapping)]
    else:
        iterable = []
    for field_update in iterable:
        name = _string(field_update.get("name") or field_update.get("field_ref"))
        if not name:
            continue
        existing = fields_by_name.get(name, {"name": name, "kind": "custom", "visibility": "metadata_visible"})
        candidate = dict(existing)
        for allowed in ("label", "description", "placeholder", "required", "form", "value_source", "value", "kind", "visibility"):
            if allowed in field_update:
                candidate[allowed] = field_update[allowed]
        if _is_secret_field(candidate) and _string(candidate.get("value")):
            raise ValueError(f"Agent augmentation attempted to set a secret value for {name}.")
        fields_by_name[name] = candidate
    if fields_by_name:
        request_payload["fields"] = list(fields_by_name.values())
    _reject_secret_values({"fields": request_payload.get("fields", [])})


def _build_template_variant(
    *,
    base_template: CredentialTemplate,
    request_payload: Mapping[str, Any],
    source_template_id: str,
) -> dict[str, Any]:
    template_payload = _template_payload_from_request(
        request_payload=request_payload,
        base_template_id=source_template_id,
        credential_kind=base_template.credential_kind,
        aliases=base_template.aliases,
        domains=base_template.domains,
    )
    template_payload["template_id"] = _approved_variant_id(template_payload)
    return {
        "template_id": template_payload["template_id"],
        "base_template_id": source_template_id,
        "template": template_payload,
    }


def _template_payload_from_request(
    *,
    request_payload: Mapping[str, Any],
    base_template_id: str,
    credential_kind: str = "custom",
    aliases: list[str] | None = None,
    domains: list[str] | None = None,
) -> dict[str, Any]:
    service_name = _string(request_payload.get("service_name")) or _string(request_payload.get("display_name")) or "Service"
    credential_key = _credential_key(request_payload.get("credential_key"))
    template_payload: dict[str, Any] = {
        "template_id": "",
        "display_name": f"{service_name} Credential",
        "description": _string(request_payload.get("purpose")) or f"Approved credential flow for {service_name}.",
        "credential_kind": credential_kind,
        "aliases": list(aliases or []),
        "domains": list(domains or []),
        "base_template_id": base_template_id,
        "default_credential_key_template": credential_key,
        "default_purpose_template": _string(request_payload.get("purpose")) or f"Credential for {service_name}.",
        "flow": _string(request_payload.get("flow")) or "credential_collection",
        "store_target": _string(request_payload.get("store_target")) or "default",
        "signup_url": request_payload.get("signup_url") or None,
        "fields": copy.deepcopy(list(request_payload.get("fields") or [])),
        "automation_steps": copy.deepcopy(list(request_payload.get("automation_steps") or [])),
    }
    _reject_secret_values(template_payload)
    return template_payload


def _approved_variant_id(template_payload: Mapping[str, Any]) -> str:
    base = _slug(_string(template_payload.get("display_name")) or "credential")
    base_template_id = _slug(_string(template_payload.get("base_template_id")) or "agent_generated")
    canonical = yaml.safe_dump(dict(template_payload), sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return f"approved_{base}_{base_template_id}_{digest}"[:128]


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
