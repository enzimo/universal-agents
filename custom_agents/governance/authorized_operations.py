"""Shared mechanics for staged, exact changes (``access_control.changes``).

Digest helpers, the file-backed plan store, atomic writes and the audit event
that every change service uses. Who may apply a change is decided elsewhere
(``custom_tools.providers.changes``); nothing here grants anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import re
from typing import Any, Generic, Mapping, Protocol, TypeVar, cast, runtime_checkable

from pydantic import BaseModel, ValidationError

from id_generation import new_id_suffix


PlanT = TypeVar("PlanT", bound=BaseModel)


def canonical_json(value: Any) -> str:
    """Serialize digest input deterministically without leaking domain policy here."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 digest of exact bytes."""

    return hashlib.sha256(value).hexdigest()


def canonical_json_sha256(value: Any) -> str:
    """Hash a value using the canonical JSON representation used by operation plans."""

    return sha256_bytes(canonical_json(value).encode("utf-8"))


def model_digest(model: BaseModel, *, digest_field: str = "plan_digest") -> str:
    """Digest a Pydantic model while treating its own digest field as empty."""

    payload = model.model_dump(mode="json")
    if digest_field not in payload:
        raise ValueError(f"Model does not define digest field {digest_field!r}.")
    payload[digest_field] = ""
    return canonical_json_sha256(payload)




def atomic_write_bytes(path: Path, content: bytes, *, immutable: bool = False) -> None:
    """Atomically replace one file, optionally making the completed artifact read-only."""

    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{new_id_suffix()}.tmp")
    try:
        temporary.write_bytes(content)
        if immutable:
            # Complete permission changes before publication. A failed
            # chmod must not leave a visible record callers treat as committed.
            temporary.chmod(0o444)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    immutable: bool = False,
) -> None:
    """Write a human-readable JSON artifact through the audited atomic primitive."""

    content = (
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, content, immutable=immutable)


@dataclass(frozen=True)
class GovernanceAuditEvent:
    """Secret-free lifecycle event owned by the governance boundary."""

    operation: str
    phase: str
    change_id: str
    plan_digest: str = ""
    task_id: str = ""
    authority_mode: str = ""
    occurred_at_utc: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@runtime_checkable
class GovernanceAuditSink(Protocol):
    """Observability port; implementations must not add plan payloads or secrets."""

    def record(self, event: GovernanceAuditEvent) -> None:
        """Record one lifecycle event."""





@runtime_checkable
class PlanStore(Protocol, Generic[PlanT]):
    """Operation-owned immutable plan persistence boundary."""

    def save(self, plan: PlanT) -> Path:
        """Persist one immutable plan."""

    def load(self, change_id: str) -> PlanT:
        """Load one plan using its operation-owned model type."""


@dataclass(frozen=True)
class FilePlanStore(Generic[PlanT]):
    """Current local-disk plan adapter; callers retain directory ownership."""

    directory: Path
    model_type: type[PlanT]
    change_id_pattern: re.Pattern[str] | None = None
    invalid_change_id_message: str = "Invalid authorized-operation change id."
    missing_plan_message: str = "Authorized-operation plan was not found: {change_id}"
    unavailable_plan_message: str | None = None

    def _normalize_change_id(self, change_id: str) -> str:
        normalized = str(change_id or "").strip()
        if self.change_id_pattern is not None and not self.change_id_pattern.fullmatch(
            normalized
        ):
            raise ValueError(self.invalid_change_id_message)
        return normalized

    def path_for(self, change_id: str) -> Path:
        normalized = self._normalize_change_id(change_id)
        return self.directory / f"{normalized}.json"

    def save(self, plan: PlanT) -> Path:
        path = self.path_for(str(getattr(plan, "change_id")))
        atomic_write_json(path, plan.model_dump(mode="json"), immutable=True)
        return path

    def load(self, change_id: str) -> PlanT:
        normalized = self._normalize_change_id(change_id)
        path = self.path_for(normalized)
        if not path.is_file():
            if self.unavailable_plan_message is not None:
                error = FileNotFoundError(path)
                raise ValueError(
                    self.unavailable_plan_message.format(error=error)
                ) from error
            raise FileNotFoundError(
                self.missing_plan_message.format(change_id=normalized)
            )
        try:
            return cast(
                PlanT,
                self.model_type.model_validate_json(path.read_text(encoding="utf-8")),
            )
        except (OSError, ValidationError) as exc:
            if self.unavailable_plan_message is None:
                raise
            raise ValueError(self.unavailable_plan_message.format(error=exc)) from exc
