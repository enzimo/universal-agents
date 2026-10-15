"""Every agent's own execution plan (catalog ``fn:execution_plan``, standard for all agents).

The functions live with the objective tools, which share their plan storage;
this provider groups the ones every agent uses for its own task, apart from
the team lead's objectives (``fn:objectives``).
"""

from custom_tools.provider_contract import ToolProvider
from custom_tools.providers.objectives import (
    record_plan_action,
    set_execution_plan,
    update_execution_plan_status,
    update_execution_plan_step,
    update_execution_unknown,
)

PROVIDER = ToolProvider(
    name="execution_plan",
    tools=(
        set_execution_plan,
        update_execution_plan_status,
        update_execution_plan_step,
        update_execution_unknown,
        record_plan_action,
    ),
)
