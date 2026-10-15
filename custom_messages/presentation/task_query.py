"""task query contracts, independent of runtime hosts and SDKs."""

from custom_messages.contracts.task_v1 import TaskQuery


def render_task_query_text(query: TaskQuery) -> str:
    """Render a TaskQuery into a text fallback, including first-class choices."""

    lines = [str(query.message or "").strip()]
    if query.choices:
        lines.append("")
        if query.choice_mode == "multiple":
            lines.append("Choose one or more options:")
        else:
            lines.append("Choose one option:")
        for index, option in enumerate(query.choices, start=1):
            marker = f"{index}. "
            label = option.label.strip() or option.option_id
            suffix = " (recommended)" if option.recommended else ""
            line = f"{marker}{label}{suffix} [{option.option_id}]"
            if option.description:
                line += f" - {option.description.strip()}"
            lines.append(line)
        if query.choice_mode == "multiple":
            lines.append("Reply with option ids or numbers separated by commas.")
        elif not query.allow_free_text:
            lines.append("Reply with one option id or number.")
    return "\n".join(line for line in lines if line is not None).strip()
