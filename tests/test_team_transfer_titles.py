"""Readable record names in the import review (``team_transfer.titles``); display only."""

from __future__ import annotations

import pytest

from team_transfer.registry import TRANSFER_SECTIONS
from team_transfer.titles import SECTION_TITLES, record_title

ARCHIVE = {
    "people": {"usr_ana": {"person_id": "usr_ana", "display_name": "Ana Lima", "role": "owner", "status": "active"}},
    "knowledge": {
        "page/pg_1": {"table": "page", "row": {"page_id": "pg_1", "title": "Release checklist", "owner_person_id": ""}},
    },
}


@pytest.mark.parametrize(
    ("section", "raw", "expected"),
    [
        ("people", {"display_name": "Ben Ode", "role": "member", "status": "disabled"}, "Ben Ode (member, disabled)"),
        (
            "pinned_memory",
            {"document": {"fact_text": "Prefers short\nreports", "person_id": "usr_ana"}},
            "Prefers short reports · for Ana Lima",
        ),
        (
            "knowledge",
            {"table": "page", "row": {"page_id": "pg_2", "title": "Travel", "owner_person_id": "usr_ana"}},
            "Travel · Ana Lima's page",
        ),
        ("knowledge", {"table": "revision", "row": {"page_id": "pg_1", "revision": 3}}, "Revision 3 of Release checklist"),
        (
            "schedules",
            {"kind": "schedule", "record_id": "s1", "schedule": {"markdown": "```yaml\nschedule_name: Morning briefing\n```\n"}},
            "Morning briefing",
        ),
        ("schedules", {"kind": "session", "record_id": "sess_ana"}, "Chat session sess_ana"),
        (
            "access",
            {"kind": "grant", "record_id": "g1", "document": {
                "subject": {"kind": "person", "id": "usr_ana"}, "target": {"kind": "tool", "id": "cli:github"},
                "level": "use", "kind": "access"}},
            "Use access to cli:github for Ana Lima",
        ),
        (
            "broker_credentials",
            {"key": "team/model_key/https://api.openai.com/v1", "owner": {"kind": "team", "owner_id": "team_a"}},
            "team/model_key/https://api.openai.com/v1 · the team",
        ),
        ("operator_secrets", {"kind": "environment", "name": "TELEGRAM_BOT_TOKEN"}, "TELEGRAM_BOT_TOKEN"),
    ],
)
def test_records_get_names_people_recognize(section, raw, expected) -> None:
    key = raw.get("key") or "the-key"
    assert record_title(section, key, raw, ARCHIVE) == expected


def test_an_unexpected_record_shape_keeps_its_key() -> None:
    assert record_title("people", "usr_x", {"person_id": "usr_x"}, ARCHIVE) == "usr_x"
    assert record_title("schedules", "schedule/s2", {"kind": "schedule", "schedule": {"markdown": "no yaml"}}, ARCHIVE) == "schedule/s2"
    assert record_title("control", "SOUL.md", {"unit": "SOUL.md"}, ARCHIVE) == "SOUL.md"


def test_long_text_is_shortened() -> None:
    title = record_title("pinned_memory", "k", {"document": {"fact_text": "x" * 500}}, ARCHIVE)
    assert len(title) == 120 and title.endswith("…")


def test_every_section_has_a_title() -> None:
    assert {section.name for section in TRANSFER_SECTIONS} <= set(SECTION_TITLES)
