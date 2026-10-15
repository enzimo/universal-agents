# Build Options

## Build Types

### Build Your Own Docker Image

The fastest way to get started (aside from using a pre-built container) is to build the `Dockerfile.bundle` with all services running in a single docker image.

#### Steps to Build

Maintained Docker builds default to the Sealbox credential CLI. On Apple
Silicon, Docker defaults to an arm64 image; if you explicitly select Bitwarden,
the pinned CLI version may need Node.js/npm because Bitwarden does not publish a
durable official Linux arm64 standalone archive for that release. Photon
iMessage also requires Node.js for its bundled Spectrum sidecar. Enable Node.js
explicitly with `--nodejs`, or provide `BW_FILE_URL_ARM64` for a matching
standalone Bitwarden CLI archive. See `/scripts/build_docker_image.sh` for all available build options. A recommended first build would look similar to the following (includes all modular components plus telegram, web, iMessage and discord integrations):

```bash
./scripts/build_docker_image.sh --bundle --latest --credential-clis sealbox --nats --no-postgres --sqlite --nodejs --no-cache ua-bundle:latest
```

Maintained Docker builds default to `STRIP_SOURCE_CODE=true`: after generated
docs are built, the image compiles app-local Python files to legacy `.pyc`
bytecode and removes `.py`/`.pyi` files from `/app`. Use
`./scripts/build_docker_image.sh --keep-source-code ...` or
`--build-arg STRIP_SOURCE_CODE=false` for a debugging image with Python source
left in place.

### Quickstart

Once the container is built, see [QUICKSTART.md](QUICKSTART.md) on how to get started.

### Refreshing External Skills at Docker Startup

Browser onboarding now persists public skill repository URLs under
`workspace.external_skill_repositories`. To refresh and fully replace the
externally managed skill directories before any agents start, launch with:

```bash
docker run ... your-image run --refresh-external-skills
```

For an older instance that does not yet have repository URLs in
`instance_config.yaml`, provide one or more launch-only overrides:

```bash
docker run ... your-image run \
  --refresh-external-skills \
  --skills-repository https://github.com/enzimo/ua-skills
```

Set `workspace.refresh_external_skills_on_start: true` for every-start refresh,
or set `TEAM_REFRESH_EXTERNAL_SKILLS=true` as a container environment override.
Refresh clones all repositories successfully before changing the mounted
workspace, completely replaces imported skill directories, removes imported
skills deleted upstream, and preserves unrelated local-only skills. An explicit
refresh failure aborts startup rather than silently using stale instructions.

### Experimental native installation

UA can run directly on Linux, macOS, or WSL2, but native installation is
experimental. It depends on host Python, a native build toolchain, Git, native message broker, credentials store,
and other system tools, so **only attempt this if you're an advanced user**.

```bash
curl -fsSL https://raw.githubusercontent.com/enzimo/universal-agents/main/install.sh \
  | bash -s -- --native
```

The native installer creates a constrained virtual environment, installs a
local message broker binary, builds the Cedar and Sealbox binaries, and installs the `ua`
launcher under `~/.local/bin`. Node.js 24 or newer must already be installed
to enable Photon/iMessage natively. Run `ua` to start browser onboarding or the
onboarded runtime in the foreground.
