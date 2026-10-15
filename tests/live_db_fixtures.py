# Direct execution bootstraps the repository path before project imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from custom_agents.conversation_store import ConversationStore
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType


logger = logging.getLogger(__name__)
TEST_RESULTS_ROOT = PROJECT_ROOT / "test_results"
TEST_DATA_ROOT = TEST_RESULTS_ROOT / "test_data"


def _sqlite_sidecars(path: Path) -> list[Path]:
    return [
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    ]


def reset_sqlite_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in _sqlite_sidecars(path):
        if candidate.exists():
            candidate.unlink()


def suite_default_db_path(suite: str, test_id: str | int) -> Path:
    TEST_DATA_ROOT.mkdir(parents=True, exist_ok=True)
    if suite == "gateway":
        return TEST_DATA_ROOT / f"task_history_live_{test_id}.db"
    if suite == "single":
        return TEST_DATA_ROOT / f"single_request_{test_id}.db"
    if suite == "reliability":
        return TEST_DATA_ROOT / f"reliability_{test_id}.db"
    return TEST_DATA_ROOT / f"{suite}_{test_id}.db"


def fixture_name_for_single_request(test_number: str | int) -> str:
    return "seeded_history" if str(test_number) == "6" else "fresh"


def fixture_name_for_reliability(test_number: str | int) -> str:
    return "fresh"


def fixture_name_for_gateway(test_number: str | int) -> str:
    return "fresh"


def _seed_history_fixture(
    db_path: Path,
    *,
    owner_agent_id: str = "fixture_manager",
    owner_agent_name: str = "fixture_manager",
    team_id: str = "fixture_team",
) -> None:
    task_manager = TaskManager(db_path=str(db_path))
    task_manager.set_owner_identity(owner_agent_id, owner_agent_name, team_id, "TeamArchitect")
    conversation_store = ConversationStore(db_path=str(db_path), artifacts_dir=Path(db_path).parent / "conversation_artifacts")

    seeded_tasks = [
        {
            "task_id": "fixture_task_history_recent_1",
            "conversation_id": "fixture_conversation_history_1",
            "correlation_id": "fixture_corr_history_1",
            "problem_statement": "Summarize recent task history for the user.",
            "final_answer": "Summarized recent task history and highlighted the last completed work.",
            "history": [
                "Read archived tasks from SQLite history.",
                "Selected the most recent relevant task IDs.",
                "Summarized execution history for the user.",
            ],
        },
        {
            "task_id": "fixture_task_history_recent_2",
            "conversation_id": "fixture_conversation_history_1",
            "correlation_id": "fixture_corr_history_2",
            "problem_statement": "Review the prior task summaries and identify open risks.",
            "final_answer": "Reviewed prior task summaries and identified one remaining open risk.",
            "history": [
                "Loaded prior conversation thread context.",
                "Compared the last two archived tasks.",
                "Returned the open-risk summary.",
            ],
        },
    ]

    for seeded in seeded_tasks:
        task = Task(
            task_id=seeded["task_id"],
            task_type=MessageMetaType.REQUEST,
            problem_statement=seeded["problem_statement"],
        )
        header = MessageHeader(
            from_agent_id="fixture_user",
            to_routing_path=owner_agent_id,
            response_routing_key="fixture_user_queue",
            agent_signature="",
        )
        meta = MessageMeta(
            from_agent_name="fixture_user",
            msg_correlation_id=seeded["correlation_id"],
            conversation_id=seeded["conversation_id"],
            requester_type=RequesterType.USER_REQUESTER,
            is_response_requested=True,
        )
        conversation_store.ensure_thread(
            conversation_id=seeded["conversation_id"],
            team_id=team_id,
            owner_agent_id=owner_agent_id,
            owner_agent_name=owner_agent_name,
            requester_name="fixture_user",
            requester_id="fixture_user",
            root_task_id=seeded["task_id"],
            subject_preview=seeded["problem_statement"],
        )
        conversation_store.append_turn(
            conversation_id=seeded["conversation_id"],
            task_id=seeded["task_id"],
            followup_to_task_id=None,
            owner_agent_id=owner_agent_id,
            requester_name="fixture_user",
            requester_id="fixture_user",
            correlation_id=seeded["correlation_id"],
            problem_text=seeded["problem_statement"],
            task_status="accepted",
        )
        added = task_manager.add_task(task, header, meta, owner_person_id=None)
        if not added:
            raise RuntimeError(f"Failed to add seeded history task {seeded['task_id']}")
        for summary in seeded["history"]:
            task_manager.append_history_entry(seeded["task_id"], summary)
        task_info = task_manager.get_task_info(seeded["task_id"])
        if task_info is None:
            raise RuntimeError(f"Seeded task {seeded['task_id']} disappeared before archival")
        task_info.task.answer = seeded["final_answer"]
        task_manager.complete_task(
            seeded["task_id"],
            datetime.now(timezone.utc).isoformat(),
        )
        conversation_store.update_turn_completion(
            task_id=seeded["task_id"],
            task_status="completed",
            final_answer_text=seeded["final_answer"],
            turn_summary_text=seeded["final_answer"],
            execution_history_text="\n".join(seeded["history"]),
        )


def provision_db(
    *,
    suite: str,
    test_id: str | int,
    db_path: Optional[Path] = None,
    owner_agent_id: Optional[str] = None,
    owner_agent_name: Optional[str] = None,
    team_id: Optional[str] = None,
) -> Path:
    target_path = db_path or suite_default_db_path(suite, test_id)
    reset_sqlite_db(target_path)

    if suite == "single":
        fixture_name = fixture_name_for_single_request(test_id)
    elif suite == "reliability":
        fixture_name = fixture_name_for_reliability(test_id)
    elif suite == "gateway":
        fixture_name = fixture_name_for_gateway(test_id)
    else:
        raise ValueError(f"Unsupported live DB fixture suite: {suite}")

    if fixture_name == "fresh":
        with sqlite3.connect(target_path):
            pass
    elif fixture_name == "seeded_history":
        _seed_history_fixture(
            target_path,
            owner_agent_id=owner_agent_id or "fixture_manager",
            owner_agent_name=owner_agent_name or owner_agent_id or "fixture_manager",
            team_id=team_id or "fixture_team",
        )
    else:
        raise ValueError(f"Unsupported live DB fixture name: {fixture_name}")

    logger.info(
        "Provisioned live test DB fixture suite=%s test_id=%s fixture=%s path=%s",
        suite,
        test_id,
        fixture_name,
        target_path,
    )
    return target_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a live-test SQLite fixture DB.")
    parser.add_argument("--suite", required=True, choices=["single", "reliability", "gateway"])
    parser.add_argument("--test", required=True)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--owner-agent-id", default=None)
    parser.add_argument("--owner-agent-name", default=None)
    parser.add_argument("--team-id", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    path = provision_db(
        suite=args.suite,
        test_id=args.test,
        db_path=Path(args.db_path).expanduser().resolve() if args.db_path else None,
        owner_agent_id=args.owner_agent_id,
        owner_agent_name=args.owner_agent_name,
        team_id=args.team_id,
    )
    print(path)


if __name__ == "__main__":
    main()
