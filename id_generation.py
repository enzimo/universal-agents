"""Shared generation for random operational identifiers."""

from __future__ import annotations

import uuid

RANDOM_ID_SUFFIX_LENGTH = 12
MAX_OPERATIONAL_ID_PREFIX_LENGTH = 15
HEARTBEAT_SOURCE_ID_PREFIX = "hb_source"
HEARTBEAT_SCHEDULE_ID_PREFIX = "hb_sched"
MEMORY_REVIEW_CORE_ID_PREFIX = "mem_rev_core"
MEMORY_REVIEW_USER_ID_PREFIX = "mem_rev_user"
MEMORY_REVIEW_STALE_ID_PREFIX = "mem_rev_stale"
INTERNAL_IMPROVEMENT_REVIEW_ID_PREFIX = "int_imp_review"
INTERNAL_IMPROVEMENT_LAUNCH_ID_PREFIX = "int_imp_launch"
INTERNAL_IMPROVEMENT_ID_PREFIX = "int_imp"
DELEGATION_WAIT_NOTIFICATION_ID_PREFIX = "notif_del_wait"
SCHEDULE_CONVERSATION_SEED_ID_PREFIX = "sched_conv_seed"
SCHEDULE_PAUSE_RECEIPT_ID_PREFIX = "sch_pause_rcpt"
KNOWLEDGE_CURATION_PERSON_ID_PREFIX = "kb_cur_person"
KNOWLEDGE_CURATION_TEAM_ID_PREFIX = "kb_cur_team"

GENERATED_OPERATIONAL_ID_PREFIXES = frozenset(
    {
        HEARTBEAT_SOURCE_ID_PREFIX,
        HEARTBEAT_SCHEDULE_ID_PREFIX,
        MEMORY_REVIEW_CORE_ID_PREFIX,
        MEMORY_REVIEW_USER_ID_PREFIX,
        MEMORY_REVIEW_STALE_ID_PREFIX,
        INTERNAL_IMPROVEMENT_REVIEW_ID_PREFIX,
        INTERNAL_IMPROVEMENT_LAUNCH_ID_PREFIX,
        INTERNAL_IMPROVEMENT_ID_PREFIX,
        DELEGATION_WAIT_NOTIFICATION_ID_PREFIX,
        SCHEDULE_CONVERSATION_SEED_ID_PREFIX,
        SCHEDULE_PAUSE_RECEIPT_ID_PREFIX,
        KNOWLEDGE_CURATION_PERSON_ID_PREFIX,
        KNOWLEDGE_CURATION_TEAM_ID_PREFIX,
    }
)


def new_id_suffix() -> str:
    """Return the standard lowercase-hex suffix for an operational identifier."""

    return uuid.uuid4().hex[:RANDOM_ID_SUFFIX_LENGTH]


def _validated_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not prefix:
        raise ValueError("identifier prefix must be a non-empty string")
    if len(prefix) > MAX_OPERATIONAL_ID_PREFIX_LENGTH:
        raise ValueError(
            "identifier prefix must be at most "
            f"{MAX_OPERATIONAL_ID_PREFIX_LENGTH} characters"
        )
    return prefix


def operational_id(prefix: str, suffix: object) -> str:
    """Build an operational ID while enforcing the shared semantic-prefix limit."""

    normalized_suffix = "" if suffix is None else str(suffix).strip()
    if not normalized_suffix:
        raise ValueError("identifier suffix must be non-empty")
    return f"{_validated_prefix(prefix)}_{normalized_suffix}"


def new_id(prefix: str) -> str:
    """Return a random operational identifier with its semantic prefix preserved."""

    return operational_id(prefix, new_id_suffix())
