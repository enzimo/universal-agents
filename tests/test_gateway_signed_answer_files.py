"""A person's answer with a file, from the real gateway to the real team lead, signed and carried as on NATS.

The other answer-file tests stop at a boundary: the gateway tests run without
a signing key and record what the gateway would send
(tests/test_gateway_answer_attachments.py), and the team lead's tests sign
answers themselves (tests/test_answer_attachments_runtime.py). Here one answer
crosses both: the real ``UserGateway`` signs it with the test gateway key
(``use_gateway_key``), the envelope goes through the transport codec as NATS
delivery does, and the real team lead's ``_handle_task_query_message`` checks
the signed person against the person the request works for
(``conversation_attachments.answer_files_refusal``) before it saves the file
with the request's conversation turn. So a disagreement between what the
gateway signs and what the team lead expects fails here instead of dropping
every answer's files with only a warning. The chat's Telegram user is linked
to the person, as people's chat accounts are; an unlinked one is signed for
nobody, and its answer's files are dropped like any unverified sender's.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from custom_messages.contracts.task_v1 import TaskQuery
from custom_messages.enums import MessageMetaType
from custom_messages.transport_codec import decode_envelope, encode_envelope
from tests.broker_access_support import forget_task_context_keys, use_gateway_key
from tests.knowledge_support import BACKENDS, knowledge_storage
from tests.test_answer_attachments_runtime import OTHER_PERSON, PERSON, ROOT, TEAM, _Lead
from tests.test_gateway_answer_attachments import _person_sends, _stored_file
from tests.test_gateway_question_queue import TELEGRAM_ID, _Chat


@pytest.fixture(autouse=True)
def _gateway_key(monkeypatch: pytest.MonkeyPatch):
    """The gateway signs with the test key (read when it starts) and the team lead trusts it."""

    use_gateway_key(monkeypatch)
    yield
    forget_task_context_keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("request_person", "kept"),
    [
        (PERSON, True),  # the request works for the person the gateway signs for
        (OTHER_PERSON, False),  # it works for someone else: the file is not the person's
    ],
)
async def test_a_gateway_signed_answer_with_a_file_is_saved_with_the_requests_turn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, backend: str, request_person: str, kept: bool
) -> None:
    (tmp_path / "lead").mkdir()
    (tmp_path / "gateway").mkdir()
    with knowledge_storage(backend, tmp_path / "lead") as storage:
        lead = _Lead(storage, tmp_path / "lead", person=request_person)
        chat = _Chat(tmp_path / "gateway", team_id=TEAM)
        # Alice is a member of the team and her Telegram account is linked to her.
        chat.store.create_gateway_user(canonical_user_id=PERSON, display_name="Alice", timezone="UTC")
        chat.store.create_team_membership(canonical_user_id=PERSON, team_id=TEAM, role="member")
        chat.store.create_channel_binding(canonical_user_id=PERSON, channel_name="telegram", external_user_id=TELEGRAM_ID)
        chat.finish_each_answered_task()

        # The team lead asks on its request; the gateway shows the question in the person's chat.
        question = lead.ask()
        chat.record_request(f"corr_{ROOT}", "Plan the shopping")
        await chat.lead_sends(f"corr_{ROOT}", question)
        receipt = _stored_file(chat, "att_answer", "receipt-march.jpg")
        await _person_sends(chat, "This one.", [receipt])

        [sent] = [
            envelope
            for envelope in chat.lead.durable
            if isinstance(envelope.msg_content, TaskQuery) and envelope.msg_content.query_type == MessageMetaType.RESPONSE
        ]
        # Signed by the gateway, for the person its chat account is linked to.
        signed = sent.msg_meta.gateway_context.task_context
        assert signed is not None and signed.context.person_id == PERSON
        delivered = decode_envelope(encode_envelope(sent))
        assert delivered.msg_content.attachments == [receipt]

        with caplog.at_level(logging.WARNING):
            await lead.agent._handle_task_query_message(
                task_query_msg=delivered.msg_content,
                incoming_msg_header=delivered.msg_header,
                incoming_msg_meta=delivered.msg_meta,
                from_runtime_mailbox=True,
            )

        [recorded] = lead.answers_recorded()
        assert (recorded.task_query_id, recorded.message) == (question.task_query_id, "This one.")
        if kept:
            assert lead.turn_file_ids() == ["att_receipt", "att_answer"]
            assert lead.answer_history_files() == [["att_answer"]]
            assert "event=answer_files_refused" not in caplog.text
        else:
            assert lead.turn_file_ids() == ["att_receipt"]
            assert all(files == [] for files in lead.answer_history_files())
            assert "event=answer_files_refused" in caplog.text and "reason=other_person" in caplog.text
