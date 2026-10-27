"""What the gateway deletes of a person's chat history, and what it deleted.

A person's history clean-up (``custom_agents.history_cleanup``) is an order
the manager records; the gateway applies the part in its own database
(``assistant_gateway.history_cleanup``) through ``delete_history_records``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConversationCut:
    """One conversation's chat records to delete: those from before ``until``.

    ``keep_correlation_id``: the first kept turn's request; the cut ends
    before its earliest record. ``whole``: the conversation is gone in the
    manager too, so sessions that point at its last deleted task
    (``latest_task_id``) stop doing so, and an emptied Web Chat thread is
    removed.
    """

    conversation_id: str
    until: str
    whole: bool
    latest_task_id: str | None = None
    # The first kept turn's request: its records stay even when they are
    # older than ``until`` (the gateway writes the question first).
    keep_correlation_id: str | None = None


@dataclass(frozen=True)
class GatewayHistoryScope:
    """The chat records of one person a clean-up deletes (all timestamps ISO UTC).

    - ``conversations``: each named or selected conversation, up to its own time.
    - ``older_than``: with no named conversations, also every other chat
      record of the person from before this time (Updates and Memory review
      threads included), except in ``kept_conversation_ids``.
    - ``selected_at``: nothing written after the plan was staged is deleted.
    """

    person_id: str
    selected_at: str
    conversations: tuple[ConversationCut, ...] = ()
    older_than: str | None = None
    kept_conversation_ids: tuple[str, ...] = ()


@dataclass
class GatewayHistoryDeleted:
    """What ``delete_history_records`` deleted; ``attachment_paths`` are upload files for the caller to delete."""

    transcript_messages: int = 0
    transcript_chars: int = 0
    requests: int = 0
    deliveries: int = 0
    questions: int = 0
    attachments: int = 0
    sessions_removed: int = 0
    sessions_restarted: int = 0
    attachment_paths: tuple[str, ...] = field(default_factory=tuple)

    def counts(self) -> dict[str, int]:
        return {
            "transcript_messages": self.transcript_messages,
            "transcript_chars": self.transcript_chars,
            "requests": self.requests,
            "deliveries": self.deliveries,
            "questions": self.questions,
            "attachments": self.attachments,
            "sessions_removed": self.sessions_removed,
            "sessions_restarted": self.sessions_restarted,
        }


__all__ = ["ConversationCut", "GatewayHistoryDeleted", "GatewayHistoryScope"]
