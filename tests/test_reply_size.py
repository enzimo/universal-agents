"""A reply's body budget leaves room for the longest envelope the contract allows (custom_messages.wire.reply_size)."""

from __future__ import annotations

from custom_messages.wire.kinds import MAX_FRAME_BYTES
from custom_messages.wire.models import Frame
from custom_messages.wire.reply_size import REPLY_BODY_MAX_BYTES, REPLY_ENVELOPE_BYTES, body_bytes, body_fits
from custom_transport.protocol1.live import encode

MAX_SAFE = 2**53 - 1


def _longest_reply_envelope() -> dict:
    """A reply frame with an empty body and every envelope field as long as its schema allows."""

    token = "t" * 64
    return {
        "protocol": "1.9999-pre",
        "kind": "k" * 41 + "." + "r" * 42,
        "message_id": "01890a5d-ac96-774b-bcce-b302099a8057",
        "idempotency_key": "k" * 41 + "." + "r" * 42 + ":" + "d" * 43,
        "issuer": {"installation": "i" * 62, "team": "m" * 24, "runtime": token, "agent": "a" * 32,
                   "session": token, "membership_epoch": MAX_SAFE, "revocation_epoch": MAX_SAFE, "key_id": "y" * 128},
        "audience": {"installation": "i" * 62, "team": "m" * 24, "target": "installation_service", "id": token,
                     "runtime": token},
        "subject": "_INBOX." + token + "." + "x" * (512 - len("_INBOX.") - len(token) - 1),
        "causation_id": "01890a5d-ac96-774b-bcce-b302099a8058",
        "issued_at": "2026-10-22T04:30:01.000Z",
        "not_before": "2026-10-22T04:30:01.000Z",
        "expires_at": "2026-10-22T04:30:31.000Z",
        "algorithm": "ed25519",
        "body_digest": "d" * 43,
        "body": {},
        "signature": "s" * 86,
    }


def test_the_longest_reply_envelope_takes_well_under_its_allowance() -> None:
    frame = _longest_reply_envelope()
    Frame.model_validate(frame)  # every value is one the contract allows

    envelope = len(encode(frame)[0])

    assert envelope * 2 < REPLY_ENVELOPE_BYTES
    assert REPLY_BODY_MAX_BYTES + REPLY_ENVELOPE_BYTES == MAX_FRAME_BYTES


def test_a_body_is_measured_as_the_frame_carries_it() -> None:
    body = {"text": "界 «quoted» \"escaped\"\n"}
    frame = {**_longest_reply_envelope(), "body": body}

    assert body_bytes(body) == len(encode(frame)[0]) - len(encode(_longest_reply_envelope())[0]) + len("{}")
    assert body_fits({"text": "x" * (REPLY_BODY_MAX_BYTES - 11)})
    assert not body_fits({"text": "x" * (REPLY_BODY_MAX_BYTES - 10)})
