#!/bin/sh
set -eu

APP_ROOT="${TEAM_APP_ROOT:-/app}"
IMAGE_MODE="${TEAM_DOCKER_IMAGE_MODE:-standard}"
DATA_ROOT="${TEAM_DATA_ROOT:-/data}"
DEFAULT_INSTANCE_ROOT="$DATA_ROOT"
RESOLVED_INSTANCE_ROOT="${TEAM_INSTANCE_ROOT:-$DEFAULT_INSTANCE_ROOT}"
RESOLVED_INSTANCE_CONFIG="${TEAM_INSTANCE_CONFIG:-$RESOLVED_INSTANCE_ROOT/instance_config.yaml}"
INSTANCE_ROOT_HINT_FILE="${TEAM_INSTANCE_ROOT_HINT_FILE:-$DATA_ROOT/.team-instance-root}"
INSTANCE_DISCOVERY_NOTE=""

# Secret variables may come from Docker secret files (<NAME>_FILE). The list of
# those variables is docker/secret_env_vars.yaml, and only env_secrets.py reads
# it: every Python program this script starts reads the files itself when it
# starts (env_secrets.materialize_secret_file_envs), and nothing here needs a
# secret value. This script only asks env_secrets.py to refuse a container
# environment that sets both <NAME> and <NAME>_FILE, or a <NAME>_FILE naming a
# missing file, before anything starts. The check runs on the container's own
# environment only; the environments the launchers build for their processes
# may hold a team.env value next to its file form on purpose.
ENV_FILE_CHECK_SCRIPT="$(dirname "$0")/../env_secrets.py"

check_secret_files() {
    check_program="$(python_program_path "$ENV_FILE_CHECK_SCRIPT")" || exit 2
    python "$check_program" check-container-env || exit 2
}

has_arg() {
    needle="$1"
    shift
    for arg in "$@"; do
        if [ "$arg" = "$needle" ]; then
            return 0
        fi
    done
    return 1
}

# The path of a Python program: the source file, or the compiled .pyc left
# when the image strips Python sources.
python_program_path() {
    script_path="$1"
    if [ -f "$script_path" ]; then
        printf '%s\n' "$script_path"
        return 0
    fi

    case "$script_path" in
        *.py) compiled_path="${script_path}c" ;;
        *) compiled_path="${script_path}.pyc" ;;
    esac
    if [ -f "$compiled_path" ]; then
        printf '%s\n' "$compiled_path"
        return 0
    fi

    echo "Python entrypoint not found: $script_path or $compiled_path" >&2
    return 1
}

exec_python_entrypoint() {
    program_path="$(python_program_path "$1")" || exit 2
    shift
    exec python "$program_path" "$@"
}

print_missing_instance_instructions() {
    hint_note=""
    if [ -n "$INSTANCE_DISCOVERY_NOTE" ]; then
        hint_note="

$INSTANCE_DISCOVERY_NOTE"
    fi
    cat >&2 <<EOF
No onboarded instance was found at:
  $RESOLVED_INSTANCE_CONFIG
$hint_note

Run interactive onboarding first:
  docker run --rm -it -v <host_instance_dir>:/data your-image onboard

That will create:
  $RESOLVED_INSTANCE_CONFIG
  $RESOLVED_INSTANCE_ROOT/control   (team control files)
  $RESOLVED_INSTANCE_ROOT/workspaces (agent work areas)

After onboarding, start the headless runtime with:
  docker run --rm -d -v <host_instance_dir>:/data your-image
EOF
}

discover_instance_config() {
    if [ "${TEAM_INSTANCE_CONFIG+x}" = "x" ]; then
        RESOLVED_INSTANCE_CONFIG="$TEAM_INSTANCE_CONFIG"
        RESOLVED_INSTANCE_ROOT="$(dirname "$RESOLVED_INSTANCE_CONFIG")"
        return 0
    fi

    if [ "${TEAM_INSTANCE_ROOT+x}" = "x" ]; then
        RESOLVED_INSTANCE_ROOT="$TEAM_INSTANCE_ROOT"
        RESOLVED_INSTANCE_CONFIG="$RESOLVED_INSTANCE_ROOT/instance_config.yaml"
        return 0
    fi

    RESOLVED_INSTANCE_ROOT="$DEFAULT_INSTANCE_ROOT"
    RESOLVED_INSTANCE_CONFIG="$RESOLVED_INSTANCE_ROOT/instance_config.yaml"
    if [ -f "$RESOLVED_INSTANCE_CONFIG" ]; then
        return 0
    fi

    if [ -f "$INSTANCE_ROOT_HINT_FILE" ]; then
        hinted_root="$(sed -n '1p' "$INSTANCE_ROOT_HINT_FILE" | tr -d '\r')"
        if [ -n "$hinted_root" ]; then
            hinted_config="$hinted_root/instance_config.yaml"
            if [ -f "$hinted_config" ]; then
                RESOLVED_INSTANCE_ROOT="$hinted_root"
                RESOLVED_INSTANCE_CONFIG="$hinted_config"
                INSTANCE_DISCOVERY_NOTE="Auto-discovered onboarded instance from $INSTANCE_ROOT_HINT_FILE."
                return 0
            fi
            INSTANCE_DISCOVERY_NOTE="Ignoring stale instance-root hint at $INSTANCE_ROOT_HINT_FILE; expected $hinted_config."
        fi
    fi

    if [ ! -d "$DATA_ROOT" ]; then
        return 0
    fi

    candidates_file="${TMPDIR:-/tmp}/team-instance-config-candidates.$$"
    find "$DATA_ROOT" -type f -name instance_config.yaml 2>/dev/null | sort > "$candidates_file"
    candidates_count="$(wc -l < "$candidates_file" | tr -d ' ')"
    if [ "$candidates_count" = "1" ]; then
        RESOLVED_INSTANCE_CONFIG="$(cat "$candidates_file")"
        RESOLVED_INSTANCE_ROOT="$(dirname "$RESOLVED_INSTANCE_CONFIG")"
        INSTANCE_DISCOVERY_NOTE="Auto-discovered onboarded instance by scanning $DATA_ROOT."
    elif [ "$candidates_count" -gt 1 ]; then
        INSTANCE_DISCOVERY_NOTE="Found multiple onboarded instances under $DATA_ROOT. Set TEAM_INSTANCE_ROOT or TEAM_INSTANCE_CONFIG explicitly."
    fi
    rm -f "$candidates_file"
    return 0
}

print_help() {
    cat <<EOF
Usage:
  docker run ... your-image [run [gateway args...]]
  docker run ... your-image onboard [assistant_cli onboard args...]
  docker run ... your-image onboard-web [assistant_cli onboard-web args...]

Modes:
  run      Start the headless TeamArchitect and gateway. This is the default.
  onboard  Run assistant_cli.py onboard. Use docker -it for interactive onboarding.
  onboard-web  Run browser-based onboarding on port 18090.

Run flags:
  --refresh-external-skills  Refresh external skill repositories before agents start.
  --skills-repository URL   Override configured repositories for this launch; repeatable.

Environment:
  TEAM_APP_ROOT         Application root inside the container. Default: /app
  TEAM_DOCKER_IMAGE_MODE  Image flavor selector. Default: standard
  TEAM_DATA_ROOT        Mounted data root inside the container. Default: /data
  TEAM_INSTANCE_ROOT    Explicit instance root inside the container. Default: \$TEAM_DATA_ROOT
  TEAM_INSTANCE_CONFIG  Explicit instance_config.yaml path. Default: \$TEAM_INSTANCE_ROOT/instance_config.yaml
  TEAM_INSTANCE_ROOT_HINT_FILE  Persisted instance-root hint file. Default: \$TEAM_DATA_ROOT/.team-instance-root
  TEAM_ASSISTANT_ENV_FILE  Optional explicit env file path to merge before launch.
  TEAM_REFRESH_EXTERNAL_SKILLS  true/false override for workspace.refresh_external_skills_on_start.
EOF
}

mode="${1:-run}"
check_secret_files

case "$mode" in
    run|onboard|onboard-web|help|--help|-h)
        [ "$#" -gt 0 ] && shift
        ;;
    -*|"")
        mode="run"
        ;;
    *)
        echo "Unknown mode: $mode" >&2
        print_help >&2
        exit 2
        ;;
esac

case "$mode" in
    help|--help|-h)
        print_help
        exit 0
        ;;
    onboard)
        if ! has_arg --non-interactive "$@" && { [ ! -t 0 ] || [ ! -t 1 ]; }; then
            cat >&2 <<EOF
Interactive onboarding requires a TTY.

Re-run with:
  docker run --rm -it -v <host_instance_dir>:/data your-image onboard

Or pass --non-interactive with the full required onboarding flags.
EOF
            exit 2
        fi
        if ! has_arg --instance-root "$@"; then
            set -- --instance-root "$RESOLVED_INSTANCE_ROOT" "$@"
        fi
        export TEAM_DATA_ROOT="$DATA_ROOT"
        export TEAM_INSTANCE_ROOT_HINT_FILE="$INSTANCE_ROOT_HINT_FILE"
        exec_python_entrypoint "$APP_ROOT/assistant_cli.py" onboard "$@"
        ;;
    onboard-web)
        if ! has_arg --instance-root "$@"; then
            set -- --instance-root "$RESOLVED_INSTANCE_ROOT" "$@"
        fi
        export TEAM_DATA_ROOT="$DATA_ROOT"
        export TEAM_INSTANCE_ROOT_HINT_FILE="$INSTANCE_ROOT_HINT_FILE"
        exec_python_entrypoint "$APP_ROOT/assistant_cli.py" onboard-web "$@"
        ;;
    run)
        export TEAM_DATA_ROOT="$DATA_ROOT"
        export TEAM_INSTANCE_ROOT_HINT_FILE="$INSTANCE_ROOT_HINT_FILE"
        discover_instance_config
        if [ ! -f "$RESOLVED_INSTANCE_CONFIG" ]; then
            echo "No onboarded instance found; starting browser onboarding on port 18090." >&2
            exec_python_entrypoint "$APP_ROOT/assistant_cli.py" onboard-web --instance-root "$RESOLVED_INSTANCE_ROOT"
        fi
        if [ -n "$INSTANCE_DISCOVERY_NOTE" ]; then
            echo "$INSTANCE_DISCOVERY_NOTE" >&2
        fi
        echo "Using instance config: $RESOLVED_INSTANCE_CONFIG" >&2
        set -- --config "$RESOLVED_INSTANCE_CONFIG" "$@"
        if [ "${TEAM_ASSISTANT_ENV_FILE+x}" = "x" ]; then
            set -- --env "$TEAM_ASSISTANT_ENV_FILE" "$@"
        fi
        if [ "$IMAGE_MODE" = "bundle" ]; then
            exec_python_entrypoint "$APP_ROOT/docker/run_bundle.py" "$@"
        fi
        exec_python_entrypoint "$APP_ROOT/docker/launch_headless.py" "$@"
        ;;
esac
