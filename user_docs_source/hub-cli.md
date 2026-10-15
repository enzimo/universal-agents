# X and Hugging Face

Both maintained Docker images include `xurl` 1.3.1 and the `hf` executable from
the hash-locked `huggingface-hub` 1.28.0 package. Agents with skills enabled
discover the bundled `xurl` and `hf-cli` skills. Copies with matching names in
`control/skills` retain precedence.

The CLIs can also be checked in a native environment with `xurl --version` and
`hf version`. Native installs need the X binary separately; installing the
runtime Python requirements supplies `hf`.

## Credential setup

The secure tool broker always runs. Give the intended agent the `secure_cli`
tool. Keep `security_layer.broker_credentials_dir` outside
`workspaces/` on persistent storage. The existing credential-state backend defaults
to Sealbox. The initial credential and refreshed X tokens use that configured
storage; credentials are never built into the image or placed in agent prompts.

Ask the agent to set up the service. It can render the matching credential
template and request the gateway's secure collection form. Submit secrets only
through that form. Existing credential catalog entries can be reused, including
entries backed by Sealbox.

Hugging Face and X choose their account differently:

- **Hugging Face uses account links.** Storing a credential does not choose it
  for the tool. Link it in chat, or let TeamLead ask for the link when an agent
  first needs it. `/link cli:huggingface` links **your own** account; anyone can
  do this, and agents working for you then use it.
  `/link cli:huggingface team` links **the team's** account; only a team owner
  or administrator can do this, and it is used for everyone who has not linked
  their own while the team shares Hugging Face, which it does until an
  administrator makes it private. See [Account links](permissions.md#account-links).
- **X uses each person's own sign-in.** `/link cli:x` signs you in to X in the
  browser (see [X sign-in](#x-sign-in)). X is private until an administrator
  shares it with `/accounts share cli:x`; after that, an administrator may link
  a team X account with `/link cli:x team`, which is used for people who have
  not signed in. Personal X account links are no longer created or used: your
  own X account is your sign-in.

`/accounts` shows whether the team shares each service or keeps it private
(see [Shared or private](permissions.md#shared-or-private)).

Neither a sign-in nor a link gives anybody access to the tool; access still
comes from grants.

No token comes from environment variables: startup stops while `HF_TOKEN` (or
`HF_TOKEN_FILE`) is set and names `/link cli:huggingface` as the replacement.
X tokens were never read from the environment.

| Service | Template | Secret field | Setup |
|---|---|---|---|
| Hugging Face | `huggingface_api_token` | `api_token` | A fine-grained HF token scoped to the required repositories and read/write operations |
| X (team account only) | `xurl_oauth2` | `oauth_json` | An authorized user OAuth 2.0 bundle with refresh token and client credentials |

The broker's link form lists the stored credentials that have the field above.

## X sign-in

Each person signs in to X with their own account. The broker uses X's OAuth 2.0
authorization code flow with PKCE (a one-time check that the browser that
started the sign-in is the one that finishes it).

### Set up the team's X app (administrators, once)

1. In the [X developer portal](https://developer.x.com/), open the app and, in
   its **User authentication settings**, turn on OAuth 2.0 with **Read and
   write** permissions.
2. Register a callback address there. Use the broker's own page,
   `<broker public address>/auth/x/callback`, so sign-ins finish by themselves.
   `http://localhost:8080/callback` also works; each person then copies the
   address their browser shows after approving and pastes it into the sign-in
   page. The address must use https, except on `localhost` or `127.0.0.1`.
3. In chat, run `/auth x team_client` (administrators and owners only) and fill
   in the form:
    - the OAuth 2.0 client ID from **Keys and tokens**;
    - the client secret, only for a "Web App". A "Native App" has none; leave
      the field empty;
    - the callback address you registered. The form suggests
      `<broker public address>/auth/x/callback`, filled in from the address
      your browser shows.

The broker keeps the app in the team's part of the credential store
(`xurl/oauth_client.json`). Storing it gives nobody access to anyone's X
account. The callback address must be the public address people's browsers
reach. The page is under the broker's `/auth/` path, so a reverse proxy that
already sends `/auth/...` to the broker needs no change.

### Sign in (each person)

1. Run `/link cli:x`.
2. Select **Open**, and approve on X.
3. The page says "You are signed in to X." with your @username. With the
   `localhost` callback, paste the address your browser shows into the page
   first.

The broker asks X for the scopes `tweet.read tweet.write users.read
offline.access` (`offline.access` gives the refresh token). It stores the
sign-in as yours in the credential store (Sealbox by default; with file-backed
broker state, `broker_credentials/users/<person>/xurl/sign_in.json`). X
requests for work done for you use it, and every refresh X makes is saved.
Tokens never reach the agent, logs or the gateway.

`/auth status` shows X as `signed_in` with your username when you signed in; the
broker checks the sign-in with X when an agent uses it. Otherwise it says how to
sign in, or that the team has no X sign-in app yet.

### The team's X account (fallback)

For people who have not signed in, the broker uses the team's linked X account
only while the team shares X. X starts private: an administrator first sends
`/accounts share cli:x`, then links the account with `/link cli:x team` (while
X is private, that link is refused). A team X account linked earlier is kept
while X is private and used again once X is shared. With neither a sign-in nor
a team account in use, an X request fails with `x_account_missing`: "No X
account is available for this work. The person signs in with /link cli:x. A
team that shares X (/accounts share cli:x) can also link one team X account for
everyone with /link cli:x team."

The team account is an `xurl_oauth2` credential with an `oauth_json` bundle.
Create it on a trusted operator computer with the
[upstream xurl instructions](https://github.com/xdevplatform/xurl/tree/v1.3.1)
(`xurl auth oauth2 --headless` supports a remote terminal), authorizing with
write and offline-access scopes. The bundle's `client_secret` may be empty for
a public app. API credits are required; app-only bearer tokens cannot publish as
a user.

From a UA checkout with runtime dependencies installed, export the authorized
account without printing its token values:

```bash
.venv/bin/python -m scripts.export_xurl_credential \
  --app APP --username HANDLE --output /private/location/x-oauth.json
```

The output is created with mode `0600` and will not overwrite an existing file.
Upload it through the `xurl_oauth2` form, link it with `/link cli:x team` (once
the team shares X), then delete the export. The default source is the operator's `~/.xurl/auth.yml`; `--source`
selects another file.
Do not use the same rotating refresh token from independent installations.
Reauthorize the operator installation if it needs its own ongoing session.

## Agent operations

Credential-resolution failures identify the failure stage, required field,
suggested action owner, and next action. Missing catalog entries or required
fields need an owner to store a compatible credential through the secure form
and link it to the tool.
Unconfigured sources, failed source reads, and empty source values need TeamLead
to investigate and involve the operator where required. These errors state
`provider_operation_attempted=false`: they do not mean X or Hugging Face rejected
authentication. No secret values, catalog keys, or upstream error text are included
in these CLI diagnostics. An account link does not prove its credential is
compatible or readable. Preserve completed approvals and repair the reported
cause before trying again.

Agents use typed `secure_cli` requests for authenticated operations. Requests
name no account, and the broker refuses a `credential_key` or other account
selector. For `cli:huggingface` it uses the linked account; if none is linked it
reports `account_link_missing` and TeamLead asks for a link. For `cli:x` it uses
the person's own X sign-in, then the team's linked X account while the team
shares X; with neither it reports `x_account_missing`. For example:

```json
{
  "provider": "xurl",
  "action": "post.create",
  "params": {"text": "The requested update."},
  "reason": "Publish the requested update to the person's X account."
}
```

First check `auth.status` and verify the returned account. Successful
publication returns a post ID and URL. Installing a CLI, loading a skill,
signing in, and linking an account do not grant posting permission; the broker's access checks
still apply. See [Tool access and account links](permissions.md#tool-access-and-account-links).

| Provider | Action | Parameters | Result |
|---|---|---|---|
| `xurl` | `auth.status` | None | Authenticated account ID and username |
| `xurl` | `post.create` | `text` | Post ID and URL |
| `huggingface` | `auth.status` | None | Authenticated username |
| `huggingface` | `file.download` | `repo_id`, `repo_type`, `filename`, `revision` | File content as base64, size, and revision |
| `huggingface` | `file.upload` | The download fields plus `content_base64` | Commit ID and URL |
| `huggingface` | `model.info` | `repo_id` | Pipeline tag, tags, and whether the model is gated or private |

TeamLead's model capability lookup (`lookup_model_capability_candidates`) reads
Hugging Face model metadata with `model.info`, so gated and private models are
found with the linked account (normally the team's). Without a link or without
access to `cli:huggingface`, it looks the model up anonymously, which finds
public models only, and says so in its evidence.

Authenticated HF transfers accept one literal file up to 1 MiB. `repo_type` is
`model`, `dataset`, or `space`; it defaults to `model`. Revisions are full
40-character commit hashes. Uploads target an existing repository's `main`
branch, and its current commit must match `revision`. The SDK supplies an atomic
parent-commit check. The broker uses the SDK for this action because `hf upload`
also creates missing repositories and branches. Requests carry explicit bytes,
not a filesystem path the broker would have to trust.

Public HF search and downloads use the regular shell tool. Choose specific
files and inspect their sizes before downloading large models:

```bash
HF_HUB_DISABLE_IMPLICIT_TOKEN=1 HF_HUB_DISABLE_UPDATE_CHECK=1 \
  hf models ls --search bert --limit 3 --json
```

Private downloads use a private temporary cache removed after the result is
returned. HF tokens are injected only into the broker's isolated CLI child.
Downloading files does not execute model code. This integration adds no actions
for paid HF Jobs, endpoint deployment, hardware changes, repository deletion,
X replies, media posts, threads, or post deletion.

## Failure handling and maintenance

An `outcome_unknown` write may have succeeded remotely. Inspect remote posts or
commits before another write; do not automatically retry. A transport failure
is not proof that nothing happened. The tool waits up to five minutes for these
broker operations, including credential resolution and refresh; each CLI child
has a 60-second limit. An occupied X account lock returns `account_busy`.

The broker serializes X token refresh across processes sharing its credential
volume and saves refreshed state before posting. Preserve that volume and the
Sealbox store across restarts. A post receipt remains valid if the result also
warns that a later refresh could not be saved; repair storage and reauthorize
before further use. Independent distributed brokers must not share a rotating
X refresh token without shared custody.

Update X using `XURL_VERSION`, `XURL_SHA256_AMD64`, and `XURL_SHA256_ARM64` build
inputs together. Both Dockerfiles use the same installer and verify SHA-256.
HF upgrades go through `requirements.txt` and generated locks. Do not run
`hf update` or global skill installers inside a managed image.

Canonical skills live in the sibling `ua-skills` repository. After reviewing
command references for the pinned releases, run
`python -m scripts.sync_hub_cli_skills`, then repeat it with `--check`. This
copies only `xurl` and `hf-cli` into their tracked runtime templates.
