#!/usr/bin/env bash
set -euo pipefail

readonly DEFAULT_IMAGE="ghcr.io/enzimo/universal-agents:latest"
readonly DEFAULT_REPOSITORY="https://github.com/enzimo/universal-agents.git"
readonly DEFAULT_CONTAINER_NAME="universal-agents"
readonly DEFAULT_WEB_PORT="18090"
readonly DEFAULT_BROKER_PORT="18600"
readonly DEFAULT_BIND_HOST="127.0.0.1"
readonly DEFAULT_BRANCH="main"
readonly DEFAULT_NATS_VERSION="v2.15.1"

mode="docker"
image="${UA_IMAGE:-$DEFAULT_IMAGE}"
repository="${UA_REPOSITORY:-$DEFAULT_REPOSITORY}"
branch="${UA_BRANCH:-$DEFAULT_BRANCH}"
install_home="${UA_HOME:-$HOME/.universal-agents}"
data_dir=""
container_name="${UA_CONTAINER_NAME:-$DEFAULT_CONTAINER_NAME}"
bind_host="${UA_BIND_HOST:-$DEFAULT_BIND_HOST}"
web_port="${UA_WEB_PORT:-$DEFAULT_WEB_PORT}"
broker_port="${UA_BROKER_PORT:-$DEFAULT_BROKER_PORT}"
public_origin="${NETWORK_PUBLIC_ORIGIN:-http://127.0.0.1}"
refresh_external_skills="false"
# Bash 3.2 treats an empty array expansion as unbound under `set -u`. Keep one
# empty sentinel so the public installer also works with the Bash shipped by
# macOS; the launch loop skips it.
skills_repositories=("")
start_after_install="true"
replace_container="false"

usage() {
  cat <<'EOF'
Universal Agents installer

Usage:
  install.sh [OPTIONS]

The default and strongly recommended mode pulls the bundled Docker image. The
bundle provides NATS/JetStream, SQLite tooling, Sealbox, the Cedar evaluator,
Node.js for Photon/iMessage, and the supervised Universal Agents runtime.

Options:
  --docker                    Install with the recommended Docker bundle (default).
  --native                    Experimental host-native source installation.
  --image IMAGE               Docker image (default: ghcr.io/enzimo/universal-agents:latest).
  --data-dir PATH             Persistent instance data directory.
  --container-name NAME       Docker container name (default: universal-agents).
  --bind-host HOST            Host interface for published ports (default: 127.0.0.1).
  --web-port PORT             Web UI/onboarding host port (default: 18090).
  --broker-port PORT          Secure broker host port (default: 18600).
  --public-origin URL         Browser-facing origin (default: http://127.0.0.1).
  --refresh-external-skills  Refresh configured external skills before agents start.
  --skills-repository URL     Override an external skills repository; repeatable.
  --no-start                  Install or pull without starting Universal Agents.
  --replace                   Recreate an existing installer-managed Docker container.
  --repository URL            Native source repository.
  --branch NAME               Native source branch (default: main).
  -h, --help                  Show this help.

Examples:
  install.sh
  install.sh --refresh-external-skills
  install.sh --skills-repository https://github.com/enzimo/ua-skills
  install.sh --image ghcr.io/enzimo/universal-agents:test-installer
  install.sh --native
EOF
}

log() {
  printf '==> %s\n' "$*"
}

warn() {
  printf 'WARNING: %s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_value() {
  local option="$1"
  local value="${2:-}"
  [[ -n "$value" ]] || die "$option requires a value"
}

validate_port() {
  local option="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$option must be an integer"
  ((value >= 1 && value <= 65535)) || die "$option must be between 1 and 65535"
}

while (($# > 0)); do
  case "$1" in
    --docker)
      mode="docker"
      shift
      ;;
    --native)
      mode="native"
      shift
      ;;
    --image)
      require_value "$1" "${2:-}"
      image="$2"
      shift 2
      ;;
    --data-dir)
      require_value "$1" "${2:-}"
      data_dir="$2"
      shift 2
      ;;
    --container-name)
      require_value "$1" "${2:-}"
      container_name="$2"
      shift 2
      ;;
    --bind-host)
      require_value "$1" "${2:-}"
      bind_host="$2"
      shift 2
      ;;
    --web-port)
      require_value "$1" "${2:-}"
      web_port="$2"
      shift 2
      ;;
    --broker-port)
      require_value "$1" "${2:-}"
      broker_port="$2"
      shift 2
      ;;
    --public-origin)
      require_value "$1" "${2:-}"
      public_origin="${2%/}"
      shift 2
      ;;
    --refresh-external-skills)
      refresh_external_skills="true"
      shift
      ;;
    --skills-repository)
      require_value "$1" "${2:-}"
      skills_repositories+=("$2")
      shift 2
      ;;
    --no-start)
      start_after_install="false"
      shift
      ;;
    --replace)
      replace_container="true"
      shift
      ;;
    --repository)
      require_value "$1" "${2:-}"
      repository="$2"
      shift 2
      ;;
    --branch)
      require_value "$1" "${2:-}"
      branch="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

validate_port "--web-port" "$web_port"
validate_port "--broker-port" "$broker_port"

if [[ -z "$data_dir" ]]; then
  data_dir="$install_home/data"
fi

install_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker is required. Install Docker Desktop or Docker Engine, then rerun this installer."
  docker info >/dev/null 2>&1 || die "Docker is installed but its daemon is not reachable. Start Docker and rerun this installer."

  mkdir -p "$data_dir"
  data_dir="$(cd "$data_dir" && pwd -P)"

  log "Pulling recommended Universal Agents bundle: $image"
  docker pull "$image"

  if [[ "$start_after_install" != "true" ]]; then
    log "Image ready. Persistent data will be stored at $data_dir"
    return
  fi

  if docker container inspect "$container_name" >/dev/null 2>&1; then
    if [[ "$replace_container" == "true" ]]; then
      managed_label="$(docker container inspect --format '{{ index .Config.Labels "io.universal-agents.installer.managed" }}' "$container_name" 2>/dev/null || true)"
      [[ "$managed_label" == "true" ]] || die "Refusing to replace '$container_name' because it was not created by this installer. Choose another --container-name."
      log "Recreating installer-managed container $container_name; data at $data_dir is preserved"
      docker rm --force "$container_name" >/dev/null
    else
      running="$(docker container inspect --format '{{.State.Running}}' "$container_name")"
      if [[ "$running" == "true" ]]; then
        log "Container $container_name is already running. Use --replace to apply changed image or launch options."
      else
        log "Starting existing container $container_name. Use --replace to apply changed image or launch options."
        docker start "$container_name" >/dev/null
      fi
      printf '\nWeb UI: %s:%s/\nLogs:   docker logs -f %s\n' "$public_origin" "$web_port" "$container_name"
      return
    fi
  fi

  run_args=(
    run --detach
    --name "$container_name"
    --restart unless-stopped
    --label io.universal-agents.installer.managed=true
    --publish "$bind_host:$web_port:18090"
    --publish "$bind_host:$broker_port:18600"
    --volume "$data_dir:/data"
    --env "NETWORK_PUBLIC_ORIGIN=$public_origin"
    --env "INTERNAL_WEBSITE_PUBLIC_PORT=$web_port"
    --env "SECURE_TOOL_BROKER_PUBLIC_PORT=$broker_port"
    "$image"
    run
  )
  if [[ "$refresh_external_skills" == "true" ]]; then
    run_args+=(--refresh-external-skills)
  fi
  for repository_url in "${skills_repositories[@]}"; do
    [[ -n "$repository_url" ]] || continue
    run_args+=(--skills-repository "$repository_url")
  done

  log "Starting Universal Agents with Web UI enabled"
  docker "${run_args[@]}" >/dev/null

  cat <<EOF

Universal Agents is starting.

  Web UI / onboarding: $public_origin:$web_port/
  Persistent data:     $data_dir
  Container logs:      docker logs -f $container_name
  Stop:                docker stop $container_name
  Start again:         docker start $container_name

The Web UI is always available. During onboarding you may additionally enable
Telegram, Discord, Photon/iMessage, or several channels together. Credentials
are collected by onboarding instead of being placed in this command's history.
EOF
}

native_python() {
  for candidate in python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if (3, 12) <= sys.version_info < (3, 14) else 1)' 2>/dev/null; then
      command -v "$candidate"
      return
    fi
  done
  return 1
}

install_native_nats() {
  local bin_dir="$1"
  if command -v nats-server >/dev/null 2>&1 || [[ -x "$bin_dir/nats-server" ]]; then
    return
  fi

  command -v curl >/dev/null 2>&1 || die "Experimental native mode needs curl to install nats-server."
  command -v tar >/dev/null 2>&1 || die "Experimental native mode needs tar to install nats-server."

  local os_name arch_name archive archive_sha256 platform temporary_dir extracted_binary actual_sha256
  case "$(uname -s)" in
    Linux) os_name="linux" ;;
    Darwin) os_name="darwin" ;;
    *) die "Experimental native mode supports Linux, macOS, and WSL2 only." ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) arch_name="amd64" ;;
    arm64|aarch64) arch_name="arm64" ;;
    *) die "Unsupported native architecture: $(uname -m)" ;;
  esac
  platform="$os_name-$arch_name"
  case "$platform" in
    darwin-amd64) archive_sha256="3cba5976a4fbd022669d3401cab4147faeb2b4c079712c882edb9ae4e52feb06" ;;
    darwin-arm64) archive_sha256="ed0fe39222ec2e317c4d0e1a9cdc16c63ec523d94f9355d24a902a4db2655ffd" ;;
    linux-amd64) archive_sha256="bf5edc7b1ce98e885c368cb43a886eb148dc5d0551d99f3cf6141fb5e1954c70" ;;
    linux-arm64) archive_sha256="e2fbab4cad7d3c76e3052eb959570393c9d73ba756b70bf8e9eea61e9ae6d49c" ;;
    *) die "No pinned NATS checksum is available for $platform" ;;
  esac
  archive="nats-server-${DEFAULT_NATS_VERSION}-${platform}.tar.gz"
  temporary_dir="$(mktemp -d)"
  trap 'rm -rf "$temporary_dir"' RETURN
  log "Installing nats-server $DEFAULT_NATS_VERSION for $platform"
  curl -fsSL "https://github.com/nats-io/nats-server/releases/download/${DEFAULT_NATS_VERSION}/${archive}" -o "$temporary_dir/$archive"
  if command -v sha256sum >/dev/null 2>&1; then
    actual_sha256="$(sha256sum "$temporary_dir/$archive" | awk '{print $1}')"
  else
    actual_sha256="$(shasum -a 256 "$temporary_dir/$archive" | awk '{print $1}')"
  fi
  [[ "$actual_sha256" == "$archive_sha256" ]] || die "NATS archive checksum verification failed."
  tar -xzf "$temporary_dir/$archive" -C "$temporary_dir"
  extracted_binary="$temporary_dir/nats-server-${DEFAULT_NATS_VERSION}-${platform}/nats-server"
  [[ -x "$extracted_binary" ]] || die "Downloaded NATS archive did not contain the expected binary."
  install -m 755 "$extracted_binary" "$bin_dir/nats-server"
  trap - RETURN
  rm -rf "$temporary_dir"
}

install_native() {
  warn "Native mode is experimental. The Docker bundle remains the supported and strongly recommended installation."
  command -v git >/dev/null 2>&1 || die "Experimental native mode requires git."
  command -v cargo >/dev/null 2>&1 || die "Experimental native mode requires Rust/Cargo to build the Cedar evaluator and managed Sealbox service."
  python_executable="$(native_python)" || die "Experimental native mode requires Python 3.12 or 3.13."

  mkdir -p "$install_home" "$data_dir"
  install_home="$(cd "$install_home" && pwd -P)"
  data_dir="$(cd "$data_dir" && pwd -P)"
  local app_dir="$install_home/app"
  local bin_dir="$install_home/bin"
  local sealbox_dir="$install_home/sealbox"
  local venv_python="$app_dir/.venv/bin/python"
  local skillspector_python="$app_dir/.skillspector/bin/python"
  mkdir -p "$bin_dir"

  if [[ -d "$app_dir/.git" ]]; then
    existing_origin="$(git -C "$app_dir" remote get-url origin 2>/dev/null || true)"
    [[ "$existing_origin" == "$repository" ]] || die "Existing native checkout has a different origin: $existing_origin"
    if [[ -n "$(git -C "$app_dir" status --porcelain)" ]]; then
      warn "Native checkout has local changes; leaving it unchanged."
    else
      log "Updating native checkout from $repository ($branch)"
      git -C "$app_dir" fetch --depth 1 origin "$branch"
      git -C "$app_dir" checkout --detach FETCH_HEAD
    fi
  elif [[ -e "$app_dir" ]]; then
    die "Native application path exists but is not a Git checkout: $app_dir"
  else
    log "Cloning Universal Agents native source ($branch)"
    git clone --depth 1 --branch "$branch" "$repository" "$app_dir"
  fi

  if [[ ! -x "$venv_python" ]]; then
    log "Creating native Python environment"
    "$python_executable" -m venv "$app_dir/.venv"
  fi
  log "Installing the hash-locked Python dependency graph"
  "$venv_python" -m pip install --require-hashes -r "$app_dir/requirements.lock.txt"
  "$venv_python" "$app_dir/scripts/build_user_docs.py"

  if [[ ! -x "$skillspector_python" ]]; then
    log "Creating isolated SkillSpector environment"
    "$python_executable" -m venv "$app_dir/.skillspector"
  fi
  log "Installing the locked SkillSpector dependency graph"
  "$skillspector_python" -m pip install \
    --require-hashes \
    -r "$app_dir/requirements.skillspector.lock.txt"
  "$app_dir/.skillspector/bin/skillspector" --version
  ln -sfn "$app_dir/.skillspector/bin/skillspector" "$bin_dir/skillspector"

  install_native_nats "$bin_dir"

  log "Building the Cedar authorization worker"
  cargo build --locked --release --manifest-path "$app_dir/Cargo.toml" --bin ua-cedar-authorizer
  install -m 755 "$app_dir/target/release/ua-cedar-authorizer" "$bin_dir/ua-cedar-authorizer"

  if [[ -d "$sealbox_dir/.git" ]]; then
    if [[ -n "$(git -C "$sealbox_dir" status --porcelain)" ]]; then
      warn "Sealbox checkout has local changes; leaving it unchanged."
    else
      git -C "$sealbox_dir" fetch --depth 1 origin main
      git -C "$sealbox_dir" checkout --detach FETCH_HEAD
    fi
  elif [[ -e "$sealbox_dir" ]]; then
    die "Sealbox path exists but is not a Git checkout: $sealbox_dir"
  else
    log "Cloning Sealbox for native encrypted credential storage"
    git clone --depth 1 https://github.com/enzimo/sealbox.git "$sealbox_dir"
  fi
  log "Building Sealbox CLI and server"
  cargo build --locked --release --manifest-path "$sealbox_dir/Cargo.toml"
  install -m 755 "$sealbox_dir/target/release/sealbox-cli" "$bin_dir/sealbox-cli"
  install -m 755 "$sealbox_dir/target/release/sealbox-server" "$bin_dir/sealbox-server"

  if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 && node -e 'const [major] = process.versions.node.split(".").map(Number); process.exit(major >= 24 ? 0 : 1)'; then
    log "Installing Photon/iMessage sidecar dependencies"
    npm --prefix "$app_dir/assistant_gateway/interactive_apps/photon_sidecar" ci --omit=dev
  else
    warn "Node.js 24+ was not found. Native Photon/iMessage support will remain unavailable."
  fi

  native_config="$install_home/native-install.env"
  {
    printf 'UA_NATIVE_APP_DIR=%q\n' "$app_dir"
    printf 'UA_NATIVE_DATA_DIR=%q\n' "$data_dir"
    printf 'NETWORK_PUBLIC_ORIGIN=%q\n' "$public_origin"
    printf 'INTERNAL_WEBSITE_BIND_HOST=%q\n' "$bind_host"
    printf 'INTERNAL_WEBSITE_BIND_PORT=%q\n' "$web_port"
    printf 'INTERNAL_WEBSITE_PUBLIC_PORT=%q\n' "$web_port"
    printf 'SECURE_TOOL_BROKER_HOST=%q\n' "$bind_host"
    printf 'SECURE_TOOL_BROKER_PORT=%q\n' "$broker_port"
    printf 'SECURE_TOOL_BROKER_PUBLIC_PORT=%q\n' "$broker_port"
  } > "$native_config"
  chmod 600 "$native_config"

  native_launcher="$bin_dir/ua"
  cat > "$native_launcher" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
launcher_path="${BASH_SOURCE[0]}"
while [[ -L "$launcher_path" ]]; do
  launcher_dir="$(cd "$(dirname "$launcher_path")" && pwd -P)"
  launcher_target="$(readlink "$launcher_path")"
  if [[ "$launcher_target" = /* ]]; then
    launcher_path="$launcher_target"
  else
    launcher_path="$launcher_dir/$launcher_target"
  fi
done
ua_home="${UA_HOME:-$(cd "$(dirname "$launcher_path")/.." && pwd -P)}"
set -a
source "$ua_home/native-install.env"
set +a
export PATH="$ua_home/bin:$UA_NATIVE_APP_DIR/.venv/bin:$PATH"
config_path="$UA_NATIVE_DATA_DIR/instance_config.yaml"
command_name="${1:-start}"
[[ $# -eq 0 ]] || shift
case "$command_name" in
  start)
    if [[ ! -f "$config_path" ]]; then
      echo "No onboarded instance found; starting browser onboarding on port ${INTERNAL_WEBSITE_BIND_PORT:-18090}." >&2
      "$UA_NATIVE_APP_DIR/.venv/bin/python" "$UA_NATIVE_APP_DIR/assistant_cli.py" onboard-web --instance-root "$UA_NATIVE_DATA_DIR"
      [[ -f "$config_path" ]] || { echo "Onboarding ended without creating $config_path" >&2; exit 1; }
    fi
    exec "$UA_NATIVE_APP_DIR/.venv/bin/python" "$UA_NATIVE_APP_DIR/docker/launch_headless.py" --config "$config_path" "$@"
    ;;
  onboard)
    exec "$UA_NATIVE_APP_DIR/.venv/bin/python" "$UA_NATIVE_APP_DIR/assistant_cli.py" onboard-web --instance-root "$UA_NATIVE_DATA_DIR" "$@"
    ;;
  *)
    echo "Usage: ua [start [--refresh-external-skills ...] | onboard]" >&2
    exit 2
    ;;
esac
EOF
  chmod 755 "$native_launcher"

  mkdir -p "$HOME/.local/bin"
  ln -sfn "$native_launcher" "$HOME/.local/bin/ua"

  cat <<EOF

Experimental native installation is ready.

  Launcher: $HOME/.local/bin/ua
  Source:   $app_dir
  Data:     $data_dir

The Docker bundle remains recommended because it supplies a controlled,
reproducible service and tool environment. Ensure $HOME/.local/bin is on PATH.
EOF

  if [[ "$start_after_install" == "true" ]]; then
    set --
    if [[ "$refresh_external_skills" == "true" ]]; then
      set -- "$@" --refresh-external-skills
    fi
    for repository_url in "${skills_repositories[@]}"; do
      [[ -n "$repository_url" ]] || continue
      set -- "$@" --skills-repository "$repository_url"
    done
    exec "$native_launcher" start "$@"
  fi
}

case "$mode" in
  docker) install_docker ;;
  native) install_native ;;
  *) die "Unsupported installation mode: $mode" ;;
esac
