"""Secure credential collection form rendering, expiry controls, and submission parsing.

Kept separate from ``SecureToolBrokerService`` (which is held to a strict
method/statement ratchet by ``tests/test_runtime_architecture_boundaries.py``)
so the standalone page and Web Chat's inline card view (``?view=card``) share
one exact renderer, one client-side expiry script, and one submission parser.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

from aiohttp import web

from secure_tool_broker.auth_pages import (
    SECRET_INPUT_TOGGLE_SCRIPT,
    render_broker_auth_page,
    render_credential_input,
    render_markdown_text,
    render_session_countdown,
)
from secure_tool_broker.credential_catalog import (
    load_catalog,
    resolve_credential_write_source,
)
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.credential_expiry import (
    CUSTOM_EXPIRY_OPTION,
    EXPIRY_PRESET_OPTIONS,
    CredentialExpiryError,
    format_utc_timestamp,
    is_expired,
    parse_utc_timestamp,
    resolve_form_expiry,
)

LOGGER = logging.getLogger(__name__)

CREDENTIAL_FORM_ID = "credential-form"
DELETE_AUTOMATICALLY_FIELD = "delete_automatically"
EXPIRY_WHEN_FIELD = "expiry_when"
EXPIRY_CUSTOM_LOCAL_FIELD = "expiry_custom_local"
EXPIRY_TIMEZONE_FIELD = "expiry_timezone"
EXPIRY_UTC_OFFSET_FIELD = "expiry_utc_offset_minutes"
DEFAULT_EXPIRY_WHEN = "1h"

_TRUTHY_FORM_VALUES = frozenset({"yes", "true", "1", "on"})


@dataclass(frozen=True)
class ExpiryFormState:
    """State of the temporary-credential expiry controls on the secure form."""

    enabled: bool = False
    when: str = DEFAULT_EXPIRY_WHEN
    custom_local: str = ""
    timezone_name: str = ""
    tz_offset_minutes: str = ""
    proposed_expires_at: str = ""


@dataclass(frozen=True)
class ParsedCredentialSubmission:
    """Validated fields and expiry resolution extracted from a form POST."""

    submitted_fields: dict[str, str] = field(default_factory=dict)
    expiry_state: ExpiryFormState = field(default_factory=ExpiryFormState)
    resolved_expires_at: datetime | None = None
    expiry_error: str | None = None
    expiry_error_code: str | None = None


CREDENTIAL_EXPIRY_FORM_SCRIPT = f"""
<script>
(() => {{
  const root = document.querySelector("[data-credential-expiry-control]");
  if (!root) {{
    return;
  }}
  const checkbox = root.querySelector("[data-expiry-checkbox]");
  const whenWrap = root.querySelector("[data-expiry-when-wrap]");
  const whenSelect = root.querySelector("[data-expiry-when]");
  const customWrap = root.querySelector("[data-expiry-custom-wrap]");
  const customInput = root.querySelector("[data-expiry-custom]");
  const tzInput = root.querySelector("[data-expiry-timezone]");
  const offsetInput = root.querySelector("[data-expiry-utc-offset]");
  if (!checkbox || !whenWrap || !whenSelect || !customWrap || !customInput) {{
    return;
  }}
  function pad2(n) {{
    return String(n).padStart(2, "0");
  }}
  function formatLocalDateTime(date) {{
    return (
      date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate()) +
      "T" + pad2(date.getHours()) + ":" + pad2(date.getMinutes())
    );
  }}
  function parseLocalDate(text) {{
    const match = /^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})T(\\d{{2}}):(\\d{{2}})(?::(\\d{{2}}))?$/.exec(String(text || "").trim());
    if (!match) {{
      return null;
    }}
    return new Date(
      Number(match[1]),
      Number(match[2]) - 1,
      Number(match[3]),
      Number(match[4]),
      Number(match[5]),
      match[6] ? Number(match[6]) : 0,
      0
    );
  }}
  function detectTimezone() {{
    try {{
      const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
      if (zone && tzInput) {{
        tzInput.value = zone;
      }}
    }} catch (_err) {{
      // Fall back to numeric offset below.
    }}
  }}
  function updateOffset() {{
    if (!offsetInput) {{
      return;
    }}
    const selected = parseLocalDate(customInput.value);
    const reference = selected && !Number.isNaN(selected.getTime()) ? selected : new Date();
    offsetInput.value = String(reference.getTimezoneOffset());
  }}
  function syncControls() {{
    const enabled = Boolean(checkbox.checked);
    const isCustom = enabled && whenSelect.value === {CUSTOM_EXPIRY_OPTION!r};
    whenWrap.hidden = !enabled;
    whenSelect.disabled = !enabled;
    customWrap.hidden = !isCustom;
    customInput.disabled = !isCustom;
    customInput.required = isCustom;
    updateOffset();
  }}
  detectTimezone();
  const proposed = root.getAttribute("data-proposed-expires-at") || "";
  if (proposed && !customInput.value) {{
    const parsed = new Date(proposed);
    if (!Number.isNaN(parsed.getTime())) {{
      customInput.value = formatLocalDateTime(parsed);
    }}
  }}
  checkbox.addEventListener("change", syncControls);
  whenSelect.addEventListener("change", syncControls);
  customInput.addEventListener("input", updateOffset);
  customInput.addEventListener("change", updateOffset);
  const form = root.closest("form");
  if (form) {{
    form.addEventListener("submit", updateOffset);
  }}
  syncControls();
}})();
</script>
"""


def credential_collection_owner_scope(
    request_payload: Mapping[str, Any],
    *,
    team_id: str | None,
    canonical_user_id: str | None,
) -> CredentialOwnerScope:
    """Resolve the catalog owner scope for a credential collection session."""

    if str(request_payload.get("owner_scope_kind") or "user").strip().lower() == "team":
        return CredentialOwnerScope.team(team_id or "")
    return CredentialOwnerScope.user(canonical_user_id or "")


def credential_key_exists(
    credential_key: str,
    *,
    scope: CredentialOwnerScope,
) -> bool:
    """Return True when ``credential_key`` currently exists (and is not expired) in ``scope``."""

    if not credential_key:
        return False
    try:
        existing = load_catalog(scope=scope).credentials.get(credential_key)
    except Exception as exc:
        LOGGER.warning(
            "Could not check existing credential key before rendering form. "
            "event=credential_form_existing_key_check_failed credential_key=%s error=%s",
            credential_key,
            type(exc).__name__,
        )
        return False
    return existing is not None and not is_expired(existing.expires_at)


def normalize_proposed_expiry(raw: Any) -> str | None:
    """Normalize an optional proposed ``expires_at`` from a session request payload."""

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    parsed = parse_utc_timestamp(raw)
    return format_utc_timestamp(parsed)


def initial_expiry_form_state(request_payload: Mapping[str, Any]) -> ExpiryFormState:
    """Build initial expiry control state from an optional proposed ``expires_at``."""

    raw = request_payload.get("expires_at")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ExpiryFormState()
    try:
        proposed_utc = format_utc_timestamp(parse_utc_timestamp(raw))
    except CredentialExpiryError:
        return ExpiryFormState()
    return ExpiryFormState(
        enabled=True,
        when=CUSTOM_EXPIRY_OPTION,
        proposed_expires_at=proposed_utc,
    )


def _render_expiry_control(
    state: ExpiryFormState,
    *,
    key_exists: bool,
) -> str:
    checked_attr = " checked" if state.enabled else ""
    when_hidden_attr = "" if state.enabled else " hidden"
    when_disabled_attr = "" if state.enabled else " disabled"
    is_custom = state.enabled and state.when == CUSTOM_EXPIRY_OPTION
    custom_hidden_attr = "" if is_custom else " hidden"
    custom_disabled_attr = "" if is_custom else " disabled"
    custom_required_attr = " required" if is_custom else ""
    options_html = "".join(
        f'<option value="{html.escape(option_value)}"'
        f'{" selected" if state.when == option_value else ""}>'
        f"{html.escape(option_label)}</option>"
        for option_value, option_label in EXPIRY_PRESET_OPTIONS
    )
    proposed_attr = (
        f' data-proposed-expires-at="{html.escape(state.proposed_expires_at, quote=True)}"'
        if state.proposed_expires_at
        else ""
    )
    existing_warning_html = (
        '<p class="expiry-existing-warning">'
        "This credential already exists. If you set a deletion time, the whole credential, "
        "including earlier versions, is deleted at that time."
        "</p>"
        if key_exists
        else ""
    )
    return (
        f'<div class="expiry-control" data-credential-expiry-control{proposed_attr}>'
        '<div class="expiry-bar">'
        f'<input id="credential_delete_automatically" type="checkbox" '
        f'name="{DELETE_AUTOMATICALLY_FIELD}" value="yes" data-expiry-checkbox{checked_attr}>'
        '<label class="expiry-bar-label" for="credential_delete_automatically">Delete automatically</label>'
        f'<div class="expiry-when-wrap" data-expiry-when-wrap{when_hidden_attr}>'
        f'<label class="expiry-when-label" for="credential_expiry_when">When</label>'
        f'<select id="credential_expiry_when" class="expiry-when-select" '
        f'name="{EXPIRY_WHEN_FIELD}" aria-label="When" data-expiry-when{when_disabled_attr}>'
        f"{options_html}</select></div>"
        '<p class="expiry-help">'
        "Off: stored until you delete it. On: stops working and is deleted automatically at the chosen time."
        "</p>"
        "</div>"
        f'<div class="expiry-custom-row" data-expiry-custom-wrap{custom_hidden_attr}>'
        '<label for="credential_expiry_custom">Delete on (your local date and time)</label>'
        f'<input id="credential_expiry_custom" name="{EXPIRY_CUSTOM_LOCAL_FIELD}" '
        f'type="datetime-local" value="{html.escape(state.custom_local, quote=True)}" '
        f'autocomplete="off" data-expiry-custom{custom_disabled_attr}{custom_required_attr}>'
        "</div>"
        f'<input type="hidden" name="{EXPIRY_TIMEZONE_FIELD}" '
        f'value="{html.escape(state.timezone_name, quote=True)}" data-expiry-timezone>'
        f'<input type="hidden" name="{EXPIRY_UTC_OFFSET_FIELD}" '
        f'value="{html.escape(state.tz_offset_minutes, quote=True)}" data-expiry-utc-offset>'
        f"{existing_warning_html}"
        "</div>"
    )


def render_credential_collection_form(
    *,
    request_payload: Mapping[str, Any],
    session_expires_at: datetime,
    field_specs: list[dict[str, Any]],
    is_secret_field: Callable[[dict[str, Any]], bool],
    signup_html: str = "",
    key_exists: bool = False,
    error_message: str | None = None,
    preserved_fields: Mapping[str, str] | None = None,
    expiry_state: ExpiryFormState | None = None,
) -> str:
    """Render the secure credential collection page (standalone or ``?view=card``)."""

    service_name = str(
        request_payload.get("service_name")
        or request_payload.get("display_name")
        or "service"
    )
    credential_key = str(request_payload.get("credential_key") or "")
    countdown_html = render_session_countdown(session_expires_at)
    preserved = preserved_fields or {}
    resolved_expiry_state = (
        expiry_state if expiry_state is not None else initial_expiry_form_state(request_payload)
    )

    inputs: list[str] = []
    for spec in field_specs:
        name = str(spec.get("name") or spec.get("field_ref") or "").strip()
        if not name:
            continue
        kind = str(spec.get("kind") or "custom").strip()
        label = str(spec.get("label") or name.replace("_", " ").title()).strip()
        value_source = str(spec.get("value_source") or "").strip().lower()
        is_secret = is_secret_field(spec)
        if is_secret and value_source == "store_generated_secret":
            inputs.append(
                '<div class="note">'
                f"<strong>{html.escape(label)}</strong> will be generated inside the credential store. "
                "The generated value is never shown to the agent."
                "</div>"
            )
            continue
        # Per ui/DESIGN.md "Errors and Results": preserve non-secret inputs on
        # re-render after an error, and never echo back secret field values.
        value = "" if is_secret else str(preserved.get(name, spec.get("value") or ""))
        raw_form_spec = spec.get("form")
        form_spec: dict[str, Any] = raw_form_spec if isinstance(raw_form_spec, dict) else {}
        input_control = (
            str(
                form_spec.get("input_control")
                or ("password" if is_secret else "text")
            )
            .strip()
            .lower()
        )
        if input_control not in {"text", "password", "textarea", "file"}:
            input_control = "password" if is_secret else "text"
        input_type = (
            "password"
            if input_control == "password" or is_secret and input_control == "text"
            else "text"
        )
        autocomplete = str(
            form_spec.get("autocomplete")
            or ("new-password" if is_secret else "username")
        ).strip()
        required = " required" if bool(spec.get("required", True)) else ""
        help_text = str(
            form_spec.get("help_text") or spec.get("description") or ""
        ).strip()
        help_html = (
            render_markdown_text(help_text, class_name="muted") if help_text else ""
        )
        if input_control == "textarea":
            inputs.append(
                "<div>"
                f'<label for="field__{html.escape(name)}">{html.escape(label)}'
                f' <span class="muted">({html.escape(kind)})</span></label>'
                f'<textarea id="field__{html.escape(name)}" name="field__{html.escape(name)}" '
                f'autocomplete="{html.escape(autocomplete)}"{required}>{html.escape(value)}</textarea>'
                f"{help_html}</div>"
            )
            continue
        if input_control == "file":
            accept = ",".join(
                str(item)
                for item in form_spec.get("accept", [])
                if str(item).strip()
            )
            accept_attr = f' accept="{html.escape(accept)}"' if accept else ""
            inputs.append(
                "<div>"
                f'<label for="field__{html.escape(name)}">{html.escape(label)}'
                f' <span class="muted">({html.escape(kind)})</span></label>'
                f'<input id="field__{html.escape(name)}" name="field__{html.escape(name)}" '
                f'type="file" autocomplete="off"{accept_attr}{required}>'
                f"{help_html}</div>"
            )
            continue
        inputs.append(
            "<div>"
            f'<label for="field__{html.escape(name)}">{html.escape(label)}'
            f' <span class="muted">({html.escape(kind)})</span></label>'
            + render_credential_input(
                input_id=f"field__{name}",
                input_type=input_type,
                autocomplete=autocomplete,
                value=value,
                placeholder=str(spec.get("placeholder") or "").strip(),
                required=bool(required),
            )
            + f"{help_html}</div>"
        )

    expiry_html = _render_expiry_control(resolved_expiry_state, key_exists=key_exists)
    error_html = (
        f'<div class="status status-error" role="alert"><strong>Nothing was saved.</strong> '
        f"{html.escape(error_message)}</div>"
        if error_message
        else ""
    )
    body = f"""
  <section class="content-panel">
    <p class="eyebrow">Secure Credential Collection</p>
    <h1>Submit credentials for {html.escape(service_name)}.</h1>
    <p class="lede broker-note">This form sends sensitive fields directly to the secure broker. Agents receive only redacted completion metadata.</p>
    {signup_html}
  </section>
  <section class="content-panel">
    <p>Credential key: <code>{html.escape(credential_key)}</code></p>
    {countdown_html}
    <form id="{CREDENTIAL_FORM_ID}" action="#{CREDENTIAL_FORM_ID}" method="post" enctype="multipart/form-data">
      {error_html}
      {"".join(inputs)}
      {expiry_html}
      <button type="submit">Store Credential</button>
    </form>
  </section>"""
    return render_broker_auth_page(
        "Secure Credential Collection",
        body,
        script=SECRET_INPUT_TOGGLE_SCRIPT + CREDENTIAL_EXPIRY_FORM_SCRIPT,
        decline=True,
    )


def parse_credential_collection_submission(
    form: Mapping[str, Any],
    *,
    request_payload: Mapping[str, Any],
    max_file_bytes: int,
    now: datetime | None = None,
) -> ParsedCredentialSubmission:
    """Extract submitted fields and validate the form's temporary-expiry controls."""

    submitted_fields: dict[str, str] = {}
    for key, value in form.items():
        if not str(key).startswith("field__"):
            continue
        field_name = str(key).removeprefix("field__")
        if isinstance(value, web.FileField):
            raw_bytes = value.file.read()
            if len(raw_bytes) > max_file_bytes:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=max_file_bytes,
                    actual_size=len(raw_bytes),
                )
            try:
                submitted_fields[field_name] = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise web.HTTPBadRequest(
                    text="Uploaded credential files must be UTF-8 text."
                ) from exc
        else:
            submitted_fields[field_name] = str(value)

    enabled = str(form.get(DELETE_AUTOMATICALLY_FIELD) or "").strip().lower() in _TRUTHY_FORM_VALUES
    when = str(form.get(EXPIRY_WHEN_FIELD) or DEFAULT_EXPIRY_WHEN).strip() or DEFAULT_EXPIRY_WHEN
    custom_local = str(form.get(EXPIRY_CUSTOM_LOCAL_FIELD) or "").strip()
    timezone_name = str(form.get(EXPIRY_TIMEZONE_FIELD) or "").strip()
    tz_offset_minutes = str(form.get(EXPIRY_UTC_OFFSET_FIELD) or "").strip()
    expiry_state = ExpiryFormState(
        enabled=enabled,
        when=when,
        custom_local=custom_local,
        timezone_name=timezone_name,
        tz_offset_minutes=tz_offset_minutes,
        proposed_expires_at="",
    )
    try:
        resolved_expires_at = resolve_form_expiry(
            enabled=enabled,
            when=when,
            custom_local=custom_local,
            tz_offset_minutes_behind_utc=tz_offset_minutes or None,
            timezone_name=timezone_name or None,
            now=now,
        )
    except CredentialExpiryError as exc:
        return ParsedCredentialSubmission(
            submitted_fields=submitted_fields,
            expiry_state=expiry_state,
            expiry_error=exc.message,
            expiry_error_code=exc.code,
        )

    if resolved_expires_at is not None:
        write_source = resolve_credential_write_source(request_payload)
        if write_source != "sealbox":
            return ParsedCredentialSubmission(
                submitted_fields=submitted_fields,
                expiry_state=expiry_state,
                expiry_error=(
                    f"Automatic deletion is only supported for Sealbox credentials, not {write_source or 'this store'}. "
                    "Untick 'Delete automatically' to save without an expiry."
                ),
                expiry_error_code="credential_expiry_unsupported_source",
            )

    return ParsedCredentialSubmission(
        submitted_fields=submitted_fields,
        expiry_state=expiry_state,
        resolved_expires_at=resolved_expires_at,
    )


__all__ = [
    "CREDENTIAL_EXPIRY_FORM_SCRIPT",
    "CREDENTIAL_FORM_ID",
    "DEFAULT_EXPIRY_WHEN",
    "DELETE_AUTOMATICALLY_FIELD",
    "EXPIRY_CUSTOM_LOCAL_FIELD",
    "EXPIRY_TIMEZONE_FIELD",
    "EXPIRY_UTC_OFFSET_FIELD",
    "EXPIRY_WHEN_FIELD",
    "ExpiryFormState",
    "ParsedCredentialSubmission",
    "credential_collection_owner_scope",
    "credential_key_exists",
    "initial_expiry_form_state",
    "normalize_proposed_expiry",
    "parse_credential_collection_submission",
    "render_credential_collection_form",
]
