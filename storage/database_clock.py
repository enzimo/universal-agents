"""Use the database clock for custody leases shared by competing runtimes."""

from datetime import datetime, timezone
from typing import Any


def database_now(connection: Any) -> datetime:
    value = connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return (
        result.replace(tzinfo=timezone.utc)
        if result.tzinfo is None
        else result.astimezone(timezone.utc)
    )
