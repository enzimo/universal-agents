"""Per-process broker tokens: the broker knows which process called it.

The supervisor holds one root secret (``SECURE_TOOL_BROKER_TOKEN`` in
``team.env``) and gives the broker that root. Every other process that calls the
broker gets its own token derived from the root and a caller label, in the same
environment variable, so a process can only present its own label:

* ``gateway`` - acts for a person directly (sign-in, credential forms, approved
  sign-ups); it never acts as an agent;
* ``manager`` - the team lead process, including the workers that run inside it
  (they share the process token, as planned in section 11 of the access plan);
* ``agent/<AgentType>`` - a separately started agent process, which may only act
  as that agent type;
* ``operator`` - the operator's own command on the host (``assistant_cli team``),
  which already holds the root secret; it may reach only the team transfer
  routes. The supervisor never gives this label to a process it starts.

A token is ``ua-caller-v1.<label>.<mac>``, where ``mac`` is HMAC-SHA256 of the
label under the root with a domain prefix. Derivation is deterministic, so a
restarted process gets the same token and the broker needs no list of tokens.
The broker compares in constant time. The root itself is not a caller token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass

TOKEN_PREFIX = "ua-caller-v1"
CALLER_GATEWAY = "gateway"
CALLER_MANAGER = "manager"
CALLER_OPERATOR = "operator"
AGENT_CALLER_PREFIX = "agent/"
_DOMAIN = b"ua-broker-caller-v1\x00"
_LABEL = re.compile(r"^(gateway|manager|operator|agent/[A-Za-z0-9_.-]{1,128})$")


def _mac(root: str, label: str) -> str:
    digest = hmac.new(root.encode("utf-8"), _DOMAIN + label.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def agent_caller_label(agent_type: str) -> str:
    return f"{AGENT_CALLER_PREFIX}{agent_type}"


def derive_caller_token(root: str, label: str) -> str:
    if not root:
        raise ValueError("A broker root secret is required to derive caller tokens.")
    if not _LABEL.match(label):
        raise ValueError(f"Unsupported broker caller label: {label!r}")
    return f"{TOKEN_PREFIX}.{label}.{_mac(root, label)}"


def with_own_caller_token(env: dict[str, str], label: str, *, name: str = "SECURE_TOOL_BROKER_TOKEN") -> None:
    """Replace a root secret in ``env`` with this process's caller token.

    For launchers that hold the root themselves (the supervisor, the manual
    single-agent launcher); a value that is already a caller token is kept.
    """

    value = str(env.get(name) or "").strip()
    if value and not value.startswith(f"{TOKEN_PREFIX}."):
        env[name] = derive_caller_token(value, label)


@dataclass(frozen=True)
class BrokerCaller:
    label: str

    @property
    def is_gateway(self) -> bool:
        return self.label == CALLER_GATEWAY

    @property
    def agent_type(self) -> str | None:
        """The one agent type a separately started agent process may act as."""

        if self.label.startswith(AGENT_CALLER_PREFIX):
            return self.label[len(AGENT_CALLER_PREFIX) :]
        return None

    @property
    def is_operator(self) -> bool:
        return self.label == CALLER_OPERATOR

    @property
    def runs_agents(self) -> bool:
        return self.label == CALLER_MANAGER or self.agent_type is not None


def verify_caller_token(root: str, supplied: str | None) -> BrokerCaller | None:
    """The caller a token belongs to, or None when it is not a valid caller token."""

    prefix, _, rest = str(supplied or "").partition(".")
    label, _, mac = rest.rpartition(".")
    if prefix != TOKEN_PREFIX or not root or not _LABEL.match(label):
        return None
    if not hmac.compare_digest(mac.encode("ascii", "replace"), _mac(root, label).encode("ascii")):
        return None
    return BrokerCaller(label)


__all__ = [
    "AGENT_CALLER_PREFIX",
    "CALLER_OPERATOR",
    "BrokerCaller",
    "CALLER_GATEWAY",
    "CALLER_MANAGER",
    "agent_caller_label",
    "derive_caller_token",
    "verify_caller_token",
    "with_own_caller_token",
]
