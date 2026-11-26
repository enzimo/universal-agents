"""Single runtime verifier for protocol 1 authority tokens (contracts/messages/v1).

Every enforcement point that checks whom a task or scheduled run works for routes through
``AuthorityTokenVerifier``:

- Verifies the token's Ed25519 signature, issuer host certificate, team membership,
  installation match, current revocations record and validity window through
  ``custom_messages.wire.frame.verify_authority_token``.
- Resolves ``token.person`` (an opaque ``person_ref``) live through ``PeopleDirectory``
  (``team_person_by_ref``) so disabled/removed owners and revoked roles take effect
  immediately while a token lasts.
- Exposes the signing certificate's ``issuer_roles`` so gateway-only checks (manager
  control commands, sent-file vouching, working requests, change writers) refuse valid
  scheduler-signed tokens even when they name an administrator (C04), and scheduler-only
  checks refuse gateway-signed tokens.
- Refuses bare schedule permit tokens (``schedule_permit:*`` / ``schedule_owner_permit:*``)
  anywhere a root-task token is expected; permits verify only through
  ``verify_schedule_permit`` inside ``ScheduleRunIdentityV1`` (C01).

Protocol 1 wire models (``custom_messages.wire.*``) are imported lazily on first
verification call so command-line tools that import ``access_control`` do not pay the
startup cost of loading the full generated contract models.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional, cast

from access_control.people_directory import PeopleDirectory, PeopleStore, PersonNow
from custom_messages.contracts.access_v2 import is_person_ref

if TYPE_CHECKING:
    from custom_messages.wire.models import AuthorityToken
    from custom_messages.wire.trust import TrustStore

ROOT_TASK_BINDING = "root_task"
GATEWAY_ISSUER_ROLE = "gateway"
SCHEDULER_ISSUER_ROLE = "scheduler"
# Binding id prefixes for scheduler-signed schedule permits (C01). Kept distinct from real
# root-task ids so bare permits are always refused when presented as task tokens.
SCHEDULE_PERMIT_PREFIX = "schedule_permit:"
SCHEDULE_OWNER_PERMIT_PREFIX = "schedule_owner_permit:"
# Request id prefix required on ScheduleRunIdentityV1 wrappers so permit runs can never
# collide with or adopt gateway message_context_* or task_context_* request ids and grants.
SCHEDULE_RUN_CONTEXT_PREFIX = "schedule_run_"

IssuerRequirement = Literal["any", "gateway", "scheduler"]
TrustProvider = Any
PeopleProvider = PeopleStore | Callable[[], PeopleStore | None]


def schedule_permit_binding_id(schedule_id: str, *, owner_permit: bool = False) -> str:
    """The ``bound_to.id`` / ``bound_to.request_id`` carried by a schedule's permit token."""

    prefix = SCHEDULE_OWNER_PERMIT_PREFIX if owner_permit else SCHEDULE_PERMIT_PREFIX
    return f"{prefix}{schedule_id}"


def is_schedule_permit_binding(bound_id: str) -> bool:
    """Whether ``bound_id`` identifies a per-schedule permit rather than a single root task."""

    return bound_id.startswith((SCHEDULE_PERMIT_PREFIX, SCHEDULE_OWNER_PERMIT_PREFIX))


class IdentityRejected(PermissionError):
    """An authority token is missing, malformed, forged, expired, revoked, or from the wrong issuer."""


@dataclass(frozen=True)
class VerifiedTaskToken:
    """Verified facts from a protocol 1 root-task or schedule-permit authority token."""

    token: AuthorityToken
    token_id: str
    installation: str
    team_id: str
    root_task_id: str
    request_id: str
    person_ref: Optional[str]
    person_id: Optional[str]
    person_roles: tuple[str, ...]
    person: Optional[PersonNow]
    schedule_id: Optional[str]
    issued_at: datetime
    expires_at: datetime
    issuer_runtime: str
    issuer_roles: frozenset[str]

    @property
    def is_permit(self) -> bool:
        return is_schedule_permit_binding(self.root_task_id)

    @property
    def is_gateway_issued(self) -> bool:
        return GATEWAY_ISSUER_ROLE in self.issuer_roles and self.schedule_id is None and not self.is_permit

    @property
    def is_scheduler_issued(self) -> bool:
        return SCHEDULER_ISSUER_ROLE in self.issuer_roles

    @property
    def issuer_kind(self) -> str:
        if self.is_gateway_issued:
            return GATEWAY_ISSUER_ROLE
        if self.is_scheduler_issued:
            return SCHEDULER_ISSUER_ROLE
        return "unknown"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _unwrap_trust_store(source: TrustProvider) -> Optional[TrustStore]:
    resolved = source() if callable(source) else source
    if resolved is None:
        return None
    inner = getattr(resolved, "store", None)
    if inner is not None and hasattr(inner, "host_certificate"):
        return cast("TrustStore", inner)
    return cast("TrustStore", resolved)


def _unwrap_people_store(source: Optional[PeopleProvider]) -> Optional[PeopleStore]:
    if source is None:
        return None
    return source() if callable(source) else source


def _parse_token(raw_token: Any) -> tuple[AuthorityToken, Mapping[str, Any]]:
    from pydantic import ValidationError

    from custom_messages.wire.models import AuthorityToken

    if raw_token is None:
        raise IdentityRejected("The task has no authority token, so nobody vouches for who it works for.")
    if isinstance(raw_token, AuthorityToken):
        return raw_token, raw_token.model_dump(mode="json")
    if not isinstance(raw_token, Mapping):
        raise IdentityRejected("The authority token is not a valid token object.")
    try:
        token = AuthorityToken.model_validate(raw_token)
    except (ValidationError, TypeError, ValueError) as exc:
        raise IdentityRejected(f"The authority token is malformed: {exc}") from exc
    return token, raw_token


class AuthorityTokenVerifier:
    """Verify protocol 1 authority tokens against a live ``TrustStore`` and ``PeopleDirectory``."""

    def __init__(
        self,
        trust: TrustProvider,
        *,
        people: Optional[PeopleProvider] = None,
        installation: Optional[str] = None,
        clock: Callable[[], datetime] = _utc_now,
        read_lock: Optional[threading.RLock] = None,
    ) -> None:
        self._trust = trust
        self._people = people
        self._installation = installation
        self._clock = clock
        self._read_lock = read_lock

    def _require_trust(self) -> tuple[TrustStore, str]:
        store = _unwrap_trust_store(self._trust)
        if store is None:
            raise IdentityRejected("No protocol 1 trust store is available to verify authority tokens.")
        expected_installation = self._installation or store.installation
        return store, expected_installation

    def _resolve_person(
        self,
        *,
        person_ref: Optional[str],
        team_id: str,
        people_override: Optional[PeopleStore],
        require_active_person: bool,
    ) -> tuple[Optional[PersonNow], Optional[str], tuple[str, ...]]:
        if person_ref is None:
            return None, None, ()
        if not is_person_ref(person_ref):
            raise IdentityRejected("The authority token does not name a person by opaque reference.")
        people_store = people_override if people_override is not None else _unwrap_people_store(self._people)
        if people_store is None:
            raise IdentityRejected("This process cannot resolve the person named by the authority token.")
        person = PeopleDirectory(people_store, team_id).person_by_ref(person_ref)
        if person is None:
            raise IdentityRejected("The person named by the authority token is not known in this team.")
        if require_active_person and not person.active:
            raise IdentityRejected(f"The person the task works for is not active ({person.describe()}).")
        roles = tuple(sorted(set(person.roles))) if person.active else ()
        return person, person.person_id, roles

    def _verify(
        self,
        raw_token: Any,
        *,
        team_id: str,
        now: Optional[datetime],
        valid_at: Optional[datetime],
        reached_at: Optional[datetime],
        require_issuer: IssuerRequirement,
        allow_permit: bool,
        require_permit: bool,
        check_expiry: bool,
        require_active_person: bool,
        people: Optional[PeopleStore],
    ) -> VerifiedTaskToken:
        from pydantic import MISSING

        from custom_messages.wire.frame import verify_authority_token
        from custom_messages.wire.models import AuthorityTokenBoundToRootTask
        from custom_messages.wire.refusals import FrameRefused
        from custom_messages.wire.values import parse_timestamp

        token, fields = _parse_token(raw_token)
        if not isinstance(token.bound_to, AuthorityTokenBoundToRootTask):
            raise IdentityRejected(f"Expected a root-task authority token, got bound_to.type={token.bound_to.type!r}.")

        bound_id = token.bound_to.id
        request_id = token.bound_to.request_id
        schedule_id = None if token.schedule_id is MISSING else token.schedule_id
        person_ref = None if token.person is MISSING else token.person

        is_permit = is_schedule_permit_binding(bound_id) or is_schedule_permit_binding(request_id)
        if is_permit and not allow_permit:
            raise IdentityRejected(
                "A schedule permit cannot be presented as a task token directly; it must travel inside a scheduled run identity."
            )
        if require_permit:
            self._check_permit_binding(bound_id, request_id, schedule_id, person_ref)

        issued_at = parse_timestamp(token.issued_at)
        expires_at = parse_timestamp(token.expires_at)
        if expires_at <= issued_at:
            raise IdentityRejected("The authority token ends at or before it was issued.")

        moment = now or self._clock()
        if check_expiry:
            effective_reached_at = reached_at if reached_at is not None else moment
            effective_valid_at = valid_at if valid_at is not None else effective_reached_at
        else:
            # Historical attribution / change-ledger snapshot: verify signature, issuer cert,
            # team, installation, and current revocations at ``moment`` without rejecting on expiry.
            effective_valid_at = issued_at
            effective_reached_at = issued_at

        lock_ctx = self._read_lock if self._read_lock is not None else nullcontext()
        with lock_ctx:
            store, expected_installation = self._require_trust()
            if token.issuer.installation != expected_installation or token.audience.installation != expected_installation:
                raise IdentityRejected("The authority token belongs to another installation.")
            if token.audience.team != team_id:
                raise IdentityRejected("The authority token belongs to another team.")
            try:
                verified = verify_authority_token(
                    token,
                    fields,
                    store,
                    valid_at=effective_valid_at,
                    now=moment,
                    reached_at=effective_reached_at,
                )
            except FrameRefused as refused:
                raise IdentityRejected(refused.detail or f"The authority token was refused ({refused.code}).") from refused

        issuer_roles = verified.certificate.roles
        if require_issuer == "gateway":
            if GATEWAY_ISSUER_ROLE not in issuer_roles or schedule_id is not None:
                raise IdentityRejected("Only a gateway-signed authority token is accepted here.")
        elif require_issuer == "scheduler":
            if SCHEDULER_ISSUER_ROLE not in issuer_roles:
                raise IdentityRejected("Only a scheduler-signed authority token is accepted here.")

        person, person_id, person_roles = self._resolve_person(
            person_ref=person_ref,
            team_id=team_id,
            people_override=people,
            require_active_person=require_active_person,
        )
        return VerifiedTaskToken(
            token=token,
            token_id=token.token_id,
            installation=expected_installation,
            team_id=token.audience.team,
            root_task_id=bound_id,
            request_id=request_id,
            person_ref=person_ref,
            person_id=person_id,
            person_roles=person_roles,
            person=person,
            schedule_id=schedule_id,
            issued_at=issued_at,
            expires_at=expires_at,
            issuer_runtime=verified.certificate.runtime,
            issuer_roles=issuer_roles,
        )

    @staticmethod
    def _check_permit_binding(
        bound_id: str,
        request_id: str,
        schedule_id: Optional[str],
        person_ref: Optional[str],
    ) -> None:
        if schedule_id is None:
            raise IdentityRejected("A schedule permit token must name its schedule.")
        regular_id = schedule_permit_binding_id(schedule_id, owner_permit=False)
        owner_id = schedule_permit_binding_id(schedule_id, owner_permit=True)
        if bound_id != request_id or bound_id not in {regular_id, owner_id}:
            raise IdentityRejected("The token is not a valid schedule permit for its schedule.")
        if bound_id == owner_id and person_ref is None:
            raise IdentityRejected("An owner schedule permit must name its owner.")

    def verify_root_task(
        self,
        raw_token: Any,
        *,
        team_id: str,
        now: Optional[datetime] = None,
        valid_at: Optional[datetime] = None,
        reached_at: Optional[datetime] = None,
        require_issuer: IssuerRequirement = "any",
        check_expiry: bool = True,
        require_active_person: bool = True,
        people: Optional[PeopleStore] = None,
    ) -> VerifiedTaskToken:
        """Verify a root-task authority token; refuses schedule permits presented bare."""

        return self._verify(
            raw_token,
            team_id=team_id,
            now=now,
            valid_at=valid_at,
            reached_at=reached_at,
            require_issuer=require_issuer,
            allow_permit=False,
            require_permit=False,
            check_expiry=check_expiry,
            require_active_person=require_active_person,
            people=people,
        )

    def verify_schedule_permit(
        self,
        raw_token: Any,
        *,
        team_id: str,
        now: Optional[datetime] = None,
        valid_at: Optional[datetime] = None,
        reached_at: Optional[datetime] = None,
        check_expiry: bool = True,
        require_active_person: bool = True,
        people: Optional[PeopleStore] = None,
    ) -> VerifiedTaskToken:
        """Verify a scheduler-signed per-schedule permit token (for ``ScheduleRunIdentityV1``)."""

        return self._verify(
            raw_token,
            team_id=team_id,
            now=now,
            valid_at=valid_at,
            reached_at=reached_at,
            require_issuer="scheduler",
            allow_permit=True,
            require_permit=True,
            check_expiry=check_expiry,
            require_active_person=require_active_person,
            people=people,
        )


__all__ = [
    "AuthorityTokenVerifier",
    "GATEWAY_ISSUER_ROLE",
    "IdentityRejected",
    "IssuerRequirement",
    "ROOT_TASK_BINDING",
    "SCHEDULER_ISSUER_ROLE",
    "SCHEDULE_OWNER_PERMIT_PREFIX",
    "SCHEDULE_PERMIT_PREFIX",
    "SCHEDULE_RUN_CONTEXT_PREFIX",
    "VerifiedTaskToken",
    "is_schedule_permit_binding",
    "schedule_permit_binding_id",
]
