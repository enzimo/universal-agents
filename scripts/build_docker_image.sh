#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/build_docker_image.sh [image-ref]
  scripts/build_docker_image.sh --bundle [image-ref]
  scripts/build_docker_image.sh --latest [image-ref]
  scripts/build_docker_image.sh --bundle --latest [image-ref]
  scripts/build_docker_image.sh --no-cache [image-ref]
  scripts/build_docker_image.sh --nocache [image-ref]
  scripts/build_docker_image.sh --openshell [--openshell-version <version>] [image-ref]
  scripts/build_docker_image.sh [--platform <platform>] [image-ref]
  scripts/build_docker_image.sh [--credential-clis <list>] [image-ref]
  scripts/build_docker_image.sh [--nodejs|--no-nodejs] [image-ref]
  scripts/build_docker_image.sh [--strip-source-code|--keep-source-code] [image-ref]
  scripts/build_docker_image.sh --bundle [--nats|--no-nats] [--postgres|--no-postgres] [--sqlite|--no-sqlite] [image-ref]

Build the Docker image from the current repository checkout.

Arguments:
  [image-ref]  Docker image reference, including an optional tag. Defaults:
               - standard build: team-assistant:latest
               - bundle build:   team-assistant-bundle:latest

Options:
  --bundle     Build the standalone bundle image from Dockerfile.bundle
  --latest     Also tag the built image as the same repository with :latest
  --no-cache   Build the Docker image without using cached layers
  --nocache    Alias for --no-cache
  --openshell  Install OpenShell CLI and gateway tooling into the image at build time
  --openshell-version <version>
               OpenShell release tag passed to the OpenShell installer. Defaults to latest.
  --platform <platform>
               Target Docker platform, e.g. linux/arm64 or linux/amd64.
               Defaults to Docker's platform selection.
  --credential-cli <name>
               Add one credential-source CLI to the image. Repeatable. Supported:
               bw, op, keepassxc, vault, sealbox. Aliases such as bitwarden,
               onepassword, keepass, hashicorp-vault, and sealbox-cli are
               accepted by the Docker installer.
  --credential-clis <list>
               Comma/space/bracket formatted credential-source CLI list, e.g.
               "bw,op" or "[bw,op]". Use "none" to install no credential-source
               CLIs. Both maintained Dockerfiles default to sealbox when this
               is omitted.
  --no-credential-clis
               Pass INSTALL_CREDENTIAL_CLIS=none.
  --nodejs
               Install Node.js and npm into the image for integrations that
               require a Node runtime.
  --no-nodejs
               Omit Node.js and npm from the image. This is the default.
  --strip-source-code
               Compile app-local Python sources to .pyc and remove .py/.pyi
               files after build-time generation. This is the default.
  --keep-source-code
               Keep app-local Python source files in the image for debugging.
  --nats / --no-nats
               Include or omit nats-server. Defaults to true for both images.
               Python NATS client packages are always installed from requirements.txt.
  --postgres / --no-postgres
               Include or omit bundled PostgreSQL server packages and pgvector.
               Defaults to false for the standard image and true for the bundle image.
  --sqlite / --no-sqlite
               Include or omit sqlite3 CLI/dev packages. Python's stdlib sqlite module
               may still be present in the base image. Defaults to false for the
               standard image and true for the bundle image.

Environment:
  TEAM_ASSISTANT_IMAGE_REPO         Default repo/name for the standard image when [image-ref] is omitted.
  TEAM_ASSISTANT_BUNDLE_IMAGE_REPO  Default repo/name for the bundle image when [image-ref] is omitted.
  INSTALL_OPENSHELL            Set to true to install OpenShell without --openshell.
  OPENSHELL_VERSION            OpenShell release tag passed to the OpenShell installer.
  DOCKER_PLATFORM              Target Docker platform, e.g. linux/arm64 or linux/amd64.
  INSTALL_NATS                 Set to true/false to select nats-server.
  INSTALL_POSTGRES             Set to true/false to select PostgreSQL packages.
  INSTALL_SQLITE               Set to true/false to select sqlite3 packages.
  INSTALL_CREDENTIAL_CLIS      Comma-separated credential-source CLI list passed
                               to Docker. Supported: bw, op, keepassxc, vault,
                               sealbox, all, none.
  INSTALL_NODEJS               Set to true/false to install Node.js and npm.
  STRIP_SOURCE_CODE            Set to true/false to strip app-local Python source after compiling bytecode.
  POSTGRES_MAJOR               PostgreSQL major package to install when selected. Defaults to Dockerfile pin.
  POSTGRES_VERSION             Exact PostgreSQL package version to install when selected. Defaults to Dockerfile pin.
  PGVECTOR_PACKAGE_VERSION     Exact PGDG pgvector package version to install when selected. Defaults to Dockerfile pin.
  SQLITE_VERSION               Optional exact Debian sqlite3/libsqlite3-0/libsqlite3-dev package version. Empty uses current Trixie repository packages.
  NATS_SERVER_VERSION          NATS server release tag to install when selected. Defaults to Dockerfile pin.
  BW_VERSION                   Bitwarden CLI release version used for the default official GitHub release archive URLs.
  BW_FILE_URL                  Override Bitwarden CLI zip URL for all image architectures.
  BW_FILE_URL_AMD64            Override Bitwarden CLI zip URL for linux/amd64 images.
  BW_FILE_URL_ARM64            Override Bitwarden CLI zip URL for linux/arm64 images.
  BW_FILE_SHA256               Override Bitwarden CLI zip SHA-256 for all image architectures.
  BW_FILE_SHA256_AMD64         Override Bitwarden CLI zip SHA-256 for linux/amd64 images.
  BW_FILE_SHA256_ARM64         Override Bitwarden CLI zip SHA-256 for linux/arm64 images.
  XURL_VERSION                X CLI version; update both XURL_SHA256_* pins with it.
  XURL_SHA256_AMD64           X CLI linux/amd64 archive SHA-256.
  XURL_SHA256_ARM64           X CLI linux/arm64 archive SHA-256.
  GOGCLI_VERSION               gogcli release version installed in the bundle image. Defaults to Dockerfile pin.
  GOGCLI_SHA256_AMD64          Override gogcli linux/amd64 archive SHA-256.
  GOGCLI_SHA256_ARM64          Override gogcli linux/arm64 archive SHA-256.
  SEALBOX_REPO_URL             Sealbox git repository URL used when installing sealbox.
  SEALBOX_GIT_REF              Sealbox git commit/ref used when installing sealbox.
EOF
}

tag_latest="false"
no_cache="false"
build_mode="standard"
install_openshell="${INSTALL_OPENSHELL:-false}"
openshell_version="${OPENSHELL_VERSION:-latest}"
docker_platform="${DOCKER_PLATFORM:-}"
docker_platform_explicit="false"
if [[ "${DOCKER_PLATFORM+x}" == "x" ]]; then
  docker_platform_explicit="true"
fi
install_nats="${INSTALL_NATS:-}"
install_postgres="${INSTALL_POSTGRES:-}"
install_sqlite="${INSTALL_SQLITE:-}"
install_credential_clis="${INSTALL_CREDENTIAL_CLIS-}"
install_credential_clis_explicit="false"
if [[ "${INSTALL_CREDENTIAL_CLIS+x}" == "x" ]]; then
  install_credential_clis_explicit="true"
fi
strip_source_code="${STRIP_SOURCE_CODE:-true}"
install_nodejs="${INSTALL_NODEJS:-false}"
postgres_major="${POSTGRES_MAJOR:-}"
postgres_version="${POSTGRES_VERSION:-}"
pgvector_package_version="${PGVECTOR_PACKAGE_VERSION:-}"
sqlite_version="${SQLITE_VERSION:-}"
nats_server_version="${NATS_SERVER_VERSION:-}"
bw_version="${BW_VERSION:-}"
bw_file_url="${BW_FILE_URL:-}"
bw_file_url_amd64="${BW_FILE_URL_AMD64:-}"
bw_file_url_arm64="${BW_FILE_URL_ARM64:-}"
bw_file_sha256="${BW_FILE_SHA256:-}"
bw_file_sha256_amd64="${BW_FILE_SHA256_AMD64:-}"
bw_file_sha256_arm64="${BW_FILE_SHA256_ARM64:-}"
gogcli_version="${GOGCLI_VERSION:-}"
gogcli_sha256_amd64="${GOGCLI_SHA256_AMD64:-}"
gogcli_sha256_arm64="${GOGCLI_SHA256_ARM64:-}"
sealbox_repo_url="${SEALBOX_REPO_URL:-}"
sealbox_git_ref="${SEALBOX_GIT_REF:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)
      build_mode="bundle"
      shift
      ;;
    --latest)
      tag_latest="true"
      shift
      ;;
    --no-cache|--nocache)
      no_cache="true"
      shift
      ;;
    --openshell)
      install_openshell="true"
      shift
      ;;
    --openshell-version)
      if [[ $# -lt 2 ]]; then
        echo "--openshell-version requires a value" >&2
        exit 2
      fi
      openshell_version="$2"
      shift 2
      ;;
    --platform)
      if [[ $# -lt 2 ]]; then
        echo "--platform requires a value" >&2
        exit 2
      fi
      docker_platform="$2"
      docker_platform_explicit="true"
      shift 2
      ;;
    --credential-cli)
      if [[ $# -lt 2 ]]; then
        echo "--credential-cli requires a value" >&2
        exit 2
      fi
      if [[ -n "${install_credential_clis}" ]]; then
        install_credential_clis="${install_credential_clis},$2"
      else
        install_credential_clis="$2"
      fi
      install_credential_clis_explicit="true"
      shift 2
      ;;
    --credential-clis)
      if [[ $# -lt 2 ]]; then
        echo "--credential-clis requires a value" >&2
        exit 2
      fi
      install_credential_clis="$2"
      install_credential_clis_explicit="true"
      shift 2
      ;;
    --no-credential-clis)
      install_credential_clis="none"
      install_credential_clis_explicit="true"
      shift
      ;;
    --nodejs)
      install_nodejs="true"
      shift
      ;;
    --no-nodejs)
      install_nodejs="false"
      shift
      ;;
    --strip-source-code)
      strip_source_code="true"
      shift
      ;;
    --keep-source-code|--no-strip-source-code)
      strip_source_code="false"
      shift
      ;;
    --nats)
      install_nats="true"
      shift
      ;;
    --no-nats)
      install_nats="false"
      shift
      ;;
    --postgres)
      install_postgres="true"
      shift
      ;;
    --no-postgres)
      install_postgres="false"
      shift
      ;;
    --sqlite|--litesql)
      install_sqlite="true"
      shift
      ;;
    --no-sqlite|--no-litesql)
      install_sqlite="false"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

if [[ $# -gt 1 ]]; then
  usage >&2
  exit 2
fi

dockerfile="Dockerfile"
default_standard_repo="${TEAM_ASSISTANT_IMAGE_REPO:-team-assistant}"
default_bundle_repo="${TEAM_ASSISTANT_BUNDLE_IMAGE_REPO:-team-assistant-bundle}"
default_image_ref="${default_standard_repo}:latest"
if [[ "${build_mode}" == "bundle" ]]; then
  dockerfile="Dockerfile.bundle"
  default_image_ref="${default_bundle_repo}:latest"
fi
if [[ -z "${install_nats}" ]]; then
  install_nats="true"
fi
if [[ -z "${install_postgres}" ]]; then
  if [[ "${build_mode}" == "bundle" ]]; then
    install_postgres="true"
  else
    install_postgres="false"
  fi
fi
if [[ -z "${install_sqlite}" ]]; then
  if [[ "${build_mode}" == "bundle" ]]; then
    install_sqlite="true"
  else
    install_sqlite="false"
  fi
fi
case "${strip_source_code}" in
  true|false) ;;
  *)
    echo "STRIP_SOURCE_CODE must be true or false, got: ${strip_source_code}" >&2
    exit 2
    ;;
esac
case "${install_nodejs}" in
  true|false) ;;
  *)
    echo "INSTALL_NODEJS must be true or false, got: ${install_nodejs}" >&2
    exit 2
    ;;
esac

image_ref="${1:-${default_image_ref}}"
if [[ $# -eq 1 && "${image_ref}" != */* && "${image_ref}" != *:* ]]; then
  image_ref="${default_image_ref%:*}:${image_ref}"
fi

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "This script must be run inside the git repository." >&2
  exit 2
}

if [[ ! -f "${repo_root}/${dockerfile}" ]]; then
  echo "Selected dockerfile not found in build context: ${dockerfile}" >&2
  exit 2
fi

docker_build_args=(
  --pull
)
if [[ -n "${docker_platform}" ]]; then
  docker_build_args+=(--platform "${docker_platform}")
fi
docker_build_args+=(
  --build-arg "INSTALL_OPENSHELL=${install_openshell}"
  --build-arg "OPENSHELL_VERSION=${openshell_version}"
  --build-arg "INSTALL_NATS=${install_nats}"
  --build-arg "INSTALL_POSTGRES=${install_postgres}"
  --build-arg "INSTALL_SQLITE=${install_sqlite}"
  --build-arg "INSTALL_NODEJS=${install_nodejs}"
  --build-arg "STRIP_SOURCE_CODE=${strip_source_code}"
  -f "${repo_root}/${dockerfile}"
  -t "${image_ref}"
)
if [[ -n "${postgres_major}" ]]; then
  docker_build_args+=(--build-arg "POSTGRES_MAJOR=${postgres_major}")
fi
if [[ -n "${postgres_version}" ]]; then
  docker_build_args+=(--build-arg "POSTGRES_VERSION=${postgres_version}")
fi
if [[ -n "${pgvector_package_version}" ]]; then
  docker_build_args+=(--build-arg "PGVECTOR_PACKAGE_VERSION=${pgvector_package_version}")
fi
if [[ -n "${sqlite_version}" ]]; then
  docker_build_args+=(--build-arg "SQLITE_VERSION=${sqlite_version}")
fi
if [[ -n "${nats_server_version}" ]]; then
  docker_build_args+=(--build-arg "NATS_SERVER_VERSION=${nats_server_version}")
fi
if [[ -n "${bw_version}" ]]; then
  docker_build_args+=(--build-arg "BW_VERSION=${bw_version}")
fi
if [[ -n "${bw_file_url}" ]]; then
  docker_build_args+=(--build-arg "BW_FILE_URL=${bw_file_url}")
fi
if [[ -n "${bw_file_url_amd64}" ]]; then
  docker_build_args+=(--build-arg "BW_FILE_URL_AMD64=${bw_file_url_amd64}")
fi
if [[ -n "${bw_file_url_arm64}" ]]; then
  docker_build_args+=(--build-arg "BW_FILE_URL_ARM64=${bw_file_url_arm64}")
fi
if [[ -n "${bw_file_sha256}" ]]; then
  docker_build_args+=(--build-arg "BW_FILE_SHA256=${bw_file_sha256}")
fi
if [[ -n "${bw_file_sha256_amd64}" ]]; then
  docker_build_args+=(--build-arg "BW_FILE_SHA256_AMD64=${bw_file_sha256_amd64}")
fi
if [[ -n "${bw_file_sha256_arm64}" ]]; then
  docker_build_args+=(--build-arg "BW_FILE_SHA256_ARM64=${bw_file_sha256_arm64}")
fi
for xurl_arg in XURL_VERSION XURL_SHA256_AMD64 XURL_SHA256_ARM64; do
  if [[ -n "${!xurl_arg:-}" ]]; then
    docker_build_args+=(--build-arg "${xurl_arg}=${!xurl_arg}")
  fi
done
if [[ -n "${gogcli_version}" ]]; then
  docker_build_args+=(--build-arg "GOGCLI_VERSION=${gogcli_version}")
fi
if [[ -n "${gogcli_sha256_amd64}" ]]; then
  docker_build_args+=(--build-arg "GOGCLI_SHA256_AMD64=${gogcli_sha256_amd64}")
fi
if [[ -n "${gogcli_sha256_arm64}" ]]; then
  docker_build_args+=(--build-arg "GOGCLI_SHA256_ARM64=${gogcli_sha256_arm64}")
fi
if [[ -n "${sealbox_repo_url}" ]]; then
  docker_build_args+=(--build-arg "SEALBOX_REPO_URL=${sealbox_repo_url}")
fi
if [[ -n "${sealbox_git_ref}" ]]; then
  docker_build_args+=(--build-arg "SEALBOX_GIT_REF=${sealbox_git_ref}")
fi
if [[ "${install_credential_clis_explicit}" == "true" ]]; then
  docker_build_args+=(--build-arg "INSTALL_CREDENTIAL_CLIS=${install_credential_clis}")
fi
if [[ "${no_cache}" == "true" ]]; then
  docker_build_args+=(--no-cache)
fi

echo "Building ${build_mode} Docker image ${image_ref} from current checkout using ${dockerfile} ..."
docker build "${docker_build_args[@]}" "${repo_root}"

if [[ "${tag_latest}" == "true" ]]; then
  image_repo_for_latest="${image_ref}"
  image_ref_last_path_component="${image_ref##*/}"
  if [[ "${image_ref_last_path_component}" == *:* ]]; then
    image_repo_for_latest="${image_ref%:*}"
  fi
  latest_ref="${image_repo_for_latest}:latest"
  echo "Tagging ${image_ref} as ${latest_ref}"
  docker tag "${image_ref}" "${latest_ref}"
fi

echo "Built image:"
echo "  ${image_ref}"
if [[ "${tag_latest}" == "true" ]]; then
  echo "  ${latest_ref}"
fi
