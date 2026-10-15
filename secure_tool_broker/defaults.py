"""Defaults shared by the secure tool broker server and clients."""

from __future__ import annotations

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
CREDENTIAL_TRANSFER_EXPORT_ROUTE = "/v1/transfer/credentials/export"
CREDENTIAL_TRANSFER_IMPORT_ROUTE = "/v1/transfer/credentials/import"
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
