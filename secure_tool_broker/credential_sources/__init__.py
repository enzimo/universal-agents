"""Credential source registry and built-in source registration."""

from __future__ import annotations

from secure_tool_broker.credential_sources.base import (
    CredentialMetadata,
    CredentialRef,
    CredentialSearchRequest,
    CredentialSecretRequest,
    CredentialSource,
    SecretFieldMetadata,
    get_credential_source,
    list_credential_sources,
    normalize_credential_source_name,
    register_credential_source,
)
from secure_tool_broker.credential_sources.bitwarden import BitwardenCredentialSource
from secure_tool_broker.credential_sources.keepass import KeepassCredentialSource
from secure_tool_broker.credential_sources.onepassword import OnePasswordCredentialSource
from secure_tool_broker.credential_sources.sealbox import SealboxCredentialSource
from secure_tool_broker.credential_sources.vault import VaultCredentialSource


register_credential_source(BitwardenCredentialSource())
register_credential_source(OnePasswordCredentialSource())
register_credential_source(KeepassCredentialSource())
register_credential_source(VaultCredentialSource())
register_credential_source(SealboxCredentialSource())

__all__ = [
    "CredentialMetadata",
    "CredentialRef",
    "CredentialSearchRequest",
    "CredentialSecretRequest",
    "CredentialSource",
    "SecretFieldMetadata",
    "get_credential_source",
    "list_credential_sources",
    "normalize_credential_source_name",
    "register_credential_source",
]
