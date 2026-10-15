"""Measure the knowledge store under many agent processes, on SQLite or PostgreSQL.

Each simulated agent is its own OS process with its own connections, like
agent processes. Each makes a weighted mix of real ``KnowledgeService`` calls
(search, read, guarded edit, note) with an optional pause between calls, and
the script reports operations per second, latency percentiles and outcomes.

Not part of the test suite: it is the evidence for choosing a database
(``ua-architecture`` research note ``2026-10-08-knowledge-base-storage-benchmark``).

Examples:
    .venv/bin/python scripts/benchmark_knowledge_store.py --backend sqlite --target /tmp/kb.db --pages 20000
    .venv/bin/python scripts/benchmark_knowledge_store.py --backend postgres \
        --target postgresql://user@127.0.0.1:5432/db --pages 20000 --agents 20 100 --pause-ms 1000
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import random
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge_base.records import KnowledgeActor, KnowledgeError  # noqa: E402
from knowledge_base.service import DraftInput, KnowledgeService  # noqa: E402
from knowledge_base.vocabulary import ActorKind  # noqa: E402
from runtime_settings.knowledge import KnowledgeSettings, KnowledgeWriteLimits  # noqa: E402
from storage.config import StorageSettings, resolve_storage_settings  # noqa: E402
from storage.knowledge_repository import create_knowledge_repository  # noqa: E402

TEAM = "team_bench"
PEOPLE = [f"person_{index}" for index in range(10)]
_SYLLABLES = ("ka", "lo", "mi", "ne", "po", "ru", "sa", "te", "vu", "zo", "bri", "cla", "dro", "fen", "gor")
_MIDDLES = ("ban", "cet", "dil", "fon", "gur", "hex", "jal", "kim", "lor", "mux", "nip", "ost", "pra", "qua", "rel")
_ENDINGS = ("a", "en", "is", "or", "um", "et", "ix", "al", "on", "ur", "ed", "ing", "ly", "er", "ist")
# A vocabulary of a few thousand words drawn with Zipf weights, like real text.
WORDS = [a + b + c for a in _SYLLABLES for b in _MIDDLES for c in _ENDINGS]
WEIGHTS = [1.0 / (index + 1) for index in range(len(WORDS))]
# The benchmark measures the store, not the limits: these let every write through.
OPEN_LIMITS = KnowledgeWriteLimits(
    page_edits_per_agent=1000,
    writes_per_agent_per_hour=10000,
    new_pages_per_work=1000,
    edit_war_edits=1000,
    archives_per_agent_per_hour=1000,
)


def text(rng: random.Random, count: int) -> str:
    return " ".join(rng.choices(WORDS, weights=WEIGHTS, k=count))


def storage(backend: str, target: str) -> StorageSettings:
    if backend == "sqlite":
        return resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": target}})
    return resolve_storage_settings({"backend": "postgres", "postgres": {"dsn": target}})


def service(settings: StorageSettings) -> KnowledgeService:
    return KnowledgeService(create_knowledge_repository(settings), settings=KnowledgeSettings(limits=OPEN_LIMITS))


def page_reference(index: int) -> tuple[str, str]:
    """40% team pages, the rest spread over people's personal spaces."""

    if index % 5 < 2:
        return "team", f"team/page-{index}"
    return PEOPLE[index % len(PEOPLE)], f"personal/page-{index}"


def seed(settings: StorageSettings, pages: int) -> float:
    svc = service(settings)
    rng = random.Random(7)
    started = time.perf_counter()
    for index in range(pages):
        owner, reference = page_reference(index)
        actor = KnowledgeActor(ActorKind.PERSON, owner if owner != "team" else "seeder", TEAM, work_person_id=owner if owner != "team" else "seeder")
        body = text(rng, rng.randint(250, 600)) + f" [[page-{rng.randrange(pages)}]]"
        svc.write(actor, reference, DraftInput(title=text(rng, 4), summary=text(rng, 12), body=body))
    return time.perf_counter() - started


def agent_loop(arguments: tuple[Any, ...]) -> dict[str, Any]:
    settings, index, duration, pages, mix, pause_ms = arguments
    svc = service(settings)
    rng = random.Random(1000 + index)
    person = PEOPLE[index % len(PEOPLE)]
    actor = KnowledgeActor(
        ActorKind.AGENT, f"agent_{index}", TEAM, agent_type="Bench", work_person_id=person, task_id=f"task_{index}"
    )
    operations, weights = zip(*mix.items())
    latencies: dict[str, list[float]] = {operation: [] for operation in operations}
    outcomes: dict[str, int] = {}
    deadline = time.time() + duration
    while time.time() < deadline:
        if pause_ms:
            time.sleep(rng.expovariate(1000.0 / pause_ms))
        operation = rng.choices(operations, weights=weights)[0]
        page = rng.randrange(pages)
        owner, reference = page_reference(page)
        if owner not in ("team", person):
            reference = f"team/page-{(page // 5) * 5}"
        started = time.perf_counter()
        try:
            if operation == "search":
                svc.search(actor, query=text(rng, 3))
            elif operation == "read":
                svc.read(actor, reference)
            elif operation == "write":
                current = svc.read(actor, reference).page
                svc.write(actor, reference, DraftInput(body=text(rng, rng.randint(250, 600))), base_revision=current.revision)
            else:
                svc.add_note(actor, reference, text(rng, 30))
            result = "ok"
        except KnowledgeError as error:
            result = error.code
        latencies[operation].append(time.perf_counter() - started)
        outcomes[f"{operation}:{result}"] = outcomes.get(f"{operation}:{result}", 0) + 1
    return {"latencies": latencies, "outcomes": outcomes}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] * 1000, 2)


def run(settings: StorageSettings, agents: int, duration: float, pages: int, mix: dict, pause_ms: float) -> dict:
    context = multiprocessing.get_context("spawn")
    with context.Pool(agents) as pool:
        results = pool.map(agent_loop, [(settings, index, duration, pages, mix, pause_ms) for index in range(agents)])
    merged: dict[str, list[float]] = {operation: [] for operation in mix}
    outcomes: dict[str, int] = {}
    for result in results:
        for operation, values in result["latencies"].items():
            merged[operation].extend(values)
        for key, count in result["outcomes"].items():
            outcomes[key] = outcomes.get(key, 0) + count
    total = sum(len(values) for values in merged.values())
    return {
        "agents": agents,
        "pause_ms": pause_ms,
        "operations_per_second": round(total / duration, 1),
        "outcomes": outcomes,
        "latency_ms": {
            operation: {"count": len(values), "p50": percentile(values, 0.5), "p95": percentile(values, 0.95), "p99": percentile(values, 0.99)}
            for operation, values in merged.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["sqlite", "postgres"], required=True)
    parser.add_argument("--target", required=True, help="SQLite file path or PostgreSQL connection address")
    parser.add_argument("--pages", type=int, default=5000)
    parser.add_argument("--agents", type=int, nargs="+", default=[1, 10, 50])
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--pause-ms", type=float, default=0, help="mean pause between one agent's calls")
    parser.add_argument("--mix", default='{"search": 55, "read": 25, "write": 10, "note": 10}')
    parser.add_argument("--skip-seed", action="store_true", help="reuse pages from an earlier run")
    args = parser.parse_args()
    settings = storage(args.backend, args.target)
    if not args.skip_seed:
        print(json.dumps({"seeded_pages": args.pages, "seconds": round(seed(settings, args.pages), 1)}))
    for agents in args.agents:
        print(json.dumps({"backend": args.backend, **run(settings, agents, args.duration, args.pages, json.loads(args.mix), args.pause_ms)}))


if __name__ == "__main__":
    main()
