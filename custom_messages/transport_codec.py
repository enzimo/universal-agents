"""The JSON encoding boundary for the version 1 transport contract."""

import json

from custom_messages.contracts.envelope_v1 import MessageEnvelope


def encode_envelope(envelope: MessageEnvelope) -> bytes:
    return envelope.model_dump_json(by_alias=True).encode("utf-8")


def decode_envelope(payload: bytes | str) -> MessageEnvelope:
    # Local construction may create an identity. Receiving already-authored
    # wire data must preserve one, never mint one during validation/redelivery.
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError("A transport envelope must be a JSON object")
    header = document.get("msg_header")
    if not isinstance(header, dict) or not header.get("message_id") or not header.get("timestamp"):
        raise ValueError("A transport envelope requires its original message_id and timestamp")
    return MessageEnvelope.model_validate(document)
