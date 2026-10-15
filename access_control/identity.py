"""Signed task contexts: who an agent task works for, verifiably.

The gateway signs a context when it admits a person's task (issuer
``gateway``); the scheduler in the manager runtime signs one when a schedule
fires for its owner (issuer ``scheduler``). The context travels inside
``GatewayContext.task_context``, which the runtime already copies onto every
delegated task, reply and scheduled run, so later hops carry the root context
unchanged and cannot widen it.

Keys are Ed25519. Private keys live only in the gateway and manager processes
(``ACCESS_GATEWAY_SIGNING_KEY``, ``ACCESS_SCHEDULER_SIGNING_KEY``); every
verifier gets the public keys (``ACCESS_CONTEXT_VERIFY_KEYS``). All come from
``team.env`` (``internal_credentials``), which agent-run code cannot read.
The signature covers canonical JSON with a domain prefix, so it cannot be
replayed as any other signed object.

The scheduler key also signs schedule permits (``SchedulePermitV1``): one
standing statement per schedule that its runs work for its owner, so a worker
without the key can start runs without asking the manager each time
(``custom_agents.schedule_permits``, checked by ``access_control.task_identity``).
A permit is signed under its own domain prefix and names its own record kind,
so a permit never verifies as a task context and a task context never
verifies as a permit.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from custom_messages.contracts.access_v2 import (
    SchedulePermitV1,
    SignedSchedulePermitV1,
    SignedTaskContextV1,
    TaskContextV1,
)
from id_generation import new_id

logger = logging.getLogger(__name__)

ENV_GATEWAY_SIGNING_KEY = "ACCESS_GATEWAY_SIGNING_KEY"
ENV_SCHEDULER_SIGNING_KEY = "ACCESS_SCHEDULER_SIGNING_KEY"
ENV_VERIFY_KEYS = "ACCESS_CONTEXT_VERIFY_KEYS"
ISSUER_GATEWAY = "gateway"
ISSUER_SCHEDULER = "scheduler"
DEFAULT_CONTEXT_LIFETIME = timedelta(days=7)
# A schedule permit lasts 30 days; the manager renews it in its last 7.
SCHEDULE_PERMIT_LIFETIME = timedelta(days=30)
_DOMAIN = b"ua-task-context-v1\x00"
_PERMIT_DOMAIN = b"ua-schedule-permit-v1\x00"


class IdentityRejected(PermissionError):
    """A task context is missing, forged, expired, or from an unknown issuer."""


def _canonical_json(record: TaskContextV1 | SchedulePermitV1) -> bytes:
    return json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical(context: TaskContextV1) -> bytes:
    return _DOMAIN + _canonical_json(context)


def _canonical_permit(permit: SchedulePermitV1) -> bytes:
    return _PERMIT_DOMAIN + _canonical_json(permit)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def generate_signing_key(issuer: str) -> tuple[str, dict[str, str]]:
    """A new private key value for team.env and its public verification entry."""

    private = Ed25519PrivateKey.generate()
    key_id = f"{issuer}-{new_id('key').split('_')[-1][:12]}"
    seed = private.private_bytes_raw()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return f"{key_id}:{_b64(seed)}", {"key_id": key_id, "issuer": issuer, "public_key": _b64(public)}


@dataclass(frozen=True)
class ContextSigner:
    issuer: str
    key_id: str
    private_key: Ed25519PrivateKey

    @classmethod
    def from_value(cls, issuer: str, value: str) -> "ContextSigner":
        key_id, _, seed = str(value or "").strip().partition(":")
        if not key_id or not seed:
            raise ValueError(f"The {issuer} signing key is not in '<key_id>:<base64>' form.")
        return cls(issuer, key_id, Ed25519PrivateKey.from_private_bytes(_unb64(seed)))

    @classmethod
    def from_env(cls, issuer: str, env: Mapping[str, str] | None = None) -> "ContextSigner | None":
        source = os.environ if env is None else env
        name = ENV_GATEWAY_SIGNING_KEY if issuer == ISSUER_GATEWAY else ENV_SCHEDULER_SIGNING_KEY
        value = str(source.get(name) or "").strip()
        return cls.from_value(issuer, value) if value else None

    def sign(self, context: TaskContextV1) -> SignedTaskContextV1:
        if context.issuer != self.issuer:
            raise ValueError(f"A {self.issuer} key cannot sign a {context.issuer} context.")
        return SignedTaskContextV1(
            context=context, key_id=self.key_id, signature=_b64(self.private_key.sign(_canonical(context)))
        )

    def issue(
        self,
        *,
        team_id: str,
        person_id: str | None,
        person_roles: tuple[str, ...] = (),
        root_task_id: str | None = None,
        conversation_id: str | None = None,
        schedule_id: str | None = None,
        lifetime: timedelta = DEFAULT_CONTEXT_LIFETIME,
        now: datetime | None = None,
    ) -> SignedTaskContextV1:
        issued = now or datetime.now(timezone.utc)
        return self.sign(
            TaskContextV1(
                context_id=new_id("task_context"),
                issuer=self.issuer,
                team_id=team_id,
                person_id=person_id,
                person_roles=tuple(sorted(set(person_roles))),
                root_task_id=root_task_id,
                conversation_id=conversation_id,
                schedule_id=schedule_id,
                issued_at_utc=issued,
                expires_at_utc=issued + lifetime,
            )
        )

    def issue_permit(
        self,
        *,
        team_id: str,
        schedule_id: str,
        person_id: str | None,
        lifetime: timedelta = SCHEDULE_PERMIT_LIFETIME,
        now: datetime | None = None,
    ) -> SignedSchedulePermitV1:
        """A schedule permit (module docstring); only the scheduler signs them."""

        if self.issuer != ISSUER_SCHEDULER:
            raise ValueError(f"A {self.issuer} key cannot sign a schedule permit.")
        issued = now or datetime.now(timezone.utc)
        permit = SchedulePermitV1(
            permit_id=new_id("schedule_permit"),
            issuer=ISSUER_SCHEDULER,
            team_id=team_id,
            schedule_id=schedule_id,
            person_id=person_id,
            issued_at_utc=issued,
            expires_at_utc=issued + lifetime,
        )
        return SignedSchedulePermitV1(
            permit=permit, key_id=self.key_id, signature=_b64(self.private_key.sign(_canonical_permit(permit)))
        )


def _check_signature(public_key: Ed25519PublicKey, signature: str, signed_bytes: bytes, what: str) -> None:
    try:
        public_key.verify(_unb64(signature), signed_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise IdentityRejected(f"The {what} signature does not match; it was changed or forged.") from exc


@dataclass(frozen=True)
class ContextVerifier:
    keys: Mapping[str, tuple[str, Ed25519PublicKey]]  # key id -> (issuer, public key)

    @classmethod
    def from_value(cls, raw: str) -> "ContextVerifier":
        entries = json.loads(raw) if str(raw or "").strip() else []
        keys: dict[str, tuple[str, Ed25519PublicKey]] = {}
        for entry in entries:
            keys[str(entry["key_id"])] = (
                str(entry["issuer"]),
                Ed25519PublicKey.from_public_bytes(_unb64(str(entry["public_key"]))),
            )
        return cls(keys)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ContextVerifier":
        source = os.environ if env is None else env
        return cls.from_value(str(source.get(ENV_VERIFY_KEYS) or ""))

    def verify(
        self,
        signed: SignedTaskContextV1 | Mapping[str, object] | None,
        *,
        team_id: str | None = None,
        now: datetime | None = None,
        check_expiry: bool = True,
    ) -> TaskContextV1:
        """The verified context; ``check_expiry=False`` only for reading an old
        context's facts, never for authorizing with it."""

        if signed is None:
            raise IdentityRejected("The task has no signed context, so nobody vouches for who it works for.")
        envelope = signed if isinstance(signed, SignedTaskContextV1) else SignedTaskContextV1.model_validate(signed)
        known = self.keys.get(envelope.key_id)
        if known is None:
            raise IdentityRejected(f"The task context was signed with an unknown key ({envelope.key_id}).")
        issuer, public_key = known
        if envelope.context.issuer != issuer:
            raise IdentityRejected("The task context names a different issuer than its key.")
        _check_signature(public_key, envelope.signature, _canonical(envelope.context), "task context")
        moment = now or datetime.now(timezone.utc)
        if check_expiry and moment >= envelope.context.expires_at_utc:
            raise IdentityRejected("The task context has expired.")
        if team_id is not None and envelope.context.team_id != team_id:
            raise IdentityRejected("The task context belongs to another team.")
        return envelope.context

    def verify_permit(
        self,
        signed: SignedSchedulePermitV1 | Mapping[str, object] | None,
        *,
        team_id: str,
        now: datetime | None = None,
        check_expiry: bool = True,
    ) -> SchedulePermitV1:
        """Verify the scheduler's signature and team.

        ``check_expiry=False`` reads historical facts only, never authority
        to start work or call tools, like ``verify`` for task contexts.
        """

        if signed is None:
            raise IdentityRejected("The scheduled run has no schedule permit.")
        envelope = (
            signed if isinstance(signed, SignedSchedulePermitV1) else SignedSchedulePermitV1.model_validate(signed)
        )
        known = self.keys.get(envelope.key_id)
        if known is None:
            raise IdentityRejected(f"The schedule permit was signed with an unknown key ({envelope.key_id}).")
        issuer, public_key = known
        if issuer != ISSUER_SCHEDULER or envelope.permit.issuer != issuer:
            raise IdentityRejected("Only the scheduler's key signs schedule permits.")
        _check_signature(public_key, envelope.signature, _canonical_permit(envelope.permit), "schedule permit")
        if check_expiry and (now or datetime.now(timezone.utc)) >= envelope.permit.expires_at_utc:
            raise IdentityRejected("The schedule permit has expired.")
        if envelope.permit.team_id != team_id:
            raise IdentityRejected("The schedule permit belongs to another team.")
        return envelope.permit


def public_entry(issuer: str, value: str) -> dict[str, str]:
    """The verification entry for a private key value from team.env."""

    signer = ContextSigner.from_value(issuer, value)
    public = signer.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"key_id": signer.key_id, "issuer": issuer, "public_key": _b64(public)}


def verify_keys_value(entries: list[dict[str, str]]) -> str:
    return json.dumps(sorted(entries, key=lambda entry: entry["key_id"]), separators=(",", ":"))


__all__ = [
    "ContextSigner",
    "ContextVerifier",
    "DEFAULT_CONTEXT_LIFETIME",
    "ENV_GATEWAY_SIGNING_KEY",
    "ENV_SCHEDULER_SIGNING_KEY",
    "ENV_VERIFY_KEYS",
    "ISSUER_GATEWAY",
    "ISSUER_SCHEDULER",
    "IdentityRejected",
    "SCHEDULE_PERMIT_LIFETIME",
    "generate_signing_key",
    "public_entry",
    "verify_keys_value",
]
