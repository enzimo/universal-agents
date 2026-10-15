"""Shared mechanics for staged, exact changes (``access_control.changes``).

Each change service owns its plans and effects; this package holds the digest
helpers, the file-backed plan store, atomic writes and the audit event they
share. Who may apply a change is decided by ``apply_change``, not here.
"""

from custom_agents.governance.authorized_operations import (
    FilePlanStore,
    GovernanceAuditEvent,
    GovernanceAuditSink,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    canonical_json_sha256,
    model_digest,
    sha256_bytes,
)

__all__ = [
    "FilePlanStore",
    "GovernanceAuditEvent",
    "GovernanceAuditSink",
    "atomic_write_bytes",
    "atomic_write_json",
    "canonical_json",
    "canonical_json_sha256",
    "model_digest",
    "sha256_bytes",
]
