"""Which Web Chat threads the sidebar lists, and their fixed names (web_chat_threads).

Owner's report, 2026-10-09: a newly set-up Web Chat listed two empty "New
Chat" rows (the gateway's proactive and memory-review delivery threads), and
a thread's name changed with every message. These run the real store query
and the session application's listing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from assistant_gateway.application.person_maintenance import person_maintenance_session_key
from assistant_gateway.application.proactive_delivery import proactive_session_key
from assistant_gateway.application.web_chat_threads import (
    TITLE_MAX_CHARACTERS,
    WebChatThreadKind,
    thread_kind,
)
from assistant_gateway.application.website_application import web_chat_threads_revision
from assistant_gateway.application.website_services import WebsiteSessionApplication
from assistant_gateway.domain.transcript import TranscriptAppendRequest, TranscriptAttachmentReference
from assistant_gateway.runtime_ports import OwnershipPrivateResourceAuthorizer
from assistant_gateway.store import GatewayStateStore

OWNER = "user_alpha"
OTHER_OWNER = "user_beta"
HEARTBEAT = "heartbeat"
THREAD = "website:web-chat:alpha"


class _Threads:
    """The store and the session application's thread listing over it."""

    def __init__(self, tmp_path: Path) -> None:
        self.store = GatewayStateStore(str(tmp_path / "gateway.db"))
        self._messages = 0
        unused = cast(Any, SimpleNamespace())
        self.application = WebsiteSessionApplication(
            sessions=unused,
            session_repository=self.store.session_repository,
            delivery_repository=unused,
            request_context=unused,
            private_authorizer=OwnershipPrivateResourceAuthorizer(),
            manager_agent_id="manager_1",
            team_id="team_1",
            awaiting_reply_session_keys=frozenset,
            proactive_session_key=HEARTBEAT,
        )

    def session(self, session_key: str, *, owner: str = OWNER, channel: str = "website", chat: str = "web-chat") -> None:
        self.store.session_repository.create_session(
            session_key=session_key,
            conversation_id=f"conversation_{session_key}",
            manager_agent_id="manager_1",
            channel_name=channel,
            user_id=owner,
            chat_id=chat,
            owner_canonical_user_id=owner,
        )

    def message(
        self,
        session_key: str,
        content: str,
        *,
        role: str = "user",
        attachments: tuple[TranscriptAttachmentReference, ...] = (),
    ) -> None:
        self._messages += 1
        self.store.transcript_repository.append_transcript_message(
            TranscriptAppendRequest(
                message_id=f"message_{self._messages}",
                session_key=session_key,
                conversation_id=f"conversation_{session_key}",
                owner_canonical_user_id=OWNER,
                channel_name="website",
                chat_id="web-chat",
                role=role,
                content=content,
                message_type="user_message" if role == "user" else "agent_response",
                message_level="normal",
                correlation_id=None,
                source_kind="client_request" if role == "user" else "delivery",
                source_id=f"source_{self._messages}",
                idempotency_key=f"idempotency_{self._messages}",
                attachment_references=attachments,
            )
        )

    def listed(self, owner: str = OWNER) -> dict[str, str | None]:
        return {thread.session_key: thread.title for thread in self.application.list_web_chat_threads(owner_canonical_user_id=owner)}

    def revision(self) -> str:
        return web_chat_threads_revision(self.application.list_web_chat_threads(owner_canonical_user_id=OWNER))


def test_delivery_threads_stay_hidden_until_something_arrives(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    maintenance = person_maintenance_session_key(OWNER)
    # What the gateway makes at every start for the first person.
    threads.session(HEARTBEAT)
    threads.session(maintenance)
    assert threads.listed() == {}, "a newly set-up Web Chat lists no empty delivery threads"

    threads.session(THREAD)
    assert threads.listed() == {THREAD: None}, "a thread the person started is listed, unnamed until its first message"

    threads.message(HEARTBEAT, "Nothing urgent today.", role="assistant")
    threads.message(maintenance, "I tidied two memories.", role="assistant")
    assert threads.listed() == {THREAD: None, HEARTBEAT: "Updates", maintenance: "Memory review"}


def test_a_delivery_thread_keeps_its_name_even_when_the_person_typed_in_it(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    maintenance = person_maintenance_session_key(OWNER)
    threads.session(maintenance)
    threads.message(maintenance, "Hello Rachel")
    assert threads.listed() == {maintenance: "Memory review"}
    threads.store.session_repository.set_session_alias(maintenance, "Old chat")
    assert threads.listed() == {maintenance: "Old chat"}, "a rename still wins"


def test_the_first_ordinary_message_names_a_thread_and_later_ones_never_rename_it(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    threads.message(THREAD, "/status")
    assert threads.listed() == {THREAD: None}, "a slash command does not name a thread"

    threads.message(THREAD, "  Plan my   week ")
    named = threads.revision()
    assert threads.listed() == {THREAD: "Plan my week"}

    threads.message(THREAD, "Here is your week.", role="assistant")
    threads.message(THREAD, "Add a dentist visit on Friday")
    threads.message(THREAD, "Hello Rachel")
    assert threads.listed() == {THREAD: "Plan my week"}
    assert threads.revision() == named, "later messages do not make open pages reload the list"

    threads.store.session_repository.set_session_alias(THREAD, "Weekly plan")
    assert threads.listed() == {THREAD: "Weekly plan"}
    assert threads.revision() != named, "a rename does"


def test_an_attachment_only_first_message_is_named_by_its_file(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    threads.message(
        THREAD,
        "",
        attachments=(
            TranscriptAttachmentReference(
                attachment_id="attachment_1", filename="budget.xlsx", content_type="application/vnd.ms-excel", kind="file"
            ),
        ),
    )
    threads.message(THREAD, "What does this say?")
    assert threads.listed() == {THREAD: "budget.xlsx"}


def test_a_long_first_message_is_shortened(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    threads.message(THREAD, "word " * 200)
    title = threads.listed()[THREAD]
    assert title is not None and len(title) == TITLE_MAX_CHARACTERS and title.endswith("…")


def test_other_channels_and_other_people_never_crowd_out_or_join_the_list(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    # Many newer threads elsewhere: the list used to read 50 threads of any
    # channel before keeping the Web Chat ones.
    for index in range(60):
        threads.session(f"telegram:chat:{index}", channel="telegram", chat=f"chat_{index}")
    threads.session("website:web-chat:beta", owner=OTHER_OWNER)
    assert threads.listed() == {THREAD: None}
    assert threads.listed(OTHER_OWNER) == {"website:web-chat:beta": None}


def test_delivery_threads_are_recognized_by_the_keys_their_own_code_makes() -> None:
    assert thread_kind(HEARTBEAT, proactive_session_key=HEARTBEAT) is WebChatThreadKind.UPDATES
    other_owner = proactive_session_key(HEARTBEAT, owner=OTHER_OWNER, default_owner=OWNER)
    assert thread_kind(other_owner, proactive_session_key=HEARTBEAT) is WebChatThreadKind.UPDATES
    assert thread_kind(person_maintenance_session_key(OWNER), proactive_session_key=HEARTBEAT) is WebChatThreadKind.MEMORY_REVIEW
    assert thread_kind("heartbeats_and_more", proactive_session_key=HEARTBEAT) is WebChatThreadKind.CONVERSATION
    assert thread_kind(THREAD, proactive_session_key=HEARTBEAT) is WebChatThreadKind.CONVERSATION
