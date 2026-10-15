"""The tool catalog: what every callable operation is and which level it needs.

This package is shared by the agent runtime and the secure broker, so it
imports only contracts (``custom_messages``) and the standard library; the
broker must never import ``custom_agents``. Definitions live in
``ua_definitions/access``. See ``access_catalog.catalog`` for loading and
resolution, ``access_catalog.cli_commands`` for command-line classification,
and ``access_catalog.http_routes`` for web API classification.
"""
