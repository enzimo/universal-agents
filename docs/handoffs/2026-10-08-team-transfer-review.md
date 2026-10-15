# Team transfer review and fixes

The review traced export, archive verification, section conversion, import
planning, database and control-file writers, startup guards, and the broker's
credential transfer routes. The requested workflow is a browser review with
editable schedule Markdown, selection of individual records, and later batches
from the same backup.

## Findings corrected

| Problem | Result after the change |
| --- | --- |
| PostgreSQL writers could use a different connection than bootstrap and launch. | Planning resolves the destination connection from environment, environment file, or secret file. All writers and subsequent launch use it. A first import refuses an occupied database. |
| Active global users could regain active membership in a team that disabled them. | Team membership standing is preserved separately from global user standing. |
| External NATS credentials and custom secret variable names could be omitted. | Both are carried by the operator-secret section. Destination instance paths replace source-root paths; obsolete absolute paths are refused. |
| Large credential imports exceeded aiohttp's ordinary request limit. | Only operator transfer routes accept archive-sized requests. A request over 1 MiB is covered by a broker integration test. |
| Parent grants could be written after child grants. | Dependency ordering writes replacement parents before their children and refuses missing dependencies. |
| Direct process startup could bypass an unfinished-import check. | The central process configuration loader checks the import marker. Failed later batches require restoration of the pre-batch backup. |
| The broker rejected previous credential section versions before conversion. | The broker uses the shared, bounded section conversion mechanism before applying records. Secrets remain broker-owned. |
| Old bootstrap model defaults could override a new release's defaults. | Unchanged bootstrap overrides are omitted; actual edits travel. Previous-version full overrides start unselected with an explicit review warning. Later selected model settings refresh untouched destination bootstrap overrides. |
| A roster could refer to rejected custom definitions. | Roster validation uses only accepted definitions plus definitions already present in the destination. |
| Onboarding could silently omit nested YAML settings. | Unconsumed settings are explicitly reported. |
| Transfer schemas tracked wrappers without all persisted payload contracts. | Payload schemas are tracked by section. A frozen fingerprint ledger catches contract changes even after schema regeneration and requires a section-version change. |
| Schedules were imported directly without editable review or exact confirmation. | Schedule configuration travels as Markdown with one YAML block. Each selected schedule needs confirmation after its latest edit; IDs, owner, routes, and approval provenance remain fixed. |
| Partial imports could omit the records a schedule needs to run or deliver. | Validation checks its owner, session, objective, worker definition, saved worker binding, and saved delivery target. Dependent access grants are checked after schedule decisions. |
| Editing the repeat count retained the old remaining run count. | An unchanged repeat count preserves progress. An edited count becomes the actual remaining count; the page shows the backup's progress. |
| Repeated YAML fields could silently discard an edited setting. | Editable setting YAML and schedule YAML reject duplicate fields. |
| Later memory imports could mistake a newly allocated destination ID for an imported source ID. | The receipt tracks source identities, so reverse-order memory batches do not lose records or create duplicates. |
| Later batches could overwrite destination edits, bootstrap files, or newly created sign-ins. | Completed source identities are skipped, edited destination control files and existing credentials are preserved, and untouched bootstrap files remain available for later selection. |
| Validation initialized or altered destination storage. | Destination snapshots use read-only repositories and connections without schema initialization. Database locations and secret sources cannot change in later batches. |
| Concurrent batches or external editor saves could change what was applied. | Import locks protect writes and receipts. Runtime locks require stopped noncredential imports. Validation and compilation use one captured set of edited bytes, and import checks that exact review digest again. |
| Credential notice failures could imply that a stored credential had failed. | Storage outcomes stay accurate, notice failures are logged and reported, and partial credential failures remain individually retryable. |
| Later account links compared only allocated IDs and could revoke a newer destination link. | Existing links match by team, owner and tool while source IDs remain the selection and receipt identity. |
| Later people imports ignored chat accounts already linked in the destination. | All active destination bindings participate in planning, including removed people and people without memberships, so collisions are refused before any writes or unfinished marker. |
| The PostgreSQL emptiness check used the raw DSN while writers could use the bundled address. | The check resolves every effective writer target under the planned environment and checks each distinct database, including activity storage. |
| Whitespace-only memory edits passed planning and failed in the repository. | The shared memory planner refuses text empty after trimming, keeping both memory-import paths consistent. |
| Syntactically valid but impossible cron dates failed only during writing. | Schedule planning computes a next run using the store's timezone rules before writes start. |
| Edits during validation or saving could enable an import of older saved text. | Ordered saves, pending-save and dirty-state guards, and edit revision checks prevent stale responses from enabling import. |
| Malformed editable YAML returned a server error containing parser diagnostics in logs. | Parser failures become a validation error with line and column and recovery guidance, without the private input excerpt. |
| An external file save could change the text a browser confirmation approved. | Confirmation hashes the exact submitted text; later file changes require fresh confirmation. Saving also clears the visible checkbox. |
| Updating to current `main` changed the configuration contract's default assistant name. | Settings section V2 tracks the new contract and converts V1 records unchanged: explicit names are preserved, omitted names use this release's default. The V1 fingerprint remains frozen. |

The implementation lives in [team_transfer](../../team_transfer/) and the
[broker transfer routes](../../secure_tool_broker/credential_transfer_api.py).
Selection and schedule-confirmation rules are recorded in
[ui/DESIGN.md](../../ui/DESIGN.md). The
[operator guide](../../user_docs_source/team-transfer.md) gives the commands,
browser workflow, later-batch restrictions, and recovery instructions.

## Verification

- 185 tests passed for transfer, onboarding HTTP routes, and architecture
  boundaries. Coverage includes real database writes, edited schedules, exact
  confirmations, read-only validation, later batches, previous-version
  conversion, schema fingerprints, and an actual old/new broker process transfer.
- After rebasing onto current `main` on October 9, the transfer suite again
  passed all 185 tests. Another 92 related runtime, storage, enum-convention,
  and memory-privacy tests passed; three live PostgreSQL tests were
  skipped because `LIVE_TEST_POSTGRES_DSN` and a database service were unavailable.
- Ruff, transfer schema checks, JavaScript syntax checks, and Git whitespace
  checks passed. The dependency files remain unchanged: existing libraries cover
  the feature without another browser framework or runtime dependency.
- Browser interactions were checked with the actual page assets and a simulated
  API; the HTTP handlers have separate integration coverage. A full browser-to-
  local-server run could not be completed in that initial pass because the
  collaborative browser could not reach the workspace loopback server.
  PostgreSQL and Sealbox were initial live-test gaps; the process test exercises
  the supported file credential store. The follow-up evidence below supersedes
  those browser and Sealbox gaps.

After the PR review, regression tests additionally cover newer destination links,
chat collisions across current and departed people, effective bundled/activity
database targeting, invalid later edits without startup-blocking writes, malformed
YAML correction, and confirmation tied to submitted text. Eight browser tests use
the actual page script to cover delayed validation, ordered successful and failed
saves, queued edits restored to an earlier value, and visible confirmation state.
These browser tests run in CI.

The final local follow-up suite passed 332 Python tests, including real Sealbox
checks; three PostgreSQL tests were skipped because no local database DSN was
available. Nineteen browser-script tests (eight transfer and eleven existing
settings tests), Ruff, schema checks and Git whitespace checks passed. The
changed-function complexity review kept the new validation helpers small;
the browser application's aggregate score reflects its shared gate and separate
route handlers. Dependencies and lock files need no change for these repairs.

A live T3 browser run against the actual review server verified stale validation
rejection, malformed YAML feedback, correction and import, and the saved target
model. A temporary test-only relay translated the remote browser origin into the
local origin; the product's loopback and origin guards stayed unchanged.
Real Sealbox tests now pass locally. Live PostgreSQL depends on the CI service;
local target-selection regressions use real configuration resolvers with substituted
database connections.

Integration tests are the main confidence source because correctness depends on
persisted records, routing, credentials, and ordering across sections. Unit tests
cover conversion and deterministic validation rules. Real service tests are
still required for PostgreSQL deployment acceptance.

The complexity review split record selection, dependency checks, and control
reference validation into focused helpers. Browser route handlers intentionally
share one token, operation lock, and validated digest in their application
closure; the aggregate complexity score includes these separate handlers.

## Operator consequences

Writing through `team import` now requires `--review`; the browser command is
`team review`. Credential import also requires a saved selection. Section
versions changed for schedules, control files, operator secrets, and broker
credentials, with the previous versions supported through the deliberate
transfer conversion step.

The first batch requires a fresh instance and an active owner. Further batches
use the same authenticated backup and cannot replace imported identities.
Noncredential batches require a stopped destination runtime; credential batches
require its running broker. Cross-store imports are not one database transaction:
an interrupted write blocks startup and requires restoring the pre-batch backup.
Edited worker instructions can require the normal worker review after startup;
browser confirmation does not grant new worker authority.

The bundled runtime-operations skill was updated. The sibling shared skill
repository has no applicable team-transfer instructions to update.
