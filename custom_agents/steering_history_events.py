"""Names of the task-history entries that record what the person sent while a request ran.

The steering runtime (``custom_agents.task_steering_runtime``) and the task
manager write these entries; ``custom_agents.conversation_attachments`` reads
them to find the files a request received after it started. They live here so
the reader never imports the steering runtime (and its dependencies) and the
import graph stays acyclic.
"""

from __future__ import annotations

# ``details["event"]`` of the history entry that records guidance the person
# sent while the request ran: accepted (``details[GUIDANCE_STEERING_KEY]`` is the
# whole ``TaskSteering``, files included) and later applied to a model turn.
HISTORY_EVENT_KEY = "event"
GUIDANCE_ACCEPTED_EVENT = "task_steering_proactive_guidance_accepted"
GUIDANCE_APPLIED_EVENT = "task_steering_proactive_guidance_applied"
GUIDANCE_STEERING_KEY = "steering"

# ``details`` key of a question's answer (a ``QUERY_EVENT`` history entry) that
# lists the files the person sent with the answer, as ``UserTaskAttachment``
# dumps. Only answers that carried files have it.
QUERY_ANSWER_FILES_KEY = "answer_files"
