"""The gateway's memory commands as contract bodies, and replies as the views it renders.

assistant_gateway.pinned_memory_commands.RuntimePinnedMemoryCodec: times a person writes
are read in their own time zone (the contract carries UTC), `never` clears an expiry, a
list asks for at most 100 facts, a body the contract would refuse is refused before sending
in plain words, and an ok reply becomes the Settings and chat view.
"""

from __future__ import annotations

import pytest

from assistant_gateway.pinned_memory_commands import RuntimePinnedMemoryCodec
from custom_messages.contracts.control_v1 import PinnedMemoryCommandPayload
from custom_messages.wire.frame import body_model
from custom_messages.wire.sizes import INLINE_TEXT_MAX_CHARS
from pydantic import ValidationError

CODEC = RuntimePinnedMemoryCodec()


def wire(command: PinnedMemoryCommandPayload, zone: str = "Europe/Berlin") -> tuple[str, dict]:
    kind, body = CODEC.wire_command(command, timezone_name=zone)
    body_model(kind)[0].validate_python({"request_id": "cmd-1", **body})
    return kind, body


def test_times_are_read_in_the_persons_zone_and_sent_in_utc() -> None:
    kind, body = wire(CODEC.parse('add "Office closed" from=2027-01-04T09:00 expires=2027-01-05T18:00', bank="usermem"))
    assert kind == "memory.create"
    assert (body["valid_from"], body["expires"]) == ("2027-01-04T08:00:00.000Z", "2027-01-05T17:00:00.000Z")
    assert body["fact_text"] == {"type": "inline", "text": "Office closed"}


def test_never_clears_an_expiry_on_a_replace_and_a_seen_version_is_kept() -> None:
    _, body = wire(PinnedMemoryCommandPayload(bank="coremem", action="replace", memory_id=3,
                                              invalid_after_utc="never", expected_record_digest="a" * 64))
    assert body == {"bank": "coremem", "memory_id": 3, "clear_expiry": True, "seen_version": "a" * 64}


def test_a_list_asks_for_at_most_one_page_from_the_ranges_start() -> None:
    _, body = wire(CODEC.parse("list range=101-400 sort=newest", bank="coremem"))
    assert body == {"bank": "coremem", "sort": "newest", "offset": 100, "limit": 100}


@pytest.mark.parametrize("command", [
    PinnedMemoryCommandPayload(bank="usermem", action="show"),
    PinnedMemoryCommandPayload(bank="usermem", action="undo"),
])
def test_a_command_missing_its_target_is_refused_before_sending(command) -> None:
    with pytest.raises(ValueError, match="requires"):
        CODEC.wire_command(command, timezone_name=None)


def test_an_ok_reply_becomes_the_view_the_gateway_renders() -> None:
    command = CODEC.parse("list range=1-2", bank="usermem")
    fact = {"memory_id": 1, "fact_text": {"type": "inline", "text": "Likes tea."}, "valid_from": "2026-11-04T08:00:00.000Z",
            "tags": ["drinks"], "created_at": "2026-11-04T08:00:00.000Z", "updated_at": "2026-11-04T08:00:00.000Z",
            "seen_version": "b" * 64}
    result = CODEC.result(command, {"outcome": "ok", "facts": [fact], "total": 3, "next_offset": 1},
                          team_id="team_one", person_id="Ada Admin")
    assert result.scope.canonical_user_id == "Ada Admin" and result.scope.team_id is None
    # Asked for positions 1-2, got one fact (the reply had to stop early): the view names what it shows.
    assert (result.total, result.returned, result.range) == (3, 1, [1, 1])
    assert result.facts[0].record_digest == "b" * 64 and result.facts[0].canonical_user_id == "Ada Admin"
    rendered = CODEC.render(result, timezone_name="UTC")
    assert "1 of 3 active facts" in rendered and "#1: Likes tea." in rendered and "tags=drinks" in rendered


@pytest.mark.parametrize("command, says", [
    pytest.param(PinnedMemoryCommandPayload(bank="usermem", action="create", fact_text="   "),
                 "cannot be empty", id="empty-create"),
    pytest.param(PinnedMemoryCommandPayload(bank="usermem", action="create", fact_text="x" * (INLINE_TEXT_MAX_CHARS + 1)),
                 f"{INLINE_TEXT_MAX_CHARS + 1:,} characters long", id="fact-too-long"),
    pytest.param(PinnedMemoryCommandPayload(bank="usermem", action="replace", memory_id=1),
                 "Say what to change", id="replace-names-nothing"),
    pytest.param(PinnedMemoryCommandPayload(bank="usermem", action="create", fact_text="Likes tea.",
                                            tags=[f"tag{number}" for number in range(101)]),
                 "at most 100", id="too-many-tags"),
    pytest.param(PinnedMemoryCommandPayload(bank="usermem", action="create", fact_text="Likes tea.", tags=["t" * 257]),
                 "at most 256 characters", id="tag-too-long"),
])
def test_a_body_the_contract_would_refuse_is_refused_before_sending_in_plain_words(command, says) -> None:
    with pytest.raises(ValueError) as refused:
        CODEC.wire_command(command, timezone_name=None)
    assert not isinstance(refused.value, ValidationError)  # the codec's own sentence, not the model's error
    message = str(refused.value)
    assert says in message
    # Words a person knows: none of the model's own error text, codes or links.
    for machine_words in ("validation error", "pydantic", "http", "type=", "input_value", "string_too_long", "[{"):
        assert machine_words not in message.lower()
