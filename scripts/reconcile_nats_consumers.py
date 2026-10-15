#!/usr/bin/env python3
"""Inspect or delete abandoned Universal Agents JetStream consumers."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from collections import Counter
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence


def _resolve_project_root() -> Path:
    candidates = [
        Path(os.environ["UA_APP_ROOT"]).expanduser()
        if os.environ.get("UA_APP_ROOT")
        else None,
        Path(__file__).resolve().parents[1],
        Path.cwd(),
        Path("/app"),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = candidate.resolve()
        if (resolved / "assistant_gateway").is_dir() and (
            resolved / "custom_transport"
        ).is_dir():
            return resolved
    raise RuntimeError(
        "Could not locate the Universal Agents application root. Set UA_APP_ROOT "
        "to the directory containing assistant_gateway and custom_transport."
    )


PROJECT_ROOT = _resolve_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from assistant_gateway.runtime_config import load_runtime_config_file  # noqa: E402
from custom_transport.nats_consumer_reconciler import (  # noqa: E402
    ConsumerReconciliationResult,
    NatsConsumerReconciler,
)
from custom_transport.subjects import NatsSubjectMapper  # noqa: E402
from default_values import TRANSPORT_DEFAULTS  # noqa: E402
from docker.nats_service import (  # noqa: E402
    NATS_PASSWORD_ENV,
    NATS_USER_ENV,
    apply_local_nats_env,
    ensure_local_nats_credentials,
    local_nats_enabled_from_env,
    start_local_nats,
)


LOGGER = logging.getLogger("reconcile_nats_consumers")
DEFAULT_CONFIG_PATH = Path("/data/instance_config.yaml")


def resolve_reconciliation_team_id(
    config: Any,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve the team id from the normalized runtime configuration."""

    env = environment if environment is not None else os.environ
    for candidate in (
        config.get("agent.default_team_id"),
        config.get("agent.bootstrap.team_id"),
        config.get("manager.team_id"),
        config.get("agent.team_id"),
        env.get("DEFAULT_TEAM_ID"),
    ):
        team_id = str(candidate or "").strip()
        if team_id and team_id != "NO_TEAM":
            return team_id
    raise RuntimeError(
        "Could not resolve a team id from agent.default_team_id, "
        "agent.bootstrap.team_id, or DEFAULT_TEAM_ID. Refusing to scan an "
        "unscoped JetStream account."
    )


async def reconcile_nats_consumers(
    *,
    config_path: str | Path,
    apply: bool,
    minimum_age_seconds: float,
    stable_agent_ids: Sequence[str] = (),
) -> ConsumerReconciliationResult:
    """Load runtime configuration and run one consumer reconciliation pass."""

    config = load_runtime_config_file(config_path)
    servers = list(
        config.get("transport.nats_servers")
        or [config.get("transport.nats_url", TRANSPORT_DEFAULTS.nats_url.value)]
    )
    team_id = resolve_reconciliation_team_id(config)
    configured_stable_id = str(config.get("agent.agent_id") or "").strip()
    protected_ids = list(stable_agent_ids)
    if configured_stable_id:
        protected_ids.append(configured_stable_id)

    try:
        import nats
    except ImportError as exc:
        raise RuntimeError("nats-py is required to reconcile consumers.") from exc

    connection_kwargs = {
        "servers": servers,
        "name": config.get("transport.nats_name") or "ua-consumer-reconciler",
        "allow_reconnect": False,
    }
    if config.get("transport.nats_user"):
        connection_kwargs["user"] = config.get("transport.nats_user")
    if config.get("transport.nats_password"):
        connection_kwargs["password"] = config.get("transport.nats_password")
    if config.get("transport.credentials_file"):
        connection_kwargs["user_credentials"] = config.get("transport.credentials_file")

    connection = await nats.connect(**connection_kwargs)
    try:
        mapper = NatsSubjectMapper(
            team_id=team_id,
            subject_prefix=str(config.get("transport.subject_prefix") or ""),
        )
        LOGGER.info(
            "Resolved NATS consumer reconciliation scope. "
            "event=nats_consumer_reconciliation_scope team_id=%s "
            "subject_prefix=%s stream_names=%s apply=%s",
            team_id,
            mapper.prefix,
            (
                mapper.work_stream_name(),
                mapper.presence_stream_name(),
            ),
            apply,
        )
        reconciler = NatsConsumerReconciler(
            jetstream=connection.jetstream(),
            mapper=mapper,
            stable_agent_ids=protected_ids,
            logger=LOGGER,
        )
        return await reconciler.reconcile(
            apply=apply,
            minimum_age_seconds=minimum_age_seconds,
        )
    finally:
        await connection.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Runtime config path (default: /data/instance_config.yaml).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete eligible consumers. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--confirm-runtime-stopped",
        action="store_true",
        help="Confirm that every Universal Agents process for this team is stopped.",
    )
    parser.add_argument(
        "--minimum-age-seconds",
        type=float,
        default=3600.0,
        help="Minimum consumer age required for deletion (default: 3600).",
    )
    parser.add_argument(
        "--stable-agent-id",
        action="append",
        default=[],
        help="Additional agent ID to protect. May be repeated.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log one line for every scanned consumer.",
    )
    parser.add_argument(
        "--start-local-nats",
        action="store_true",
        help=(
            "Start the bundled NATS server for this maintenance run. Use this "
            "only while the normal bundle runtime is stopped."
        ),
    )
    return parser


def _stop_local_nats(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.minimum_age_seconds < 0:
        parser.error("--minimum-age-seconds cannot be negative.")
    if args.apply and not args.confirm_runtime_stopped:
        parser.error(
            "--apply requires --confirm-runtime-stopped. Stop every Universal "
            "Agents process for this team before deleting legacy consumers."
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    local_nats_process = None
    try:
        if args.start_local_nats:
            local_nats_env = dict(os.environ)
            apply_local_nats_env(local_nats_env)
            local_nats_process = start_local_nats(
                env=local_nats_env,
                logger=LOGGER,
                cwd=PROJECT_ROOT,
            )
            os.environ.update(
                {
                    "NATS_URL": local_nats_env["NATS_URL"],
                    "NATS_SERVERS": local_nats_env["NATS_SERVERS"],
                    "NATS_JETSTREAM_ENABLED": "true",
                    NATS_USER_ENV: local_nats_env[NATS_USER_ENV],
                    NATS_PASSWORD_ENV: local_nats_env[NATS_PASSWORD_ENV],
                }
            )
        elif local_nats_enabled_from_env(os.environ, default=True):
            # The running local server requires the credentials it generated.
            ensure_local_nats_credentials(os.environ, logger=LOGGER, create=False)
        result = asyncio.run(
            reconcile_nats_consumers(
                config_path=args.config,
                apply=bool(args.apply),
                minimum_age_seconds=float(args.minimum_age_seconds),
                stable_agent_ids=tuple(args.stable_agent_id),
            )
        )
    except Exception:
        LOGGER.exception(
            "NATS consumer reconciliation failed. "
            "event=nats_consumer_reconciliation_failed config_path=%s apply=%s",
            Path(args.config).expanduser(),
            bool(args.apply),
        )
        return 1
    finally:
        if local_nats_process is not None:
            _stop_local_nats(local_nats_process)

    reason_counts = Counter(decision.reason for decision in result.decisions)
    LOGGER.info(
        "NATS consumer reconciliation complete. "
        "event=nats_consumer_reconciliation_complete apply=%s scanned_count=%s "
        "candidate_count=%s deleted_count=%s account_consumer_count=%s "
        "account_max_consumers=%s reasons=%s",
        result.applied,
        result.scanned_count,
        result.candidate_count,
        result.deleted_count,
        result.account_consumer_count,
        result.account_max_consumers,
        dict(sorted(reason_counts.items())),
    )
    if (
        result.scanned_count == 0
        and result.account_consumer_count is not None
        and result.account_consumer_count > 0
    ):
        LOGGER.warning(
            "The resolved team streams contained no consumers even though the "
            "JetStream account contains consumers. Check the reconciliation "
            "scope log before applying cleanup. "
            "event=nats_consumer_reconciliation_scope_empty "
            "account_consumer_count=%s",
            result.account_consumer_count,
        )
    if (
        result.account_consumer_count is not None
        and result.account_max_consumers is not None
        and result.account_max_consumers > 0
        and result.account_consumer_count >= result.account_max_consumers * 0.9
    ):
        LOGGER.warning(
            "JetStream consumer usage is at or above 90 percent. "
            "event=nats_consumer_capacity_high account_consumer_count=%s "
            "account_max_consumers=%s",
            result.account_consumer_count,
            result.account_max_consumers,
        )
    if args.verbose:
        for decision in result.decisions:
            LOGGER.info(
                "Consumer decision. event=nats_consumer_reconciliation_decision "
                "stream_name=%s consumer_name=%s owner_agent_id=%s "
                "consumer_role=%s listening=%s num_pending=%s "
                "num_ack_pending=%s delete=%s reason=%s",
                decision.stream_name,
                decision.consumer_name,
                decision.owner_agent_id,
                decision.consumer_role,
                decision.listening,
                decision.num_pending,
                decision.num_ack_pending,
                decision.delete,
                decision.reason,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
