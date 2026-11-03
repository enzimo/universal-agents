"""The team services: the team's operations, placement and agent sessions, in a process with no model.

The message contract gives a team one set of services that every host asks
(``svc.team``): they place agents and sign their sessions, answer the team's
operations, and, as the scheduler, sign scheduled runs (plan decisions 44 and 58).
Exactly one instance serves a team at a time (``lock``). Today's operations still run
in the team lead's process; they move here one step at a time.
"""
