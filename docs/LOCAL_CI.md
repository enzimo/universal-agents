# Local CI

Tests, dependency audits, source releases, and AMD64 container
builds use `[self-hosted, Linux, X64, ua-build, ua-linux]`. ARM64 container
builds use `[self-hosted, macOS, ARM64, ua-build, ua-macos]` with a native
ARM64 Docker daemon. A final Linux job combines both immutable platform digests
before applying version tags and the release attestation policy. Native builds pass
architecture-specific digests through job outputs, not Actions artifacts. Docker
build-record uploads are disabled so an Actions storage quota cannot prevent image
publication. The manifest validator rejects missing digests or unexpected platforms.
After attestation succeeds or an exact-tag waiver applies, publishing attaches
`container-image.txt` and its checksum to the GitHub Release. The file records the
source commit, both platforms, and an immutable image pull command. Publishing triggers remain unchanged; a readiness check
must use the explicit build-only mode when dispatching the container workflow.

GitHub Actions artifacts and logs use seven-day retention across the organization.
The skill-installation canary and fast tests must pass, but archiving their evidence
runs afterward and reports upload failures as warnings. Documentation pull requests
test and build the site without uploading it. Main pushes, stable release tags and
manual runs on main or stable tags upload the Pages site, and that upload must
succeed before deployment. Container builds
and image publication do not require these stored artifacts.

The fast job allows 75 minutes for setup, the complete hermetic suite, and
mandatory runner cleanup. A successful suite took 50 minutes on the shared
runner; setup and cleanup brought the job to 63 minutes and exceeded the
previous one-hour deadline. It stops at the first failure so its traceback is
reported before a later deadline can hide it, and reports the slowest tests
after completion.

The runtime produces Linux amd64 and arm64 container images that run under
Docker on Windows, macOS, and Linux. Native desktop installers belong to the
sibling Universal Agents Desktop repository. Linux CI can run inside WSL 2,
but requires its own Linux Actions runner. A Windows runner cannot execute
GitHub Linux container actions directly.

The shared host setup uses the `Universal Agents` organization runner group
for private repositories, with repository allowlists enforced by installed hooks.
One Windows host runs a Windows runner and two Linux runners; the Linux runners
use a dedicated `Ubuntu-24.04` WSL 2 distribution and its own Docker daemon. A
Mac runs the ARM64 runner.
Mac and Linux runner registration and group access require organization runner
administration permission. A maintainer can receive it through a delegated
runner administration role while their base organization membership remains
`member`.

## Public documentation publishing

`publish-docs.yml` uses GitHub-hosted `ubuntu-24.04` runners for both build and
deployment. The repository is public, while the managed local runners deliberately
accept only private repositories. Keep those local guards intact; public pull
requests must never execute on personal build hosts.

The documentation job installs RTK 0.51.0 from its upstream Linux release with a
pinned SHA-256 through a JavaScript action before running any shell steps. Those
steps retain the RTK shell wrapper. Python 3.13 and Node.js 24.20.0 are set up by
pinned actions. Hosted runners do not use the local cleanup hooks, build caches,
`UA_CI_PYTHON`, or the local Python relocation helper. Their machines are disposable.

In the repository's **Settings > Pages > Build and deployment**, set **Source** to
**GitHub Actions**. The existing workflow builds the MkDocs sources under
`user_docs_source/`; no suggested Jekyll or static HTML template is needed.

Pushes to `main` and stable `vX.Y.Z` tags publish after all documentation checks and
the strict build pass. To publish the current main manually, open **Actions >
Publish Documentation > Run workflow**, select `main`, and run it. Pull requests
validate with read-only repository access and never upload or deploy. Manual runs
on feature branches or pre-release tags cannot deploy. The `github-pages`
environment must allow `main` and stable release tags if deployment protection
rules are configured. The deployed quickstart is under `/universal-agents/quickstart/`.

Verify workflow changes with `tests/test_publish_docs_workflow.py` and
`tests/test_ci_workflows.py`, the served-documentation tests listed in the workflow,
`node --test tests/setup_guide.test.cjs`, and a strict Pages build using
`scripts/build_user_docs.py --github-pages`. The event/ref matrix checks that both
public and private repositories publish only the intended refs. The real Actions
run and an HTTP check of the published quickstart complete deployment verification.

## Toolchains and isolation

Provision RTK 0.48.0, system Python 3.12+, Node.js 22+, Git, Rust 1.95.0,
Docker, Buildx, QEMU/binfmt, and GitHub CLI 2.100.0. Sidecar jobs select Node.js 24.20.0. Runtime
installation checks use Python 3.12 and 3.13; other Python CI selects 3.13.
The isolated SkillSpector environment uses the provisioned system Python from
`UA_CI_PYTHON`. Its restricted subprocess environment removes `LD_LIBRARY_PATH`,
which GitHub's downloaded Python needs to find its shared library. CI checks
the scanner without that variable before running the live installation canary.
Rust setup uses `--no-self-update` and the job's `RUSTUP_TOOLCHAIN`, leaving
the account's default toolchain unchanged. Each runner owns its work directory.

Install reviewed `.github/runner` hooks outside both the checkout and runner
application directory, using system Python. Restart the idle runner afterward.
Do not manually set `UA_CI_HOOK_VERSION`; jobs require the started hook to
export it. The implementation is mirrored in the desktop repository and must
remain synchronized when its contract changes.

```sh
rtk proxy python3 .github/runner/provision_sccache.py /opt/ua-ci-tools
rtk proxy python3 .github/runner/install_hooks.py \
  --sccache /opt/ua-ci-tools/sccache \
  --runner /opt/actions-runner \
  --destination /opt/ua-ci-hooks \
  --repository enzimo/universal-agents \
  --repository enzimo/universal-agents-desktop
rtk proxy python3 -m unittest discover -s .github/runner -v
```

Dispatch **Local runner preflight** after registration and hook installation.
It builds and runs disposable amd64 and arm64 Docker images. Check the completed hook's logs
and confirm the builder, image, checkout, and disposable caches were reclaimed. The
`exercise_hooks.py` utility tests the installed wrappers locally without
registering a runner or using GitHub credentials.

## Reclamation

Python packages install into a disposable `.venv`. Each job has separate
Cargo, XDG, Docker CLI, and temporary directories. Compiler and npm/pip download
caches have the separate bounded retention policy below. NATS and PostgreSQL
containers use the exact job ownership label and dynamically assigned loopback
ports. The launcher passes each selected port explicitly to Docker so NATS
retains it through restart tests. It retries only a verified port-binding
collision. Documentation image builds use a dedicated Buildx builder, avoiding the
shared default builder's unbounded cache.

Completed hooks run after artifact uploads and action post steps. They remove
the job's containers and anonymous volumes, built images, Buildx builder/cache,
checkout, environments, outputs, downloaded actions, and setup-action tool
cache. They do not prune unrelated Docker resources or access UA persistent
data. Fixed system toolchains and the small shared upstream Docker image set
are retained. Audit the retained images when upgrading their versions.

Interrupted-job manifests are retried before the next job. Cleanup failures
block new work. A minimum 30 GiB free-space gate protects the work and state
filesystems; it is an admission check, not a per-job quota. Uploaded test evidence
expires after seven days, while runner diagnostic logs older than seven days
are removed at job start.

Temporary files can disappear during a cancelled process's shutdown. Cleanup
accepts an already-removed entry on Python 3.12 as well as newer Python versions.
It retries deletion of read-only entries, but other errors retain the manifest
and block the next job.

WSL deletion reclaims space inside Linux. Enable `fstrim.timer` and verify sparse
VHD support to return deleted blocks to Windows. Do not shut down unrelated WSL
distributions or compact Docker Desktop's VHD during jobs. Keep WSL running via
a launcher at Windows logon so its Linux runner can receive work.

The [desktop local CI guide](https://github.com/enzimo/universal-agents-desktop/blob/main/docs/LOCAL_CI.md)
covers native Windows/macOS prerequisites and the shared host setup in detail.

On WSL the hook installer requires `--reserve-path /mnt/c`, or the mount for
the Windows drive actually holding the distro VHD. This checks physical Windows
free space as well as Linux filesystem space before admitting a job.

### WSL disk maintenance

The inspected WSL 2.7.13 installation rejects sparse VHD activation because of
potential data corruption. Do not use its `--allow-unsafe` override. Job cleanup
still frees blocks for reuse inside the distro. A VHD can retain its previous
peak size on Windows until offline compaction.

Keep `fstrim.timer` enabled. During a maintenance window, drain and stop the Linux
runners, trim their filesystem, then stop only the dedicated distro. Wait for
Windows to release the VHD. From an elevated Windows session, run:

```powershell
rtk proxy powershell -NoProfile -ExecutionPolicy Bypass -File .github/runner/compact_wsl.ps1 -Distribution Ubuntu-24.04
```

The helper resolves that distro's VHD from its registration, refuses a running
distro, and invokes DiskPart only for that file. It neither stops other distros
nor forces sparse mode. If Windows still has the file open, it fails. Retry in
the next idle maintenance window after the file is released. Schedule this
maintenance together with the WSL runner launcher when activating the runner;
keep the Windows disk reserve check enabled between maintenance windows.

## Validation record

The September 2026 local validation passed 3,321 fast-profile tests on Python
3.12, all 54 live NATS tests, live PostgreSQL validation, both documentation
image stages, and amd64/arm64 Docker smoke builds. The desktop project also
built native installers on all three hosts. The activated runners and
GitHub verification are recorded below.

## Activated runners

Every runner uses the reviewed labels and installed cleanup hooks. Keep runner
names, host names, accounts, hardware details and host paths in each host's own
setup notes, not in this repository.

The Mac uses a launch agent. The Windows runner uses an automatic Windows
service. A Windows scheduled task starts WSL at Windows boot without an
interactive login and keeps the distro running. Both Linux systemd
services start automatically inside that distro. Physical VHD compaction needs
an idle maintenance window; drain and stop both Linux runners first.

Windows hooks inherit `PSExecutionPolicyPreference=Bypass` from the runner's
`.env`. This changes the runner process and its children, not account-wide
PowerShell policy. GitHub's hook invocation does not supply an execution-policy
flag, so direct SSH tests must exercise the same environment.

### Two Linux jobs on one host

Each runner accepts one job at a time, so the two Linux runners provide two
concurrent AMD64 job slots. Both have `[self-hosted, Linux, X64, ua-build,
ua-linux]` labels; workflows can use either without changes. A runner may carry
one extra label for an explicitly targeted maintenance check.

Give each Linux runner its own application and work directory (with its
`_work`), its own hook directory and its own systemd service
(`actions.runner.<organization>.<runner name>.service`).

Each hook directory owns separate job state and retained caches. Each runner
has a 10 GiB cache budget, seven-day expiry, and 30 GiB reserve checks for Linux
and `/mnt/c`. Register each runner independently; never copy another runner's
credentials or share work, state, or cache directories.

The runners share one CI account, fixed toolchains, Docker daemon, and host
resources. Docker resources carry each job's ownership label, service ports are
assigned dynamically, and compiler-cache servers use separate Unix sockets.
Each job keeps the existing two-worker Cargo compilation limit. Size the WSL
memory limit and the number of Linux runners to the host; adding a runner
increases job concurrency within the host's resources.

When the second Linux runner was added in October 2026, it passed 36
lifecycle/cache tests, a real Rust cache-hit check, and AMD64 plus emulated
ARM64 Docker builds. Its installed completed hook reclaimed the test workspace,
disposable caches, and Docker resources. GitHub then ran security checks and
live NATS tests on the two Linux runners at the same time. Each host keeps its
own setup and validation records outside this repository.

## Native container architectures

Cold ARM64 compilation under QEMU was CPU-bound for more than 30 minutes before
finishing the first Rust and Python dependency stages. Container CI therefore
builds AMD64 on WSL and ARM64 in a dedicated Colima `ua-ci` Linux VM on the Mac. WSL still
supports lightweight ARM64 emulation, verified by preflight.

Install the Mac hooks with `--docker-host` pointing to the provisioned Unix
socket and `--colima-profile ua-ci`. Each job uses an isolated Docker CLI config, so it cannot
rely on the account's named Docker context. Do not copy registry credentials or
change the account's default context. GitHub provides a short-lived job token
for publication. Keep the Colima daemon available at runner startup.

Registry logins stay in the job's own Docker settings file, never in a system
credential store. Docker otherwise hands a login to any installed helper, such
as `docker-credential-osxkeychain` from Homebrew. The runner service cannot use
the macOS keychain even while it is unlocked on screen, so the login fails with
`User interaction is not allowed (-25308)`. Before its first Docker command,
each publishing job writes `config.json` in its private settings folder (mode
0600, folder 0700). It names an empty helper for the registry, which makes
Docker keep the login in that file. After logging in, the job confirms the
login is there. The token in the file is the job's own `GITHUB_TOKEN`: it stops
working when the job ends, the login action logs out in its post step, and the
completed hook deletes the folder. Docker's warning that the credentials are
stored unencrypted is expected. Only the runner account can read the file, and
that account already holds the token while the job runs.

Container jobs check the native daemon architecture and require 30 GiB free
inside Docker's filesystem as well as the host reserve checks. Each platform
uses a separate registry cache tag during publication; build-only runs publish
neither images nor caches. After both builds succeed, the merge job verifies
that the digests contain exactly Linux AMD64 and ARM64 before publishing tags.
The dedicated Colima VM has 4 CPUs, 8 GiB RAM, and a 100 GiB data disk. It has
no host directory mounts, SSH-agent forwarding, or emulation enabled. The
runner launch wrapper starts this profile before accepting jobs, without
changing the account's active Docker context. Cleanup trims the CI VM's Docker
filesystem after removing its builder. The existing default Colima profile is
preserved, including its unrelated images, containers, volumes, and caches.

## Bounded CI monitoring and release builds

Use `.github/scripts/watch_ci.py` to monitor a commit without an interactive
assistant loop. It polls GitHub every 30 seconds, saves job logs once, writes
`summary.json`, and exits on success, failure, or its default 45-minute deadline.
Exit codes are 0 (success), 1 (failed job), 2 (deadline), and 3 (API error).
It neither retries jobs nor publishes releases. A timeout leaves running jobs
under their workflow deadlines; inspect the summary before deciding to cancel.

```bash
rtk proxy python3 .github/scripts/watch_ci.py --repo OWNER/REPOSITORY \
  --sha FULL_COMMIT_SHA --workflow "WORKFLOW NAME" --output /tmp/ua-ci-result
```

Repeat `--workflow` for every required workflow. Pass `--run RUN_ID` instead to
track a known release run. Keep this process outside the runner work directory;
job cleanup removes that directory. Review failures or the final summary instead
of repeatedly fetching logs. Remove downloaded evidence when it is no longer needed.

Documentation-only commits skip runtime test profiles and native desktop builds;
the documentation workflows still validate the guides. Runtime documentation CI
also exercises the served pages, onboarding links, channel references, and guide
routes before uploading the site, so stale documentation assertions fail in the
small documentation job rather than waiting for the full runtime profile. Dependency audits run for
dependency changes and weekly. Native AMD64/ARM64 runtime images build in parallel
on WSL 2 and the Mac. Desktop release tags promote five installers from successful
main-branch builds of the exact commit, verifying GitHub artifact digests. Tag only
after those builds succeed and before their seven-day artifact retention expires;
the release fails immediately if any build is missing, failed, or unfinished.

The disposable Linux `setup-python` executable gets a relative library search
path through `patchelf` before creating virtual environments. This lets restricted
child processes load Python without `LD_LIBRARY_PATH`, preserving their environment
sanitization. Provision `patchelf` on the Linux runner; system Python runs the helper
and is never modified. The adjusted toolchain is reclaimed after the job.

## Bounded build caches and faster feedback

The installed hooks use pinned sccache 0.17.0 for Rust compiler output and retain
npm/pip download caches. npx execution environments and npm logs are discarded
after each job, including their generated executable symlinks. The provisioner verifies a pinned upstream archive
SHA-256 before installing the binary. Cargo target output, Python environments,
Docker configuration and credentials remain disposable. No remote cache service
or GitHub cache storage is used. Rust incremental compilation remains disabled.

`--cache-gib 20` limits the combined retained caches for both repositories on
one runner. `--cache-days 7` expires scopes unused for seven days. The hooks
check these limits at job admission and completion, evicting whole scopes in
least-recently-used order. They also evict caches when a filesystem falls below
the existing 30 GiB reserve, including the WSL backing drive. These are retention
limits, not a filesystem quota during an active job. Each compiler cache also
has sccache's 3 GiB limit. Expiry is enforced when the runner next handles work.

Repositories and refs use separate hashed cache directories. Only main pushes
and manual main builds share the main cache. Pull requests, feature branches,
and release tags have separate scopes and never populate main's cache. There
is one cache root per runner, with an ownership marker; unknown directories,
unexpected files, symlinks and junctions are rejected before eviction. The
metadata files a desktop's file browser leaves in a folder it opens
(`.DS_Store` and `._*` on macOS, `Thumbs.db` and `desktop.ini` on Windows) are
removed with a warning instead, since browsing the cache root on a runner used
to stop the next job. Never share this directory between runner processes or
remove its ownership marker.

Each job starts a dedicated compiler-cache server and stops it before cleanup
or eviction. Unix hosts use a socket inside job state. Windows reserves TCP
port 48227 for this runner; change `--cache-server-port` if that port is already
assigned. An occupied endpoint fails admission. No personal sccache server is
used or stopped. The completed hook prints cache statistics into the job log.
Local runner preflight proves an actual cache hit after deleting compiler output.

Desktop pull requests still test and compile all platforms, including both
macOS architectures, but omit installer packaging and bundle upload. Pushes
retain installer production for exact-commit release promotion. Manual desktop
workflows expose a `package` boolean, default true; select false for compilation
and tests without installers. Cold and warm runs should be compared separately;
compiler caches do not cache final linking or guarantee a particular duration.

## Repairing a missing release image

Dispatch `publish-container.yml` with `release_tag` set to the existing tag, such
as `v0.6.0`, and `build_only=false`. Run the workflow from a branch containing any
workflow correction. Both build and publication jobs check out the unchanged
release tag; image version tags and source metadata come from that checkout.
Never move the release tag to repair a workflow. `image_tag` remains an optional
additional registry tag for manual builds.

Both native builds must succeed before any combined tags are published. Release
attachments are added only after the configured attestation requirement succeeds.
Private repositories without GitHub Enterprise Cloud require the user's explicit
per-release waiver before setting `PRIVATE_RELEASE_ATTESTATION_WAIVER` to the exact
release tag. An unset waiver never exempts a manual branch build. The workflow
waits up to ten minutes for the independent source-release workflow to create the
release before uploading the image reference. Publication reads its release
scripts from the workflow's own commit, not the release tag, so a repair works
for releases older than those scripts. Repairing a stable release also proposes
it for the desktop launcher's compatibility list (see below).

## Pre-releases and nightly builds

`VERSION` names the newest published release; it changes only in the
`Release vX.Y.Z` commit. A pre-release names the release it leads up to, which
must be the next patch, minor or major version after `VERSION`. With `VERSION`
at `0.9.1`, `v0.9.2-beta.1`, `v0.10.0-rc.1` and `v1.0.0-beta.1` are valid, but
`v0.9.1-beta.1` is not: semantic versioning sorts `0.9.1-beta.1` before the
already published `0.9.1`, so the desktop launcher would never offer it.
`scripts/validate_release_tag.py` enforces these rules for every release
workflow. Its `--kind` option prints `stable`, `beta`, `rc` or `nightly`.

| Tag | Made by | GitHub release | Image tags | Docs site |
|-----|---------|----------------|------------|-----------|
| `vX.Y.Z` | a person, per the release process | release, may become Latest | `X.Y.Z`, `X.Y`, `X`, `latest` | published |
| `vX.Y.Z-beta.N`, `vX.Y.Z-rc.N` | a person (signed tag on `main`) | pre-release, never Latest | `X.Y.Z-beta.N` only | not built |
| `vX.Y.Z-nightly.YYYYMMDDHHMM` | `nightly.yml` | pre-release, never Latest | `X.Y.Z-nightly.…` only | not built |

`N` starts at 1 and has no leading zeros. Generated release notes always start
at the previous stable release, so betas and nightlies never shorten a stable
release's notes. A beta or release candidate in this private repository needs
the same exact-tag `PRIVATE_RELEASE_ATTESTATION_WAIVER` as a stable release.
Beta and release-candidate releases include the source bundle and its checksum,
but no setup-guide attachment: the tracked guide installs the last stable
version. Existing pre-releases are marked as pre-releases and removed from
Latest on a rerun, which also removes any stale setup-guide attachments.

`nightly.yml` runs daily at 03:17 UTC and can be started by hand from `main`.
When `main` has changed since the newest nightly, it creates the pre-release
`v{next patch}-nightly.{UTC minute}` on that commit (notes: "Nightly build of main
at <sha>. Not checked for desktop launcher compatibility; for testing only."),
starts `publish-container.yml` for the new tag, and deletes nightly releases and
their tags beyond the newest seven. It never deletes stable releases, betas or
release candidates. Deleting a release does not delete its GHCR image versions.
Nightly releases have no source archive; GitHub's own source download remains.
Cleanup deletes each old tag before its release. If either request fails, the
release remains discoverable so a later run can retry the unfinished cleanup.

A nightly image is published only after attestation succeeds or a waiver
applies. In this private repository that requires the owner to set the
repository variable `PRIVATE_NIGHTLY_ATTESTATION_WAIVER` to `true`, a standing
waiver for nightly pre-releases only; every waived nightly shows a warning.
Without it, a nightly's container run stops at attestation and its release has
no `container-image.txt`.

The nightly workflow creates tags with its own `GITHUB_TOKEN`. GitHub starts no
workflows for tags or releases created with that token, so `publish-release.yml`
and the tag trigger of `publish-container.yml` never run for a nightly. A
`workflow_dispatch` request is the exception, which is why
`scripts/nightly_release.py` starts `publish-container.yml` explicitly. If that
request or the build fails, the next scheduled run skips the same commit; to
retry, dispatch `publish-container.yml` with `release_tag` set to the nightly tag.

## Desktop launcher compatibility list

The desktop launcher offers only UA versions listed in
`compatible-runtimes.json` on `main` of `enzimo/universal-agents-desktop`. After
a stable release's image and `container-image.txt` are published,
`publish-container.yml` runs `scripts/propose_desktop_compatibility.py`. It
appends the release with the host contract of the newest listed version,
commits that to branch `ua-release/vX.Y.Z` and opens a pull request titled
`Add UA X.Y.Z to the compatibility list`. The launcher repository's check
verifies the digest against `container-image.txt` and tests the published image
against the desktop contract on native AMD64 and ARM64, then merges the pull
request. The tests exercise data and workspace paths, synthetic sign-in import,
onboarding, configured startup, browser routes, restart and data preservation.
Internal source changes alone do not stop automatic approval. Failed runtime
tests, an unknown host contract or a mismatched release digest keep the pull
request open for review. Betas, release
candidates and nightlies are never proposed.

Reruns are safe: a version already listed with the same image needs nothing,
and an existing branch or open pull request is reused. The script fails if the
version is listed with a different image. It skips, with a warning, a release
older than the newest listed version, because the list is append-only.

The step uses the repository secret `DESKTOP_COMPATIBILITY_TOKEN`: a
fine-grained token for `enzimo/universal-agents-desktop` with **Contents:
read and write** and **Pull requests: read and write**. Without the secret the
release still completes and the step shows a warning instead. To open the pull
request by hand from a checkout of `main`:

```sh
rtk gh release download vX.Y.Z --repo enzimo/universal-agents \
  --pattern container-image.txt --dir /tmp/ua-vX.Y.Z
GH_TOKEN=<token> rtk proxy python3 scripts/propose_desktop_compatibility.py vX.Y.Z \
  --container-image-file /tmp/ua-vX.Y.Z/container-image.txt --dry-run
```

Check the printed file and pull request text, then run the same command
without `--dry-run`.
