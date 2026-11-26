"""Defaults shared by the secure tool broker server and clients."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

LOGGER = logging.getLogger(__name__)

DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_BIND_PORT = 18600
DEFAULT_INTERNAL_HOST = "127.0.0.1"
DEFAULT_INTERNAL_BASE_URL = f"http://{DEFAULT_INTERNAL_HOST}:{DEFAULT_BIND_PORT}"
DEFAULT_CREDENTIALS_DIR_NAME = "broker_credentials"
DEFAULT_CREDENTIAL_STATE_MODE = "credential_source"
DEFAULT_CREDENTIAL_STATE_SOURCE = "sealbox"
DEFAULT_CREDENTIAL_STATE_NAMESPACE = "team-cred-store"
DEFAULT_CREDENTIAL_STATE_FALLBACK_TO_FILES = False
DEFAULT_SEALBOX_ENABLED = True
DEFAULT_SEALBOX_MANAGE_SERVICE = True
DEFAULT_SEALBOX_AUTO_INITIALIZE = True
DEFAULT_SEALBOX_BIND_HOST = "127.0.0.1"
DEFAULT_SEALBOX_BIND_PORT = 8767
DEFAULT_SEALBOX_STORE_PATH = "dbs/sealbox.db"
DEFAULT_SEALBOX_SERVICE_URL: str | None = None
DEFAULT_SEALBOX_ROOT_TOKEN_FILE = "sealbox/root_token"
DEFAULT_SEALBOX_TENANT_MATERIAL_DIR = "sealbox/tenants"
DEFAULT_SEALBOX_API_VERSION = "v2"
DEFAULT_SEALBOX_READY_TIMEOUT_SECONDS = 30
# Default User-Agent for brokered web API requests (credential.api_request) when
# the request sets none: what Helium on Linux sends (Chrome's reduced User-Agent;
# Helium adds no token of its own).
DEFAULT_API_REQUEST_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)


def resolve_secure_tool_broker_internal_base_url(
    configured_internal_base_url: str | None,
) -> str:
    """Where agents and the gateway reach the broker: the configured address, else the local default.

    The broker always runs, so there is always an address.
    """

    return str(configured_internal_base_url or "").strip() or DEFAULT_INTERNAL_BASE_URL


# Gateway-only route that deletes a removed person's credentials
# (secure_tool_broker.departed_person_credentials).
PERSON_CREDENTIAL_DELETION_ROUTE = "/v1/people/credentials/delete"
# Gateway-only routes for immediate revocation of broker-held SSH sessions and targets
# (secure_tool_broker.execution_ssh).
EXECUTION_SSH_ROUTE_PREFIX = "/v1/execution-ssh/"
EXECUTION_SSH_REVOKE_SESSION_ROUTE = f"{EXECUTION_SSH_ROUTE_PREFIX}sessions/{{session_id}}/revoke"
EXECUTION_SSH_REVOKE_TARGET_ROUTE = f"{EXECUTION_SSH_ROUTE_PREFIX}targets/{{target_id}}/revoke"
# Team transfer (secure_tool_broker.credential_transfer): the operator caller only.
TRANSFER_ROUTE_PREFIX = "/v1/transfer/"
CREDENTIAL_TRANSFER_EXPORT_ROUTE = f"{TRANSFER_ROUTE_PREFIX}credentials/export"
CREDENTIAL_TRANSFER_IMPORT_ROUTE = f"{TRANSFER_ROUTE_PREFIX}credentials/import"
# /upgrade's export (secure_tool_broker.team_export_page, team_transfer.requested_export):
# the export process collects the owner's passphrase and reports how the export
# ended; the supervisor reports an export process that ended without reporting.
TEAM_EXPORT_PASSPHRASE_ROUTE = f"{TRANSFER_ROUTE_PREFIX}export/passphrase"
TEAM_EXPORT_RESULT_ROUTE = f"{TRANSFER_ROUTE_PREFIX}export/result"
TEAM_EXPORT_ENDED_ROUTE = f"{TRANSFER_ROUTE_PREFIX}export/ended"
# Gateway-only route that links the keys typed during onboarding as the team's
# accounts (secure_tool_broker.onboarding_account_keys); under /v1/account-link/,
# which only the gateway may call.
ONBOARDING_ACCOUNT_KEYS_ROUTE = "/v1/account-link/onboarding-keys"
# Model keys (secure_tool_broker.model_keys): lookup and report are for the
# processes that make model calls; status is gateway-only, under /v1/auth/,
# and never holds a key.
MODEL_KEY_ROUTE_PREFIX = "/v1/model-keys/"
MODEL_KEY_LOOKUP_ROUTE = f"{MODEL_KEY_ROUTE_PREFIX}lookup"
MODEL_KEY_REPORT_ROUTE = f"{MODEL_KEY_ROUTE_PREFIX}report"
# The team's saved endpoint addresses (never keys), for TeamLead's configuration view.
MODEL_KEY_ENDPOINTS_ROUTE = f"{MODEL_KEY_ROUTE_PREFIX}endpoints"
MODEL_KEY_STATUS_ROUTE = "/v1/auth/model-keys/status"

# -- Broker pages people open in a browser ------------------------------------------
#
# Which paths are pages, the card view and the sign-in statuses are shared with
# the gateway in secure_tools.broker_pages.
# The status a sign-in page's own script polls (sign_in_pages.sign_in_wait_script).
AUTH_PAGE_STATUS_ROUTE = "/auth/{provider}/{session_id}/status"
# The hidden field of a page's Decline form (auth_pages.render_broker_auth_page).
# Decline is its own small form, so no value typed into the page travels with it.
DECLINE_FIELD = "ua_decline"
DECLINE_VALUE = "yes"


def is_decline(form: Mapping[str, object]) -> bool:
    """Whether a submitted page form is its Decline button."""

    return str(form.get(DECLINE_FIELD) or "").strip().lower() == DECLINE_VALUE


ENV_CREDENTIAL_MAX_FILE_BYTES = "SECURE_TOOL_BROKER_CREDENTIAL_COLLECTION_MAX_FILE_BYTES"
DEFAULT_CREDENTIAL_MAX_FILE_BYTES = 262_144
MAX_CREDENTIAL_MAX_FILE_BYTES = 1_048_576
ENV_NOTIFY_ON_AGENT_CAPTURE = "SECURE_TOOL_BROKER_NOTIFY_ON_AGENT_CAPTURE"
DEFAULT_NOTIFY_ON_AGENT_CAPTURE = True
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSY_ENV_VALUES = frozenset({"0", "false", "no", "off"})


def credential_max_file_bytes() -> int:
    """Maximum credential file size in bytes, shared by the secure form and agent file capture."""

    raw = str(os.getenv(ENV_CREDENTIAL_MAX_FILE_BYTES) or "").strip()
    if not raw:
        return DEFAULT_CREDENTIAL_MAX_FILE_BYTES
    try:
        return max(1, min(int(raw), MAX_CREDENTIAL_MAX_FILE_BYTES))
    except ValueError:
        return DEFAULT_CREDENTIAL_MAX_FILE_BYTES


def notify_on_agent_capture_enabled() -> bool:
    """Whether the broker sends an Inbox notice after credential.capture_file stores a credential."""

    raw = os.getenv(ENV_NOTIFY_ON_AGENT_CAPTURE)
    if raw is None or not raw.strip():
        return DEFAULT_NOTIFY_ON_AGENT_CAPTURE
    lowered = raw.strip().lower()
    if lowered in _TRUTHY_ENV_VALUES:
        return True
    if lowered in _FALSY_ENV_VALUES:
        return False
    LOGGER.warning(
        "Ignoring unrecognized boolean value %r for %s; using default %s.",
        raw,
        ENV_NOTIFY_ON_AGENT_CAPTURE,
        DEFAULT_NOTIFY_ON_AGENT_CAPTURE,
    )
    return DEFAULT_NOTIFY_ON_AGENT_CAPTURE
