"""Semantic validation shared by generated protocol models and frame checks."""

from datetime import datetime, timezone


def parse_timestamp(text: str) -> datetime:
    """Read a real UTC calendar time after the schema has checked its text form."""

    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def validate_timestamp(text: str) -> str:
    """Keep timestamps as wire strings while refusing impossible dates and times."""

    parse_timestamp(text)
    return text
