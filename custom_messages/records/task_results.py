"""Fields a runtime response may update on an existing task."""

from pydantic import BaseModel, ConfigDict

from custom_messages.contracts.delegated_work_v1 import TaskFailureV1
from custom_messages.contracts.task_v1 import TaskHistoryEntry


class TaskResultUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str | None = None
    errors: list[str] | None = None
    execution_history: list[TaskHistoryEntry] | None = None
    failure: TaskFailureV1 | None = None
