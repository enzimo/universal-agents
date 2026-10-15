#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/migrate_bundled_postgres_15_to_18.sh export --data-dir <dir> --old-image <image> [--env-file <team.env>] [--dump-file <path-under-data-dir>]
  scripts/migrate_bundled_postgres_15_to_18.sh import --data-dir <dir> --new-image <image> [--env-file <team.env>] [--dump-file <path-under-data-dir>] [--backup-dir <dir>]
  scripts/migrate_bundled_postgres_15_to_18.sh migrate --data-dir <dir> --old-image <image> --new-image <image> [--env-file <team.env>] [--dump-file <path-under-data-dir>] [--backup-dir <dir>]

Export a bundled PostgreSQL 15 data directory with the old image, then import
it into a fresh PostgreSQL 18 data directory with the new image.

The bundled Postgres data directory is expected at:
  <data-dir>/services/postgres/data

The default dump file is:
  <data-dir>/services/postgres_migration/pg15_dump.sql

The script auto-discovers a team.env file from the data dir when possible and
passes it into the migration containers. The default bundled Postgres cluster
uses local trust auth for the postgres superuser. If your cluster requires a
password, put one of these migration-specific credential sets in team.env or
pass --env-file explicitly:
  POSTGRES_MIGRATION_DSN=postgresql://postgres:<password>@127.0.0.1:5432/postgres
  POSTGRES_SUPERUSER_DSN=postgresql://postgres:<password>@127.0.0.1:5432/postgres
  PGUSER=postgres plus PGPASSWORD=<password>
Each of these secrets may instead be read from a file: set <NAME>_FILE to a
path inside the container (under /data); a file value beats a direct value.
EOF
}

mode="${1:-}"
if [[ -z "${mode}" || "${mode}" == "-h" || "${mode}" == "--help" ]]; then
  usage
  exit 0
fi
shift

data_dir=""
old_image=""
new_image=""
dump_file=""
backup_dir=""
env_file=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dir)
      [[ $# -ge 2 ]] || { echo "--data-dir requires a value" >&2; exit 2; }
      data_dir="$2"
      shift 2
      ;;
    --old-image)
      [[ $# -ge 2 ]] || { echo "--old-image requires a value" >&2; exit 2; }
      old_image="$2"
      shift 2
      ;;
    --new-image)
      [[ $# -ge 2 ]] || { echo "--new-image requires a value" >&2; exit 2; }
      new_image="$2"
      shift 2
      ;;
    --dump-file)
      [[ $# -ge 2 ]] || { echo "--dump-file requires a value" >&2; exit 2; }
      dump_file="$2"
      shift 2
      ;;
    --env-file)
      [[ $# -ge 2 ]] || { echo "--env-file requires a value" >&2; exit 2; }
      env_file="$2"
      shift 2
      ;;
    --backup-dir)
      [[ $# -ge 2 ]] || { echo "--backup-dir requires a value" >&2; exit 2; }
      backup_dir="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${mode}" != "export" && "${mode}" != "import" && "${mode}" != "migrate" ]]; then
  echo "Unknown mode: ${mode}" >&2
  usage >&2
  exit 2
fi
if [[ -z "${data_dir}" ]]; then
  echo "--data-dir is required" >&2
  exit 2
fi
if [[ "${mode}" != "import" && -z "${old_image}" ]]; then
  echo "--old-image is required for ${mode}" >&2
  exit 2
fi
if [[ "${mode}" != "export" && -z "${new_image}" ]]; then
  echo "--new-image is required for ${mode}" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 2
fi

data_dir="$(cd "${data_dir}" && pwd)"
postgres_root="${data_dir}/services/postgres"
postgres_data="${postgres_root}/data"

resolve_env_file() {
  if [[ -n "${env_file}" ]]; then
    if [[ ! -f "${env_file}" ]]; then
      echo "--env-file points to a missing file: ${env_file}" >&2
      exit 2
    fi
    env_parent="$(dirname "${env_file}")"
    env_file="$(cd "${env_parent}" && pwd)/$(basename "${env_file}")"
    return
  fi

  if [[ -f "${data_dir}/team.env" ]]; then
    env_file="${data_dir}/team.env"
    return
  fi
  if [[ -f "${data_dir}/team.env" ]]; then
    env_file="${data_dir}/team.env"
    return
  fi

  local hint_file hinted_root host_hinted_root
  hint_file="${data_dir}/.team-instance-root"
  if [[ -f "${hint_file}" ]]; then
    hinted_root="$(sed -n '1p' "${hint_file}" | tr -d '\r')"
    if [[ "${hinted_root}" == /data/* ]]; then
      host_hinted_root="${data_dir}/${hinted_root#/data/}"
      if [[ -f "${host_hinted_root}/team.env" ]]; then
        env_file="${host_hinted_root}/team.env"
        return
      fi
      if [[ -f "${host_hinted_root}/team.env" ]]; then
        env_file="${host_hinted_root}/team.env"
        return
      fi
    elif [[ -n "${hinted_root}" && -f "${hinted_root}/team.env" ]]; then
      env_file="${hinted_root}/team.env"
      return
    elif [[ -n "${hinted_root}" && -f "${hinted_root}/team.env" ]]; then
      env_file="${hinted_root}/team.env"
      return
    fi
  fi

  local matches_count
  matches_count="$(find "${data_dir}" -type f -name team.env 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "${matches_count}" == "1" ]]; then
    env_file="$(find "${data_dir}" -type f -name team.env 2>/dev/null | sed -n '1p')"
    return
  fi
  matches_count="$(find "${data_dir}" -type f -name team.env 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "${matches_count}" == "1" ]]; then
    env_file="$(find "${data_dir}" -type f -name team.env 2>/dev/null | sed -n '1p')"
  fi
}

resolve_env_file

docker_env_args=()
if [[ -n "${env_file}" ]]; then
  docker_env_args+=(--env-file "${env_file}")
  echo "Using env file for migration container credentials: ${env_file}"
else
  echo "No configured team.env file discovered; relying on bundled Postgres local trust auth."
fi

if [[ -z "${dump_file}" ]]; then
  dump_file="${data_dir}/services/postgres_migration/pg15_dump.sql"
fi
dump_parent="$(dirname "${dump_file}")"
mkdir -p "${dump_parent}"
dump_file="$(cd "${dump_parent}" && pwd)/$(basename "${dump_file}")"

container_path_for_dump() {
  local host_path="$1"
  case "${host_path}" in
    "${data_dir}"/*)
      printf '/data/%s\n' "${host_path#"${data_dir}/"}"
      ;;
    *)
      echo "--dump-file must be inside --data-dir so it is visible to the migration container: ${host_path}" >&2
      exit 2
      ;;
  esac
}

dump_container_path="$(container_path_for_dump "${dump_file}")"

run_postgres_export() {
  echo "Exporting PostgreSQL 15 cluster from ${postgres_data} with ${old_image}"
  docker run --rm \
    "${docker_env_args[@]}" \
    --entrypoint /bin/sh \
    -v "${data_dir}:/data" \
    "${old_image}" \
    -ceu '
      find_pg_binary() {
        if command -v "$1" >/dev/null 2>&1; then
          command -v "$1"
          return 0
        fi
        find /usr/lib/postgresql -path "*/bin/$1" -type f | sort -Vr | head -n 1
      }
      apply_pg_credentials() {
        for pair in "POSTGRES_MIGRATION_DSN_FILE:POSTGRES_MIGRATION_DSN" "POSTGRES_SUPERUSER_DSN_FILE:POSTGRES_SUPERUSER_DSN" "POSTGRES_MIGRATION_PASSWORD_FILE:POSTGRES_MIGRATION_PASSWORD" "POSTGRES_SUPERUSER_PASSWORD_FILE:POSTGRES_SUPERUSER_PASSWORD" "PGPASSWORD_FILE:PGPASSWORD"; do
          file_var="${pair%%:*}"
          value_var="${pair##*:}"
          file_path="$(eval "printf %s \"\${${file_var}:-}\"")"
          if [ -n "${file_path}" ]; then
            eval "export ${value_var}=\$(cat \"${file_path}\")"
          fi
        done
        if [ -n "${POSTGRES_MIGRATION_PASSWORD:-}" ]; then
          export PGPASSWORD="${POSTGRES_MIGRATION_PASSWORD}"
        elif [ -n "${POSTGRES_SUPERUSER_PASSWORD:-}" ]; then
          export PGPASSWORD="${POSTGRES_SUPERUSER_PASSWORD}"
        fi
        dsn="${POSTGRES_MIGRATION_DSN:-${POSTGRES_SUPERUSER_DSN:-}}"
        if [ -n "${dsn}" ]; then
          eval "$(python - "${dsn}" <<'"'"'PY'"'"'
from urllib.parse import unquote, urlsplit
import shlex
import sys
parts = urlsplit(sys.argv[1])
values = {}
if parts.username:
    values["PGUSER"] = unquote(parts.username)
if parts.password:
    values["PGPASSWORD"] = unquote(parts.password)
if parts.path and parts.path != "/":
    values["PGDATABASE"] = unquote(parts.path.lstrip("/"))
for key, value in values.items():
    print(f"export {key}={shlex.quote(value)}")
PY
)"
        fi
        export PGUSER="${PGUSER:-postgres}"
        export PGDATABASE="${PGDATABASE:-postgres}"
      }

      data="/data/services/postgres/data"
      dump="'"${dump_container_path}"'"
      postgres="$(find_pg_binary postgres)"
      pg_isready="$(find_pg_binary pg_isready)"
      pg_dumpall="$(find_pg_binary pg_dumpall)"
      test -n "${postgres}" && test -n "${pg_isready}" && test -n "${pg_dumpall}"
      if [ ! -f "${data}/PG_VERSION" ]; then
        echo "No bundled Postgres data directory found at ${data}" >&2
        exit 1
      fi
      source_major="$(tr -d "[:space:]" < "${data}/PG_VERSION")"
      if [ "${source_major}" != "15" ]; then
        echo "Expected source PostgreSQL major 15, found ${source_major} in ${data}/PG_VERSION" >&2
        exit 1
      fi
      apply_pg_credentials

      su -s /bin/sh postgres -c "exec ${postgres} -D ${data} -h 127.0.0.1 -p 5432 -k /tmp" &
      pg_pid="$!"
      trap "kill ${pg_pid} >/dev/null 2>&1 || true; wait ${pg_pid} >/dev/null 2>&1 || true" EXIT
      for _ in $(seq 1 90); do
        if "${pg_isready}" -h 127.0.0.1 -p 5432 -U "${PGUSER}" -d "${PGDATABASE}" >/dev/null 2>&1; then
          break
        fi
        sleep 1
      done
      "${pg_isready}" -h 127.0.0.1 -p 5432 -U "${PGUSER}" -d "${PGDATABASE}"
      mkdir -p "$(dirname "${dump}")"
      "${pg_dumpall}" --quote-all-identifiers -h 127.0.0.1 -p 5432 -U "${PGUSER}" > "${dump}.tmp"
      mv "${dump}.tmp" "${dump}"
    '
  echo "Wrote dump: ${dump_file}"
}

prepare_postgres18_data_dir() {
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ -z "${backup_dir}" ]]; then
    backup_dir="${data_dir}/services/postgres.pg15-backup-${stamp}"
  fi

  backup_container_path="$(container_path_for_dump "${backup_dir}")"
  echo "Preparing fresh PostgreSQL 18 data directory under ${postgres_data}"
  docker run --rm \
    --entrypoint /bin/sh \
    -v "${data_dir}:/data" \
    "${new_image}" \
    -ceu '
      root="/data/services/postgres"
      data="${root}/data"
      backup="'"${backup_container_path}"'"
      if [ -f "${data}/PG_VERSION" ]; then
        existing_major="$(tr -d "[:space:]" < "${data}/PG_VERSION")"
        if [ "${existing_major}" = "18" ]; then
          echo "Refusing to import: ${data} already contains a PostgreSQL 18 cluster" >&2
          exit 1
        fi
        if [ -e "${backup}" ]; then
          echo "Backup destination already exists: ${backup}" >&2
          exit 1
        fi
        echo "Moving existing Postgres service directory to ${backup}"
        mv "${root}" "${backup}"
      elif [ -d "${data}" ] && [ -n "$(find "${data}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
        echo "Refusing to import: ${data} is non-empty but has no PG_VERSION" >&2
        exit 1
      fi
      mkdir -p "${data}"
    '
}

run_postgres_import() {
  if [[ ! -f "${dump_file}" ]]; then
    echo "Dump file not found: ${dump_file}" >&2
    exit 1
  fi
  prepare_postgres18_data_dir

  echo "Importing dump into a fresh PostgreSQL 18 cluster with ${new_image}"
  docker run --rm \
    "${docker_env_args[@]}" \
    --entrypoint /bin/sh \
    -v "${data_dir}:/data" \
    "${new_image}" \
    -ceu '
      find_pg_binary() {
        if command -v "$1" >/dev/null 2>&1; then
          command -v "$1"
          return 0
        fi
        find /usr/lib/postgresql -path "*/bin/$1" -type f | sort -Vr | head -n 1
      }
      apply_pg_credentials() {
        for pair in "POSTGRES_MIGRATION_DSN_FILE:POSTGRES_MIGRATION_DSN" "POSTGRES_SUPERUSER_DSN_FILE:POSTGRES_SUPERUSER_DSN" "POSTGRES_MIGRATION_PASSWORD_FILE:POSTGRES_MIGRATION_PASSWORD" "POSTGRES_SUPERUSER_PASSWORD_FILE:POSTGRES_SUPERUSER_PASSWORD" "PGPASSWORD_FILE:PGPASSWORD"; do
          file_var="${pair%%:*}"
          value_var="${pair##*:}"
          file_path="$(eval "printf %s \"\${${file_var}:-}\"")"
          if [ -n "${file_path}" ]; then
            eval "export ${value_var}=\$(cat \"${file_path}\")"
          fi
        done
        if [ -n "${POSTGRES_MIGRATION_PASSWORD:-}" ]; then
          export PGPASSWORD="${POSTGRES_MIGRATION_PASSWORD}"
        elif [ -n "${POSTGRES_SUPERUSER_PASSWORD:-}" ]; then
          export PGPASSWORD="${POSTGRES_SUPERUSER_PASSWORD}"
        fi
        dsn="${POSTGRES_MIGRATION_DSN:-${POSTGRES_SUPERUSER_DSN:-}}"
        if [ -n "${dsn}" ]; then
          eval "$(python - "${dsn}" <<'"'"'PY'"'"'
from urllib.parse import unquote, urlsplit
import shlex
import sys
parts = urlsplit(sys.argv[1])
values = {}
if parts.username:
    values["PGUSER"] = unquote(parts.username)
if parts.password:
    values["PGPASSWORD"] = unquote(parts.password)
if parts.path and parts.path != "/":
    values["PGDATABASE"] = unquote(parts.path.lstrip("/"))
for key, value in values.items():
    print(f"export {key}={shlex.quote(value)}")
PY
)"
        fi
        export PGUSER="${PGUSER:-postgres}"
        export PGDATABASE="${PGDATABASE:-postgres}"
      }

      data="/data/services/postgres/data"
      dump="'"${dump_container_path}"'"
      initdb="$(find_pg_binary initdb)"
      postgres="$(find_pg_binary postgres)"
      pg_isready="$(find_pg_binary pg_isready)"
      psql="$(find_pg_binary psql)"
      test -n "${initdb}" && test -n "${postgres}" && test -n "${pg_isready}" && test -n "${psql}"
      "${postgres}" --version | grep -E "PostgreSQL 18\\."
      apply_pg_credentials
      chown -R postgres:postgres /data/services/postgres
      su -s /bin/sh postgres -c "exec ${initdb} -D ${data} --username postgres --auth-local trust --auth-host trust"
      su -s /bin/sh postgres -c "exec ${postgres} -D ${data} -h 127.0.0.1 -p 5432 -k /tmp" &
      pg_pid="$!"
      trap "kill ${pg_pid} >/dev/null 2>&1 || true; wait ${pg_pid} >/dev/null 2>&1 || true" EXIT
      for _ in $(seq 1 90); do
        if "${pg_isready}" -h 127.0.0.1 -p 5432 -U "${PGUSER}" -d "${PGDATABASE}" >/dev/null 2>&1; then
          break
        fi
        sleep 1
      done
      "${pg_isready}" -h 127.0.0.1 -p 5432 -U "${PGUSER}" -d "${PGDATABASE}"
      "${psql}" -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U "${PGUSER}" -d "${PGDATABASE}" -f "${dump}"
      "${psql}" -h 127.0.0.1 -p 5432 -U "${PGUSER}" -d "${PGDATABASE}" -Atc "SHOW server_version;"
    '
  echo "Imported dump: ${dump_file}"
  if [[ -n "${backup_dir}" ]]; then
    echo "Old Postgres service backup: ${backup_dir}"
  fi
}

case "${mode}" in
  export)
    run_postgres_export
    ;;
  import)
    run_postgres_import
    ;;
  migrate)
    run_postgres_export
    run_postgres_import
    ;;
esac
