# First-use model-key storage fix

The defect remains in fetched `origin/main` at `a44c4530`. Work was isolated
in `fix/model-key-first-use` to preserve unrelated changes in the original
Universal Agents checkout. No release or deployment was requested or made.

## Root cause and reproduction

`ModelKeyStore.save()` reads the owner's existing credential-state record
before writing so it can retain keys for other endpoints. Sealbox provisions
an owner's tenant only on an authorized write. An unprovisioned owner's read
returned `sealbox_tenant_not_provisioned` as a failure, preventing that first
write. The same read blocked the personal-to-team selection path.

Regression tests were added and run before implementation changes. A real
Sealbox instance and authenticated broker reproduced the signed lookup error:

```text
HTTP 503: The broker could not read the model keys from its credential store.
No Sealbox tenant has been provisioned for this credential owner.
```

Fresh team and personal reads and wrapped credential-error classification
also failed on the unchanged implementation. Existing authentication handling
incorrectly treated Sealbox `auth_required` as an absent record.

## Changes and rationale

- Credential-state reads treat only Sealbox's unprovisioned-tenant and missing
  secret results as absent. Reads do not provision tenants. Existing authorized
  writes retain ownership of provisioning.
- Sealbox authentication, tenant-material and unexpected storage failures stay
  visible. File fallback cannot hide a Sealbox error or redirect a failed write
  into a replacement file. This deliberately tightens existing fallback behavior;
  configuration fields, defaults and stored schemas are unchanged.
- Tenant resolution parses both RSA PEM files and verifies that they match.
  Live testing showed why this is needed: Sealbox reads could succeed with a
  corrupt public file, and writes could succeed with a corrupt private file.
  Neither operation now proceeds with a damaged pair. No files or mappings are
  repaired, replaced or removed by error handling.
- Provider classification refuses typed `ModelKeyError` failures throughout
  an exception chain, including OpenAI and Strands wrappers. The failover entry
  point also refuses a throttling wrapper whose cause is a credential failure.
  Strands' `original_exception` field is recognized when Python supplied no
  cause or context.
- The common error base lives in dependency-free `model_key_errors.py`.
  A first implementation using a local import failed the existing import-graph
  check; extraction removes the cycle while existing callers keep the same
  error type.
- The displayed error identifies the broker and credential store and directs
  administrators to restore existing missing or damaged tenant files from a
  backup. It does not describe provider overload.

Public operator documentation, configuration descriptions, the bundled runtime
operations skill and the canonical shared onboarding reference were updated.
The owning credential and runtime architecture documents in `../ua-architecture`
were synchronized. No UI layout or design rules changed.

## Verification

Final verification used Python 3.13.15 in this worktree's `.venv`, installed
from `requirements-dev.lock.txt` with hash verification. Earlier reproduction
also ran in the original workspace environment, which uses Python 3.14.

644 distinct targeted tests passed across three runs:

- 125 core credential regressions, provider classification, failover, model-key
  runtime and architecture checks.
- 477 existing broker, credential, onboarding, gateway, provider-adapter,
  watchdog, personal-model and failure-reporting tests.
- 42 configuration and settings tests.

The core run includes all 17 live Sealbox tests. They verify first team and
personal saves, no tenant creation on reads, onboarding handover consumption,
authenticated signed lookup, authorized team selection without a personal
tenant, refused team selection, personal ownership, forged owner claims, and
broker/Sealbox restart persistence. Missing token/public/private files, corrupt
files and mismatched RSA pairs block reads and writes. File digests and tenant
mappings remain unchanged after refusal. Model-key canaries are absent from
captured UA logs with INFO logging enabled.

Broker HTTP 500, 503 and 504 are tested through the HTTP model-key client,
directly and under OpenAI, Strands and throttling wrappers. Failover checks
verify no model switch, retry outcome, requeue, provider demotion or state file.

Ruff, strict documentation builds, runtime-contract schema checks, dependency
checks and `git diff --check` pass. The architecture import graph is acyclic.
Changed production functions remain below the complexity advisory threshold.
Scoped mypy reports the same 13 existing errors as unchanged main. The shared
error, classifier and tenant modules pass scoped strict typing separately.
Dependency inputs and locks remain unchanged; retain their tested versions
for this fix and make any refresh a separate compatibility-tested change.

Test receipts are `/tmp/ua-model-key-final-core.xml`,
`/tmp/ua-model-key-final-existing.xml` and `/tmp/ua-model-key-final-config.xml`.

## Remaining gaps and review

Goal alignment is satisfied for the requested local storage and error paths.
Consequence risk is moderate because credential state is shared and Sealbox
file-fallback behavior is deliberately stricter. Evidence is strong at the
real broker and Sealbox boundaries. The final review found no unresolved
implementation defect in the tested paths.

Docker is not installed, so container acceptance was not executed. The Python
repository has no runtime container acceptance runner to extend. The existing
private combined-build acceptance runner was inspected and already requires
onboarding save and signed lookup before and after restart; it was unchanged.
Its health checks are not counted as storage verification here.

No `config.yaml` or `debug.env` was available in the worktree or original UA
checkout, and the private testing configuration was absent. Hosted provider
smoke tests were attempted with all opt-in flags enabled: 18 skipped for missing
configuration and one for an unavailable OpenAI key. Local HTTP provider-adapter
tests passed. One optional Bitwarden/OpenShell test was skipped; the `bw` CLI
and unlocked account are unavailable. No key or private environment file was
printed, and no external credential store or live account was changed.
