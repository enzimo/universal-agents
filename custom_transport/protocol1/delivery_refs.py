"""Opaque delivery references: where a person is reached, without naming the chat (contract ``delivery_ref``).

A schedule list for one chat names that chat with a ``delivery_ref``: ``dr_`` and a keyed
hash of the gateway's session key. Frames never carry session keys, chat ids or account
ids (a Signal chat id is a phone number), and only the gateway resolves a reference, by
comparing it with the references of its own sessions.

The team services, which read the stored schedules, need the same reference for a
schedule's session to filter a chat's list. So both compute it with one key: the launcher
creates it in the instance's trust folder (``InstanceLayout.trust_delivery_ref_key_path``,
mode 0600, beside the hosts' keys) before any host starts, and the gateway and the team
services read it. An unkeyed hash would let anyone holding a frame test a guessed chat id.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from pathlib import Path

from custom_transport.protocol1.host_keys import _KEY_FILE_VERSION, _read_private, _write_exclusively
from runtime_settings.instance_layout import InstanceLayout

DELIVERY_REF_PREFIX = "dr_"
_DOMAIN = b"universal-agents/delivery-ref/v1\n"


def ensure_delivery_ref_key(layout: InstanceLayout) -> bool:
    """Create the instance's delivery reference key when missing; returns whether this call made it."""

    path = layout.trust_delivery_ref_key_path
    if path.exists():
        return False
    return _write_exclusively(path, {"version": _KEY_FILE_VERSION, "key": secrets.token_urlsafe(32)})


@dataclass(frozen=True)
class DeliveryRefs:
    """Delivery references for one team, with the instance's key."""

    team: str
    key: bytes

    @classmethod
    def load(cls, layout: InstanceLayout, team: str) -> "DeliveryRefs":
        return cls.from_path(layout.trust_delivery_ref_key_path, team)

    @classmethod
    def from_path(cls, path: Path, team: str) -> "DeliveryRefs":
        return cls(team, str(_read_private(path)["key"]).encode())

    def ref(self, session_key: str) -> str:
        """The reference of one gateway session (43 base64url characters after ``dr_``)."""

        digest = hmac.new(self.key, _DOMAIN + f"{self.team}\n{session_key}".encode(), hashlib.sha256).digest()
        return DELIVERY_REF_PREFIX + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
