"""``/auth model``: administrators store the team's model provider keys.

The page lists every endpoint address the team's configuration uses (the
gateway passes them when it opens the page, because the broker cannot read
the instance configuration), every address that has a stored key, and what
the running processes reported: which source they use, whether an
environment variable overrides the stored key, and when a provider last
rejected a key.

Saving runs one real request with the key first
(``model_providers.connection_test``); a failed test saves nothing and shows
the provider's answer without the key, with what it most likely means. An
OpenAI-compatible address is tried with and without ``/v1``
(``check_endpoint_address``), because the table shows only the host and
people type either form. A result is shown just above the form it came from,
and the page scrolls there, because the page is long and the person is
usually far below its top.

The page stays usable for 24 hours (``MODEL_KEY_PAGE_SECONDS``, owner's
decision 2026-10-02: people add several keys over one sitting and close the
page themselves with its Close buttons, which end it at once). Because a page
lives that long, every submission re-checks the person's current standing and
roles in the gateway's copy of the team's people
(``access_control.people_directory``), not only the roles signed into the page
when it opened. Only administrators may use the team page: checked when the
page is opened (``SecureToolBrokerService``'s ``_require_method_authority``)
and again on every submission.
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Mapping

from pydantic import ValidationError

from access_control.people import is_administrator
from access_control.people_directory import PeopleDirectory
from access_control.person_settings import PersonSettings
from custom_messages.contracts.model_keys_v1 import (
    MODEL_KEY_AUTH_PROVIDER,
    PERSONAL_MODEL_KEY_METHOD,
    TEAM_MODEL_KEY_METHOD,
    ConnectionPurposeV1,
    KeySource,
    ModelEndpointStatusV1,
    ModelKeyEndpointUseV1,
    StoredKeyState,
)
from custom_messages.contracts.person_settings_v1 import (
    MIN_CONTEXT_WINDOW_TOKENS,
    PersonModelProfileV1,
    PersonSettingsV1,
)
from model_providers.connection_test import (
    CONNECTION_TEST_TIMEOUT_SECONDS,
    ConnectionPurpose,
    ConnectionTestRequest,
    check_endpoint_address,
    failure_explanation,
)
from model_providers.endpoints import (
    WELL_KNOWN_PROVIDERS,
    EndpointKind,
    InvalidEndpointAddress,
    checked_endpoint_url,
    display_host,
    endpoint_address,
)
from runtime_settings.model_request_options import (
    LITELLM_API_MODE_CHAT_COMPLETIONS,
    LITELLM_API_MODE_RESPONSES,
)
from secure_tool_broker.auth_pages import (
    SECRET_INPUT_TOGGLE_SCRIPT,
    render_broker_auth_page,
    render_credential_input,
)
from secure_tool_broker.model_keys import (
    PERSON_SCOPE,
    ModelKeyReports,
    ModelKeyStore,
    ModelKeyStoreError,
    StoredModelKey,
    model_keys_status,
)
from secure_tool_broker.sign_in_pages import session_status_payload
from secure_tools.chat_commands import MODEL_KEY_COMMAND

LOGGER = logging.getLogger(__name__)

ADMIN_REQUIRED_MESSAGE = "Only a team administrator or owner can change the team's model provider keys."
# How long the page stays open (module docstring).
MODEL_KEY_PAGE_SECONDS = 24 * 60 * 60
NOT_ACTIVE_MESSAGE = "Your account is not active in this team any more, so this page cannot change keys."
# Form actions.
_SAVE = "save"
_REMOVE = "remove"
_CLOSE = "close"
_STATUS_KEY = "status"
_MESSAGE_KEY = "message"
_ERROR_KEY = "error"
# Which part of the page the last result belongs to, and what was typed into the key form (never the key).
_FORM_KEY = "form_id"
_TYPED_KEY = "typed_values"
_CLOSED_STATUS = "closed"
# Part ids: a result is shown at the top of the part it came from, and the page scrolls there.
_ENDPOINTS_ID = "endpoints"
_KEY_FORM_ID = "key-form"
_TOP_ID = "page-top"
_TYPED_FIELDS = (
    "preset", "kind", "base_url", "purpose", "model", "api_mode",
    "use_as_model", "supports_images", "supports_files", "context_window",
)
_REMOVE_MODEL = "remove_model"
_OWN_MODEL_ID = "your-model"
_NEW_ADDRESS_PRESET = "new"
_PURPOSE_LABELS = {ConnectionPurposeV1.CHAT: "Model requests", ConnectionPurposeV1.EMBEDDING: "Embeddings"}
_API_MODE_LABELS = {
    LITELLM_API_MODE_CHAT_COMPLETIONS: "Chat Completions",
    LITELLM_API_MODE_RESPONSES: "Responses",
}


@lru_cache(maxsize=8)
def team_model_key_store(team_id: str) -> ModelKeyStore:
    """The one store per team in this broker, shared by the page and the lookup route (one lock, one cache)."""

    return ModelKeyStore(team_id)


@lru_cache(maxsize=256)
def person_model_key_store(person_id: str) -> ModelKeyStore:
    """One person's own keys (``/auth model me``), in their own credential scope."""

    return ModelKeyStore(person_id, scope=PERSON_SCOPE)


@lru_cache(maxsize=1)
def model_key_reports() -> ModelKeyReports:
    """The broker's one record of what processes reported."""

    return ModelKeyReports()


_PEOPLE: list[PeopleDirectory] = []


_SETTINGS: list[PersonSettings] = []


def use_people_directory(directory: PeopleDirectory | None) -> None:
    """The broker's reader of the team's people, for the live checks on every submission (module docstring)."""

    _PEOPLE.clear()
    if directory is not None:
        _PEOPLE.append(directory)


def use_person_settings(settings: PersonSettings | None) -> None:
    """Where a person's own model is saved (``access_control.person_settings``)."""

    _SETTINGS.clear()
    if settings is not None:
        _SETTINGS.append(settings)


def use_access_store(store: Any | None, team_id: str) -> None:
    """Both readers from the broker's access store (None without storage: roles signed into the page only)."""

    use_people_directory(PeopleDirectory(store, team_id) if store is not None else None)
    use_person_settings(PersonSettings(store, team_id) if store is not None else None)


def _person_settings() -> PersonSettings | None:
    return _SETTINGS[0] if _SETTINGS else None


def _standing_refusal(session: Any, *, administrators_only: bool) -> str | None:
    """Why the person may not change keys now, from their current record; None when they may.

    Without a people store (a broker started without storage) or before the
    gateway has recorded everyone once, the roles signed into the page are
    all there is, and they were checked when it opened.
    """

    if not _PEOPLE or not _PEOPLE[0].synced():
        return ADMIN_REQUIRED_MESSAGE if administrators_only and not is_administrator(session.owner_roles) else None
    person = _PEOPLE[0].person(str(session.owner_canonical_user_id or ""))
    if not person.active:
        return NOT_ACTIVE_MESSAGE
    if administrators_only and not is_administrator(person.roles):
        return ADMIN_REQUIRED_MESSAGE
    return None


class _ModelKeyPage:
    """What the team page and a person's own page share: closing, live checks, where results show."""

    provider = MODEL_KEY_AUTH_PROVIDER
    kind = "form"
    page_seconds = MODEL_KEY_PAGE_SECONDS
    administrators_only = False

    def start(self, session: Any, payload: Mapping[str, Any], logger: logging.Logger) -> None:
        session.state["endpoint_uses"] = [use.model_dump(mode="json") for use in _endpoint_uses(payload, logger)]

    def submit(self, session: Any, form: Mapping[str, Any], logger: logging.Logger) -> None:
        session.state.pop(_ERROR_KEY, None)
        session.state.pop(_MESSAGE_KEY, None)
        action = str(form.get("action") or "")
        session.state[_FORM_KEY] = _form_id(form)
        if action == _CLOSE:
            # A closed page answers "already used" from now on; the browser tab closes itself.
            session.used = True
            session.state[_STATUS_KEY] = _CLOSED_STATUS
            return
        refusal = _standing_refusal(session, administrators_only=self.administrators_only)
        if refusal is not None:
            logger.warning(
                "Model key change refused at submission. event=model_key_submit_refused "
                "person_id=%s page=%s reason=%s",
                session.owner_canonical_user_id,
                self.method,
                "admin_required" if refusal == ADMIN_REQUIRED_MESSAGE else "not_active",
            )
            session.state[_ERROR_KEY] = refusal
            return
        if action == _SAVE:
            session.state[_TYPED_KEY] = json.dumps({name: str(form.get(name) or "") for name in _TYPED_FIELDS})
        try:
            session.state[_MESSAGE_KEY] = self._apply(session, form, action)
        except (_FormProblem, ModelKeyStoreError) as exc:
            session.state[_ERROR_KEY] = str(exc)
        else:
            session.state.pop(_TYPED_KEY, None)

    def render(self, session: Any) -> str:
        if session.state.get(_STATUS_KEY) == _CLOSED_STATUS:
            return _closed_page(self.display_name)
        return render_broker_auth_page(
            self.display_name, self._body(session), script=SECRET_INPUT_TOGGLE_SCRIPT + _PRESET_SCRIPT
        )

    def status_payload(self, session: Any) -> dict[str, Any]:
        return session_status_payload(session)

    def cleanup(self, session: Any) -> None:
        return

    method = ""
    display_name = ""

    def _apply(self, session: Any, form: Mapping[str, Any], action: str) -> str:
        raise NotImplementedError

    def _body(self, session: Any) -> str:
        raise NotImplementedError


class ModelKeyFlow(_ModelKeyPage):
    """``/auth model``: the team's model provider keys, for administrators."""

    method = TEAM_MODEL_KEY_METHOD
    display_name = "Model provider keys"
    administrators_only = True

    def _apply(self, session: Any, form: Mapping[str, Any], action: str) -> str:
        store = team_model_key_store(str(session.owner_team_id or ""))
        if action == _REMOVE:
            return _remove(store, form, person_id=session.owner_canonical_user_id)
        return _test_and_save(store, form, person_id=session.owner_canonical_user_id)

    def _body(self, session: Any) -> str:
        uses = _session_uses(session)
        store = team_model_key_store(str(session.owner_team_id or ""))
        try:
            status = model_keys_status(store, model_key_reports())
            statuses = {item.address: item for item in status.endpoints}
            saved = store.entries()
            store_problem = ""
        except ModelKeyStoreError as exc:
            statuses, saved, store_problem = {}, (), str(exc)
        return (
            _intro_panel(session, store_problem)
            + _endpoints_panel(session, uses, statuses)
            + _key_form_panel(session, uses, saved=saved)
            + _closing_panel()
        )


class PersonalModelKeyFlow(_ModelKeyPage):
    """``/auth model me``: a person's own model and keys (owner's decisions 2026-09-30 and 2026-10-02).

    Anyone active may use it. One form: choosing an endpoint fills in the
    fields. With “Use this as my model” the tested endpoint becomes their
    model for all agents' work for them (any provider and address,
    ``custom_agents.person_model_runtime``) and the key is saved as theirs;
    without it, the key is theirs for an endpoint the team uses. Their keys
    and model are used only for tasks that work for them
    (``secure_tool_broker.model_keys.lookup_handler`` checks the signed task
    context); work for nobody keeps the team's. When a provider rejects their
    key, their task stops and they are told: it never falls back to the
    team's key, so nobody's spending changes without a decision.
    """

    method = PERSONAL_MODEL_KEY_METHOD
    display_name = "Your model and keys"

    def _apply(self, session: Any, form: Mapping[str, Any], action: str) -> str:
        person_id = str(session.owner_canonical_user_id or "")
        store = person_model_key_store(person_id)
        if action == _REMOVE_MODEL:
            return _remove_own_model(person_id)
        if action == _REMOVE:
            return _remove(store, form, person_id=person_id, afterwards="Your work uses the team's key there again.")
        if str(form.get("use_as_model") or "") == "1":
            return _save_own_model(store, form, person_id=person_id)
        allowed = frozenset(use.address for use in _session_uses(session))
        return _test_and_save(store, form, person_id=person_id, allowed_addresses=allowed)

    def _body(self, session: Any) -> str:
        uses = _session_uses(session)
        person_id = str(session.owner_canonical_user_id or "")
        try:
            own = {entry.address: entry for entry in person_model_key_store(person_id).entries()}
            store_problem = ""
        except ModelKeyStoreError as exc:
            own, store_problem = {}, str(exc)
        settings = _person_settings()
        try:
            record = settings.settings(person_id) if settings is not None else None
        except Exception:  # noqa: BLE001 - shown on the page; nothing is changed
            LOGGER.exception("Could not read a person's settings. event=person_settings_read_failed person_id=%s", person_id)
            record, store_problem = None, store_problem or "Your saved model could not be read. Try again in a minute."
        return (
            _intro_panel(session, store_problem, personal=True)
            + _own_model_panel(session, record)
            + _personal_endpoints_panel(session, uses, own)
            + _key_form_panel(
                session,
                uses,
                personal=True,
                use_as_model_default=record is not None and not record.may_use_team_keys,
                saved=tuple(own.values()),
            )
            + _closing_panel()
        )


def _session_uses(session: Any) -> list[ModelKeyEndpointUseV1]:
    return [ModelKeyEndpointUseV1.model_validate(item) for item in session.state.get("endpoint_uses") or ()]


class _FormProblem(ValueError):
    """Something the administrator can fix on the page; the message says what."""


def _endpoint_uses(payload: Mapping[str, Any], logger: logging.Logger) -> list[ModelKeyEndpointUseV1]:
    uses = []
    for raw in payload.get("endpoints") or ():
        try:
            uses.append(ModelKeyEndpointUseV1.model_validate(json.loads(str(raw))))
        except (ValidationError, ValueError) as exc:
            logger.warning(
                "The gateway passed an endpoint the model key page cannot show. event=model_key_page_endpoint_invalid "
                "error=%s",
                exc,
            )
    return uses


@dataclass(frozen=True)
class _TestedKey:
    """A key (or "no key needed") that answered one real request, and the address form that answered."""

    kind: EndpointKind
    typed_url: str
    base_url: str
    model: str
    api_mode: str
    purpose: ConnectionPurposeV1
    key: str = field(repr=False)
    no_key_needed: bool

    @property
    def address(self) -> str:
        return endpoint_address(self.base_url)

    @property
    def moved(self) -> bool:
        return self.base_url != self.typed_url


def _tested_key(form: Mapping[str, Any], *, person_id: str, allowed_addresses: frozenset[str] | None = None) -> _TestedKey:
    """Check the form and test the key with one real request; ``_FormProblem`` says why not (nothing saved)."""

    kind = _kind(form)
    base_url = WELL_KNOWN_PROVIDERS[kind].base_url if kind in WELL_KNOWN_PROVIDERS else _base_url(form)
    if allowed_addresses is not None and endpoint_address(base_url) not in allowed_addresses:
        raise _FormProblem(
            "Without “Use this as my model”, you can save your own key only for an endpoint the team uses. Tick "
            "it to use this endpoint as your model for all your work."
        )
    model = str(form.get("model") or "").strip()
    if not model:
        raise _FormProblem("Enter the model to test the key with.")
    no_key_needed = str(form.get("no_key") or "") == "1"
    key = "" if no_key_needed else str(form.get("key") or "").strip()
    if not key and not no_key_needed:
        raise _FormProblem("Enter the key, or tick “This endpoint needs no key”.")
    purpose = _purpose(form)
    api_mode = str(form.get("api_mode") or LITELLM_API_MODE_CHAT_COMPLETIONS)
    if api_mode not in _API_MODE_LABELS:
        raise _FormProblem("Choose Chat Completions or Responses.")
    outcome = check_endpoint_address(
        ConnectionTestRequest(
            kind=kind,
            base_url=base_url,
            key=key,
            model=model,
            purpose=ConnectionPurpose(purpose.value),
            api_mode=api_mode,
        )
    )
    if not outcome.ok:
        address = endpoint_address(base_url)
        LOGGER.warning(
            "A model key failed its test and was not saved. event=model_key_test_failed address=%s model=%s "
            "api_mode=%s status_code=%s url=%s elapsed_seconds=%s tried=%s person_id=%s",
            address,
            model,
            api_mode,
            outcome.result.status_code,
            outcome.result.url,
            outcome.result.elapsed_seconds,
            ",".join(outcome.tried),
            person_id,
        )
        raise _FormProblem(
            f"Nothing was saved. The test request to {display_host(address)} failed. "
            f"{failure_explanation(outcome)} The endpoint's answer: {outcome.result.message}"
        )
    return _TestedKey(
        kind=kind,
        typed_url=base_url,
        base_url=outcome.base_url,
        model=model,
        api_mode=api_mode,
        purpose=purpose,
        key=key,
        no_key_needed=no_key_needed,
    )


def _save_key(store: ModelKeyStore, tested: _TestedKey, *, person_id: str) -> None:
    store.save(
        StoredModelKey(
            address=tested.address,
            kind=tested.kind,
            key=tested.key,
            no_key_needed=tested.no_key_needed,
            saved_at=datetime.now(timezone.utc),
            saved_by_person_id=person_id,
            tested_model_id=tested.model,
            base_url=tested.base_url,
            tested_api_mode=tested.api_mode,
        )
    )


def _test_and_save(
    store: ModelKeyStore,
    form: Mapping[str, Any],
    *,
    person_id: str,
    allowed_addresses: frozenset[str] | None = None,
) -> str:
    """Test the key with one real request and save it; ``allowed_addresses`` limits a person's own keys."""

    tested = _tested_key(form, person_id=person_id, allowed_addresses=allowed_addresses)
    _save_key(store, tested, person_id=person_id)
    what = "No key is needed" if tested.no_key_needed else "The key works"
    host = display_host(tested.address)
    # Keys belong to the host, so the path never matters for the key; it matters for switching endpoints.
    where = (
        f" The endpoint answers at {tested.base_url}, not at {tested.typed_url}; use {tested.base_url} "
        "when you switch the team to it."
        if tested.moved
        else ""
    )
    if allowed_addresses is not None:
        return (
            f"{what} for {host} (tested with {tested.model}) and is saved as yours. Agents use it for "
            f"your work from their next request; no restart is needed.{where}"
        )
    model_key_reports().clear_rejection(tested.address)
    return (
        f"{what} for {host} (tested with {tested.model}) and is saved. "
        f"Agents use it on their next request; no restart is needed.{where}"
    )


def _save_own_model(store: ModelKeyStore, form: Mapping[str, Any], *, person_id: str) -> str:
    """Test, then save the key as the person's and the model as theirs for all their work (``PersonModelProfileV1``)."""

    settings = _person_settings()
    if settings is None:
        raise _FormProblem("This broker has no access store, so it cannot save your model. Tell an administrator.")
    images, files, window = _profile_capabilities(form)
    tested = _tested_key(form, person_id=person_id)
    if tested.purpose is not ConnectionPurposeV1.CHAT:
        raise _FormProblem("Your model answers your requests: choose Model requests under Used for.")
    profile = PersonModelProfileV1(
        kind=tested.kind,
        base_url=tested.base_url,
        model_id=tested.model,
        api_mode="responses" if tested.api_mode == LITELLM_API_MODE_RESPONSES else "chat_completions",
        supports_images=images,
        supports_files=files,
        context_window_tokens=window,
        tested_at_utc=datetime.now(timezone.utc),
    )
    _save_key(store, tested, person_id=person_id)
    settings.set_own_model(person_id, profile, by=person_id)
    LOGGER.info(
        "A person saved their own model. event=person_model_saved person_id=%s address=%s model=%s",
        person_id,
        tested.address,
        tested.model,
    )
    where = f" It answers at {tested.base_url}." if tested.moved else ""
    return (
        f"Your model is saved: {tested.model} at {display_host(tested.address)}. All agents' work for you uses it "
        f"from your next request; no restart is needed.{where}"
    )


def _profile_capabilities(form: Mapping[str, Any]) -> tuple[bool, bool, int | None]:
    text = str(form.get("context_window") or "").strip().replace(",", "").replace("_", "")
    window: int | None = None
    if text:
        if not text.isdigit() or int(text) < MIN_CONTEXT_WINDOW_TOKENS:
            raise _FormProblem(
                f"The context window is a number of tokens, at least {MIN_CONTEXT_WINDOW_TOKENS}, such as 128000. "
                "Leave it empty if you do not know it."
            )
        window = int(text)
    return str(form.get("supports_images") or "") == "1", str(form.get("supports_files") or "") == "1", window


def _remove_own_model(person_id: str) -> str:
    settings = _person_settings()
    if settings is None or settings.own_model(person_id) is None:
        return "You have no model of your own saved."
    settings.set_own_model(person_id, None, by=person_id)
    LOGGER.info("A person removed their own model. event=person_model_removed person_id=%s", person_id)
    if settings.may_use_team_keys(person_id):
        return "Your model is removed. Agents use the team's models for your work again; your saved keys stay."
    return (
        "Your model is removed. Your work may not use the team's model keys, so your requests do not run until "
        "you save a model again; your saved keys stay."
    )


_TEAM_KEY_REMOVED = "Agents can no longer call it unless an environment variable sets a key."


def _remove(store: ModelKeyStore, form: Mapping[str, Any], *, person_id: str, afterwards: str = _TEAM_KEY_REMOVED) -> str:
    address = endpoint_address(_base_url(form))
    if not store.remove(address, person_id=person_id):
        return f"No key was stored for {display_host(address)}."
    return f"The key for {display_host(address)} is removed. {afterwards}"


def _kind(form: Mapping[str, Any]) -> EndpointKind:
    try:
        return EndpointKind(str(form.get("kind") or EndpointKind.OPENAI_COMPATIBLE.value))
    except ValueError as exc:
        raise _FormProblem("Choose the provider.") from exc


def _base_url(form: Mapping[str, Any]) -> str:
    try:
        return checked_endpoint_url(form.get("base_url"))
    except InvalidEndpointAddress as exc:
        raise _FormProblem(str(exc)) from exc


def _purpose(form: Mapping[str, Any]) -> ConnectionPurposeV1:
    try:
        return ConnectionPurposeV1(str(form.get("purpose") or ConnectionPurposeV1.CHAT.value))
    except ValueError as exc:
        raise _FormProblem("Choose what the endpoint is used for.") from exc


# -- Rendering ------------------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape(str(value))


def _form_id(form: Mapping[str, Any]) -> str:
    """The id of the submitted form; only ids this page renders are kept, anything else shows at the top."""

    value = str(form.get("form_id") or "")
    if value in (_ENDPOINTS_ID, _KEY_FORM_ID, _OWN_MODEL_ID):
        return value
    return _TOP_ID


def _form_start(form_id: str, action: str) -> str:
    """A form that posts back to this page and lands on itself, so its result is in view."""

    return (
        f'<form method="post" action="#{form_id}">'
        f'<input type="hidden" name="form_id" value="{form_id}">'
        f'<input type="hidden" name="action" value="{action}">'
    )


def _result_notice(session: Any, form_id: str) -> str:
    """The last result, when it belongs to ``form_id``: red with its explanation for a failure."""

    if str(session.state.get(_FORM_KEY) or _TOP_ID) != form_id:
        return ""
    error = str(session.state.get(_ERROR_KEY) or "")
    if error:
        return f'<p class="status status-error" role="alert">{_e(error)}</p>'
    message = str(session.state.get(_MESSAGE_KEY) or "")
    return f'<p class="status status-success" role="status">{_e(message)}</p>' if message else ""


def _close_button() -> str:
    return (
        f'<div class="page-actions">{_form_start(_TOP_ID, _CLOSE)}'
        '<button type="submit" class="secondary-action">Close</button></form></div>'
    )


def _closing_panel() -> str:
    return f'<section class="content-panel">{_close_button()}</section>'


def _closed_page(title: str) -> str:
    body = (
        '<section class="content-panel"><p class="eyebrow">Model provider keys</p><h1>This page is closed.</h1>'
        "<p>Nothing more can be changed with it. If this tab stays open, close it yourself. Open a new page "
        f"with {_e(MODEL_KEY_COMMAND)} when you need one.</p></section>"
    )
    return render_broker_auth_page(title, body, script="<script>window.close();</script>")


def _intro_panel(session: Any, store_problem: str, *, personal: bool = False) -> str:
    notices = _result_notice(session, _TOP_ID)
    if store_problem:
        notices += f'<p class="status status-error" role="alert">{_e(store_problem)}</p>'
    heading, lede = (
        (
            "Your model and keys",
            "You can choose your own model: every agent's work for you then uses it, with your key. You can also "
            "save your own key for an endpoint the team uses. Each key is tested with one real request before it "
            "is saved and is sent only to its own address. If a provider rejects your key, your request stops and "
            "you are told; it never switches to the team's key.",
        )
        if personal
        else (
            "Team model keys",
            "Agents send each key only to the address it is saved for. Every key is tested with one real request "
            "before it is saved, and agents use a new key on their next request without a restart.",
        )
    )
    return (
        f'<section class="content-panel" id="{_TOP_ID}"><p class="eyebrow">Model provider keys</p>'
        f"<h1>{heading}</h1>"
        f'<p class="lede">{lede}</p>'
        f"{notices}<p>This page stays open for 24 hours. Close it when you are done.</p>{_close_button()}</section>"
    )


def _endpoints_panel(
    session: Any, uses: list[ModelKeyEndpointUseV1], statuses: dict[str, ModelEndpointStatusV1]
) -> str:
    addresses = list(dict.fromkeys([use.address for use in uses] + sorted(statuses)))
    heading = f'<section class="content-panel" id="{_ENDPOINTS_ID}"><h2>Endpoints</h2>{_result_notice(session, _ENDPOINTS_ID)}'
    if not addresses:
        return f"{heading}<p>No endpoint uses a model key yet.</p></section>"
    rows = "".join(_endpoint_row(address, [use for use in uses if use.address == address], statuses.get(address)) for address in addresses)
    return (
        f'{heading}<p>Keys belong to a host, so an address is shown without its path (such as /v1).</p>'
        '<div class="message-table-wrap"><table>'
        "<thead><tr><th>Address</th><th>Used for</th><th>Key</th><th>Notes</th><th></th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div></section>"
    )


def _remove_form(address: str) -> str:
    return (
        f'{_form_start(_ENDPOINTS_ID, _REMOVE)}<input type="hidden" name="base_url" value="{_e(address)}">'
        '<button type="submit">Remove</button></form>'
    )


def _endpoint_row(address: str, uses: list[ModelKeyEndpointUseV1], status: ModelEndpointStatusV1 | None) -> str:
    used_for = "<br>".join(_e(_use_text(use)) for use in uses) or "Not in the current configuration"
    state = status.state if status is not None else StoredKeyState.MISSING
    remove = ""
    if state is StoredKeyState.STORED:
        key_text = "Stored"
    elif state is StoredKeyState.NO_KEY_NEEDED:
        key_text = "No key needed"
    else:
        key_text = "No key stored"
    if status is not None and status.saved_at is not None:
        key_text += f"<br>saved {_e(_when(status.saved_at))}"
        remove = _remove_form(address)
    notes = "<br>".join(_e(note) for note in _notes(status, state, uses))
    return (
        f"<tr><td>{_e(display_host(address))}</td><td>{used_for}</td><td>{key_text}</td>"
        f"<td>{notes}</td><td>{remove}</td></tr>"
    )


def _use_text(use: ModelKeyEndpointUseV1) -> str:
    text = use.label or _PURPOSE_LABELS[use.purpose]
    return f"{text} ({use.model_id})" if use.model_id else text


def _notes(status: ModelEndpointStatusV1 | None, state: StoredKeyState, uses: list[ModelKeyEndpointUseV1]) -> list[str]:
    notes: list[str] = []
    overrides = status.environment_overrides if status is not None else ()
    for override in overrides:
        notes.append(
            f"The {override.caller} process gets {override.env_var} from team.env or the container's environment, "
            "and it overrides the stored key. The Status page names the exact file or setting; a shell opened with "
            "docker exec does not show team.env values. To use the stored key, remove it there and restart the "
            "container."
        )
    if uses and state is StoredKeyState.MISSING and not overrides:
        notes.append("Agents cannot call this endpoint until a key is saved here.")
    rejection = status.last_rejection if status is not None else None
    if rejection is not None:
        whose = f"the {rejection.env_var} environment variable" if rejection.source is KeySource.ENVIRONMENT else "the stored key"
        code = f" (HTTP {rejection.status_code})" if rejection.status_code else ""
        notes.append(f"The provider rejected {whose}{code} {_when(rejection.rejected_at)}. Save a new key to fix it.")
    return notes


def _when(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("on %Y-%m-%d at %H:%M UTC")


def _personal_endpoints_panel(session: Any, uses: list[ModelKeyEndpointUseV1], own: dict[str, StoredModelKey]) -> str:
    rows = ""
    for address in dict.fromkeys([use.address for use in uses] + sorted(own)):
        used_for = "<br>".join(_e(_use_text(use)) for use in uses if use.address == address) or "Your own model"
        entry = own.get(address)
        key_text, remove = "The team's key", ""
        if entry is not None:
            key_text = ("Yours: no key needed" if entry.no_key_needed else "Yours") + f"<br>saved {_e(_when(entry.saved_at))}"
            remove = _remove_form(address)
        rows += f"<tr><td>{_e(display_host(address))}</td><td>{used_for}</td><td>{key_text}</td><td>{remove}</td></tr>"
    if not rows:
        return ""
    return (
        f'<section class="content-panel" id="{_ENDPOINTS_ID}"><h2>Keys</h2>{_result_notice(session, _ENDPOINTS_ID)}'
        '<div class="message-table-wrap"><table>'
        "<thead><tr><th>Address</th><th>Used for</th><th>Key for your work</th><th></th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div></section>"
    )


def _own_model_panel(session: Any, record: PersonSettingsV1 | None) -> str:
    """The person's own model now, and whether their work may use the team's model keys."""

    own = record.own_model if record is not None else None
    may_use_team_keys = record is None or record.may_use_team_keys
    if own is not None:
        inputs = [name for name, on in (("images", own.supports_images), ("files", own.supports_files)) if on]
        window = f"{own.context_window_tokens:,} tokens" if own.context_window_tokens else "as the team's model"
        body = (
            f"<p>All agents' work for you uses <strong>{_e(own.model_id)}</strong> at "
            f"{_e(display_host(own.address))} ({_e(_API_MODE_LABELS.get(own.api_mode, own.api_mode))}). Context "
            f"window: {_e(window)}. Extra input: {_e(', '.join(inputs) or 'none')}. Saved {_e(_when(own.tested_at_utc))}.</p>"
            f"{_form_start(_OWN_MODEL_ID, _REMOVE_MODEL)}<button type=\"submit\" class=\"secondary-action\">"
            "Stop Using My Model</button></form>"
        )
    elif may_use_team_keys:
        body = (
            "<p>You have no model of your own. Agents use the team's models for your work. To choose your own, "
            "save it below with “Use this as my model”.</p>"
        )
    else:
        body = (
            '<p class="status status-error" role="alert">You haven\'t set up your own model yet. An administrator '
            "decided that your work uses your own model, not the team's, so your requests do not run until you "
            "save one below with “Use this as my model”.</p>"
        )
    return (
        f'<section class="content-panel" id="{_OWN_MODEL_ID}"><h2>Your Model</h2>'
        f"{_result_notice(session, _OWN_MODEL_ID)}{body}</section>"
    )


def _key_fields(prefix: str) -> str:
    return (
        f'<div><label for="{prefix}key">Key</label>'
        + render_credential_input(
            input_id=f"{prefix}key", name="key", input_type="password", autocomplete="new-password"
        )
        + "</div>"
        '<label class="broker-confirmation"><input type="checkbox" name="no_key" value="1">'
        "<span>This endpoint needs no key</span></label>"
    )


@dataclass(frozen=True)
class _Preset:
    """One choice in the Endpoint list: what it fills into the form's fields."""

    key: str
    label: str
    kind: EndpointKind
    base_url: str = ""
    purpose: ConnectionPurposeV1 = ConnectionPurposeV1.CHAT
    model: str = ""
    api_mode: str = LITELLM_API_MODE_CHAT_COMPLETIONS

    @property
    def fixed_address(self) -> bool:
        return self.kind in WELL_KNOWN_PROVIDERS


def _presets(
    uses: list[ModelKeyEndpointUseV1], *, personal: bool, saved: tuple[StoredModelKey, ...] = ()
) -> list[_Preset]:
    """The endpoints the team uses, saved keys for other endpoints, then well-known providers and a new address."""

    presets = [
        _Preset(
            key=f"use{index}",
            label=f"{_use_text(use)}: {display_host(use.address)}",
            kind=use.kind,
            base_url=use.base_url,
            purpose=use.purpose,
            model=use.model_id,
            api_mode=use.api_mode or LITELLM_API_MODE_CHAT_COMPLETIONS,
        )
        for index, use in enumerate(uses)
    ]
    # A saved key for an endpoint the configuration does not use (Bifrost before a switch): testing it
    # again repeats the test it passed, with the same address form, model and request API.
    in_configuration = {use.address for use in uses}
    presets += [
        _Preset(
            key=f"saved{index}",
            label=f"Saved key: {display_host(entry.address)}" + (f" ({entry.tested_model_id})" if entry.tested_model_id else ""),
            kind=entry.kind,
            base_url=entry.base_url or (WELL_KNOWN_PROVIDERS[entry.kind].base_url if entry.kind in WELL_KNOWN_PROVIDERS else entry.address),
            model=entry.tested_model_id,
            api_mode=entry.tested_api_mode or LITELLM_API_MODE_CHAT_COMPLETIONS,
        )
        for index, entry in enumerate(saved)
        if entry.address not in in_configuration
    ]
    # The team page lists only well-known providers the team does not use yet; a person may choose any.
    in_use = set() if personal else {preset.kind for preset in presets if preset.purpose is ConnectionPurposeV1.CHAT}
    presets += [
        _Preset(key=kind.value, label=f"{provider.label} ({display_host(provider.address)})", kind=kind, base_url=provider.base_url)
        for kind, provider in WELL_KNOWN_PROVIDERS.items()
        if kind not in in_use
    ]
    presets.append(_Preset(key=_NEW_ADDRESS_PRESET, label="New OpenAI-compatible address", kind=EndpointKind.OPENAI_COMPATIBLE))
    return presets


def _typed_values(session: Any) -> dict[str, str]:
    """What was typed into the key form before a failed test (never the key), to fill it in again."""

    stored = session.state.get(_TYPED_KEY)
    if not stored:
        return {}
    try:
        values = json.loads(str(stored))
    except ValueError:
        return {}
    return {name: str(values.get(name) or "") for name in _TYPED_FIELDS} if isinstance(values, dict) else {}


def _options(choices: list[tuple[str, str]], selected: str) -> str:
    return "".join(
        f'<option value="{_e(value)}"{" selected" if value == selected else ""}>{_e(label)}</option>'
        for value, label in choices
    )


def _preset_option(preset: _Preset, selected: bool) -> str:
    data = "".join(
        f' data-{name}="{_e(value)}"'
        for name, value in (
            ("kind", preset.kind.value),
            ("base-url", preset.base_url),
            ("purpose", preset.purpose.value),
            ("model", preset.model),
            ("api-mode", preset.api_mode),
            ("fixed-address", "1" if preset.fixed_address else ""),
        )
    )
    return f'<option value="{_e(preset.key)}"{data}{" selected" if selected else ""}>{_e(preset.label)}</option>'


def _key_form_panel(
    session: Any,
    uses: list[ModelKeyEndpointUseV1],
    *,
    personal: bool = False,
    use_as_model_default: bool = False,
    saved: tuple[StoredModelKey, ...] = (),
) -> str:
    """The page's one key form: choosing an endpoint fills in the fields below it (owner's decision 2026-10-02)."""

    presets = _presets(uses, personal=personal, saved=saved)
    heading = (
        f'<section class="content-panel" id="{_KEY_FORM_ID}"><h2>{"Save Your Model or Key" if personal else "Save a Key"}</h2>'
        f"{_result_notice(session, _KEY_FORM_ID)}"
    )
    if not presets:
        return f"{heading}<p>The team uses no endpoint that takes a key yet.</p></section>"
    typed = _typed_values(session)
    chosen = next((preset for preset in presets if preset.key == typed.get("preset")), presets[0])
    values = {
        "kind": chosen.kind.value,
        "base_url": chosen.base_url,
        "purpose": chosen.purpose.value,
        "model": chosen.model,
        "api_mode": chosen.api_mode,
    }
    values.update({name: value for name, value in typed.items() if name != "preset" and value})
    fixed = values["kind"] in _FIXED_ADDRESS_KINDS
    address_note = (
        "Choose an endpoint to fill in the fields, or choose New OpenAI-compatible address and type it. "
        "OpenAI, Anthropic, Google Gemini and xAI have fixed addresses. You can leave out /v1: the test tries "
        "the address with and without it."
    )
    purposes = _options([(purpose.value, label) for purpose, label in _PURPOSE_LABELS.items()], values["purpose"])
    modes = _options(list(_API_MODE_LABELS.items()), values["api_mode"])
    options = "".join(_preset_option(preset, preset is chosen) for preset in presets)
    readonly = " readonly" if fixed else ""
    return (
        f"{heading}<p>Saving replaces the key stored for that address. The test sends one short request to the "
        f"model.</p>{_form_start(_KEY_FORM_ID, _SAVE)}"
        f'<p class="status" role="status" aria-live="polite" data-model-key-progress hidden>{_e(_TESTING_NOTE)}</p>'
        f'<input type="hidden" name="kind" value="{_e(values["kind"])}" data-model-key-field="kind">'
        "<fieldset><legend>Endpoint and key</legend>"
        f"<p>{_e(address_note)}</p>"
        '<div><label for="key-preset">Endpoint</label>'
        f'<select id="key-preset" name="preset" data-model-key-preset>{options}</select></div>'
        '<div><label for="key-base_url">Endpoint address</label>'
        f'<input id="key-base_url" name="base_url" type="url" autocomplete="off" value="{_e(values["base_url"])}" '
        f'placeholder="https://example.com" data-model-key-field="base-url"{readonly}></div>'
        f'<div><label for="key-purpose">Used for</label><select id="key-purpose" name="purpose" data-model-key-field="purpose">{purposes}</select></div>'
        '<div><label for="key-model">Model to test with</label>'
        f'<input id="key-model" name="model" type="text" autocomplete="off" value="{_e(values["model"])}" required '
        'data-model-key-field="model"></div>'
        '<div><label for="key-api_mode">Request API (OpenAI-compatible endpoints)</label>'
        f'<select id="key-api_mode" name="api_mode" data-model-key-field="api-mode">{modes}</select></div>'
        f'{_model_fields(typed, use_as_model_default) if personal else ""}'
        f'{_key_fields("key-")}<button type="submit">Test and Save</button></fieldset></form></section>'
    )


def _checkbox(name: str, label: str, checked: bool) -> str:
    return (
        f'<label class="broker-confirmation"><input type="checkbox" name="{name}" value="1"'
        f'{" checked" if checked else ""}><span>{_e(label)}</span></label>'
    )


def _model_fields(typed: dict[str, str], use_as_model_default: bool) -> str:
    """A person's page: use the endpoint as their model, and what that model accepts."""

    use_as_model = typed.get("use_as_model") == "1" if typed else use_as_model_default
    return (
        _checkbox(
            "use_as_model",
            "Use this as my model: every agent's work for me uses this model and key. Without it, the key is "
            "used only with the team's own models at an endpoint the team uses.",
            use_as_model,
        )
        + _checkbox("supports_images", "My model accepts images", typed.get("supports_images") == "1")
        + _checkbox("supports_files", "My model accepts files such as PDFs", typed.get("supports_files") == "1")
        + '<div><label for="key-context_window">Context window in tokens (optional)</label>'
        f'<input id="key-context_window" name="context_window" type="text" inputmode="numeric" autocomplete="off" '
        f'value="{_e(typed.get("context_window", ""))}" placeholder="128000"></div>'
    )


_FIXED_ADDRESS_KINDS = frozenset(kind.value for kind in WELL_KNOWN_PROVIDERS)
# Shown while a test runs, because a slow model can keep the page waiting (owner's report 2026-10-02).
_TESTING_NOTE = (
    "Sending one test request to the endpoint. A slow model can take up to "
    f"{round(CONNECTION_TEST_TIMEOUT_SECONDS / 60)} minutes to answer; keep this page open."
)

# Fills the key form's fields from the chosen endpoint; a well-known provider's address cannot be edited.
_PRESET_SCRIPT = """
<script>
(() => {
  // Every action shows that it is running and cannot be sent twice; the key test also says what it waits for.
  for (const form of document.querySelectorAll("form[method=post]")) {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type=submit]");
      const progress = form.querySelector("[data-model-key-progress]");
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        button.textContent = progress ? "Testing\u2026" : "Working\u2026";
      }
      if (progress) {
        const address = document.querySelector('[data-model-key-field="base-url"]');
        if (address && address.value) {
          progress.textContent = progress.textContent.replace("the endpoint", address.value);
        }
        progress.hidden = false;
      }
    });
  }
  // A page restored from the browser's back cache shows its buttons ready again.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) window.location.reload();
  });
  const preset = document.querySelector("[data-model-key-preset]");
  if (!preset) return;
  const field = (name) => document.querySelector(`[data-model-key-field="${name}"]`);
  preset.addEventListener("change", () => {
    const option = preset.selectedOptions[0];
    if (!option) return;
    for (const name of ["kind", "base-url", "purpose", "model", "api-mode"]) {
      const target = field(name);
      const value = option.dataset[name.replace(/-([a-z])/g, (_m, c) => c.toUpperCase())] || "";
      // Selects keep their current choice when the endpoint has none (a new address).
      if (target && (value || target.tagName !== "SELECT")) target.value = value;
    }
    const address = field("base-url");
    address.readOnly = option.dataset.fixedAddress === "1";
    (address.value ? field("model") : address).focus();
  });
})();
</script>
"""


__all__ = [
    "ADMIN_REQUIRED_MESSAGE",
    "MODEL_KEY_COMMAND",
    "MODEL_KEY_PAGE_SECONDS",
    "NOT_ACTIVE_MESSAGE",
    "ModelKeyFlow",
    "PersonalModelKeyFlow",
    "model_key_reports",
    "team_model_key_store",
    "use_access_store",
    "use_people_directory",
    "use_person_settings",
]
