#!/usr/bin/env bash
set -euo pipefail

app_root="${1:-/app}"

if [[ ! -d "${app_root}" ]]; then
  echo "Application root does not exist: ${app_root}" >&2
  exit 2
fi

echo "Compiling Python sources under ${app_root} to legacy .pyc files..."
python -m compileall -q -b "${app_root}"

# Every Python program the image starts by file path: docker/entrypoint.sh,
# docker/run_bundle.py and docker/launch_headless.py find each one through its
# .pyc, and the agents' code sandbox starts its helper the same way. A program
# missing here stops the build instead of failing when the container starts.
# tests/test_docker_python_entrypoints.py checks that this list names every
# program those launchers start.
required_entrypoints=(
  "${app_root}/assistant_cli.py"
  "${app_root}/env_secrets.py"
  "${app_root}/strands_main.py"
  "${app_root}/custom_tools/code_sandbox_exec.py"
  "${app_root}/docker/run_bundle.py"
  "${app_root}/docker/launch_headless.py"
  "${app_root}/scripts/reconcile_nats_consumers.py"
)
missing_compiled=()
for source_path in "${required_entrypoints[@]}"; do
  compiled_path="${source_path}c"
  if [[ ! -f "${compiled_path}" ]]; then
    missing_compiled+=("${compiled_path}")
  fi
done
if (( ${#missing_compiled[@]} > 0 )); then
  printf 'Missing compiled Python entrypoint: %s\n' "${missing_compiled[@]}" >&2
  exit 2
fi

source_count="$(find "${app_root}" -type f \( -name '*.py' -o -name '*.pyi' \) | wc -l | tr -d ' ')"
find "${app_root}" -type f \( -name '*.py' -o -name '*.pyi' \) -delete
find "${app_root}" -type d -name __pycache__ -prune -exec rm -rf '{}' +

remaining_count="$(find "${app_root}" -type f \( -name '*.py' -o -name '*.pyi' \) | wc -l | tr -d ' ')"
if [[ "${remaining_count}" != "0" ]]; then
  echo "Python source stripping left ${remaining_count} source files under ${app_root}." >&2
  exit 2
fi

echo "Removed ${source_count} Python source files from ${app_root}; runtime will use .pyc entrypoints."
