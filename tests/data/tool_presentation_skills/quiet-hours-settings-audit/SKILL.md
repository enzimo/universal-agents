---
name: quiet-hours-settings-audit
description: Audits which quiet-hours delivery settings the team could change and names them exactly. Use when asked to review or audit the quiet-hours or delivery settings.
metadata:
  ua-tools: fn:runtime_settings
---

# Quiet-hours settings audit

Test skill for the live tool presentation comparison
(`tests/test_live_tool_presentation.py`). Its procedure needs a read-level
function from a group that loads on request and that the tool index setups do
not show at the start, so the run measures how an agent reaches a skill's
tools in each setting.

1. List the runtime settings you could stage a change for with
   `list_runtime_settings_options()`.
2. Report every setting whose path starts with `delivery.quiet_hours.`, by its
   exact path, and say how many there are.

Change nothing: do not stage or apply any setting change.
