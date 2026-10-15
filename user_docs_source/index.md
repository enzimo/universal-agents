# Universal Agents

These docs cover user-facing setup and operation for Universal Agents:
onboarding, Docker deployment, configuration, chat channels, multi-user
onboarding, secure tool authentication, generated sites, and
verification checks.

## Start Here

Use this order for a first deployment:

1. Follow the [Quickstart](quickstart.md) for the recommended bundled Docker install.
2. Choose and test a [Model Provider](model-providers.md), including xAI Grok OAuth when desired.
3. Skim [Agent Features](features.md) to see what the agent can do.
4. Read [Onboarding](onboarding.md) for development and automation alternatives.
5. Fill in [Configuration](configuration.md), especially `team.env` secrets.
6. Read [Access and approval](permissions.md) before giving agents access to tools and folders.
7. Configure one or more [Channels](channels/index.md).
8. Read [Multi-User Onboarding](newuser/index.md) before adding additional users.
9. Configure [Tools and Authentication](tools/index.md) for GitHub, Google, and password managers.

## What Runs

The agent runs these main processes:

- A `TeamArchitect` agent runtime.
- A gateway process for local chat and app adapters.
- Optional channel adapters for Telegram, WhatsApp, Discord, Signal, and Photon/iMessage.
- A website HTTP service for docs, skill references, and generated sites.
- An activity collector service.
- A secure tool broker, which always runs, for credentialed tool calls.

## Important Boundaries

- User secrets belong in `team.env`, Docker secrets, or broker-only env files.
- `instance_config.yaml` should contain durable non-secret runtime settings.
- Agents may request secure broker actions, but credential material stays in the broker.
- Agents work only in the person's folder, the shared `team/` and `agents/` folders, and folders you give them; shell and Python commands run in a sandbox with the same limits.
- Channel adapters share the same session model but have provider-specific setup and verification steps.

## Common Local URLs

Default browser-facing endpoints:

| Service | Default |
| --- | --- |
| Installer Web UI, Web Chat, docs, skills, and generated sites | `http://127.0.0.1:18090` on the host and inside the container |
| Raw Docker or development website host | derived from `network.public_origin` plus `interactive_apps.plugins.builtin.internal_website.settings.public_port`, defaulting to `http://127.0.0.1:18090` |
| Secure tool broker auth links | derived from `network.public_origin` plus `security_layer.broker_public_port`, commonly `http://127.0.0.1:18600` |
| Activity collector | configured by `activity_collector.base_url` or bind settings |
