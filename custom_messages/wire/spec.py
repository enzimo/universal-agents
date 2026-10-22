"""What the registry states about one kind, and dropping unknown fields from a newer minor version."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from pydantic import TypeAdapter


def prune(value: Any, shape: Any) -> Any:
    """Keep only what ``shape`` names. Used for frames from a newer minor version, whose
    unknown optional fields are ignored (contracts/messages/v1/README.md, "Versions").
    Anything the shape does not describe is left for strict validation to judge."""

    if shape is None:
        return value
    if "one_of" in shape:
        if isinstance(value, dict):
            variant = shape["variants"].get(str(value.get(shape["one_of"])))
            if variant is not None:
                return prune(value, variant)
        return value
    if "properties" in shape and isinstance(value, dict):
        return {key: prune(item, shape["properties"][key]) for key, item in value.items() if key in shape["properties"]}
    if "items" in shape and isinstance(value, list):
        return [prune(item, shape["items"]) for item in value]
    return value


@dataclass(frozen=True)
class KindSpec:
    name: str
    delivery_class: str
    addresses: tuple[str, ...]
    body_model: TypeAdapter[Any]
    reply_model: Optional[TypeAdapter[Any]]
    reply_errors: tuple[str, ...]
    senders: tuple[str, ...]
    receivers: tuple[str, ...]
    expiry_seconds: int
    authority: str
    execution_slot: bool
    accepted_while_paused: bool
    priority: str
    routing: str
    topology_edge: str
    ledger: str
    wakes_model: Any
    rendering: str
    body_shape: Any
    reply_shape: Any

    verification: str = "standard"

    def prune(self, body: Any) -> Any:
        return prune(body, self.body_shape)


@dataclass(frozen=True)
class StateSpec:
    """A signed key-value record: its bucket, key form, the roles that may write it and its body model."""

    name: str
    bucket: str
    key: str
    writers: tuple[str, ...]
    ttl_seconds: Optional[int]
    body_model: TypeAdapter[Any]
