"""Defaults shared by the secure tool broker server and clients."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
# Sign-ins (/auth/...), credential collection and deletion forms
# (/credential-collection/...) and account link forms (/account-link/...). Each
# address holds the page's private link; the caller-token middleware lets these
# paths through and the page itself checks the link (secure_tool_broker.service).
BROKER_PAGE_PREFIXES = ("/auth/", "/credential-collection/", "/account-link/")
# The status a sign-in page's own script polls (sign_in_pages.sign_in_wait_script).
AUTH_PAGE_STATUS_ROUTE = "/auth/{provider}/{session_id}/status"

# Web Chat shows a broker page inside the chat as a card: the same address with
# ``?view=card`` (secure_tool_broker.page_view). Only the Web Chat's own address
# may frame that view, and the page is always sandboxed.
CARD_VIEW_PARAM = "view"
CARD_VIEW_VALUE = "card"
# The sandbox both for the card view's Content-Security-Policy and for the
# iframe Web Chat builds (assistant_gateway/onboarding_web/assets/broker_card.js).
# Never allow-same-origin: behind a reverse proxy the broker shares the chat's
# address, and only an opaque origin keeps the chat page from reading the form.
CARD_FRAME_SANDBOX = "allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox"


def card_view_url(url: str) -> str:
    """The card view of a broker page address: the same address with ``view=card``."""

    parts = urlsplit(url)
    query = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True) if name != CARD_VIEW_PARAM]
    query.append((CARD_VIEW_PARAM, CARD_VIEW_VALUE))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
