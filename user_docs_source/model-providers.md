# Model Providers

Choose the model provider on the **Runtime Basics** onboarding page. Test the
connection before continuing.

| Onboarding choice | Saved provider | Authentication |
| --- | --- | --- |
| OpenAI | `openai` | its key, saved with `/auth model` |
| OpenAI Codex OAuth | `openai-codex` | ChatGPT/Codex OAuth store or an ephemeral Codex access-token file |
| xAI Grok OAuth | `xai-oauth` | xAI device OAuth store or an ephemeral xAI OAuth access-token file |
| Anthropic | `anthropic` | its key, saved with `/auth model` |
| Google AI | `gemini` | its key, saved with `/auth model` |
| xAI (API key) | `xai` | its key, saved with `/auth model` (also used for Grok voice) |
| OpenRouter | `litellm` | the OpenRouter API endpoint plus its key, saved with `/auth model` |
| OpenAI Compatible | `litellm` | its API base URL plus its key, saved with `/auth model` |

Fresh onboarding selects **OpenRouter**, uses
`https://openrouter.ai/api/v1`, and pre-fills DeepSeek V4.1 Flash as
`deepseek/deepseek-v4.1-flash`. Imported configs and explicit operator choices
still take precedence. Always confirm the provider, endpoint, and model shown
on the Review page.

## Model Profile Controls

The selected provider and model are saved with their model-specific controls:

- `reasoning_effort` is an editable provider-specific hint. Common values are
  `low`, `medium`, `high`, and `max`, but the selected provider decides which
  values it accepts.
- `context_window_tokens` controls prompt budgeting and compaction and must be
  at least 1024 when set.
- `supports_image_input`, `supports_audio_input`, and
  `supports_file_input` are independent flags. File support refers to native
  document input (including supported text and Markdown files), not access to
  ordinary file tools.

Live changes use the same complete profile in `control/LLM_OVERRIDES.yaml`.
A provider/model override change clears omitted model-specific fields to unknown or the
provider default, avoiding accidental reuse of another model's settings. The
LiteLLM API mode (`litellm_api_mode`) describes the route to the endpoint rather
than one model, so a model-only switch keeps it; only a provider change resets
it. Set it explicitly when the new model needs a different mode. Audio
support is recorded as capability metadata; the general runtime does not
automatically create a native-audio bridge for every provider.

When you ask TeamLead to switch an agent's model with access control on, the
change applies at once if you are an administrator who gave TeamLead model
choice (the Trusted profile does); otherwise an administrator approves it in
the Inbox. The API mode is not part of model choice, because it changes the
endpoint route. See [Permissions](permissions.md).

Before a switch, TeamLead looks the model up: first on your own LiteLLM
server (`GET <litellm_api_base>/model/info`, with the same key the agents
already use for model requests), then in OpenRouter's model directory and
LiteLLM's built-in model data. It takes the context window and the image,
audio and file input from that lookup and asks you only when the sources
disagree or say nothing. An agent or workspace-default override switch resets
these settings. When the runtime cannot identify a model's window and none is
set, it assumes 128,000 tokens and
the runtime logs a warning (`event=context_window_fallback`); a window that is
too small makes the runtime hide tool results early. The server's key stays
in the request header and never appears in what the agent reads. To give your server's answer, set `max_input_tokens`,
`supports_vision`, `supports_pdf_input`, `supports_audio_input` and
`supports_reasoning` in each model's `model_info` in the LiteLLM
configuration.

TeamLead then tries the new model once with the settings it would use (a
short test call) before it asks you anything. If the model answers, TeamLead
tells you it works and asks you to confirm. Your confirmation applies it at
once with access control off, or with access control on when you gave TeamLead
model choice; otherwise an administrator approves it in the Inbox. If the test fails, nothing is staged or
changed, and TeamLead tells you the error. The model is tested again right
before the settings are saved.

The same tests run for instance-baseline changes (`llm.*`), using the baseline's
own endpoint and request settings. Baseline fields you omit keep their configured
values, so include the new model's window and input capabilities explicitly.
Baseline changes take effect after the reported restart.

After a model change applies, TeamLead shows a table of every model setting
that agent type now runs with: provider, model, reasoning effort and summary,
request API, context window, and image, audio and file input. For an OpenAI
reasoning model (`gpt-5` and later, `o`-series) reached through LiteLLM's Chat
Completions API, it also suggests the Responses API
(`litellm_api_mode: responses`) with `reasoning_summary: auto`, which lets you
read the model's reasoning summaries. That suggestion changes nothing, and
changing the request API needs an administrator's approval.

## Model Call Time Limits

Three `llm` settings stop a model call that hangs, so a task does not stay
"in progress" forever:

- `request_timeout_seconds` (default 120) is the HTTP timeout: the longest
  wait to connect, or for the next bytes of a response.
- `stream_idle_timeout_seconds` (default 300) is the longest wait for the next
  piece of model output: text, a reasoning summary, a tool call, or the end of
  the answer. Keep-alive pings that some proxies (such as a LiteLLM proxy) send
  on an idle stream do not count, so they no longer keep a stalled call open.
- `model_call_timeout_seconds` (default 1800) is the longest one model call may
  take in total.

When a limit is reached, the runtime closes the call and logs
`event=model_stream_timeout` with the limit that was reached. If model failover
is set up, the task continues on the next fallback model; otherwise the task
ends and the person sees "The model service timed out before completing this
request, and no fallback model was available. Please try again." Reasoning
models can think for minutes without sending anything. If a model with high
reasoning effort is stopped while it is still working, raise
`stream_idle_timeout_seconds`, for all agents or for one agent type under
`agent_types.<type>.llm`.

## xAI Grok OAuth

xAI is supported directly. Select **xAI Grok OAuth**, click **Connect xAI**,
approve the device flow, and run **Test Connection**. If no model is supplied,
the provider uses `grok-4.5`.

For an existing instance:

```bash
./.venv/bin/python assistant_cli.py xai-oauth-login \
  --config <instance_root>/instance_config.yaml
./.venv/bin/python assistant_cli.py xai-oauth-status \
  --config <instance_root>/instance_config.yaml
```

The provider stores rotating credentials in the instance-owned auth store.
`XAI_OAUTH_ACCESS_TOKEN_FILE` is available for ephemeral automation. The
`xai-oauth` provider does not fall back to `XAI_API_KEY`; that variable is used
by API-key workflows such as Grok voice. See
[xAI Grok OAuth](auth/xai-oauth.md).

## OpenAI Codex OAuth

Use `openai-codex` for ChatGPT/Codex subscription OAuth rather than an OpenAI
API key. Authenticate a file-backed Codex CLI home and mount it read-only, or
import it into the instance-owned Universal Agents auth store. See
[OpenAI Codex OAuth](auth/openai-codex.md).

## Model Keys

Model keys never go in `instance_config.yaml` or chat messages. Each key
belongs to one endpoint address (for example `https://bifrost.example.com`),
and agents send it only to that address.

**Save a key with `/auth model`** (administrators and owners). The command
opens a secure broker page that lists the endpoints your configuration uses,
which key each one has, and anything that overrides it. The page has one form:
pick an endpoint from the **Endpoint** list (the team's endpoints, OpenAI,
Anthropic, Google Gemini, xAI, or **New OpenAI-compatible address**) and it
fills in the address, model and request API; then enter the key. Saving
first sends one short test request with the key; if the test fails, nothing
is saved and the result, in red just above the form, says what most likely
went wrong (a refused key, an address where nothing answers, an unknown
model) and shows the provider's answer. You can type an address with or
without `/v1`: the test tries both and the page says which one answered.
Agents use a saved key on their next request, without a restart. **No key
needed** is for local servers that accept requests without one. The page
stays open for 24 hours; its **Close** buttons (top and bottom) end it.

**Environment variables still work and win.** `LITELLM_API_KEY` (the
OpenAI-compatible endpoint) and `EMBEDDING_API_KEY` (the embedding service),
or their `_FILE` forms, override the key saved for that endpoint.
`LITELLM_API_KEY` also covers the embedding service when it has the same
address as the model endpoint. `/auth model` and `/auth status` say when an
override is in effect, and the Status page names where it comes from: the
instance's `team.env`, a secret file, or the container's own environment.
The launcher passes `team.env` values only to the processes it starts, so a
shell opened with `docker exec` does not show them; look in the file. To use the saved key instead, remove the variable from
`team.env` (or its secret file) and restart the container.

**When a provider rejects a key** (expired, revoked or out of credit), the
request stops and you are told whose key it was and who fixes it.
Administrators get an Inbox notice. An administrator runs `/auth model` and
saves a working key; this works even while no agent can reach its model.
Then send the request again. A key set by an environment variable is fixed
in `team.env` instead, followed by a container restart.

If the broker cannot read its credential store, the request stops with a
credential-store error. This does not trigger provider overload retries or
switch to a fallback model. An administrator checks that the broker and
Sealbox are running. For an existing Sealbox tenant with missing, damaged or
mismatched token/key files, restore the original files from a backup. Do not
delete the tenant mapping or generate replacement keys, because existing
encrypted credentials need the original material.

An owner without a Sealbox tenant has no saved credentials yet. Looking up
their key creates no tenant; the first authorized save creates it. Someone
without a personal key can use the team's key when their settings allow it.

**Your own model and keys: `/auth model me`.** Anyone on the team can open
this page (members also get it from plain `/auth model`). It uses the same
one-form layout:

- With **Use this as my model** ticked, the endpoint you test becomes *your
  model*: any provider and any address, tested before it is saved. Every
  agent's work for you then uses it with your key, including agent types an
  administrator set to a particular model: your chats, your scheduled runs,
  and work agents hand to each other for you. You can say whether it accepts
  images or files and give its context window (otherwise agents assume the
  window of the team's model). **Stop Using My Model** removes it; your keys
  stay.
- Without it, the key is yours for an endpoint the team already uses: agents
  keep the team's models but use your key for your work.

Work for nobody, such as internal maintenance, keeps the team's models and
keys. Embeddings (memory search) and Grok voice always use the team's keys.
If a provider rejects your key, your request stops and you are told; it
never switches to the team's key, so nobody's spending changes without a
decision. When a person is removed from the team, their keys and their model
are deleted with their other sign-ins.

**Who may use the team's keys.** Administrators decide per person whether
their work may use the team's model keys: `--team-keys` or `--own-keys` when
adding them, and `/user team-keys <usr_...> on|off` later (see
[Multi-User Onboarding](newuser/index.md)). People added without a choice get
the team's model-key sharing: shared until someone chooses, so they may use
the team's keys. Owners and administrators change it with `/accounts share
model` or `/accounts private model` (see
[Shared or private](permissions.md#shared-or-private)); it decides only what
people added from now on start with, and people already in the team keep
their setting. The setting `gateway.users.new_people_use_team_model_keys` is removed, and
startup refuses a configuration that still has it. Someone without the
team's keys and without a model of their own is told "You haven't set up
your own model yet. Use /auth model me." and their requests do not run. This
also applies to keys from environment variables, which are the team's too.
The Status page lists such people as a warning, and people with their own
model as a note.

**Switching the OpenAI-compatible endpoint.** Ask TeamLead, for example
"switch to Bifrost". TeamLead sees every endpoint whose key is already saved
(addresses only, never keys) and picks the one you named; give the address
only if no saved one matches. A new endpoint needs its key saved first with
`/auth model` → **New OpenAI-compatible address**, or TeamLead tells you that
is missing. When the change is prepared, one test request goes to the
endpoint (a bare host is tried with `/v1` first, then without), and the form
that answers is the one saved. The test waits as long as agents' own requests
do (`llm.request_timeout_seconds`, 120 seconds by default), because reasoning
models can think for a minute before their first output. An administrator
approves the change in the Inbox; its text names the new host and whose key
the requests use. Before anything is saved, one test request goes to the new
endpoint with the team's model, and a failure changes nothing. No restart
is needed: each agent switches at the start of its next request (one that is
running finishes on the old endpoint), so for a minute some agents may still
use the old one.
Because keys belong to an address, the old endpoint's key is never sent to
the new one. If `LITELLM_API_BASE` is set in `team.env` or the container
environment, it overrides the saved endpoint, so the switch is refused until
the operator removes it and restarts.

OpenAI, Anthropic, Google AI and xAI each have one fixed address
(`api.openai.com`, `api.anthropic.com`, Google's Gemini API, `api.x.ai`), so
choosing the provider chooses the endpoint; save their keys on `/auth model`
the same way. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` and
`XAI_API_KEY` (or their `_FILE` forms) override the saved key while they are
set. Only the OpenAI-compatible endpoint (`llm.litellm_api_base`) can be
switched to another service.

For OpenRouter or another OpenAI-compatible endpoint, provide both the API base
URL and a model ID accepted by that endpoint. In Universal Agents,
`llm.provider: litellm` is equivalent to choosing the OpenAI API-compatible
runtime path; it does not require an OpenAI-operated endpoint or a separately
deployed LiteLLM proxy.

For Fireworks and Novita, UA selects its reduced provider-compatible tool and
terminal-output schemas from either the provider marker in the model ID or the
configured API-base hostname.

## Model Failover Is Opt-In

Fresh configurations keep `llm.failover.enabled: false` and have no fallback
candidates. To enable cross-provider switching, configure an ordered
`llm.failover.fallbacks` list and then enable the policy explicitly. This
setting is under **Advanced Setup** during onboarding.

An active fallback lease never takes precedence over a later explicit model
profile change in `control/LLM_OVERRIDES.yaml`. Removing the leased target or
changing the primary model invalidates the lease immediately; a failure from a
request that was already in flight applies the new primary without creating a
new lease for the superseded model. Disabling failover also clears an active
lease while retaining provider-health history.

## LiteLLM Connection Failures

UA keeps explicit overload and rate-limit responses separate from endpoint
connectivity failures. An overload may offer retry choices. A refused
connection instead reports a connectivity/configuration failure after any
enabled and configured model fallback is unavailable or exhausted.

Check `llm.litellm_api_base` and any `LITELLM_API_BASE` environment override
from inside the UA runtime. In Docker, `localhost` and `127.0.0.1` identify the
UA container itself; use a container-network service name, a host-gateway
address, or the externally reachable OpenAI-compatible endpoint URL as
appropriate.

## Model Keys and `/auth`

Model API keys (OpenAI-compatible endpoints such as OpenRouter or Bifrost,
OpenAI, Anthropic, Gemini, xAI and embedding services) are saved with
`/auth model` and `/auth model me`; see [Model Keys](#model-keys). The OpenAI
Codex and xAI Grok OAuth sign-ins are not part of `/auth`: they use their own
stores, set up as described in [OpenAI Codex OAuth](auth/openai-codex.md) and
[xAI Grok OAuth](auth/xai-oauth.md).

## Response-copy mismatch diagnostics

`openai_reasoning_output_sources_mismatch` compares completed streamed items
with the final response's copy of those items. The runtime keeps the completed
stream for tool-loop replay when it contains tool calls; a reasoning-only partial
stream uses the final response instead. This warning does not mean a tool failed.

Lifecycle status and equivalent JSON argument spacing/key order are logged at
debug level. So is `encrypted_content` that differs only because OpenAI
encrypted the same reasoning separately for each copy: both copies are present,
have the same length, and neither carries LiteLLM's `litellm_enc:` wrapper.
Changes to arguments, call identity or reasoning text still warn, as does
encrypted content that is missing from one copy, changes length, or is wrapped
by the proxy on only one side. Logs identify the response, differing item
indexes and field names without including arguments, reasoning text or
encrypted content. Use those fields to investigate a provider/proxy
discrepancy; an index-only warning cannot establish its cause.

When `encrypted_content` differs, `encrypted_content_diagnostics` describes each
differing item without its content: both lengths, whether each copy carries
LiteLLM's `litellm_enc:` routing wrapper, whether the copies match once that
wrapper is removed, and how many leading characters they share. A wrapper on
only one side with `equal_after_proxy_unwrap: True` means the LiteLLM proxy
rewrote one copy. Same-length unwrapped copies that share only their first 12
characters are OpenAI's normal separate encryption of each copy: its encrypted
content is a Fernet token whose first 12 characters encode the format version
and timestamp, followed by a fresh random value. A long shared prefix with
different lengths means one copy was cut short.
