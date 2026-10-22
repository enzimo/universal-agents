"""Sign and verify protocol 1 state documents: the signed key-value records (contracts/messages/v1).

A state document is signed with Ed25519 over ``STATE_SIGNING_DOMAIN`` followed by
the canonical JSON of the document without its signature. ``verify_state`` checks
one document as a reader does and refuses with the frame refusal codes. The
writer's certificate checks (the certificate valid when the record was written,
unless revoked since) use the trusted keys the caller passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from custom_messages.wire.canonical import CanonicalJsonError, canonical_bytes, parse
from custom_messages.wire.frame import FrameRefused, TrustedKey, b64url, b64url_decode, body_digest, parse_timestamp
from custom_messages.wire.kinds import STATE_RECORDS, STATE_SIGNING_DOMAIN
from custom_messages.wire.models import State

_DOMAIN_PREFIX = STATE_SIGNING_DOMAIN.encode("ascii") + b"\n"


def state_signing_bytes(document: Mapping[str, Any]) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    return _DOMAIN_PREFIX + canonical_bytes(unsigned)


def sign_state(document: Mapping[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Return ``document`` with its body digest and signature filled in."""

    signed = {key: value for key, value in document.items() if key not in {"signature", "body_digest"}}
    signed["algorithm"] = "ed25519"
    signed["body_digest"] = body_digest(signed["body"])
    signed["signature"] = b64url(private_key.sign(state_signing_bytes(signed)))
    return signed


def _expected_key(record: str, body: Mapping[str, Any]) -> str | None:
    """The key a record must be stored under, where its body determines it."""

    if record in {"agent", "placement"}:
        return f"{body['team']}.{body['agent']}"
    if record == "host":
        return str(body["runtime"])
    if record == "certificate":
        return f"{body['runtime']}.{body['membership_epoch']}"
    if record == "revocations":
        return "revocations"
    return None


@dataclass(frozen=True)
class VerifiedState:
    document: State
    body: Any


def verify_state(data: bytes, *, keys: Mapping[str, TrustedKey]) -> VerifiedState:
    try:
        raw = parse(data)
        document = State.model_validate(raw)
    except (CanonicalJsonError, ValidationError) as error:
        raise FrameRefused("malformed", str(error).splitlines()[0]) from error
    trusted = keys.get(document.writer.key_id)
    if trusted is None or trusted.runtime != document.writer.runtime:
        raise FrameRefused("unknown_key", document.writer.key_id)
    try:
        trusted.public_key.verify(b64url_decode(document.signature), state_signing_bytes(raw))
    except (InvalidSignature, ValueError) as error:
        raise FrameRefused("bad_signature") from error
    if body_digest(raw["body"]) != document.body_digest:
        raise FrameRefused("bad_digest")
    if trusted.revoked_at is not None:
        raise FrameRefused("revoked")
    spec = STATE_RECORDS[document.record]
    try:
        body = spec.body_model.validate_python(raw["body"])
    except ValidationError as error:
        raise FrameRefused("invalid_body", str(error).splitlines()[0]) from error
    expected = _expected_key(document.record, raw["body"])
    if expected is not None and expected != document.key:
        raise FrameRefused("invalid_body", f"a {document.record} record belongs under key {expected}")
    if document.expires_at is not None and parse_timestamp(document.expires_at) <= parse_timestamp(document.issued_at):
        raise FrameRefused("invalid_body", "a record cannot expire before it is written")
    return VerifiedState(document=document, body=body)
