"""Trusted remediation text; never forward secret-source messages to agents."""

from typing import Any

CREDENTIAL_NOT_FOUND = "credential_not_found"
CREDENTIAL_FIELD_MISSING = "credential_field_missing"
CREDENTIAL_SOURCES_UNCONFIGURED = "credential_sources_unconfigured"
CREDENTIAL_SOURCE_FAILED = "credential_source_failed"
CREDENTIAL_VALUE_UNAVAILABLE = "credential_value_unavailable"
CREDENTIAL_FIELD_NOT_SECRET = "credential_field_not_secret"
CREDENTIAL_UNAVAILABLE = "credential_unavailable"

# Codes describe observations, not guesses about upstream authentication.
_REMEDIATION = {
    CREDENTIAL_NOT_FOUND: (
        "human",
        "The selected credential is absent from the current owner catalog. Ask the owner to select an existing credential in the secure workflow; do not guess another catalog key.",
    ),
    CREDENTIAL_FIELD_MISSING: (
        "human",
        "The selected credential lacks the field required by this tool. Ask the owner to select or create a compatible credential in the secure workflow.",
    ),
    CREDENTIAL_SOURCES_UNCONFIGURED: (
        "team_lead",
        "The required field has no secret source configured. Have TeamLead coordinate correction of the credential metadata through the authorized credential workflow.",
    ),
    CREDENTIAL_SOURCE_FAILED: (
        "team_lead",
        "A configured secret source could not be read and no source supplied a value. Have TeamLead investigate broker secret-source configuration and availability, involving the operator if needed. This does not establish that the external service rejected the credential.",
    ),
    CREDENTIAL_VALUE_UNAVAILABLE: (
        "team_lead",
        "Configured sources returned no usable value. Have TeamLead check the field references and broker environment or mounts; ask the owner to supply missing material only through the secure workflow.",
    ),
    CREDENTIAL_FIELD_NOT_SECRET: (
        "human",
        "The required field is stored as non-secret metadata. Ask the owner to configure it as a secret through the secure workflow; never copy its value into chat.",
    ),
    CREDENTIAL_UNAVAILABLE: (
        "team_lead",
        "Credential resolution failed without a recognized diagnosis. Have TeamLead obtain broker diagnostics; do not repeat approval, consent, or restart requests without identifying the cause.",
    ),
}


def credential_resolution_failure(code: str, *, required_field: str) -> dict[str, Any]:
    """Only allow trusted codes and provider-owned field names across the boundary."""
    safe_code = code if code in _REMEDIATION else CREDENTIAL_UNAVAILABLE
    owner, next_action = _REMEDIATION[safe_code]
    return {
        "error_code": safe_code,
        "failure_stage": "credential_resolution",
        "required_field": required_field,
        "provider_operation_attempted": False,
        "retry_safe": False,
        "action_owner": owner,
        "next_action": next_action,
    }
