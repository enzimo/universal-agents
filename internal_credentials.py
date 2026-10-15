"""Generate and maintain env-owned internal service credentials."""

from __future__ import annotations

import argparse
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import quote

from access_control.identity import (
    ENV_GATEWAY_SIGNING_KEY,
    ENV_SCHEDULER_SIGNING_KEY,
    ENV_VERIFY_KEYS,
    ISSUER_GATEWAY,
    ISSUER_SCHEDULER,
    generate_signing_key,
    public_entry,
    verify_keys_value,
)


INTERNAL_ENV_KEYS = {
    "ACCESS_GATEWAY_SIGNING_KEY",
    "ACCESS_SCHEDULER_SIGNING_KEY",
    "ACCESS_CONTEXT_VERIFY_KEYS",
    "NATS_USER",
    "NATS_PASSWORD",
    "POSTGRES_DSN",
    "ACTIVITY_COLLECTOR_USER_TOKEN",
    "ACTIVITY_COLLECTOR_ADMIN_TOKEN",
    "ACTIVITY_COLLECTOR_INGEST_TOKEN",
    "WEB_CHAT_AUTH_TOKEN",
    "SECURE_TOOL_BROKER_TOKEN",
    "GOG_KEYRING_PASSWORD",
}


@dataclass(frozen=True)
class InternalCredentialRequest:
    instance_name: str
    storage_backend: str = "sqlite"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str | None = None
    username: str = "generated"
    include_nats_credentials: bool = False
    include_web_chat_token: bool = True
    existing_postgres_dsn: str | None = None
    rotate: bool = False


def slugify_instance_name(instance_name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", str(instance_name or "").strip().lower()).strip("_")
    return slug or "assistant"


def generated_username(instance_name: str) -> str:
    return f"team_{slugify_instance_name(instance_name)}"


def _random_secret(prefix: str = "") -> str:
    value = secrets.token_urlsafe(32)
    return f"{prefix}{value}" if prefix else value


def _resolve_username(request: InternalCredentialRequest) -> str:
    raw = str(request.username or "generated").strip()
    if raw in {"", "generated"}:
        return generated_username(request.instance_name)
    return raw


def _postgres_dsn(*, username: str, password: str, host: str, port: int, database: str) -> str:
    return (
        "postgresql://"
        f"{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{int(port)}/{database}"
    )


def generate_internal_credentials(
    request: InternalCredentialRequest,
    *,
    existing: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return env values for internal services, preserving existing values unless rotating."""
    existing_values = {str(k): str(v) for k, v in dict(existing or {}).items() if v not in (None, "")}

    def pick(key: str, generated: str, *, unsafe_values: Iterable[str] = ()) -> str | None:
        if existing_values.get(f"{key}_FILE"):
            return None
        existing_value = existing_values.get(key)
        unsafe = {str(value).strip() for value in unsafe_values}
        if not request.rotate and existing_value and existing_value.strip() not in unsafe:
            return existing_value
        return generated

    values: dict[str, str] = {}

    activity_user_token = pick("ACTIVITY_COLLECTOR_USER_TOKEN", _random_secret("acu_"))
    if activity_user_token is not None:
        values["ACTIVITY_COLLECTOR_USER_TOKEN"] = activity_user_token
    activity_admin_token = pick("ACTIVITY_COLLECTOR_ADMIN_TOKEN", _random_secret("aca_"))
    if activity_admin_token is not None:
        values["ACTIVITY_COLLECTOR_ADMIN_TOKEN"] = activity_admin_token
    activity_ingest_token = pick("ACTIVITY_COLLECTOR_INGEST_TOKEN", _random_secret("aci_"))
    if activity_ingest_token is not None:
        values["ACTIVITY_COLLECTOR_INGEST_TOKEN"] = activity_ingest_token
    if request.include_web_chat_token:
        web_chat_auth_token = pick("WEB_CHAT_AUTH_TOKEN", _random_secret("uaweb_"))
        if web_chat_auth_token is not None:
            values["WEB_CHAT_AUTH_TOKEN"] = web_chat_auth_token

    if request.include_nats_credentials and not existing_values.get("NATS_CREDENTIALS_FILE"):
        nats_user = pick("NATS_USER", _resolve_username(request), unsafe_values={"guest"})
        if nats_user is not None:
            values["NATS_USER"] = nats_user
        nats_password = pick("NATS_PASSWORD", _random_secret("nats_"), unsafe_values={"guest"})
        if nats_password is not None:
            values["NATS_PASSWORD"] = nats_password

    storage_backend = str(request.storage_backend or "sqlite").strip().lower()
    if storage_backend == "postgresql":
        storage_backend = "postgres"
    if storage_backend == "postgres":
        if existing_values.get("POSTGRES_DSN_FILE"):
            postgres_dsn = None
        elif request.existing_postgres_dsn and not request.rotate and not existing_values.get("POSTGRES_DSN"):
            postgres_dsn = request.existing_postgres_dsn
        else:
            postgres_username = _resolve_username(request)
            postgres_password = _random_secret()
            database = request.postgres_db or f"{slugify_instance_name(request.instance_name)}_runtime"
            postgres_dsn = pick(
                "POSTGRES_DSN",
                _postgres_dsn(
                    username=postgres_username,
                    password=postgres_password,
                    host=request.postgres_host,
                    port=request.postgres_port,
                    database=database,
                ),
            )
        if postgres_dsn is not None:
            values["POSTGRES_DSN"] = postgres_dsn

    # The secure tool broker always runs: its token, and the keyring password
    # of the Google Workspace CLI it runs, are always generated.
    broker_token = pick("SECURE_TOOL_BROKER_TOKEN", _random_secret("stb_"))
    if broker_token is not None:
        values["SECURE_TOOL_BROKER_TOKEN"] = broker_token
    gog_keyring_password = pick("GOG_KEYRING_PASSWORD", _random_secret("gog_"))
    if gog_keyring_password is not None:
        values["GOG_KEYRING_PASSWORD"] = gog_keyring_password

    values.update(_task_context_keys(existing_values, rotate=request.rotate))
    return values


def internal_credential_names(request: InternalCredentialRequest) -> tuple[str, ...]:
    """The names ``generate_internal_credentials`` creates for ``request``.

    Installs whose secrets come from the environment (launch.secrets_source:
    environment) generate nothing, so onboarding uses this list to say which
    values the environment must hold. Keep it in step with
    ``generate_internal_credentials``; tests compare the two.
    """

    names = [
        "ACTIVITY_COLLECTOR_USER_TOKEN",
        "ACTIVITY_COLLECTOR_ADMIN_TOKEN",
        "ACTIVITY_COLLECTOR_INGEST_TOKEN",
    ]
    if request.include_web_chat_token:
        names.append("WEB_CHAT_AUTH_TOKEN")
    if request.include_nats_credentials:
        names += ["NATS_USER", "NATS_PASSWORD"]
    if str(request.storage_backend or "sqlite").strip().lower() in {"postgres", "postgresql"}:
        names.append("POSTGRES_DSN")
    names += ["SECURE_TOOL_BROKER_TOKEN", "GOG_KEYRING_PASSWORD"]
    names += [ENV_GATEWAY_SIGNING_KEY, ENV_SCHEDULER_SIGNING_KEY, ENV_VERIFY_KEYS]
    return tuple(names)


def _task_context_keys(existing: Mapping[str, str], *, rotate: bool) -> dict[str, str]:
    """Ed25519 keys that sign task contexts (access_control.identity).

    The gateway and the scheduler each get a private key; the verification list
    is always derived from those private keys, so the two can never disagree.
    """

    keys: dict[str, str] = {}
    entries = []
    for issuer, name in (
        (ISSUER_GATEWAY, ENV_GATEWAY_SIGNING_KEY),
        (ISSUER_SCHEDULER, ENV_SCHEDULER_SIGNING_KEY),
    ):
        value = "" if rotate else str(existing.get(name) or "").strip()
        if not value:
            value, _entry = generate_signing_key(issuer)
        keys[name] = value
        entries.append(public_entry(issuer, value))
    keys[ENV_VERIFY_KEYS] = verify_keys_value(entries)
    return keys


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _format_env_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_env(values: Mapping[str, str]) -> str:
    return "\n".join(f"{key}={_format_env_value(str(values[key]))}" for key in sorted(values)) + "\n"


def merge_env_file(
    path: Path,
    generated_values: Mapping[str, str],
    *,
    rotate: bool = False,
    remove_keys: Iterable[str] = (),
) -> dict[str, str]:
    """Merge generated internal values into a dotenv file while preserving other lines."""
    existing = parse_env_file(path)
    merged = dict(existing)
    stale_keys = {str(key) for key in remove_keys if key not in generated_values}
    for key in stale_keys:
        merged.pop(key, None)
    for key, value in generated_values.items():
        # The verification list is derived from the private keys, so it is
        # always rewritten to match them.
        if rotate or not merged.get(key) or key == ENV_VERIFY_KEYS:
            merged[key] = str(value)

    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for raw_line in existing_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            output.append(raw_line)
            continue
        key = raw_line.split("=", 1)[0].strip()
        if key in stale_keys:
            seen.add(key)
            continue
        if key in generated_values:
            output.append(f"{key}={_format_env_value(merged[key])}")
            seen.add(key)
        else:
            output.append(raw_line)
            seen.add(key)

    missing_keys = [key for key in generated_values if key not in seen]
    if missing_keys:
        if output and output[-1].strip():
            output.append("")
        output.append("# Generated internal service credentials. Keep this file outside the workspace.")
        for key in sorted(missing_keys):
            output.append(f"{key}={_format_env_value(merged[key])}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    return merged


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate internal service credentials for NATS-backed Team Agents.")
    parser.add_argument("--instance-name", required=True)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--storage-backend", choices=["sqlite", "postgres"], default="sqlite")
    parser.add_argument("--postgres-host", default="localhost")
    parser.add_argument("--postgres-port", type=int, default=5432)
    parser.add_argument("--postgres-db", default=None)
    parser.add_argument("--username", default="generated")
    parser.add_argument("--include-nats-credentials", action="store_true")
    parser.add_argument("--rotate", action="store_true")
    parser.add_argument("--print", action="store_true", dest="print_values")
    parser.add_argument("--write", action="store_true")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    env_path = Path(args.env_file).expanduser().resolve() if args.env_file else None
    existing = parse_env_file(env_path) if env_path else {}
    request = InternalCredentialRequest(
        instance_name=args.instance_name,
        storage_backend=args.storage_backend,
        postgres_host=args.postgres_host,
        postgres_port=args.postgres_port,
        postgres_db=args.postgres_db,
        username=args.username,
        include_nats_credentials=args.include_nats_credentials,
        rotate=args.rotate,
    )
    generated = generate_internal_credentials(request, existing=existing)
    if args.write:
        if env_path is None:
            raise SystemExit("--write requires --env-file")
        merged = merge_env_file(env_path, generated, rotate=args.rotate)
        if args.print_values:
            print(render_env({key: merged[key] for key in generated if key in merged}), end="")
        return
    print(render_env(generated), end="")


if __name__ == "__main__":
    main()
