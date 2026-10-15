"""Durable recovery for model-specific OpenAI reasoning-summary rejection."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from config_backup_retention import prune_config_backups
from config_loader import (
    LLM_OVERRIDE_ENTRY_KEYS,
    lookup_configured_model_request_constraints,
    resolve_llm_override_path,
    validate_llm_override_payload,
)
from custom_agents.strands_model_factory import uses_openai_reasoning_summary_transport

try:  # pragma: no cover - supported deployments are Unix based.
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


_SUMMARY_PARAMETER_NAMES = {
    "reasoning.summary",
    "reasoning_summary",
    "summary",
}
_UNSUPPORTED_MARKERS = (
    "not supported",
    "unsupported",
    "does not support",
    "unknown parameter",
    "unrecognized parameter",
    "invalid parameter",
    "invalid value",
)
_BAD_REQUEST_CODES = {
    "invalid_parameter",
    "invalid_request",
    "invalid_request_error",
    "unsupported_parameter",
    "unsupported_value",
}


@dataclass(frozen=True)
class ReasoningSummaryRepair:
    provider: str
    model_id: str
    requested_summary: str
    override_path: Path
    yaml_path: str
    backup_path: Path | None
    changed: bool


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _error_details(error: BaseException) -> tuple[set[int], set[str], set[str], str]:
    statuses: set[int] = set()
    parameters: set[str] = set()
    codes: set[str] = set()
    fragments: list[str] = []
    for current in _exception_chain(error):
        fragments.append(str(current))
        current_status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if current_status is None and response is not None:
            current_status = getattr(response, "status_code", None)
        if isinstance(current_status, int):
            statuses.add(current_status)
        current_code = str(getattr(current, "code", None) or "").strip().lower()
        if current_code:
            codes.add(current_code)
            fragments.append(current_code)

        body = getattr(current, "body", None)
        if isinstance(body, dict):
            fragments.append(str(body))
            body_error = body.get("error")
            if isinstance(body_error, dict):
                fragments.extend(
                    str(body_error.get(key) or "")
                    for key in ("message", "code", "type")
                )
                body_code = str(body_error.get("code") or "").strip().lower()
                if body_code:
                    codes.add(body_code)
                parameter = str(body_error.get("param") or "").strip().lower()
                if parameter:
                    parameters.add(parameter)
            else:
                parameter = str(body.get("param") or "").strip().lower()
                if parameter:
                    parameters.add(parameter)
        parameter = str(getattr(current, "param", None) or "").strip().lower()
        if parameter:
            parameters.add(parameter)
    return statuses, parameters, codes, " ".join(fragments).lower()


def is_unsupported_reasoning_summary_error(
    error: BaseException,
    *,
    provider: Any,
    model_id: Any,
    litellm_api_mode: Any,
    requested_summary: Any,
) -> bool:
    """Classify only an explicit OpenAI-model rejection of reasoning.summary."""

    requested = str(requested_summary or "").strip()
    if not requested or not uses_openai_reasoning_summary_transport(
        provider=provider,
        model_id=model_id,
        litellm_api_mode=litellm_api_mode,
    ):
        return False

    statuses, parameters, codes, details = _error_details(error)
    is_bad_request = (
        bool(statuses & {400, 422})
        or bool(codes & _BAD_REQUEST_CODES)
        or any(
            type(current).__name__ in {"BadRequestError", "UnprocessableEntityError"}
            for current in _exception_chain(error)
        )
    )
    if not is_bad_request or not any(
        marker in details for marker in _UNSUPPORTED_MARKERS
    ):
        return False
    parameter_matches = bool(parameters & _SUMMARY_PARAMETER_NAMES)
    message_matches = "summary" in details and "reasoning" in details
    return parameter_matches or message_matches


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise RuntimeError(
            "Cannot safely update LLM_OVERRIDES.yaml because fcntl is unavailable."
        )
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _validated_payload(raw: Any, *, source: Path) -> dict[str, Any]:
    payload = {} if raw is None else raw
    validation = validate_llm_override_payload(payload)
    if not validation["valid"]:
        errors = "; ".join(validation["errors"])
        raise ValueError(f"Cannot repair invalid LLM override file {source}: {errors}")
    if not isinstance(payload, dict):  # validation already supplies the useful error
        raise ValueError(f"Cannot repair non-mapping LLM override file {source}.")
    return payload


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    serialized = yaml.safe_dump(payload, sort_keys=False).encode("utf-8")
    target_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, target_mode)
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def disable_unsupported_reasoning_summary(
    *,
    provider: Any,
    model_id: Any,
    requested_summary: Any,
    instance_root: str | None,
    explicit_override_path: str | None,
) -> ReasoningSummaryRepair:
    """Persist a model-scoped explicit null and verify the resulting YAML."""

    normalized_provider = str(provider or "").strip().lower()
    normalized_model_id = str(model_id or "").strip()
    requested = str(requested_summary or "").strip()
    if not normalized_provider or not normalized_model_id or not requested:
        raise ValueError(
            "provider, model_id, and requested_summary are required for repair."
        )
    override_path = resolve_llm_override_path(
        instance_root=instance_root,
        explicit_override_path=explicit_override_path,
    )
    if override_path is None:  # retained for the resolver's optional return contract
        raise ValueError("The LLM override path could not be resolved.")
    override_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path = f"model_capabilities.{normalized_provider}.{normalized_model_id}.reasoning_summary"

    with _exclusive_lock(override_path):
        previous_bytes = override_path.read_bytes() if override_path.exists() else b""
        raw = yaml.safe_load(previous_bytes.decode("utf-8")) if previous_bytes else {}
        payload = _validated_payload(raw, source=override_path)
        model_capabilities = payload.setdefault("model_capabilities", {})
        if not isinstance(model_capabilities, dict):  # defensive after validation
            raise ValueError(f"{override_path}: model_capabilities must be a mapping.")
        provider_map = model_capabilities.get(normalized_provider)
        if provider_map is None:
            provider_map = {}
            model_capabilities[normalized_provider] = provider_map
        if not isinstance(provider_map, dict):
            raise ValueError(
                f"{override_path}: model_capabilities.{normalized_provider} must be a mapping."
            )
        if set(provider_map) & LLM_OVERRIDE_ENTRY_KEYS:
            composite_key = f"{normalized_provider}/{normalized_model_id}"
            model_entry = model_capabilities.setdefault(composite_key, {})
            yaml_path = f"model_capabilities.{composite_key}.reasoning_summary"
        else:
            model_entry = provider_map.setdefault(normalized_model_id, {})
        if not isinstance(model_entry, dict):
            raise ValueError(
                f"{override_path}: {yaml_path.rsplit('.', 1)[0]} must be a mapping."
            )

        if (
            "reasoning_summary" in model_entry
            and model_entry["reasoning_summary"] is None
        ):
            return ReasoningSummaryRepair(
                provider=normalized_provider,
                model_id=normalized_model_id,
                requested_summary=requested,
                override_path=override_path,
                yaml_path=yaml_path,
                backup_path=None,
                changed=False,
            )

        model_entry["reasoning_summary"] = None
        _validated_payload(payload, source=override_path)
        backup_path: Path | None = None
        if previous_bytes:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup_path = override_path.with_name(
                f"{override_path.name}.bak.{timestamp}"
            )
            backup_path.write_bytes(previous_bytes)
            os.chmod(backup_path, stat.S_IMODE(override_path.stat().st_mode))
        _atomic_write(override_path, payload)
        persisted = _validated_payload(
            yaml.safe_load(override_path.read_text(encoding="utf-8")),
            source=override_path,
        )
        persisted_constraints = lookup_configured_model_request_constraints(
            persisted.get("model_capabilities", {}),
            provider=normalized_provider,
            model_id=normalized_model_id,
        )
        if (
            "reasoning_summary" not in persisted_constraints
            or persisted_constraints["reasoning_summary"] is not None
        ):
            raise RuntimeError(f"Failed to verify {yaml_path}=null in {override_path}.")
        prune_config_backups(
            override_path,
            preserve_paths=(backup_path,) if backup_path is not None else (),
        )

    return ReasoningSummaryRepair(
        provider=normalized_provider,
        model_id=normalized_model_id,
        requested_summary=requested,
        override_path=override_path,
        yaml_path=yaml_path,
        backup_path=backup_path,
        changed=True,
    )
