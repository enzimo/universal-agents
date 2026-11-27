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
- Preserves distinct refusal codes on ``IdentityRejected.code`` (unknown issuer, stale
  revocations, revoked issuer, expired, bad signature, wrong issuer, etc.) and supports
  one on-demand async issuer/revocations fetch via ``LiveHost``, ``MessageHost``,
  ``AgentProcessHost``, or ``TrustReader`` before retrying verification once (C08).

Protocol 1 wire models (``custom_messages.wire.models`` / ``frame``) are imported lazily
on first verification call so command-line tools that import ``access_control`` do not
pay the startup cost of loading the full generated contract models.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional, cast

from access_control.people_directory import PeopleDirectory, PeopleStore, PersonNow
from custom_messages.contracts.access_v2 import is_person_ref
from custom_messages.wire.authority_bindings import (
    AUTHORITY_ISSUER_ROLES,
    AuthorityTokenRecord,
    GATEWAY_ISSUER_ROLE,
    ROOT_TASK_BINDING,
    SCHEDULER_ISSUER_ROLE,
    SCHEDULE_OWNER_PERMIT_PREFIX,
    SCHEDULE_PERMIT_PREFIX,
    SCHEDULE_RUN_CONTEXT_PREFIX,
    extract_token_issuer,
    extract_token_log_fields,
    is_schedule_permit_binding,
    is_unvouched_authority_binding,
    schedule_permit_binding_id,
)

if TYPE_CHECKING:
    from custom_messages.wire.models import AuthorityToken
    from custom_messages.wire.refusals import FrameRefused
    from custom_messages.wire.trust import TrustStore

logger = logging.getLogger(__name__)

# Distinct refusal codes carried on ``IdentityRejected.code`` so callers and logs can
# distinguish an unfetched issuer certificate or stale revocations record from permanent
# refusals such as a revoked issuer, bad signature, or expired token (C08).
REFUSAL_MISSING = "missing"
REFUSAL_MALFORMED = "malformed"
REFUSAL_PERMIT_NOT_ALLOWED = "permit_not_allowed"
REFUSAL_INVALID_PERMIT = "invalid_permit"
REFUSAL_NO_TRUST_STORE = "no_trust_store"
REFUSAL_WRONG_INSTALLATION = "wrong_installation"
REFUSAL_WRONG_TEAM = "wrong_team"
REFUSAL_UNKNOWN_ISSUER = "unknown_issuer"
REFUSAL_REVOCATIONS_NOT_CURRENT = "revocations_not_current"
REFUSAL_REVOKED_ISSUER = "revoked_issuer"
REFUSAL_ISSUER_CERT_NOT_VALID = "issuer_cert_not_valid"
REFUSAL_KEY_MISMATCH = "key_mismatch"
REFUSAL_BAD_SIGNATURE = "bad_signature"
REFUSAL_EXPIRED = "expired"
REFUSAL_WRONG_ISSUER = "wrong_issuer"
REFUSAL_INVALID_PERSON_REF = "invalid_person_ref"
REFUSAL_NO_PEOPLE_STORE = "no_people_store"
REFUSAL_PERSON_UNKNOWN = "person_unknown"
REFUSAL_PERSON_INACTIVE = "person_inactive"
REFUSAL_BAD_AUTHORITY = "bad_authority"
FETCHABLE_REFUSAL_CODES = frozenset({REFUSAL_UNKNOWN_ISSUER, REFUSAL_REVOCATIONS_NOT_CURRENT})

IssuerRequirement = Literal["any", "gateway", "scheduler"]
TrustProvider = Any
PeopleProvider = PeopleStore | Callable[[], PeopleStore | None]
IssuerFetcher = Callable[[Any], Awaitable[bool]]


class IdentityRejected(PermissionError):
    """An authority token is missing, malformed, forged, expired, revoked, or from the wrong issuer."""

    def __init__(self, message: str, *, code: str = REFUSAL_BAD_AUTHORITY) -> None:
        super().__init__(message)
        self.code = code


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
    def is_unvouched(self) -> bool:
        """Whether the signed token or permit carries the unvouched 'no roles' marker (F2)."""
        return is_unvouched_authority_binding(self.root_task_id, self.request_id)

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


def _resolve_trust_source(
    source: TrustProvider,
    *,
    explicit_fetcher: Optional[IssuerFetcher],
) -> tuple[Optional[TrustStore], Optional[threading.RLock], Optional[IssuerFetcher]]:
    """Resolve ``(TrustStore, RLock | None, IssuerFetcher | None)`` via the explicit ``trust_state()`` interface (C11).

    Accepts ``PersistentTrustStore``, ``TrustReader``, ``LiveHost``, ``MessageHost``,
    ``AgentProcessHost`` (each exposing ``trust_state() -> (TrustStore, PersistentTrustStore.lock)``
    or ``None`` before start), a zero-arg callable returning one of them, or a bare ``TrustStore``
    for tests. Never guesses attribute names or substitutes a different lock than the store's own.
    """

    resolved = source() if callable(source) else source
    if resolved is None:
        return None, None, explicit_fetcher
    fetcher = explicit_fetcher
    if fetcher is None and callable(getattr(resolved, "fetch_token_issuer", None)):
        fetcher = cast(IssuerFetcher, resolved.fetch_token_issuer)
    trust_state = getattr(resolved, "trust_state", None)
    if callable(trust_state):
        state = trust_state()
        if state is None:
            return None, None, fetcher
        store, lock = state
        return cast("TrustStore", store), cast(Optional[threading.RLock], lock), fetcher
    if hasattr(resolved, "host_certificate"):
        return cast("TrustStore", resolved), None, fetcher
    return None, None, fetcher


def _unwrap_people_store(source: Optional[PeopleProvider]) -> Optional[PeopleStore]:
    if source is None:
        return None
    return source() if callable(source) else source


def _parse_token(raw_token: Any) -> tuple[AuthorityToken, Mapping[str, Any]]:
    from pydantic import ValidationError

    from custom_messages.wire.models import AuthorityToken

    if raw_token is None:
        raise IdentityRejected(
            "The task has no authority token, so nobody vouches for who it works for.",
            code=REFUSAL_MISSING,
        )
    if isinstance(raw_token, AuthorityToken):
        return raw_token, raw_token.model_dump(mode="json")
    if isinstance(raw_token, AuthorityTokenRecord):
        dumped = raw_token.model_dump(mode="json")
        return AuthorityToken.model_validate(dumped), dumped
    if not isinstance(raw_token, Mapping):
        raise IdentityRejected("The authority token is not a valid token object.", code=REFUSAL_MALFORMED)
    try:
        token = AuthorityToken.model_validate(raw_token)
    except (ValidationError, TypeError, ValueError) as exc:
        raise IdentityRejected(f"The authority token is malformed: {exc}", code=REFUSAL_MALFORMED) from exc
    return token, raw_token


def _classify_frame_refusal(
    refused: FrameRefused,
    *,
    token: AuthorityToken,
    store: TrustStore,
    issued_at: datetime,
    moment: datetime,
) -> tuple[str, str]:
    """Map a ``verify_authority_token`` refusal into a specific ``IdentityRejected.code`` and plain message."""

    at = refused.at or ""
    if at.endswith("/issuer"):
        certificate = store.host_certificate(
            token.issuer.installation,
            token.issuer.runtime,
            token.issuer.membership_epoch,
        )
        if certificate is None:
            return (
                REFUSAL_UNKNOWN_ISSUER,
                f"No certificate is held yet for token issuer {token.issuer.runtime} "
                f"(membership epoch {token.issuer.membership_epoch}).",
            )
        if certificate.key_id != token.issuer.key_id:
            return (REFUSAL_KEY_MISMATCH, "The token issuer's certificate names another key.")
        if store.revoked(certificate):
            return (REFUSAL_REVOKED_ISSUER, "The token issuer's certificate has been revoked.")
        if not store.revocations_current(certificate.installation, moment):
            return (
                REFUSAL_REVOCATIONS_NOT_CURRENT,
                "No current revocations record is held for this installation.",
            )
        if not certificate.valid_at(issued_at):
            return (
                REFUSAL_ISSUER_CERT_NOT_VALID,
                "The token issuer's certificate was not valid when the token was issued.",
            )
        return (
            REFUSAL_WRONG_ISSUER,
            refused.detail or "The token issuer's certificate is not allowed to sign this token.",
        )
    if at.endswith("/signature"):
        return (REFUSAL_BAD_SIGNATURE, refused.detail or "The token's signature does not verify.")
    if at.endswith("/expires_at"):
        return (REFUSAL_EXPIRED, refused.detail or "The authority token was not valid at the checked time.")
    if at.endswith("/schedule_id"):
        return (REFUSAL_WRONG_ISSUER, refused.detail or "Only a scheduler may name a schedule in an authority token.")
    return (refused.code or REFUSAL_BAD_AUTHORITY, refused.detail or f"The authority token was refused ({refused.code}).")


class AuthorityTokenVerifier:
    """Verify protocol 1 authority tokens against a live ``TrustStore`` and ``PeopleDirectory``."""

    def __init__(
        self,
        trust: TrustProvider,
        *,
        people: Optional[PeopleProvider] = None,
        installation: Optional[str] = None,
        clock: Callable[[], datetime] = _utc_now,
        fetch_issuer: Optional[IssuerFetcher] = None,
    ) -> None:
        self._trust = trust
        self._people = people
        self._installation = installation
        self._clock = clock
        self._fetch_issuer = fetch_issuer

    @property
    def has_people(self) -> bool:
        return self._people is not None

    def now(self) -> datetime:
        """Current time according to this verifier's clock."""

        return self._clock()

    def _resolve(self) -> tuple[Optional[TrustStore], Optional[threading.RLock], Optional[IssuerFetcher]]:
        return _resolve_trust_source(self._trust, explicit_fetcher=self._fetch_issuer)

    def _require_trust(self) -> tuple[TrustStore, str, Optional[threading.RLock]]:
        store, lock, _ = self._resolve()
        if store is None:
            raise IdentityRejected(
                "No protocol 1 trust store is available to verify authority tokens.",
                code=REFUSAL_NO_TRUST_STORE,
            )
        expected_installation = self._installation or store.installation
        return store, expected_installation, lock

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
            raise IdentityRejected(
                "The authority token does not name a person by opaque reference.",
                code=REFUSAL_INVALID_PERSON_REF,
            )
        people_store = people_override if people_override is not None else _unwrap_people_store(self._people)
        if people_store is None:
            raise IdentityRejected(
                "This process cannot resolve the person named by the authority token.",
                code=REFUSAL_NO_PEOPLE_STORE,
            )
        person = PeopleDirectory(people_store, team_id).person_by_ref(person_ref)
        if person is None:
            raise IdentityRejected(
                "The person named by the authority token is not known in this team.",
                code=REFUSAL_PERSON_UNKNOWN,
            )
        if require_active_person and not person.active:
            raise IdentityRejected(
                f"The person the task works for is not active ({person.describe()}).",
                code=REFUSAL_PERSON_INACTIVE,
            )
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
            raise IdentityRejected(
                f"Expected a root-task authority token, got bound_to.type={token.bound_to.type!r}.",
                code=REFUSAL_MALFORMED,
            )

        bound_id = token.bound_to.id
        request_id = token.bound_to.request_id
        schedule_id = None if token.schedule_id is MISSING else token.schedule_id
        person_ref = None if token.person is MISSING else token.person

        is_permit = is_schedule_permit_binding(bound_id) or is_schedule_permit_binding(request_id)
        if is_permit and not allow_permit:
            raise IdentityRejected(
                "A schedule permit cannot be presented as a task token directly; it must travel inside a scheduled run identity.",
                code=REFUSAL_PERMIT_NOT_ALLOWED,
            )
        if require_permit:
            self._check_permit_binding(bound_id, request_id, schedule_id, person_ref)

        issued_at = parse_timestamp(token.issued_at)
        expires_at = parse_timestamp(token.expires_at)
        if expires_at <= issued_at:
            raise IdentityRejected("The authority token ends at or before it was issued.", code=REFUSAL_MALFORMED)

        moment = now or self._clock()
        if check_expiry:
            effective_reached_at = reached_at if reached_at is not None else moment
            effective_valid_at = valid_at if valid_at is not None else effective_reached_at
        else:
            # Historical attribution / change-ledger snapshot: verify signature, issuer cert,
            # team, installation, and current revocations at ``moment`` without rejecting on expiry.
            effective_valid_at = issued_at
            effective_reached_at = issued_at

        store, expected_installation, lock = self._require_trust()
        lock_ctx = lock if lock is not None else nullcontext()
        with lock_ctx:
            if token.issuer.installation != expected_installation or token.audience.installation != expected_installation:
                raise IdentityRejected(
                    "The authority token belongs to another installation.",
                    code=REFUSAL_WRONG_INSTALLATION,
                )
            if token.audience.team != team_id:
                raise IdentityRejected("The authority token belongs to another team.", code=REFUSAL_WRONG_TEAM)
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
                code, message = _classify_frame_refusal(
                    refused,
                    token=token,
                    store=store,
                    issued_at=issued_at,
                    moment=moment,
                )
                raise IdentityRejected(message, code=code) from refused

        issuer_roles = verified.certificate.roles
        if require_issuer == "gateway":
            if GATEWAY_ISSUER_ROLE not in issuer_roles or schedule_id is not None:
                raise IdentityRejected(
                    "Only a gateway-signed authority token is accepted here.",
                    code=REFUSAL_WRONG_ISSUER,
                )
        elif require_issuer == "scheduler":
            if SCHEDULER_ISSUER_ROLE not in issuer_roles:
                raise IdentityRejected(
                    "Only a scheduler-signed authority token is accepted here.",
                    code=REFUSAL_WRONG_ISSUER,
                )

        person, person_id, person_roles = self._resolve_person(
            person_ref=person_ref,
            team_id=team_id,
            people_override=people,
            require_active_person=require_active_person,
        )
        if is_unvouched_authority_binding(bound_id, request_id):
            # Signed "no roles" marker (F2): applies whoever issued the token, without live lookup.
            person_roles = ()
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
            raise IdentityRejected("A schedule permit token must name its schedule.", code=REFUSAL_INVALID_PERMIT)
        regular_id = schedule_permit_binding_id(schedule_id, owner_permit=False)
        unvouched_id = schedule_permit_binding_id(schedule_id, owner_permit=False, unvouched=True)
        owner_id = schedule_permit_binding_id(schedule_id, owner_permit=True)
        if bound_id != request_id or bound_id not in {regular_id, unvouched_id, owner_id}:
            raise IdentityRejected(
                "The token is not a valid schedule permit for its schedule.",
                code=REFUSAL_INVALID_PERMIT,
            )
        if bound_id == owner_id and person_ref is None:
            raise IdentityRejected("An owner schedule permit must name its owner.", code=REFUSAL_INVALID_PERMIT)
        if bound_id == unvouched_id and person_ref is None:
            raise IdentityRejected("An unvouched schedule permit must name its owner.", code=REFUSAL_INVALID_PERMIT)

    async def ensure_issuer_known(self, raw_token: Any, *, now: Optional[datetime] = None) -> bool:
        """Fetch the token's issuer certificate (or fresh revocations) if not already held locally.

        Returns ``True`` when a new trust record was fetched, ``False`` on the warm path or when
        no fetcher is configured.
        """

        extracted = extract_token_issuer(raw_token)
        if extracted is None:
            return False
        installation, runtime, epoch = extracted
        store, lock, fetcher = self._resolve()
        if store is None or fetcher is None:
            return False
        expected_installation = self._installation or store.installation
        if installation != expected_installation:
            return False
        moment = now or self._clock()
        lock_ctx = lock if lock is not None else nullcontext()
        with lock_ctx:
            has_cert = store.host_certificate(installation, runtime, epoch) is not None
            revocations_ok = store.revocations_current(installation, moment)
        if has_cert and revocations_ok:
            return False
        return await fetcher(raw_token)

    async def _verify_with_fetch(
        self,
        raw_token: Any,
        verify_fn: Callable[..., VerifiedTaskToken],
        **kwargs: Any,
    ) -> VerifiedTaskToken:
        try:
            return verify_fn(raw_token, **kwargs)
        except IdentityRejected as refused:
            if refused.code not in FETCHABLE_REFUSAL_CODES:
                raise
            issuer_runtime, membership_epoch, team, request_id = extract_token_log_fields(raw_token)
            _, _, fetcher = self._resolve()
            if fetcher is None:
                logger.warning(
                    "Authority token refused because a trust record is missing and no fetcher is available. "
                    "event=authority_token_refused code=%s issuer_runtime=%s membership_epoch=%s "
                    "team=%s request_id=%s",
                    refused.code,
                    issuer_runtime,
                    membership_epoch,
                    team,
                    request_id,
                )
                raise
            fetched = await fetcher(raw_token)
            if not fetched:
                logger.warning(
                    "Authority token refused; fetching trust records produced nothing new. "
                    "event=authority_token_refused code=%s issuer_runtime=%s membership_epoch=%s "
                    "team=%s request_id=%s",
                    refused.code,
                    issuer_runtime,
                    membership_epoch,
                    team,
                    request_id,
                )
                raise
            logger.info(
                "Fetched missing trust record for authority token issuer; retrying verification once. "
                "event=authority_token_issuer_fetched code=%s issuer_runtime=%s membership_epoch=%s "
                "team=%s request_id=%s",
                refused.code,
                issuer_runtime,
                membership_epoch,
                team,
                request_id,
            )
            return verify_fn(raw_token, **kwargs)

    def verify_historical_root_task(
        self,
        raw_token: Any,
        *,
        team_id: str,
        now: Optional[datetime] = None,
        require_issuer: IssuerRequirement = "any",
        people: Optional[PeopleStore] = None,
    ) -> VerifiedTaskToken:
        """Verify historical root-task attribution (change ledger saves and stops).

        Checks Ed25519 signature, issuer certificate validity at ``issued_at``,
        installation, team, issuer role, max lifetime, and non-permit binding,
        without failing on token expiry, a later issuer revocation, or stale
        revocations. Logs a warning when the issuer is revoked or revocations
        are stale so operators see it, while keeping snapshot saves and stop
        commands working for already-admitted tasks. Grants no authority.
        """

        from cryptography.exceptions import InvalidSignature
        from pydantic import MISSING

        from custom_messages.wire.encoding import b64url_decode
        from custom_messages.wire.frame import authority_signing_bytes
        from custom_messages.wire.kinds import AUTHORITY_TOKEN_MAX_SECONDS
        from custom_messages.wire.models import AuthorityTokenBoundToRootTask
        from custom_messages.wire.values import parse_timestamp

        token, fields = _parse_token(raw_token)
        if not isinstance(token.bound_to, AuthorityTokenBoundToRootTask):
            raise IdentityRejected(
                f"Expected a root-task authority token, got bound_to.type={token.bound_to.type!r}.",
                code=REFUSAL_MALFORMED,
            )

        bound_id = token.bound_to.id
        request_id = token.bound_to.request_id
        schedule_id = None if token.schedule_id is MISSING else token.schedule_id
        person_ref = None if token.person is MISSING else token.person
        if is_schedule_permit_binding(bound_id) or is_schedule_permit_binding(request_id):
            raise IdentityRejected(
                "A schedule permit cannot be presented as a task token directly; it must travel inside a scheduled run identity.",
                code=REFUSAL_PERMIT_NOT_ALLOWED,
            )

        issued_at = parse_timestamp(token.issued_at)
        expires_at = parse_timestamp(token.expires_at)
        if expires_at <= issued_at:
            raise IdentityRejected("The authority token ends at or before it was issued.", code=REFUSAL_MALFORMED)
        if (expires_at - issued_at).total_seconds() > AUTHORITY_TOKEN_MAX_SECONDS:
            raise IdentityRejected("The authority token lasts longer than the contract allows.", code=REFUSAL_EXPIRED)

        moment = now or self._clock()
        store, expected_installation, lock = self._require_trust()
        lock_ctx = lock if lock is not None else nullcontext()
        with lock_ctx:
            if token.issuer.installation != expected_installation or token.audience.installation != expected_installation:
                raise IdentityRejected(
                    "The authority token belongs to another installation.",
                    code=REFUSAL_WRONG_INSTALLATION,
                )
            if token.audience.team != team_id:
                raise IdentityRejected("The authority token belongs to another team.", code=REFUSAL_WRONG_TEAM)
            certificate = store.host_certificate(
                token.issuer.installation,
                token.issuer.runtime,
                token.issuer.membership_epoch,
            )
            if certificate is None:
                raise IdentityRejected(
                    f"No certificate is held yet for token issuer {token.issuer.runtime} "
                    f"(membership epoch {token.issuer.membership_epoch}).",
                    code=REFUSAL_UNKNOWN_ISSUER,
                )
            if certificate.key_id != token.issuer.key_id:
                raise IdentityRejected("The token issuer's certificate names another key.", code=REFUSAL_KEY_MISMATCH)
            if not certificate.valid_at(issued_at):
                raise IdentityRejected(
                    "The token issuer's certificate was not valid when the token was issued.",
                    code=REFUSAL_ISSUER_CERT_NOT_VALID,
                )
            if not (certificate.roles & AUTHORITY_ISSUER_ROLES) or team_id not in certificate.teams:
                raise IdentityRejected(
                    "The token issuer's certificate is not allowed to sign this token.",
                    code=REFUSAL_WRONG_ISSUER,
                )
            if schedule_id is not None and SCHEDULER_ISSUER_ROLE not in certificate.roles:
                raise IdentityRejected(
                    "Only a scheduler may name a schedule in an authority token.",
                    code=REFUSAL_WRONG_ISSUER,
                )
            if person_ref is None and schedule_id is None:
                raise IdentityRejected(
                    "The authority token names neither a person nor a schedule.",
                    code=REFUSAL_BAD_AUTHORITY,
                )
            try:
                certificate.public_key.verify(
                    b64url_decode(fields["signature"]),
                    authority_signing_bytes(fields),
                )
            except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
                raise IdentityRejected("The token's signature does not verify.", code=REFUSAL_BAD_SIGNATURE) from exc

            issuer_revoked = store.revoked(certificate)
            revocations_current = store.revocations_current(certificate.installation, moment)
            issuer_roles = certificate.roles
            issuer_runtime = certificate.runtime

        if issuer_revoked:
            logger.warning(
                "Historical token attribution accepted a token whose issuer certificate is now revoked; "
                "this records request lifecycle only and grants no access. "
                "event=change_ledger_historical_issuer_revoked issuer_runtime=%s membership_epoch=%s "
                "team=%s request_id=%s",
                issuer_runtime,
                token.issuer.membership_epoch,
                team_id,
                request_id,
            )
        if not revocations_current:
            logger.warning(
                "Historical token attribution ran without a current revocations record; "
                "this records request lifecycle only and grants no access. "
                "event=change_ledger_historical_revocations_stale issuer_runtime=%s membership_epoch=%s "
                "team=%s request_id=%s",
                issuer_runtime,
                token.issuer.membership_epoch,
                team_id,
                request_id,
            )

        if require_issuer == "gateway":
            if GATEWAY_ISSUER_ROLE not in issuer_roles or schedule_id is not None:
                raise IdentityRejected(
                    "Only a gateway-signed authority token is accepted here.",
                    code=REFUSAL_WRONG_ISSUER,
                )
        elif require_issuer == "scheduler":
            if SCHEDULER_ISSUER_ROLE not in issuer_roles:
                raise IdentityRejected(
                    "Only a scheduler-signed authority token is accepted here.",
                    code=REFUSAL_WRONG_ISSUER,
                )

        person, person_id, person_roles = self._resolve_person(
            person_ref=person_ref,
            team_id=team_id,
            people_override=people,
            require_active_person=False,
        )
        if is_unvouched_authority_binding(bound_id, request_id):
            person_roles = ()
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
            issuer_runtime=issuer_runtime,
            issuer_roles=issuer_roles,
        )

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

    async def verify_root_task_async(
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
        """Verify a root-task token, fetching an unknown issuer cert or stale revocations once on demand."""

        return await self._verify_with_fetch(
            raw_token,
            self.verify_root_task,
            team_id=team_id,
            now=now,
            valid_at=valid_at,
            reached_at=reached_at,
            require_issuer=require_issuer,
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

    async def verify_schedule_permit_async(
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
        """Verify a schedule permit token, fetching an unknown issuer cert or stale revocations once on demand."""

        return await self._verify_with_fetch(
            raw_token,
            self.verify_schedule_permit,
            team_id=team_id,
            now=now,
            valid_at=valid_at,
            reached_at=reached_at,
            check_expiry=check_expiry,
            require_active_person=require_active_person,
            people=people,
        )


_PROCESS_VERIFIER_LOCK = threading.RLock()
_PROCESS_VERIFIER: Optional[AuthorityTokenVerifier] = None


def set_process_verifier(verifier: Optional[AuthorityTokenVerifier]) -> None:
    """Register or clear this process's active ``AuthorityTokenVerifier`` (hosts, change ledger, tests)."""

    global _PROCESS_VERIFIER
    with _PROCESS_VERIFIER_LOCK:
        _PROCESS_VERIFIER = verifier


def get_process_verifier() -> Optional[AuthorityTokenVerifier]:
    """Return this process's active ``AuthorityTokenVerifier``, or ``None`` if none is registered."""

    with _PROCESS_VERIFIER_LOCK:
        return _PROCESS_VERIFIER


@contextmanager
def use_process_verifier(verifier: Optional[AuthorityTokenVerifier]) -> Iterator[Optional[AuthorityTokenVerifier]]:
    """Temporarily bind the process verifier for one scope (useful in tests and one-shot commands)."""

    with _PROCESS_VERIFIER_LOCK:
        previous = _PROCESS_VERIFIER
        set_process_verifier(verifier)
    try:
        yield verifier
    finally:
        set_process_verifier(previous)


__all__ = [
    "AuthorityTokenVerifier",
    "FETCHABLE_REFUSAL_CODES",
    "GATEWAY_ISSUER_ROLE",
    "IdentityRejected",
    "IssuerFetcher",
    "IssuerRequirement",
    "REFUSAL_BAD_AUTHORITY",
    "REFUSAL_BAD_SIGNATURE",
    "REFUSAL_EXPIRED",
    "REFUSAL_INVALID_PERMIT",
    "REFUSAL_INVALID_PERSON_REF",
    "REFUSAL_ISSUER_CERT_NOT_VALID",
    "REFUSAL_KEY_MISMATCH",
    "REFUSAL_MALFORMED",
    "REFUSAL_MISSING",
    "REFUSAL_NO_PEOPLE_STORE",
    "REFUSAL_NO_TRUST_STORE",
    "REFUSAL_PERMIT_NOT_ALLOWED",
    "REFUSAL_PERSON_INACTIVE",
    "REFUSAL_PERSON_UNKNOWN",
    "REFUSAL_REVOCATIONS_NOT_CURRENT",
    "REFUSAL_REVOKED_ISSUER",
    "REFUSAL_UNKNOWN_ISSUER",
    "REFUSAL_WRONG_INSTALLATION",
    "REFUSAL_WRONG_ISSUER",
    "REFUSAL_WRONG_TEAM",
    "ROOT_TASK_BINDING",
    "SCHEDULER_ISSUER_ROLE",
    "SCHEDULE_OWNER_PERMIT_PREFIX",
    "SCHEDULE_PERMIT_PREFIX",
    "SCHEDULE_RUN_CONTEXT_PREFIX",
    "VerifiedTaskToken",
    "get_process_verifier",
    "is_schedule_permit_binding",
    "schedule_permit_binding_id",
    "set_process_verifier",
    "use_process_verifier",
]
