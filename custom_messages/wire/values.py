"""Semantic validation shared by generated protocol models and frame checks."""

from datetime import datetime, timezone
from typing import Any, Callable


def parse_timestamp(text: str) -> datetime:
    """Read a real UTC calendar time after the schema has checked its text form."""

    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def validate_timestamp(text: str) -> str:
    """Keep timestamps as wire strings while refusing impossible dates and times."""

    parse_timestamp(text)
    return text


def exact_type(*types: type) -> Callable[[Any], Any]:
    """A check that a value's type is one of ``types`` exactly: JSON's true is not 1, nor 1 true."""

    def check(value: Any) -> Any:
        if type(value) not in types:
            raise ValueError(f"must be {' or '.join(kind.__name__ for kind in types)}, not {type(value).__name__}")
        return value

    return check
