"""Each person's stored data in bytes, as the repositories that keep it count it.

The records are written through the real repositories into real databases:
SQLite always, and PostgreSQL when ``LIVE_TEST_POSTGRES_DSN`` is set
(``tests/knowledge_support.knowledge_storage`` gives each test its own
schema). Texts carry characters that take several bytes in UTF-8, because
SQLite's ``LENGTH`` counts characters and would under-count them.

Covered: chat text by owner (transcripts, requests, replies; records without
an owner left out); uploads counted only while kept in the uploads folder;
conversation turns, summaries and completed tasks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant_gateway.domain.transcript import TranscriptAppendRequest
from assistant_gateway.store import GatewayStateStore
from storage.conversation_repository import create_conversation_repository
from storage.task_archive_repository import create_task_archive_repository
from tests.knowledge_support import BACKENDS, knowledge_storage

ALICE = "usr_alice"
BOB = "usr_bob"
# 11 characters, 26 bytes in UTF-8.
WIDE = "Grüße 日本語 ✓"


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _session(store: GatewayStateStore, owner: str) -> str:
    session_key = f"website:web-chat:{owner}"
    store.session_repository.create_session(
        session_key=session_key,
        conversation_id=f"conversation_{owner}",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=owner,
        chat_id="web-chat",
        owner_canonical_user_id=owner,
    )
    return session_key


def _transcript(store: GatewayStateStore, session_key: str, owner: str, message_id: str, content: str) -> None:
    store.transcript_repository.append_transcript_message(
        TranscriptAppendRequest(
            message_id=message_id,
            session_key=session_key,
            conversation_id=f"conversation_{owner}",
            owner_canonical_user_id=owner,
            channel_name="website",
            chat_id="web-chat",
            role="user",
            content=content,
            message_type="user_message",
            message_level="normal",
            correlation_id=f"corr_{message_id}",
            source_kind="client_request",
            source_id=f"source_{message_id}",
            idempotency_key=f"idempotency_{message_id}",
        )
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_chat_text_is_counted_in_bytes_for_each_owner(backend: str, tmp_path: Path) -> None:
    with knowledge_storage(backend, tmp_path) as settings:
        store = GatewayStateStore("", storage_settings=settings)
        alice, bob = _session(store, ALICE), _session(store, BOB)
        _transcript(store, alice, ALICE, "m1", WIDE)
        _transcript(store, alice, ALICE, "m2", "plain")
        _transcript(store, bob, BOB, "m3", "hi")
        store.request_repository.record_request(
            correlation_id="corr_a", session_key=alice, request_kind="chat", request_text=WIDE * 2,
            conversation_id=None, followup_to_task_id=None, owner_canonical_user_id=ALICE,
        )
        # A request without an owner (its session is gone, so none is filled in) belongs to nobody.
        store.request_repository.record_request(
            correlation_id="corr_none", session_key="website:web-chat:gone", request_kind="chat", request_text="x" * 500,
            conversation_id=None, followup_to_task_id=None,
        )
        payload = {"text": WIDE}
        store.delivery_repository.add_deferred_delivery(
            session_key=alice, origin_kind="notification", payload=payload, rendered_text=WIDE,
            available_after="2026-10-27T00:00:00+00:00", owner_canonical_user_id=ALICE,
        )

        counted = store.chat_text_bytes_by_owner()

    delivery = _bytes(WIDE) + _bytes(json.dumps(payload, sort_keys=True))
    assert counted == {
        ALICE: _bytes(WIDE) + _bytes("plain") + 2 * _bytes(WIDE) + delivery,
        BOB: _bytes("hi"),
    }
    assert counted[ALICE] > len(WIDE) * 4 + len("plain")  # bytes, not characters


@pytest.mark.parametrize("backend", BACKENDS)
def test_only_uploads_still_in_the_uploads_folder_count(backend: str, tmp_path: Path) -> None:
    uploads = tmp_path / "instance" / "state" / "uploads"
    with knowledge_storage(backend, tmp_path) as settings:
        store = GatewayStateStore("", storage_settings=settings)
        for attachment_id, owner, path, size in (
            ("att_1", ALICE, uploads / "web" / "a.pdf", 1200),
            ("att_2", ALICE, uploads / "web" / "b.png", 300),
            ("att_3", BOB, uploads / "web" / "c.txt", 40),
            # A copy of a file sent to Alice lives in her folder and counts there.
            ("att_4", ALICE, tmp_path / "instance" / "workspaces" / "users" / ALICE / "scratch" / "sent" / "d.txt", 999),
            # A sibling folder whose name starts like the uploads folder.
            ("att_5", ALICE, tmp_path / "instance" / "state" / "uploads_old" / "e.txt", 777),
        ):
            store.delivery_repository.upsert_attachment(
                attachment_id=attachment_id, session_key="website:web-chat:a", conversation_id=None,
                correlation_id=None, filename=path.name, content_type="text/plain", kind="document",
                size_bytes=size, sha256="0" * 64, local_path=str(path), owner_canonical_user_id=owner,
            )

        counted = store.upload_bytes_by_owner(uploads_dir=str(uploads))

    assert counted == {ALICE: 1500, BOB: 40}


@pytest.mark.parametrize("backend", BACKENDS)
def test_conversation_turns_summaries_and_completed_tasks_count_by_owner(backend: str, tmp_path: Path) -> None:
    with knowledge_storage(backend, tmp_path) as settings:
        conversations = create_conversation_repository(settings, artifacts_dir=tmp_path / "artifacts")
        tasks = create_task_archive_repository(settings)
        conversations.ensure_thread(
            conversation_id="conv_a", team_id="team", owner_agent_id="lead", owner_agent_name="Nova",
            requester_name=None, requester_id=None, root_task_id="task_a", owner_canonical_user_id=ALICE,
        )
        conversations.append_turn(
            conversation_id="conv_a", task_id="task_a", followup_to_task_id=None, owner_agent_id="lead",
            requester_name=None, requester_id=None, correlation_id=None, problem_text="question",
            owner_canonical_user_id=ALICE,
        )
        before = conversations.stored_text_bytes_by_owner()
        conversations.update_turn_completion(
            task_id="task_a", task_status="completed", final_answer_text=WIDE * 3, turn_summary_text=WIDE,
        )
        after = conversations.stored_text_bytes_by_owner()
        tasks.save_completed_task(
            archive_id="arch_a", task_id="task_a", owner_agent_id="lead", owner_agent_name="Nova", team_id="team",
            task_json=json.dumps({"answer": WIDE}, ensure_ascii=False), status="completed",
            owner_canonical_user_id=ALICE,
        )
        tasks.save_completed_task(
            archive_id="arch_none", task_id="task_none", owner_agent_id="lead", owner_agent_name="Nova",
            team_id="team", task_json="{}" * 100, status="completed",
        )
        completed = tasks.stored_text_bytes_by_owner()
        owners = conversations.conversation_owners()

    assert set(before) == {ALICE} and before[ALICE] > _bytes("question")
    assert after[ALICE] - before[ALICE] == 4 * _bytes(WIDE)
    assert completed == {ALICE: _bytes(json.dumps({"answer": WIDE}, ensure_ascii=False))}
    assert owners == {"conv_a": ALICE}
