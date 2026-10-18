"""Read the storage host's wall clock for custody leases and due-time checks."""

from datetime import datetime, timezone
from typing import Any

from storage.sql_dialect import SQLITE


def database_now(connection: Any) -> datetime:
    # SQLite runs in this process and shares its host clock. Its SQL clock
    # truncates to seconds (strftime to milliseconds), which wrongly refuses
    # microsecond run times already due. Use that same clock at full precision.
    if connection.dialect == SQLITE:
        return datetime.now(timezone.utc)
    # A PostgreSQL transaction can wait for a row lock: use the current wall
    # time rather than CURRENT_TIMESTAMP, fixed at transaction start.
    value = connection.execute("SELECT clock_timestamp()").fetchone()[0]
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return (
        result.replace(tzinfo=timezone.utc)
        if result.tzinfo is None
        else result.astimezone(timezone.utc)
    )
