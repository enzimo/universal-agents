"""Load the protocol 1 examples as contracts/messages/v1/README.md ("Examples") describes.

Shared by the contract tests and the live NATS contract tests. A receiver of the
examples starts from ``examples/trust/anchors.json`` and every record under
``examples/trust`` except ``extra/``; an example's context adds or removes records,
sets minimum epochs, and replaces the receiver, time, arrival subject, NATS headers,
replay record or answered request.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from custom_messages.wire.encoding import b64url_decode
from custom_messages.wire.frame import Receiver, ReplayKey, ReplayLookup, VerifiedFrame, sign_frame, verify_frame
from custom_messages.wire.models import Frame
from custom_messages.wire.trust import AuthorityKey, TrustStore, VerifiedRecord
from custom_messages.wire.values import parse_timestamp

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "messages" / "v1"
EXAMPLES = CONTRACT / "examples"
TRUST = EXAMPLES / "trust"
KEYS: dict[str, dict[str, str]] = json.loads((EXAMPLES / "keys.json").read_text(encoding="utf-8"))["keys"]
NOW = datetime(2026, 10, 22, 4, 30, 1, tzinfo=timezone.utc)
# Signers must be known before the records they sign.
RECORD_ORDER = ("agreement", "certificate", "revocations", "agent_session")
# The receiving host when a frame's audience names none.
RECEIVING_HOST = "receiving-host"
# Edge routes the examples' edge adapter serves for outside agents (others are gateway routes).
OUTSIDE_EDGES = {"a2a_partner"}
_UNSET: Any = object()


def private_key(key_id: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(b64url_decode(KEYS[key_id]["seed"]))


def anchors() -> list[AuthorityKey]:
    document = json.loads((TRUST / "anchors.json").read_text(encoding="utf-8"))
    return [AuthorityKey(document["installation"], key["key_id"], Ed25519PublicKey.from_public_bytes(b64url_decode(key["public_key"])))
            for key in document["authority_keys"]]


def default_records() -> list[str]:
    return sorted(path.relative_to(TRUST).as_posix() for path in TRUST.rglob("*.json")
                  if path.name != "anchors.json" and path.parent.name != "extra")


def trust_store(add: Iterable[str] = (), remove: Iterable[str] = (), extra_documents: Iterable[bytes] = (),
                now: datetime = NOW) -> TrustStore:
    """The examples' trust store, loaded at ``now``; ``add`` and ``remove`` name records under examples/trust."""

    names = [name for name in default_records() if name not in set(remove)] + list(add)
    documents = [(TRUST / name).read_bytes() for name in names] + list(extra_documents)
    documents.sort(key=lambda data: RECORD_ORDER.index(json.loads(data)["record"]))
    store = TrustStore(anchors()[0].installation, anchors())
    for data in documents:
        store.add(data, now=now)
    return store


def valid_frames() -> dict[str, dict[str, Any]]:
    return {path.relative_to(EXAMPLES / "valid").as_posix(): json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((EXAMPLES / "valid").rglob("*.json"))}


def agent_roles() -> dict[tuple[str, str], str]:
    """Each example agent's place in its team, from its session record."""

    roles = {}
    for path in (TRUST / "sessions").glob("*.json"):
        body = json.loads(path.read_text(encoding="utf-8"))["body"]
        roles[(body["team"], body["agent"])] = body["agent_role"]
    return roles


def default_receiver(frame: Mapping[str, Any]) -> Receiver:
    audience = frame["audience"]
    role = agent_roles().get((audience["team"], audience["id"])) if audience["target"] == "agent" else None
    outside = audience["target"] == "edge" and audience["id"] in OUTSIDE_EDGES
    return Receiver(audience["installation"], audience["team"], audience["target"], audience["id"],
                    audience.get("runtime", RECEIVING_HOST), role, outside)


def replay_holding(digest: Optional[str] = None) -> ReplayLookup:
    """A replay record that holds ``digest`` for every key (none when ``digest`` is None)."""

    return lambda _key: digest


def answered_request(frame: Mapping[str, Any], name: Any = _UNSET) -> Optional[Frame]:
    """The request a reply answers: a named valid example, or the one whose message id it names."""

    if name is None or not str(frame.get("kind", "")).endswith(".reply"):
        return None
    frames = valid_frames()
    if name is not _UNSET:
        return Frame.model_validate(frames[name])
    for candidate in frames.values():
        if candidate["message_id"] == frame.get("causation_id"):
            return Frame.model_validate(candidate)
    return None


def resign(frame: Mapping[str, Any]) -> bytes:
    """``frame`` signed again by its issuer's test key."""

    return json.dumps(sign_frame(frame, private_key(frame["issuer"]["key_id"]))).encode()


def verify(data: bytes, frame: Mapping[str, Any], context: Optional[Mapping[str, Any]] = None, *,
           trust: Optional[TrustStore] = None) -> VerifiedFrame:
    """Verify ``data`` (whose parsed content is ``frame``) with the defaults an example's context overrides."""

    context = context or {}
    if trust is None:
        trust = trust_store(**{key: value for key, value in context.get("trust", {}).items() if key in {"add", "remove"}},
                            now=parse_timestamp(context["now"]) if "now" in context else NOW)
    for minimum in context.get("minimum_epochs", []):
        trust.require_epochs(minimum["installation"], minimum.get("runtime"),
                             membership=minimum.get("membership", 0), revocation=minimum.get("revocation", 0))
    receiver = replace(default_receiver(frame), **context.get("receiver", {}))
    digest = context.get("replay", {}).get("digest")
    return verify_frame(
        data,
        trust=trust,
        receiver=receiver,
        arrived_on=context.get("arrived_on", frame.get("subject")),
        now=parse_timestamp(context["now"]) if "now" in context else NOW,
        replay=replay_holding(digest),
        headers=context.get("headers"),
        answering=answered_request(frame, context.get("answering", _UNSET)),
    )


def verify_state(data: bytes, context: Optional[Mapping[str, Any]] = None, *, trust: Optional[TrustStore] = None,
                 record: Optional[str] = None) -> VerifiedRecord:
    """Read a state report as a reader does, with the defaults a state example's context overrides.

    ``record`` is the record type the reader reads; by default the one the document
    names. The context may add or remove trust records, set minimum epochs, replace the
    time (``now``) and name the record the reader holds for the key (``held``: sequence
    and digest).
    """

    context = context or {}
    if trust is None:
        trust = trust_store(**{key: value for key, value in context.get("trust", {}).items() if key in {"add", "remove"}},
                            now=parse_timestamp(context["now"]) if "now" in context else NOW)
    for minimum in context.get("minimum_epochs", []):
        trust.require_epochs(minimum["installation"], minimum.get("runtime"),
                             membership=minimum.get("membership", 0), revocation=minimum.get("revocation", 0))
    held = context.get("held")
    return trust.verify(data, record=record or json.loads(data)["record"],
                        now=parse_timestamp(context["now"]) if "now" in context else NOW,
                        held=(held["sequence"], held["digest"]) if held else None)


def example_frame(kind: str, name: str = "basic") -> dict[str, Any]:
    return json.loads((EXAMPLES / "valid" / kind / f"{name}.json").read_text(encoding="utf-8"))


__all__ = [
    "CONTRACT", "EXAMPLES", "KEYS", "NOW", "ReplayKey", "anchors", "answered_request", "default_receiver", "example_frame",
    "private_key", "replay_holding", "resign", "trust_store", "valid_frames", "verify", "verify_state",
]
