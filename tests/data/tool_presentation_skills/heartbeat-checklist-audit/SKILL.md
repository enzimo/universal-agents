---
name: heartbeat-checklist-audit
description: Audits the team's heartbeat checklist and reports how many items it has and what they check. Use when asked to review or audit the heartbeat checklist.
metadata:
  ua-tools: fn:heartbeat
---

# Heartbeat checklist audit

Test skill for the live tool presentation comparison
(`tests/test_live_tool_presentation.py`). Its procedure needs a read-level
function that the tool index setups do not show at the start, so the run
measures how an agent reaches a skill's tools in each setting.

1. Read the checklist with `list_heartbeat_items()`.
2. Report the number of items. When there are none, say that the checklist is
   empty. Otherwise name what each item checks.

Change nothing: do not add, edit or remove checklist items.
