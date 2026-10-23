# Contributing

Thank you for helping improve Universal Agents.

## Before opening a change

- Search existing issues and pull requests for related work.
- Open an issue before beginning a large behavioral or architectural change.
- Never commit credentials, private configuration, runtime databases, user
  data, or generated instance directories.
- Keep changes focused and update documentation when behavior, configuration,
  or operator workflows change.
- Keep personal details out of the repository (see below).

## Personal details

Everything tracked here ships to other people: source release archives contain
every tracked file, container images copy the source tree, and Git history
keeps old versions of every file. Removing a personal detail later means
rewriting history. Keep these out of code, tests, documentation, examples,
handoff notes and commit metadata:

- Real names, nicknames, handles, email addresses, phone numbers and chat or
  account IDs of maintainers, users or their assistants. This includes the
  names people give their own assistant or bot, team, instance or company.
- Real domain names, server addresses, machine names, runner names, host
  accounts, hardware details and host folder paths from anyone's own setup.
- Real IDs from a running instance (schedule, task, run or message IDs) and
  copied chat messages, emails, reports or logs from a person's real work.

What to do instead:

- Use made-up placeholders: `exampleuser`, `example-owner`, `alice`, `bob`,
  `jordan`, the assistant name `Nova`, the company `Acme`; domains under
  `example.com`, `example.net` or `.test`; phone numbers in the `+1555` range;
  Telegram and Discord IDs such as `123456789` or `100200300`.
- Turn a real incident into a summary or a made-up equivalent before adding it
  to a test or note. Keep the behavior being tested, not the person's content.
- Describe your own machines generically (for example "one Windows host runs
  two Linux runners") and keep their exact names, accounts and paths in the
  host's own notes outside this repository.
- Commit with an anonymized email: your GitHub noreply address
  (`<id>+<login>@users.noreply.github.com`, shown at
  https://github.com/settings/emails). Any contributor may commit; the checks
  look only at the email, not the name. Use your login rather than your real
  name as `git config user.name`. Turn on GitHub's "Keep my email addresses
  private" and "Block command line pushes that expose my email" settings, so
  merges you make on github.com use the noreply address too.
- Organization and repository names used in real addresses (download links,
  image names, the license holder) are public by design and may stay.

### Automated checks

`scripts/check_personal_details.py` reports email addresses outside the
reserved example domains, phone numbers outside the fictional 555 ranges, home
folder paths with a real account name, and commit emails that are not
anonymized (GitHub noreply addresses and service mailboxes such as
`noreply@github.com` pass; names are not checked). It checks tracked files and
commits: the email, the message and every added line, including lines a later
commit deleted. It needs only Python 3.9+ and Git. It cannot recognize names,
handles, machine names or copied chats without listing them, so those still
depend on review.

- Turn on the Git hooks once in every clone:
  `git config core.hooksPath .githooks`. They check staged files and the
  commit email (pre-commit), the commit message (commit-msg), and every
  commit a push would upload (pre-push).
- The **Personal details** workflow runs the checker's tests, then checks
  tracked files and only the commits each push or pull request adds
  (`--github-event`). A manual run checks every commit, and so does a run
  whose starting commit is missing, such as after a force push, with a
  warning. Its log shows where a finding is but not the value.
- `.personal-details-allowlist` records this project's decisions: `text:`
  lines for real values that may appear and `account:` lines for made-up
  account names in example paths. It does not list who may commit.
  Fix a finding instead of adding it there.
- A detail that was already pushed stays in history after a new commit deletes
  it. Follow `docs/personal-details-cleanup.md`.

## Development setup

Use Python 3.13 and the repository virtual environment. Python 3.12 remains the
supported fallback for experimental native installs:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock.txt
```

`requirements.txt` owns runtime dependencies. Containers and native installs use
`requirements.lock.txt`. `requirements-dev.txt` adds documentation, tests, lint,
typing, and audit tools. Its generated lock constrains shared packages to the
runtime lock, so verification exercises the production versions. CI also installs
only the runtime lock into a fresh environment on Python 3.12 and 3.13, checks the
installed graph, and exercises entry-point imports, configuration, and SQLite.

Both Dockerfiles build documentation in the disposable `user-docs-builder` stage
using the development lock, then copy the generated site into the runtime image.
The final image installs only the runtime lock. CI builds the documentation and
Rust tools stages of both Dockerfiles, including the copied Rust source patches.
It checks the generated pages, search index and container document converter.
Keep build tools in the stage that runs them when changing dependency ownership.

LiteLLM initializes through `custom_agents.litellm_runtime` with its bundled
model catalog, before agent construction starts worker threads. Its import-time
pricing download can start a retry thread that races its logging imports when
the network fails. Catalog data therefore updates with the installed LiteLLM
version; explicit endpoint metadata requests remain live. Fast tests and
collection select the same bundled mode for direct SDK imports. Subprocess tests
force the failed-download thread ordering and verify both production import paths
make no catalog HTTP requests or retry threads. Their 90-second watchdog detects
deadlocks rather than enforcing a cold-start speed target; thread stacks are
captured every 20 seconds and included if the subprocess times out.

After editing an input, regenerate the universal, hash-checked locks with the
development environment's uv 0.13.4:

```bash
UV_BIN="$PWD/.venv/bin/uv" ./scripts/regenerate_python_lockfiles.sh
```

The uv pin and the generator's required version move together. Version 0.13.4
removes a compromised certificate root from uv's own HTTPS trust store; fixing
certifi alone does not fix uv's separate bundle.

The release's distrusted-root fingerprints live in
`data/security/distrusted_roots.json`, shared by Debian image builds and the
Photon sidecar. Test a change with `tests/test_system_certificate_store.py`
on Debian with `update-ca-certificates` and OpenSSL available, and `npm test`
in `assistant_gateway/interactive_apps/photon_sidecar`. These integration tests
verify the system bundle, OpenSSL's hashed directory, Node's effective defaults,
private-CA TLS and explicit gRPC bundles. Python lock audits alone cannot cover
system or Node trust.

Generation retains existing versions where constraints permit. Pass `--upgrade`
for an intentional dependency refresh. Do not hand-edit generated lockfiles.
`requirements.skillspector.txt` retains its separate environment and lock so the
scanner graph does not alter the agent runtime. Removing a development dependency
from an existing environment requires recreating that environment; installing the
runtime lock alone does not uninstall packages already present.
SkillSpector itself is pinned by its source archive and SHA-256. `pip-audit`
checks its registry dependencies, but skips the SkillSpector package because
that source release is not on PyPI; a successful audit does not cover it.

The dependency-audit workflow audits the runtime, development, and SkillSpector
Python locks, Photon production dependencies, and the Rust workspace. Rust auditing
fails on known vulnerabilities, yanked versions, and unsoundness warnings.
Unmaintained dependencies remain visible warnings. As reviewed on 2026-09-23,
AnyDoc's `pdf-inspector` requires `ttf-parser 0.25.1`, which has
[no patched version](https://rustsec.org/advisories/RUSTSEC-2026-0192.html).
`lopdf 0.45` no longer depends on it, but
[pdf-inspector 1.24.0](https://docs.rs/crate/pdf-inspector/1.24.0/source/Cargo.toml)
still requires that parser. Retain AnyDoc's current API and document formats while
tracking the upstream font-parser replacement; do not suppress the advisory.
The yanked `chacha20 0.10.1` pin has been replaced by `0.10.2`.
ZIP 8 is also yanked and vulnerable. Until the document-converter dependencies
publish compatible releases, checksum-verified source patches retain their current
versions with ZIP 9.0.5. See [the source patch record](rust/vendor/README.md) for
the exact changes, verification command and removal criteria.

Photon's contract test declares `@photon-ai/otel` as a development dependency.
Use `npm ci` before `npm test`; production images continue to use `--omit=dev`.

Build the Cedar evaluator and run the hermetic test profile:

```bash
cargo build --locked --workspace
./scripts/run_tests.sh fast -q
```

Use events to hold fake workers while testing concurrent requests, and release
them in a `finally` block. Put a deadline around the whole test rather than
letting a held worker time out: a slow status request must not end the operation
whose pending state the test is checking.
When several agents save results independently, wait for every saved record
that the test checks. One agent's reply or saved task does not establish that
another agent has finished saving its task.

Keep SQLite timing tests independent of disk speed. A test that holds a
database lock for a set time takes it with `tests/sqlite_lock_support.py`,
whose connection neither flushes its commit nor checkpoints the write-ahead log
when it closes. A plain connection does both while it still holds the lock, and
on a slow CI disk that outlasted the stores' 2.5-second lock wait. Likewise, set
up files before timing an operation: a new file's first write flushes to disk.

The access tests start the evaluator (`target/debug/ua-cedar-authorizer
serve-access`) as a real process. After changing the Cedar files in
`ua_definitions/access/cedar/`, check that they still validate strictly:

```bash
cargo test --locked --workspace
target/debug/ua-cedar-authorizer validate-access \
  --policy ua_definitions/access/cedar/policies.cedar \
  --schema ua_definitions/access/cedar/schema.cedarschema
```

The code sandbox tests (`tests/test_code_sandbox.py`, `tests/test_code_access.py`)
run real Landlock commands. They are skipped on a kernel without Landlock, and
the network tests are skipped below Landlock ABI 4 (Linux 6.7), so check the
skip list before relying on a local run after changing `custom_tools/code_sandbox*.py`
or `custom_agents/code_access.py`. The OpenShell tests use a stand-in CLI that
records its arguments; they do not start real sandboxes.

Run the relevant live NATS or PostgreSQL validation when a change affects
messaging, persistence, routing, recovery, or deployment wiring.

Protocol 1 contract changes also run `tests/test_message_contract.py` and the
isolated `tests/test_live_message_contract.py` with NATS 2.15.1 or later on PATH.
The latter starts its own broker so generated role permissions can be tested
without changing a team's users or reading its credentials. It checks durable
fetch/acknowledgement and signed request/reply delivery, including denied access
to other consumers. The NATS CI job supplies the server binary. Regenerate the
contract sources with `python scripts/export_message_contract.py`, then run its
`--check` mode. See `contracts/messages/v1/README.md` for the language-neutral
validation and signing rules.

PostgreSQL connection strings in logs and error messages go through
`storage.postgres_connection.redact_postgres_dsn`. It uses psycopg's libpq parser
for both URLs and keyword settings, then writes redacted keyword settings.
Generic URL parsing is unsafe here: libpq accepts `?` and `#` in URL passwords,
and a keyword value may itself contain `://`. Invalid strings are replaced as
a whole. Connection strings used to connect or stored in settings are unchanged.
Live storage validation logs and saved reports use this same helper.

The PostgreSQL tests are the ones marked `postgres`: the knowledge store tests
and the PostgreSQL variants of the storage, runtime recovery, snapshot and
schedule-worker transaction tests. The CI PostgreSQL job runs exactly that
marker, so a new PostgreSQL variant must carry it next to `live`
(`tests/test_ci_workflows.py` fails otherwise). Run them against an isolated
database created with UTF-8 encoding; on a `SQL_ASCII` database psycopg returns
bytes and most of them fail:

```bash
LIVE_TEST_POSTGRES_DSN=postgresql://postgres@127.0.0.1:5432/ua_live_tests \
  .venv/bin/pytest tests -m postgres -q
```

Credential-state or model-key changes also require the real Sealbox suite:

```bash
.venv/bin/pytest tests/test_live_sealbox_integration.py -q
```

It starts isolated Sealbox and broker services using `sealbox-server` and
`sealbox-cli`, found on PATH or through `SEALBOX_SERVER_PATH` and
`SEALBOX_CLI_PATH`. It checks the first team and personal saves, onboarding
handover consumption, signed lookup, owner isolation, restart persistence and
damaged tenant refusal. Missing binaries are an explicit coverage gap.
Container acceptance must verify a real onboarding credential save and signed
lookup before and after restart; service health alone does not prove storage
works. Never include the key in logs or acceptance output.

Message and snapshot models under `custom_messages/contracts/` have checked-in
JSON schemas in `data/schemas/`, and the fast profile fails when they drift.
After changing a model, regenerate the schemas in the same change and confirm
them:

```bash
.venv/bin/python scripts/export_runtime_contract_schemas.py
.venv/bin/python scripts/export_runtime_contract_schemas.py --check
```

`export_access_record_schemas.py` (generated-specialist and change-approval
records), `export_mcp_schemas.py`,
`export_runtime_manifest_schemas.py`, and `export_gateway_transcript_contract.py`
(the Web Chat transcript models in `website_application.py`) follow the same
pattern for their contracts; each has a `--check` mode, and the fast profile
fails when a tracked schema drifts from its models.

The secret variable tables in `user_docs_source/docker.md` are generated the
same way from `docker/secret_env_vars.yaml`, the list of secret environment
variables and the processes that receive them. After changing that list, run
`.venv/bin/python scripts/export_secret_env_docs.py` (it also has `--check`).

Live tests that accept either `config.yaml` or `instance_config.yaml` use
`TEAM_NATS_LIVE_CONFIG` or `UA_LIVE_CONFIG`. The loader identifies the file by
its required instance identity fields because the two schemas have independent
version numbers. The `external` and `full` profiles use short tracebacks and do
not replay captured provider output. This prevents credential-bearing client
arguments from appearing in broad test failure reports.

The knowledge base privacy scenarios call the configured model many times, so
they require `RUN_LIVE_KNOWLEDGE_PRIVACY=1`. The external and full profiles
collect `tests/test_live_knowledge_privacy_scenarios.py`, but it skips without
that opt-in. Set `UA_LIVE_CONFIG` and `TEAM_NATS_LIVE_ENV` (the file holding
the model key) and use `-m "live and external"`. The module docstring lists
their settings; results go to `test_results/knowledge_privacy/`. The suite
uses each agent type's resolved memory and knowledge settings and tool-call
limit, including overrides, and refuses a run if A's planted facts did not
reach its prompt or entered B's prompt. Cross-person checks decode tool JSON
and check answers, results and inputs for distinctive private paraphrases,
including personal preferences; category words from the request, such as
"salary", do not make a safe refusal a leak. The spill worker must propose a
team change, and the curator must decide every contribution. The
injection worker must read the injected text, and B's indirect request must
successfully search. Refusing to edit after reading the injected page is valid.
Explicit rejection of private contributions is a completed
review; leaving them pending is not.
The deterministic cross-person search and read probes must succeed too.

## Cognitive complexity

Cognitive complexity is an advisory review signal for how difficult a function
is to follow. It does not decide whether a change may merge. The repository
analyzes only Python files changed relative to the selected Git baseline and
highlights the changed functions in the diff summary, so unrelated files do not
create a cleanup obligation.

Design and write for a mostly linear reading path:

- Give each function one job and separate deciding what to do from performing
  side effects.
- Keep I/O and error translation at boundaries; keep the central transformation
  logic small and testable.
- Prefer guard clauses, focused helpers, and data-driven dispatch over nested
  branch ladders.
- Treat tests as a design signal: if one behavior needs a large matrix of
  unrelated setup, the production function may own too many responsibilities.
- When protocol conversion or a state machine is honestly complex, explain the
  reason in the pull request instead of contorting the code to lower a score.

The advisory attention threshold is 15 and is owned by `complexipy.toml`. A
changed function above that threshold—or one that becomes materially harder to
follow—should prompt review and usually a preparatory refactor, but the report
never fails CI. Run the same comparison locally with:

```bash
.venv/bin/python scripts/report_cognitive_complexity.py origin/main
```

To inspect only uncommitted work against the current commit, run:

```bash
.venv/bin/python scripts/report_cognitive_complexity.py HEAD
```

## Static typing

`mypy.ini` defines the reproducible strict production-code inventory and enables
Pydantic's supported plugin. The historical codebase is not expected to be
globally clean yet. Treat each diagnostic as evidence to investigate: change
runtime code only for a demonstrated behavioral defect or an unsound public,
wire, persistence, or authorization boundary. Do not add casts, assertions, or
broad ignores merely to reduce the count.

Run the full inventory with:

```bash
.venv/bin/mypy
```

New or extracted boundary modules should pass strict mypy in isolation before
their package is added to a blocking gate.

## String enums

Define enums whose values are strings as `enum.StrEnum`, never as
`class X(str, Enum)`. On Python 3.11 and later, `str()`, f-strings and
`format()` of a `(str, Enum)` member give the qualified name
(`RoutingType.ALL_AGENTS_ROUTE`) instead of the value (`++<all>++`), so such
text silently differs from the JSON, database and NATS value; this put the
qualified name into a NATS header and into task history shown to models. A
`StrEnum` member is its value everywhere. `tests/test_enum_conventions.py`
checks Git-tracked Python sources for `(str, Enum)` classes. Installed libraries
and virtual environments are outside this project convention.

## Pull requests

- Explain the problem, design, behavioral impact, and verification performed.
- Add tests that exercise both successful and failure paths.
- Keep structured logging useful and avoid `print` in runtime code.
- Preserve structured Pydantic JSON contracts for inter-agent messages.
- Confirm that generated files and local configuration are absent from the
  proposed Git tree.

By contributing, you agree that your contribution is licensed under the
Apache License 2.0.

## Local CI runners

Security coverage runs the hermetic test profile. Tests marked `live` or
`external` need their dedicated service jobs; the NATS job starts a broker
before running live message and permission tests.

All workflows select the local Linux runner, including audits, docs, and publishing. Install the reviewed job hooks before enabling its labels. Dependencies, build output, and job Docker resources are reclaimed after each job; failed cleanup blocks the next job. [Local CI](docs/LOCAL_CI.md) describes the shared Windows, macOS, and WSL 2 hosts and their setup. The runtime remains a Linux container on all three host platforms.
