from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import tempfile
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - live utility fallback
    load_dotenv = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nats

from config_loader import load_config, resolve_agent_type_settings
from custom_agents.context_engine.thread_renderer import BudgetedThreadContextRenderer
from custom_agents.conversation_compaction import ConversationCompactionConfig, build_conversation_compactor
from custom_agents.conversation_store import ConversationStore
from live_storage_utils import (
    apply_storage_env,
    reset_postgres_storage,
    resolve_live_storage_settings,
    storage_identifier,
)


def _redact_url(raw_url: str) -> str:
    parts = urlsplit(raw_url)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, hostinfo = netloc.rsplit("@", 1)
        if ":" in userinfo:
            username, _password = userinfo.split(":", 1)
            userinfo = f"{username}:***"
        netloc = f"{userinfo}@{hostinfo}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def _verify_live_nats(config_path: str) -> str:
    config = load_config(config_path)
    servers = config.get("transport.nats_servers") or [config.get("transport.nats_url", "nats://127.0.0.1:4222")]
    connection = await nats.connect(servers=servers)
    try:
        await connection.flush(timeout=2)
    finally:
        await connection.close()
    return ", ".join(_redact_url(server) for server in servers)


def _seed_turns(store: ConversationStore, *, conversation_id: str, token: str) -> None:
    store.ensure_thread(
        conversation_id=conversation_id,
        team_id="live-context-epoch",
        owner_agent_id="agent_live_context_epoch",
        owner_agent_name="live-context-agent",
        requester_name="user",
        requester_id="user",
        root_task_id="task_1",
        subject_preview="context epoch live verification",
    )
    for index in range(1, 6):
        task_id = f"task_{index}"
        store.append_turn(
            conversation_id=conversation_id,
            task_id=task_id,
            followup_to_task_id=f"task_{index - 1}" if index > 1 else None,
            owner_agent_id="agent_live_context_epoch",
            requester_name="user",
            requester_id="user",
            correlation_id=f"corr_{index}",
            problem_text=(
                f"Turn {index} live-config context background token {token}. "
                f"raw-only-sentinel-live-{index if index == 1 else 'suffix'}"
            ),
            requester_type="user",
            delivery_state="not_applicable",
            task_status="completed",
        )
        store.update_turn_completion(
            task_id=task_id,
            task_status="completed",
            final_answer_text=f"Acknowledged turn {index} with token {token}.",
            turn_summary_text=f"Turn {index} preserved token {token} and live-config summary state.",
            user_request_summary=f"User request summary {index} with token {token}.",
            assistant_response_summary=f"Assistant response summary {index} with token {token}.",
            context_summary_text=f"Context summary {index} with token {token}.",
            artifact_summary=[],
            tool_artifact_manifest=[],
            summary_generator="live_config_synthetic",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify context epochs using live env/config, Postgres storage, and a live NATS connection."
    )
    parser.add_argument("--env", default="debug.env")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--report-path", default="test_results/test_data/live_context_epoch_verification_report.json")
    args = parser.parse_args()

    if args.env and load_dotenv is not None:
        load_dotenv(args.env, override=True)
        print(f"Loaded environment variables from {args.env}")
    cfg = load_config(args.config)
    settings = resolve_agent_type_settings(cfg, "CodeWritingAgent")
    print(f"Loaded configuration from {Path(args.config).resolve()}")
    redacted_nats_servers = asyncio.run(_verify_live_nats(args.config))
    print(f"Verified live NATS connection to {redacted_nats_servers}")

    storage_settings = resolve_live_storage_settings(
        workspace_dir=Path(__file__).resolve().parents[1],
        config_path=args.config,
    )
    if storage_settings.backend != "postgres":
        raise AssertionError(
            f"Live context epoch verification requires PostgreSQL storage; resolved {storage_settings.backend!r}."
        )
    reset_postgres_storage(storage_settings)
    apply_storage_env(os.environ, storage_settings)
    print(f"Verified PostgreSQL live storage at {storage_identifier(storage_settings)}")

    store = ConversationStore(db_path="", storage_settings=storage_settings, artifacts_dir=Path(tempfile.mkdtemp(prefix="ua-conversation-artifacts-")))
    conversation_id = "conversation_live_context_epoch"
    token = "live-context-token-epoch"
    _seed_turns(store, conversation_id=conversation_id, token=token)

    compactor = build_conversation_compactor(
        "heuristic_sliding_window",
        config=ConversationCompactionConfig(
            context_window_tokens=int(settings.get("context_window_tokens") or 16000),
            trigger_ratio=0.01,
            target_ratio=0.01,
            min_recent_turns=1,
            max_recent_turns=1,
            trigger_turn_count=2,
        ),
        logger=logging.getLogger("live_context_epoch_verification"),
    )
    result = compactor.compact_if_needed(
        store=store,
        conversation_id=conversation_id,
        latest_request_text="What token was preserved?",
    )
    epochs = store.list_context_epochs(conversation_id, limit=20)
    if not result.compacted or not epochs:
        raise AssertionError("Expected live-config compaction to create at least one epoch.")
    if len(epochs) > 1 and not any(epoch.get("rehydration_manifest", {}).get("source_epoch_ids") for epoch in epochs[1:]):
        raise AssertionError("Expected later epochs to record prior epoch lineage.")
    for field_name in ["user_request_summary", "assistant_response_summary", "context_summary_text"]:
        if not store.get_turn_by_task_id("task_1").get(field_name):
            raise AssertionError(f"Turn summary field was lost: {field_name}")

    rendered = BudgetedThreadContextRenderer().render(
        store=store,
        conversation_id=conversation_id,
        exclude_task_id="task_5",
        available_tokens=20_000,
    )
    latest_epoch = epochs[0]
    if latest_epoch["context_epoch_id"] not in rendered.text:
        raise AssertionError("Rendered context did not include latest epoch boundary.")
    if "### Turn 1" in rendered.text or "User Request:\nTurn 1" in rendered.text:
        raise AssertionError("Renderer replayed a pre-epoch raw turn block.")
    if token not in rendered.text:
        raise AssertionError("Rendered context lost the token preserved by summaries/epoch.")

    report = {
        "nats_servers": redacted_nats_servers,
        "storage_backend": storage_settings.backend,
        "storage_locator": storage_identifier(storage_settings),
        "conversation_id": conversation_id,
        "epoch_count": len(epochs),
        "latest_epoch_id": latest_epoch["context_epoch_id"],
        "summary_layering": latest_epoch.get("rehydration_manifest", {}).get("summary_layering"),
        "source_epoch_ids": latest_epoch.get("rehydration_manifest", {}).get("source_epoch_ids"),
        "rendered_metadata": rendered.metadata,
    }
    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"Wrote report to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
