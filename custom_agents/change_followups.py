"""Report a failed follow-up without denying a change already saved.

Only use this after the owning store has confirmed the change. A history,
notification or inspection failure must not invite the model to repeat a
successful create. Each follow-up runs separately so one failure does not
skip the remaining work, such as waking the scheduler.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from logging import Logger
from typing import Any

from strands.interrupt import InterruptException


@contextmanager
def after_saved_change(
    payload: dict[str, Any], *, record_id: str, step: str, logger: Logger
) -> Iterator[None]:
    """Keep the saved result, add an actionable warning, and log the failed step."""

    try:
        yield
    except InterruptException:
        raise
    except Exception:
        logger.exception(
            "A change was saved but its follow-up failed. event=change_followup_failed record_id=%s step=%s",
            record_id,
            step,
        )
        payload.setdefault("warnings", []).append(
            f"The change to {record_id} was saved, but {step} failed. Do not repeat the change; "
            "inspect the saved record before taking further action."
        )
