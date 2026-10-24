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
    # Audience target -> the address that delivers to it: the kind's own addresses
    # and its copies (registry copy_to). Each address delivers to one target.
    addresses: dict[str, str]
    # The audience targets that get a copy of each frame (README, "Durable copies").
    copies: tuple[str, ...]
    # JSON pointers into the frame whose values make the idempotency key.
    idempotency: tuple[str, ...]
    body_model: TypeAdapter[Any]
    reply_model: Optional[TypeAdapter[Any]]
    reply_errors: tuple[str, ...]
    senders: tuple[str, ...]
    # Audience target -> the fewer roles that may publish to that address (registry
    # senders_by_address); other targets take ``senders``.
    senders_by_target: dict[str, tuple[str, ...]]
    receivers: tuple[str, ...]
    expiry_seconds: int
    authority: str
    # What a token on this kind is bound to: (type, JSON pointer to the body field
    # holding the id), or (type, None) when the handler checks the task it holds.
    authority_binding: Optional[tuple[str, Optional[str]]]
    # What the schema cannot say, checked after it (README, "What bodies carry").
    body_rules: tuple[dict[str, Any], ...]
    execution_slot: bool
    accepted_while_paused: bool
    priority: str
    routing: str
    # The edges an agent may send it on (team-topologies.yaml), or ("none",).
    topology_edges: tuple[str, ...]
    ledger: str
    wakes_model: Any
    rendering: str
    body_shape: Any
    reply_shape: Any
    # standard, or proof_of_possession: signed by the key the body presents (a join).
    verification: str = "standard"

    def prune(self, body: Any) -> Any:
        return prune(body, self.body_shape)


@dataclass(frozen=True)
class StateSpec:
    """A signed key-value record: its bucket and key form, who signs and stores it, and its body model."""

    name: str
    bucket: str
    key: str
    signer: str
    signer_roles: tuple[str, ...]
    writers: tuple[str, ...]
    body_model: TypeAdapter[Any]
