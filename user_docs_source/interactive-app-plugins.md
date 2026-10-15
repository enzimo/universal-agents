# Interactive App Plugins

Interactive app adapters use a versioned, registry-backed package contract.
Bundled and separately installed adapters declare the same descriptor,
capabilities, settings model, dependency probe, lifecycle factory, and
onboarding metadata.

## Operator Configuration

The `plugins` map is the only supported interactive-app configuration shape.
Bundled entries omit `distribution`. A separately installed plugin must name
both its stable plugin id and its normalized Python distribution name:

```yaml
interactive_apps:
  adapter_host:
    enabled: true
    transport: unix
    host_id: python-adapter-host
    allowed_host_ids: [python-adapter-host]
    token_env_var: ADAPTER_HOST_AUTH_TOKEN
  plugins:
    example.matrix:
      distribution: "acme-ua-matrix"
      execution: adapter_host
      adapter_instance_id: instance-example-matrix
      settings:
        enabled: true
        homeserver: "https://matrix.example.com"
```

The plugin id and distribution are an exact operator allowlist. The gateway
does not import an entry point whose distribution does not match, and it never
installs packages at runtime. Installation is a separate operator action.

Inspect the effective catalog and package diagnostics with:

```bash
.venv/bin/python assistant_cli.py interactive-app-list \
  --config <instance_root>/instance_config.yaml \
  --env <instance_root>/team.env
```

`run-app --app` accepts either the registered plugin id or its declared channel
name. The bundled headless launcher starts the adapter host automatically when
it is enabled and at least one enabled channel selects `execution:
adapter_host`. For manual operation, start the gateway first and then run:

```bash
.venv/bin/python assistant_cli.py run-adapter-host \
  --config <instance_root>/instance_config.yaml \
  --env <instance_root>/team.env
```

Set `ADAPTER_HOST_AUTH_TOKEN` (or its `_FILE` form) to a random value of at
least 32 characters. `transport: unix` also verifies the peer process user and
applies the configured socket permissions. Use `transport: mtls` across
containers or hosts; configure separate listener and connection addresses,
server/client certificates, private keys, trust bundles, and a server name.
The client certificate DNS/URI identity or common name must exactly match the
claimed `host_id`.

## Package Entry Point

Packages register one zero-argument descriptor provider in the versioned
`universal_agents.chat_adapters.v1` entry-point group. The entry-point name must
equal the descriptor's `plugin_id`:

```toml
[project.entry-points."universal_agents.chat_adapters.v1"]
"example.matrix" = "acme_ua_matrix:interactive_app_descriptor"
```

Import the package-facing API only from
`assistant_gateway.interactive_apps.plugin_api`. A descriptor provides:

- a stable plugin id, channel name, API version, and implementation version;
- an explicit Pydantic settings model containing `enabled: bool`;
- declared capabilities;
- a local dependency probe that performs no external lifecycle work;
- a factory that receives only the validated plugin settings, gateway
  adapter-application protocol, normalized inbound bridge, and explicit
  bounded host ports;
- onboarding summary, setup order, secret-reference names, and documentation.

The host derives distribution name, version, and entry-point provenance from
installed package metadata. A plugin cannot self-assert or override that
provenance.

## Boundary Rules

Package plugins do not receive aggregate gateway configuration, repositories,
database handles, message-broker clients, authorization implementations, or
credential values. They translate platform events into normalized application
requests and translate application delivery events back to the platform.
Because canonical gateway identity is also unavailable outside this boundary,
an externally hosted adapter applies its configured platform user/chat
allowlists exactly; it cannot treat a linked gateway user as an implicit edge
allowlist entry.

One plugin's discovery, dependency, construction, or startup failure is
reported independently. Unrelated enabled adapters continue by default; an
explicit `run-app` selection remains strict so operator tests fail immediately.

The in-process API is intended for trusted, explicitly installed packages. It
is not a sandbox or an authorization boundary. External hosting adds process
and transport isolation, not arbitrary-code sandboxing; packages remain an
operator installation and allowlist decision.

## Delivery and Recovery Semantics

One adapter-host process may run several configured plugins. Registration and
health remain independent per plugin. Each registration negotiates capabilities,
payload and attachment sizes, queue credit, heartbeat timing, acknowledgement
deadlines, and retention.

Inbound requests are durably admitted before the gateway returns acceptance.
Outbound delivery has separate durable `received` and platform-completion
acknowledgements. Reconnect creates a new epoch and resumes from committed
cursors; stale epochs and sequence gaps fail closed. The transport is
at-least-once. A platform call with an ambiguous result is recorded as
`outcome_unknown` and is not blindly repeated.

The gateway keeps a bounded persistent outbox per adapter. At capacity it
returns observable backpressure instead of dropping an event. Health reports
lease liveness, readiness, queue depth, in-flight work, oldest pending age,
cursor progress, reconnect count, and redacted adapter errors. Terminal records
are pruned only after the configured retention window; a client older than that
floor receives an explicit resynchronization error.

The internal Website/Web Chat plugin must use `execution: in_process`. It is an
operator surface owned by the gateway rather than a channel-only adapter.
