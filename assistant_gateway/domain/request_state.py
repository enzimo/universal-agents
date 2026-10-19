"""Gateway request-coordination persistence records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RequestRecord:
    correlation_id: str
    session_key: str
    request_kind: str
    request_text: str
    conversation_id: str | None
    followup_to_task_id: str | None
    created_at: str
    status: str
    owner_canonical_user_id: str | None = None
    active_task_id: str | None = None


@dataclass(frozen=True)
class CredentialCollectionRecord:
    """A form's recoverable watch, without submitted fields, secrets or page addresses."""

    credential_request_id: str
    broker_session_id: str
    person_id: str
    session_key: str
    correlation_id: str
    conversation_id: str | None
    parent_task_id: str | None
    service_name: str
    credential_key: str
    display_name: str | None
    flow: str
    source: str | None
    source_item_id: str | None
    source_item_name: str | None
    continue_task: bool
    expires_at_utc: str


@dataclass
class ToolSetupRecord:
    """One setup page a task waits on (``assistant_gateway.application.tool_setup``).

    ``status`` is ``waiting`` until the page's result is sent to the task,
    then how it ended (``completed``, ``declined``, ``expired``,
    ``unavailable``). Finishing is a conditional update, so exactly one result
    reaches the task even when the page's watcher and the person's own
    ``/link`` finish together. The row outlives a gateway restart, so a page
    nobody watches any more is reported instead of leaving the task waiting.
    ``correlation_id`` is the chat request the task answers. Page addresses
    are never stored.
    """

    setup_request_id: str
    parent_task_id: str
    person_id: str
    tool_id: str
    kind: str
    owner_kind: str
    session_key: str
    correlation_id: str
    conversation_id: str | None
    status: str
    created_at: str
    finished_at: str | None = None


@dataclass
class PendingQueryRecord:
    session_key: str
    correlation_id: str
    task_query_id: str
    parent_task_id: str | None
    conversation_id: str | None
    query_text: str
    created_at: str
    status: str
    query_payload_json: str | None = None
    owner_canonical_user_id: str | None = None
