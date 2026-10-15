"""Model provider keys in the broker's credential store, by endpoint address.

Why the broker: model keys used to come only from environment variables
(``LITELLM_API_KEY`` and others), so changing one meant editing ``team.env``
and restarting the whole container, and a wrong or expired key could only be
fixed there. Now an administrator enters the team's key on the ``/auth
model`` page (``secure_tool_broker.model_key_page``), which tests it with a
real request before saving it here, and running processes pick it up on
their next model call (``custom_agents.model_keys``).

- **Storage.** One record per owner, ``model.endpoint_keys_json`` in the
  credential-state store (Sealbox by default): for each endpoint address, its
  key or "no key needed", who saved it, when, and the model it was tested
  with. The owner is the team (and, from phase 5, each person). Writes are
  serialized with a lock so two administrators cannot overwrite each other's
  change, and reads are cached briefly because every Sealbox read starts a
  process.
- **Release.** ``MODEL_KEY_LOOKUP_ROUTE`` hands a key only to processes that
  run agents (the manager and agent processes, by their caller tokens;
  ``SecureToolBrokerService._caller_may_use``). Agents cannot call it as a
  tool, and sandboxed code cannot read those tokens.
- **Reports.** Processes say which source they use for each address (an
  environment variable, by name only, overrides the stored key) and when a
  provider rejected a key. The broker keeps the latest report per process in
  memory; processes repeat their reports, so a restarted broker learns them
  again. ``/auth model`` and the Status page read them through
  ``MODEL_KEY_STATUS_ROUTE`` (gateway only).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from access_control.caller_tokens import BrokerCaller
from access_control.person_settings import PersonSettings
from custom_messages.contracts.model_keys_v1 import (
    ConnectionPurposeV1,
    KeyOwner,
    KeyRejectionV1,
    KeySource,
    KeyReportEvent,
    ModelEndpointStatusV1,
    ModelKeyLookupV1,
    ModelKeyReplyV1,
    ModelKeyReportV1,
    ModelKeysStatusV1,
    ProcessKeySourceV1,
    SavedEndpointsV1,
    SavedEndpointV1,
    StoredKeyState,
)
from model_providers.endpoints import WELL_KNOWN_PROVIDERS, EndpointKind, endpoint_address
from secure_tool_broker.credential_state import credential_owner_context
from secure_tool_broker.credential_state_store import read_credential_state, write_credential_state
from secure_tools.providers import ToolResult

LOGGER = logging.getLogger(__name__)

MODEL_KEYS_STATE_KEY = "model.endpoint_keys_json"
# The only keys the gateway may look up: Grok voice (assistant_gateway.voice) uses
# the xAI key. Every other key goes only to processes that run agents.
GATEWAY_KEY_ADDRESSES = frozenset({WELL_KNOWN_PROVIDERS[EndpointKind.XAI].address})
_RECORD_SCHEMA_VERSION = 1
# A cached record is reread after this long, so a key saved by another broker
# (or restored from a backup) is noticed without a restart.
_STORE_CACHE_SECONDS = 30.0
_MAX_KEY_CHARS = 8192


class ModelKeyStoreError(RuntimeError):
    """The credential store could not be read or written; the message names what to check."""


class StoredModelKey(BaseModel):
    """One address's entry as stored (the key is part of the stored record, never of a reply)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    address: str
    kind: EndpointKind
    key: str = Field(default="", max_length=_MAX_KEY_CHARS, repr=False)
    no_key_needed: bool = False
    saved_at: datetime
    saved_by_person_id: str
    tested_model_id: str = ""
    # The address form that answered the test (with or without /v1), so agents can switch to it as is,
    # and the request API it was tested with.
    base_url: str = ""
    tested_api_mode: str = ""

    @property
    def state(self) -> StoredKeyState:
        return StoredKeyState.NO_KEY_NEEDED if self.no_key_needed else StoredKeyState.STORED


class _ModelKeyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = _RECORD_SCHEMA_VERSION
    keys: dict[str, StoredModelKey] = Field(default_factory=dict)


@dataclass
class _Cached:
    record: _ModelKeyRecord
    read_at: float


TEAM_SCOPE = "team"
PERSON_SCOPE = "user"


class ModelKeyStore:
    """One owner's model keys in the credential store: the team's, or one person's own.

    One record per owner, read-modify-write under a lock. A person's record
    lives in their own credential scope, so removing the person deletes it
    (``departed_person_credentials``, ``SignInService.MODEL_KEYS``).
    """

    def __init__(self, owner_id: str, *, scope: str = TEAM_SCOPE, clock: Callable[[], float] = time.monotonic) -> None:
        self._owner_id = owner_id
        self._scope = scope
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: _Cached | None = None

    def get(self, address: str) -> StoredModelKey | None:
        return self._record().keys.get(endpoint_address(address))

    def entries(self) -> tuple[StoredModelKey, ...]:
        return tuple(sorted(self._record().keys.values(), key=lambda item: item.address))

    def save(self, entry: StoredModelKey) -> None:
        with self._lock:
            record = self._read_uncached()
            record.keys[entry.address] = entry
            self._write(record)
        LOGGER.info(
            "A model key was stored. event=model_key_stored scope=%s owner_id=%s address=%s kind=%s "
            "no_key_needed=%s person_id=%s tested_model=%s",
            self._scope,
            self._owner_id,
            entry.address,
            entry.kind.value,
            entry.no_key_needed,
            entry.saved_by_person_id,
            entry.tested_model_id,
        )

    def remove(self, address: str, *, person_id: str) -> bool:
        normalized = endpoint_address(address)
        with self._lock:
            record = self._read_uncached()
            if record.keys.pop(normalized, None) is None:
                return False
            self._write(record)
        LOGGER.info(
            "A model key was removed. event=model_key_removed scope=%s owner_id=%s address=%s person_id=%s",
            self._scope,
            self._owner_id,
            normalized,
            person_id,
        )
        return True

    def _record(self) -> _ModelKeyRecord:
        cached = self._cached
        if cached is not None and self._clock() - cached.read_at < _STORE_CACHE_SECONDS:
            return cached.record
        with self._lock:
            return self._read_uncached()

    def _read_uncached(self) -> _ModelKeyRecord:
        with credential_owner_context(self._owner_id, scope_kind=self._scope):
            stored = read_credential_state(MODEL_KEYS_STATE_KEY)
        if isinstance(stored, ToolResult):
            LOGGER.error(
                "Model keys could not be read. event=model_keys_unreadable scope=%s owner_id=%s error=%s",
                self._scope,
                self._owner_id,
                stored.error,
            )
            raise ModelKeyStoreError(
                f"The broker could not read the model keys from its credential store. {stored.error or ''}".strip()
            )
        record = _parse_record(str(stored or ""), owner_id=self._owner_id)
        self._cached = _Cached(record=record, read_at=self._clock())
        return record.model_copy(deep=True)

    def _write(self, record: _ModelKeyRecord) -> None:
        with credential_owner_context(self._owner_id, scope_kind=self._scope):
            result = write_credential_state(MODEL_KEYS_STATE_KEY, record.model_dump_json())
        if result.status != "success":
            LOGGER.error(
                "Model keys could not be saved. event=model_keys_store_failed scope=%s owner_id=%s error=%s",
                self._scope,
                self._owner_id,
                result.error,
            )
            raise ModelKeyStoreError(
                f"The broker could not save the model key in its credential store. {result.error or ''}".strip()
            )
        self._cached = _Cached(record=record.model_copy(deep=True), read_at=self._clock())


def _parse_record(text: str, *, owner_id: str) -> _ModelKeyRecord:
    if not text.strip():
        return _ModelKeyRecord()
    try:
        return _ModelKeyRecord.model_validate_json(text)
    except ValidationError as exc:
        # The record is the broker's own; a record it cannot read is a defect,
        # not something to repair silently. Every lookup now fails loudly, and
        # saving a key on /auth model replaces the record.
        LOGGER.error(
            "Stored model keys are not readable. event=model_keys_invalid owner_id=%s errors=%s",
            owner_id,
            exc.error_count(),
        )
        raise ModelKeyStoreError(
            "The stored model keys are damaged. Save each key again with /auth model."
        ) from exc


@dataclass
class _AddressReports:
    sources: dict[str, ProcessKeySourceV1] = field(default_factory=dict)
    kind: EndpointKind | None = None
    last_rejection: KeyRejectionV1 | None = None


class ModelKeyReports:
    """The latest report of each process for each address, in memory."""

    def __init__(self, *, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._by_address: dict[str, _AddressReports] = {}

    def record(self, caller: str, report: ModelKeyReportV1) -> None:
        if report.source is KeySource.PERSONAL:
            # A person's own key is their matter: it never shows as the team's
            # key state, and a rejection was already told to that person.
            return
        now = self._now()
        with self._lock:
            entry = self._by_address.setdefault(report.address, _AddressReports())
            entry.kind = report.kind
            entry.sources[caller] = ProcessKeySourceV1(
                caller=caller, source=report.source, env_var=report.env_var, reported_at=now
            )
            if report.event is KeyReportEvent.REJECTED:
                entry.last_rejection = KeyRejectionV1(
                    caller=caller,
                    source=report.source,
                    env_var=report.env_var,
                    provider=report.provider,
                    model_id=report.model_id,
                    status_code=report.status_code,
                    rejected_at=now,
                )
        if report.event is KeyReportEvent.REJECTED:
            LOGGER.warning(
                "A model provider rejected a key. event=model_key_rejected_reported caller=%s address=%s source=%s "
                "env_var=%s provider=%s model_id=%s status_code=%s",
                caller,
                report.address,
                report.source.value,
                report.env_var,
                report.provider,
                report.model_id,
                report.status_code,
            )

    def clear_rejection(self, address: str) -> None:
        """A new key was saved for ``address``; the old rejection no longer describes it."""

        with self._lock:
            entry = self._by_address.get(address)
            if entry is not None:
                entry.last_rejection = None

    def snapshot(self) -> dict[str, _AddressReports]:
        with self._lock:
            return {
                address: _AddressReports(dict(entry.sources), entry.kind, entry.last_rejection)
                for address, entry in self._by_address.items()
            }


def model_keys_status(store: ModelKeyStore, reports: ModelKeyReports) -> ModelKeysStatusV1:
    """Every address the store or a process knows, with the stored key's details (never the key)."""

    stored = {entry.address: entry for entry in store.entries()}
    reported = reports.snapshot()
    endpoints = []
    for address in sorted(set(stored) | set(reported)):
        entry = stored.get(address)
        report = reported.get(address, _AddressReports())
        endpoints.append(
            ModelEndpointStatusV1(
                address=address,
                kind=entry.kind if entry is not None else report.kind,
                state=entry.state if entry is not None else StoredKeyState.MISSING,
                saved_at=entry.saved_at if entry is not None else None,
                saved_by_person_id=entry.saved_by_person_id if entry is not None else None,
                tested_model_id=(entry.tested_model_id or None) if entry is not None else None,
                sources=tuple(sorted(report.sources.values(), key=lambda item: item.caller)),
                last_rejection=report.last_rejection,
            )
        )
    return ModelKeysStatusV1(endpoints=tuple(endpoints))


Handler = Callable[[web.Request], Awaitable[web.Response]]


def lookup_handler(
    team_store: ModelKeyStore,
    person_store: Callable[[str], ModelKeyStore],
    gate: Any,
    caller_key: web.RequestKey[BrokerCaller],
) -> Handler:
    """``MODEL_KEY_LOOKUP_ROUTE``: the key of one address, for a process that runs agents.

    With a task context, the broker verifies it like a tool call's
    (``BrokerAccessGate.verified_person_id``) and answers with that person's
    own key when they saved one for the address; otherwise (or for work for
    nobody) with the team's. A person's key never reaches another person's
    task. A person whose work may not use the team's keys
    (``access_control.person_settings``) gets no team key for model requests:
    the reply says so (``team_keys_allowed``) and the call does not run.
    The reply holds a key, so it is never logged; the log names the address,
    the caller and whose key it was.
    """

    async def handle(request: web.Request) -> web.Response:
        try:
            lookup = ModelKeyLookupV1.model_validate(await request.json())
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=f"Invalid model key lookup: {exc}") from exc
        caller = request[caller_key]
        person_id = _verified_person(gate, caller, lookup)
        try:
            entry, owner = await asyncio.to_thread(_entry_for, team_store, person_store, person_id, lookup.address)
            refused = owner == "team" and await asyncio.to_thread(_team_keys_refused, gate, person_id, lookup)
        except ModelKeyStoreError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc
        if refused:
            LOGGER.info(
                "A person's work may not use the team's model keys. event=model_key_team_keys_refused "
                "caller=%s address=%s person_id=%s",
                caller.label,
                lookup.address,
                person_id,
            )
            return web.json_response(
                ModelKeyReplyV1(
                    address=lookup.address, state=StoredKeyState.MISSING, team_keys_allowed=False
                ).model_dump(mode="json")
            )
        if entry is None:
            LOGGER.info(
                "No model key is stored for an address a process asked for. event=model_key_lookup_missing "
                "caller=%s address=%s person_id=%s",
                caller.label,
                lookup.address,
                person_id,
            )
            return web.json_response(
                ModelKeyReplyV1(address=lookup.address, state=StoredKeyState.MISSING).model_dump(mode="json")
            )
        reply = ModelKeyReplyV1(
            address=entry.address,
            state=entry.state,
            key="" if entry.no_key_needed else entry.key,
            saved_at=entry.saved_at,
            owner=owner,
        )
        return web.json_response(reply.model_dump(mode="json"))

    return handle


def _team_keys_refused(gate: Any, person_id: str | None, lookup: ModelKeyLookupV1) -> bool:
    """Whether a model request for this person must not use the team's key (``access_control.person_settings``)."""

    if person_id is None or lookup.purpose is not ConnectionPurposeV1.CHAT:
        return False
    store = getattr(gate, "links", None)
    if store is None:
        # A broker without the access store has no per-person settings: everyone uses the team's keys.
        return False
    try:
        return not PersonSettings(store, gate.team_id).may_use_team_keys(person_id)
    except Exception as exc:  # noqa: BLE001 - fail closed: the person's work does not get the team's key
        LOGGER.exception(
            "Could not read whether a person may use the team's model keys. "
            "event=person_settings_unreadable person_id=%s",
            person_id,
        )
        raise ModelKeyStoreError(
            "The broker could not read whether this person may use the team's model keys. Try again in a minute."
        ) from exc


def _verified_person(gate: Any, caller: BrokerCaller, lookup: ModelKeyLookupV1) -> str | None:
    """Whose key the lookup may get: the person its verified task context names, or None for the team's.

    The gateway (Grok voice) may ask only for ``GATEWAY_KEY_ADDRESSES``, without a task context.
    """

    if not caller.runs_agents and (lookup.address not in GATEWAY_KEY_ADDRESSES or lookup.context is not None):
        LOGGER.warning(
            "A model key lookup was refused for this process. event=model_key_lookup_refused caller=%s address=%s",
            caller.label,
            lookup.address,
        )
        raise web.HTTPForbidden(text="This process may look up only the key Grok voice uses.")
    if lookup.context is None:
        return None
    try:
        person: str | None = gate.verified_person_id(caller, lookup.context)
        return person
    except (PermissionError, ValueError) as exc:
        LOGGER.warning(
            "A model key lookup's task context did not verify. event=model_key_lookup_refused caller=%s "
            "address=%s reason=%s",
            caller.label,
            lookup.address,
            exc,
        )
        raise web.HTTPForbidden(text=f"The task context did not verify: {exc}") from exc


def _entry_for(
    team_store: ModelKeyStore, person_store: Callable[[str], ModelKeyStore], person_id: str | None, address: str
) -> tuple[StoredModelKey | None, KeyOwner]:
    if person_id is not None:
        personal = person_store(person_id).get(address)
        if personal is not None:
            return personal, "person"
    return team_store.get(address), "team"


def report_handler(reports: ModelKeyReports, caller_key: web.RequestKey[BrokerCaller]) -> Handler:
    """``MODEL_KEY_REPORT_ROUTE``: a process says which source it uses, or that a key was rejected."""

    async def handle(request: web.Request) -> web.Response:
        try:
            report = ModelKeyReportV1.model_validate(await request.json())
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text=f"Invalid model key report: {exc}") from exc
        reports.record(request[caller_key].label, report)
        return web.json_response({"recorded": True})

    return handle


def endpoints_handler(store: ModelKeyStore) -> Handler:
    """``MODEL_KEY_ENDPOINTS_ROUTE``: the team's saved endpoints without keys, for TeamLead's configuration view."""

    async def handle(_request: web.Request) -> web.Response:
        try:
            entries = await asyncio.to_thread(store.entries)
        except ModelKeyStoreError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc
        saved = SavedEndpointsV1(
            endpoints=tuple(
                SavedEndpointV1(
                    address=entry.address,
                    base_url=entry.base_url,
                    kind=entry.kind,
                    tested_model_id=entry.tested_model_id,
                    tested_api_mode=entry.tested_api_mode,
                    saved_at=entry.saved_at,
                    no_key_needed=entry.no_key_needed,
                )
                for entry in entries
            )
        )
        return web.json_response(saved.model_dump(mode="json"))

    return handle


def status_handler(store: ModelKeyStore, reports: ModelKeyReports) -> Handler:
    """``MODEL_KEY_STATUS_ROUTE``: what the broker knows about each address, without keys (gateway only)."""

    async def handle(_request: web.Request) -> web.Response:
        try:
            status = await asyncio.to_thread(model_keys_status, store, reports)
        except ModelKeyStoreError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc
        return web.json_response(status.model_dump(mode="json"))

    return handle


__all__ = [
    "MODEL_KEYS_STATE_KEY",
    "ModelKeyReports",
    "ModelKeyStore",
    "ModelKeyStoreError",
    "StoredModelKey",
    "lookup_handler",
    "model_keys_status",
    "report_handler",
    "status_handler",
]
