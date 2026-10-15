#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    cat <<'EOF'
Usage: scripts/run_tests.sh [fast|live|live-nats|external|full|collect] [pytest arguments...]

Profiles:
  fast       Hermetic tests that do not require live or external services (default).
  live       Live local/service tests, excluding external providers.
  live-nats  Live tests that require NATS/JetStream.
  external   Opt-in tests that call external providers or mutate auth state.
  full       Every collected pytest test; opt-in tests retain their own skip guards.
  collect    Collect every pytest test without executing it.
EOF
}

profile="${1:-fast}"
profile_args=()
if [[ $# -gt 0 ]]; then
    shift
fi

if [[ -n "${PYTEST:-}" ]]; then
    pytest_command=("${PYTEST}")
elif [[ -x "${ROOT_DIR}/.venv/bin/pytest" ]]; then
    pytest_command=("${ROOT_DIR}/.venv/bin/pytest")
else
    pytest_command=("${PYTHON:-python3}" -m pytest)
fi

case "${profile}" in
    fast)
        profile_args=(-m "not live and not external")
        ;;
    live)
        profile_args=(-m "live and not external")
        ;;
    live-nats)
        profile_args=(-m "live and nats and not external")
        ;;
    external)
        profile_args=(-m "external" --tb=short --show-capture=no)
        ;;
    full)
        profile_args=(--tb=short --show-capture=no)
        ;;
    collect)
        profile_args=(--collect-only)
        ;;
    -h|--help|help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

cd "${ROOT_DIR}"
if [[ ${#profile_args[@]} -gt 0 ]]; then
    exec "${pytest_command[@]}" "${profile_args[@]}" "$@"
fi
exec "${pytest_command[@]}" "$@"
