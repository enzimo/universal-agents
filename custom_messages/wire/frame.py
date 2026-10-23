"""Sign and verify protocol 1 frames (contracts/messages/v1).

A frame is signed with Ed25519 over ``SIGNING_DOMAIN`` followed by the canonical
JSON of the frame without its signature. ``verify_frame`` runs the receiver's
checks in the registry's order and refuses with the first failing code, so every
implementation refuses the same frame the same way. Checks that need runtime
state (keys, the receiver, the time, the replay record) use what the caller
passes; without it, that check is skipped.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import ValidationError

from custom_messages.wire.canonical import CanonicalJsonError, canonical_bytes, parse
from custom_messages.wire.kinds import FRAME_SHAPE, KINDS, MAX_FRAME_BYTES, PROTOCOL, RENEW_KEY_PROOF_DOMAIN, SIGNING_DOMAIN
from custom_messages.wire.models import Frame
from custom_messages.wire.spec import prune

_DOMAIN_PREFIX = SIGNING_DOMAIN.encode("ascii") + b"\n"
# Installation-wide traffic (joining, renewing) names this scope instead of a team.
INSTALLATION_SCOPE = "_install"
_HOST_RENEW = "host.renew"
_KEY_ROTATION = "application_key_rotation"


class FrameRefused(ValueError):
    """A receiver refuses the frame; ``code`` is one of the registry's verification refusals."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def format_timestamp(moment: datetime) -> str:
    """UTC with exactly three fractional digits, as the protocol requires."""

    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_timestamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def body_digest(body: Mapping[str, Any]) -> str:
    return b64url(hashlib.sha256(canonical_bytes(dict(body))).digest())


def signing_bytes(frame: Mapping[str, Any]) -> bytes:
    """What the signature covers: the domain prefix and the canonical frame without its signature."""

    unsigned = {key: value for key, value in frame.items() if key != "signature"}
    return _DOMAIN_PREFIX + canonical_bytes(unsigned)


def sign_frame(unsigned: Mapping[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Return ``unsigned`` with its body digest and signature filled in."""

    frame = {key: value for key, value in unsigned.items() if key not in {"signature", "body_digest"}}
    frame["algorithm"] = "ed25519"
    frame["body_digest"] = body_digest(frame["body"])
    frame["signature"] = b64url(private_key.sign(signing_bytes(frame)))
    return frame


def renew_key_proof_bytes(frame: Mapping[str, Any]) -> bytes:
    """Bind the new key to the complete renewal, excluding the dependent signatures.

    The old key signs the finished frame, including this proof. Omitting the
    body digest and both signatures here avoids a circular signing dependency.
    """

    if frame["kind"] != _HOST_RENEW:
        raise ValueError("a renewal key proof belongs only to host.renew")
    unsigned = {key: value for key, value in frame.items() if key not in {"signature", "body_digest"}}
    unsigned["algorithm"] = "ed25519"
    body = dict(frame["body"])
    body[_KEY_ROTATION] = {key: value for key, value in body[_KEY_ROTATION].items() if key != "proof"}
    unsigned["body"] = body
    return RENEW_KEY_PROOF_DOMAIN.encode("ascii") + b"\n" + canonical_bytes(unsigned)


def sign_renew_key_proof(frame: Mapping[str, Any], new_private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Fill the new key's proof before calling ``sign_frame`` with the old key."""

    body = dict(frame["body"])
    body[_KEY_ROTATION] = dict(body[_KEY_ROTATION], proof=b64url(new_private_key.sign(renew_key_proof_bytes(frame))))
    return dict(frame, body=body)


@dataclass(frozen=True)
class TrustedKey:
    """A key the receiver trusts, from the certificate valid when frames were issued."""

    public_key: Ed25519PublicKey
    team: str
    runtime: str
    revoked_at: Optional[datetime] = None
    min_membership_epoch: int = 0
    min_revocation_epoch: int = 0


# Records a frame by (issuer runtime, membership epoch, message id) with its digest, and
# returns the digest stored earlier for the same key, if any.
ReplayRecord = Callable[[str, int, str, str], Optional[str]]


@dataclass(frozen=True)
class Receiver:
    team: str
    target: str
    id: str


@dataclass(frozen=True)
class VerifiedFrame:
    frame: Frame
    body: Any
    raw: dict[str, Any] = field(repr=False)


def protocol_minor(label: str) -> int:
    return int(label.split(".", 1)[1].split("-", 1)[0])


def _newer_minor(raw: Mapping[str, Any]) -> bool:
    label = raw.get("protocol")
    try:
        return isinstance(label, str) and label.startswith("1.") and protocol_minor(label) > protocol_minor(PROTOCOL)
    except ValueError:
        return False


def body_model(kind: str) -> tuple[Any, Any]:
    """The body model and shape for a kind, or for an operation's reply (``<operation>.reply``)."""

    spec = KINDS.get(kind)
    if spec is not None:
        return spec.body_model, spec.body_shape
    if kind.endswith(".reply"):
        spec = KINDS.get(kind.removesuffix(".reply"))
        if spec is not None and spec.reply_model is not None:
            return spec.reply_model, spec.reply_shape
    raise FrameRefused("unknown_kind", kind)


def _verification_key(raw: Mapping[str, Any], frame: Frame, trusted: Optional[TrustedKey],
                      proof_of_possession: bool) -> Ed25519PublicKey:
    if trusted is not None and (trusted.runtime != frame.issuer.runtime
                               or frame.issuer.team not in {trusted.team, INSTALLATION_SCOPE}):
        raise FrameRefused("unknown_key", frame.issuer.key_id)
    if not proof_of_possession:
        if trusted is None:
            raise FrameRefused("unknown_key", frame.issuer.key_id)
        return trusted.public_key
    # Joining always proves possession of the presented key, including retries
    # after the authority has learned this key id. A known id cannot change keys.
    presented = raw["body"].get("application_key")
    if not isinstance(presented, dict) or presented.get("key_id") != frame.issuer.key_id:
        raise FrameRefused("unknown_key", frame.issuer.key_id)
    try:
        public_key = Ed25519PublicKey.from_public_bytes(b64url_decode(str(presented.get("public_key", ""))))
    except ValueError as error:
        raise FrameRefused("unknown_key", frame.issuer.key_id) from error
    if trusted is not None and public_key.public_bytes_raw() != trusted.public_key.public_bytes_raw():
        raise FrameRefused("unknown_key", "a known key id must present the same public key")
    return public_key


def _verify_renew_key_proof(raw: Mapping[str, Any]) -> None:
    if raw["kind"] != _HOST_RENEW or _KEY_ROTATION not in raw["body"]:
        return
    rotation = raw["body"][_KEY_ROTATION]
    if not isinstance(rotation, dict):
        raise FrameRefused("invalid_body", "application_key_rotation must contain a key and proof")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(b64url_decode(rotation["key"]["public_key"]))
        public_key.verify(b64url_decode(rotation["proof"]), renew_key_proof_bytes(raw))
    except (InvalidSignature, ValueError) as error:
        raise FrameRefused("invalid_body", "the renewal's new-key proof does not verify") from error


def verify_frame(
    data: bytes,
    *,
    keys: Mapping[str, TrustedKey],
    arrived_on: Optional[str] = None,
    receiver: Optional[Receiver] = None,
    now: Optional[datetime] = None,
    replay: Optional[ReplayRecord] = None,
) -> VerifiedFrame:
    """Check one received frame in the registry's order; raise ``FrameRefused`` on the first failure."""

    if len(data) > MAX_FRAME_BYTES:
        raise FrameRefused("too_large", f"{len(data)} bytes")
    try:
        raw = parse(data)
        newer_minor = _newer_minor(raw)
        frame = Frame.model_validate(prune(raw, FRAME_SHAPE) if newer_minor else raw)
    except (CanonicalJsonError, ValidationError) as error:
        raise FrameRefused("malformed", str(error).splitlines()[0]) from error
    spec = KINDS.get(frame.kind)
    trusted = keys.get(frame.issuer.key_id)
    proof_of_possession = spec is not None and spec.verification == "proof_of_possession"
    public_key = _verification_key(raw, frame, trusted, proof_of_possession)
    try:
        public_key.verify(b64url_decode(frame.signature), signing_bytes(raw))
    except (InvalidSignature, ValueError) as error:
        raise FrameRefused("bad_signature") from error
    if body_digest(raw["body"]) != frame.body_digest:
        raise FrameRefused("bad_digest")
    # Queued work is verified with the certificate valid when it was issued; a revoked
    # certificate refuses it whenever it was revoked (contracts/messages/v1/README.md).
    if trusted is not None and trusted.revoked_at is not None:
        raise FrameRefused("revoked")
    queued = spec is not None and spec.delivery_class in {"durable", "dead_letter"}
    # A join has no certificate epochs, even when its key became known after
    # an earlier attempt. The same signed retry must recover a lost reply.
    if trusted is not None and not queued and not proof_of_possession and (
            frame.issuer.membership_epoch < trusted.min_membership_epoch
            or frame.issuer.revocation_epoch < trusted.min_revocation_epoch):
        raise FrameRefused("epoch_too_old")
    if receiver is not None:
        if frame.audience.team != receiver.team:
            raise FrameRefused("wrong_team")
        if (frame.audience.target, frame.audience.id) != (receiver.target, receiver.id):
            raise FrameRefused("wrong_audience")
    subject_tokens = frame.subject.split(".")
    if subject_tokens[2] != frame.issuer.team or subject_tokens[3] != frame.issuer.runtime:
        raise FrameRefused("subject_mismatch", "the subject's team and sender must be the issuer's")
    if arrived_on is not None and arrived_on != frame.subject:
        raise FrameRefused("subject_mismatch", "the frame arrived on another subject")
    if now is not None:
        if now >= parse_timestamp(frame.expires_at):
            raise FrameRefused("expired")
        if frame.not_before is not None and now < parse_timestamp(frame.not_before):
            raise FrameRefused("not_yet_valid")
    if replay is not None:
        earlier = replay(frame.issuer.runtime, frame.issuer.membership_epoch, frame.message_id, frame.body_digest)
        if earlier is not None and earlier != frame.body_digest:
            raise FrameRefused("replayed")
    model, shape = body_model(frame.kind)
    try:
        body = model.validate_python(prune(raw["body"], shape) if newer_minor else raw["body"])
    except ValidationError as error:
        raise FrameRefused("invalid_body", str(error).splitlines()[0]) from error
    _verify_renew_key_proof(raw)
    return VerifiedFrame(frame=frame, body=body, raw=raw)
