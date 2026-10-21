#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
uv_executable="${UV_BIN:-uv}"
required_uv_version="uv 0.13.4"

actual_uv_version="$(${uv_executable} --version)"
if [[ "${actual_uv_version}" != "${required_uv_version}" \
  && "${actual_uv_version}" != "${required_uv_version} ("* ]]; then
  echo "Python lock generation requires ${required_uv_version}; found ${actual_uv_version}." >&2
  exit 2
fi

cd "${repo_root}"

compile_args=(
  --universal
  --python-version 3.12
  --generate-hashes
  --no-progress
  --custom-compile-command "./scripts/regenerate_python_lockfiles.sh"
  "$@"
)

"${uv_executable}" pip compile \
  requirements.txt \
  --output-file requirements.lock.txt \
  "${compile_args[@]}"

"${uv_executable}" pip compile \
  requirements-dev.txt \
  --constraint requirements.lock.txt \
  --output-file requirements-dev.lock.txt \
  "${compile_args[@]}"

"${uv_executable}" pip compile \
  requirements.skillspector.txt \
  --output-file requirements.skillspector.lock.txt \
  "${compile_args[@]}"
