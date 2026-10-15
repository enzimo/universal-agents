---
name: hf-cli
description: Searches Hugging Face models and datasets, downloads Hub files, and uploads explicit file content through Universal Agents' broker. Use for Hugging Face Hub workflows, not generic model training or unrelated file transfers.
---

# Hugging Face Hub

Use `shell-execution-workflows` for CLI execution and
`secure-credential-workflows` for broker credentials, account links, and
authorization. The executable is `hf`, supplied by `huggingface_hub`; do not
install a package named `hf-cli` or run a runtime self-update.

Broker requests take no `credential_key` or other account selector. The broker
uses the account linked to the `cli:huggingface` tool: the person's own link
first, then the team's while the team shares Hugging Face (the default). Each
person may link their own Hugging Face account; only a team administrator or
owner links the team's. When an administrator makes Hugging Face private
(`/accounts private cli:huggingface`), the team's account is not used, not even
for work that serves no person: each person's work needs their own link, and
a team account link is refused. `/accounts` shows whether the team shares it. No Hugging Face token comes from
environment variables: never set or ask for `HF_TOKEN` (startup refuses it).

1. Choose the requested repository, type, operation, and artifact. Search public
   models or datasets with bounded results. Use `--json` for structured output:

   ```bash
   HF_HUB_DISABLE_IMPLICIT_TOKEN=1 HF_HUB_DISABLE_UPDATE_CHECK=1 hf models ls --search bert --limit 3 --json
   ```

2. Inspect file metadata and sizes before downloading. Pin a commit revision
   when reproducibility matters, select specific files, and use a workspace
   destination. Do not execute downloaded model code as part of a download.
3. For private/gated downloads or uploads, check `secure_cli` provider
   `huggingface`, action `auth.status`, with `params={}`. Verify the returned
   account. If the broker reports `account_link_missing`, TeamLead calls
   `set_up_tools` for `cli:huggingface`, which opens the account link form
   (a specialist escalates to TeamLead). If
   no Hugging Face token is stored yet, the person types it into that account
   link form, which saves it and links it in one step.
   Never ask for token values in chat or hydrate them into the agent's shell,
   workspace, Git credentials, or skills.
4. Use `file.download` with `repo_id`, `repo_type`, `filename`, and a full
   40-character commit `revision`. Decode the returned `content_base64`
   into the intended workspace artifact. The broker accepts one literal file
   up to 1 MiB and returns its revision and size.
5. Use `file.upload` with the same fields plus explicit `content_base64`. The
   revision must equal the current `main` commit in an existing repository.
   The broker writes only that file using the HF SDK's atomic parent-commit
   check, because the upstream `hf upload` CLI also creates missing repositories
   and branches. Report the returned commit ID and URL.

Model metadata for gated or private models (pipeline tag, tags, gated/private
flags) comes from `secure_cli` provider `huggingface`, action `model.info`, with
`params={"repo_id": "org/model"}` and `cli:huggingface` at `read`. TeamLead's
`lookup_model_capability_candidates` already uses it and falls back to public
metadata, saying so, when no account is linked.

When a broker result has `failure_stage=credential_resolution`, read its
`error_code`, `required_field`, `action_owner`, and `next_action`. If
`provider_operation_attempted=false`, explain that the tool operation was not
attempted; do not claim the external service rejected authentication. A missing
field means the linked credential is incompatible (X needs secret
`oauth_json`; Hugging Face needs secret `api_token`). Ask TeamLead to have the
owner store a matching credential through the secure form and link it to the
tool. Source configuration, read failures, or empty values go to TeamLead for
diagnosis and operator help when needed. Correct only issues within existing
authority; these diagnostics grant no permission. Preserve completed setup,
never guess credential keys or repeat account links, access requests, or restart
indiscriminately, and do not retry the unchanged request when
`retry_safe=false`. Never request or transmit secrets through chat.

On revision conflict, inspect the remote change and reassess the requested upload.
On an unknown write outcome, reconcile remote commits before another write.
On credential denial, follow the exact trusted access-request workflow.
For larger private artifacts, paid Jobs, endpoints, hardware changes, repository
creation/deletion, or remote execution, report the unsupported broker operation;
do not work around it with credentialed arbitrary shell commands.

Read [the CLI reference](references/cli-reference.md) for public command examples
and the pinned version. Keep caches for public artifacts separate from any
private material. Installing the CLI or skill grants no additional authority.
