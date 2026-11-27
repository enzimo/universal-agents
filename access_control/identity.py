"""Shared constants and exception re-export for task identity verification.

Protocol 1 authority tokens (``custom_transport.protocol1.authority_tokens``)
verified by ``access_control.authority_verifier.AuthorityTokenVerifier`` and
resolved by ``access_control.task_identity.resolve_task_identity`` replaced the
legacy 0.9 signed-context path.
"""

from __future__ import annotations

from datetime import timedelta

from access_control.authority_verifier import IdentityRejected

ISSUER_GATEWAY = "gateway"
ISSUER_SCHEDULER = "scheduler"
DEFAULT_CONTEXT_LIFETIME = timedelta(days=7)

__all__ = [
    "DEFAULT_CONTEXT_LIFETIME",
    "ISSUER_GATEWAY",
    "ISSUER_SCHEDULER",
    "IdentityRejected",
]
