"""The access service: grants, decisions, and verified identity.

Shared by the agent runtime, the gateway, and the secure broker, so it imports
only contracts, the tool catalog, runtime settings and storage, never
``custom_agents``. Modules:

- ``cedar``: client for the Rust evaluator (``ua-cedar-authorizer serve-access``);
- ``entities``: principal and target entities built by trusted code;
- ``store``: the grant store (SQLite or Postgres) and the access event log;
- ``decisions``: materializes valid grants, runs the agent and person checks,
  consumes one-time grants, and creates hand-downs;
- ``identity``: signs and verifies task contexts.
"""
