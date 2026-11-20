"""Who works on a person's request: the gateway's side of Web Chat's agent trail.

Agents name the people's requests their model turns run for in their signed
records (``requests``, from verified gateway-signed contexts only;
``custom_agents.working_requests``). This service turns that into two things
for the thread list:

- **Who worked on each request (durable).** ``observe`` hears every member
  change from the team view (``assistant_gateway.team_view``) and hands each
  new (request, agent type) pair to one writer thread, which records it in the
  store (``RequestRepository.record_request_agent``). The store changes only a
  request this gateway admitted (running or finished, whatever continuations
  made its kind) whose task the id names, so an id the gateway does not hold
  changes nothing, and checks that with a plain read before it takes its write
  lock; the thread and its owner come from the gateway's own request record,
  never from the agent. The writer keeps first-reported order; the store's
  locks keep each thread's copy right. Its queue holds at most
  ``WRITE_QUEUE_LIMIT`` pairs, so a host that invents ids cannot grow it
  without bound; a pair that does not fit is logged and forgotten, so the
  agent's next record change can name it again (review F15).
  ``request_sent`` records the team lead when the gateway hands it the
  request: the lead always works first, and a lead turn shorter than the
  record's two-second write interval would otherwise be missed.
- **Who works on it now (live).** ``working`` matches the live members'
  records against the running request of each thread a page lists (the list
  query already reads it), so the owner rule holds by construction: a page
  only ever looks at its own person's threads.

A write that fails is tried again by the writer, in order, after
``WRITE_RETRY_SECONDS`` (plan review C16): the team view tells observers only
when a record changes, and the change after the one that named a request is
usually the one that drops it, so waiting for the agent's next record would
lose the pair. After the last try the failure is logged with the request's task
and the agent type, and the pair is forgotten. A type the store refuses
(``ValueError``) is final: logged once and kept remembered, so later changes of
the same record do not log it again. Nothing here raises into the team view or
the request path.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Protocol

# The display name the thread list gives the team lead.
TEAM_LEAD_NAME = "Team lead"
# (request, agent type) pairs already handed to the writer, so a record refreshed every minute writes nothing.
RECENT_PAIRS_LIMIT = 4096
# How long the agent catalog's display names are kept before they are read again.
NAMES_FRESH_SECONDS = 60.0
# The waits before each further try of a write that failed (a busy store, a slow disk); about 7.5 s in all.
WRITE_RETRY_SECONDS = (0.5, 2.0, 5.0)
# The longest name a page shows (``WebChatWorkingAgentV1.name``); a longer catalog display name is cut.
NAME_MAX_CHARACTERS = 200
# The most agents one thread lists as working now (``WebChatThreadWorkingV1.agents``).
WORKING_AGENTS_MAX = 64
# The most (request, agent type) pairs waiting for the writer (module docstring).
WRITE_QUEUE_LIMIT = 4096
# A full queue is logged at most this often.
QUEUE_FULL_LOG_SECONDS = 60.0


@dataclass(frozen=True)
class AgentReport:
    """What one live member's signed record says, as the team view read it."""

    agent_id: str
    agent_type: str
    lead: bool
    requests: tuple[str, ...] = ()
    exiting: bool = False


@dataclass(frozen=True)
class WorkingAgent:
    """An agent type working on a thread's request now, with the name the page shows."""

    agent_type: str
    name: str
    lead: bool


def agent_name(agent_type: str, lead: bool, catalog: Mapping[str, str]) -> str:
    """The name the page shows: "Team lead", the agent catalog's display name, else the type itself."""

    if lead:
        return TEAM_LEAD_NAME
    name = " ".join(str(catalog.get(agent_type) or agent_type).split()) or agent_type
    # Specialists may carry long display names; the page's contract holds NAME_MAX_CHARACTERS (plan review C15).
    return name if len(name) <= NAME_MAX_CHARACTERS else name[: NAME_MAX_CHARACTERS - 1].rstrip() + "…"


class _AgentRecorder(Protocol):
    def record_request_agent(self, *, active_task_id: str, agent_type: str, lead: bool) -> str | None: ...


class GatewayRequestAgents:
    """The gateway's agent trail service (module docstring)."""

    def __init__(
        self,
        *,
        repository: _AgentRecorder,
        live_reports: Callable[[], Iterable[AgentReport]],
        lead_agent_type: str,
        agent_names: Callable[[], Mapping[str, str]],
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.monotonic,
        retry_seconds: tuple[float, ...] = WRITE_RETRY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repository = repository
        self._retry_seconds = retry_seconds
        self._sleep = sleep
        self._live_reports = live_reports
        self._lead_agent_type = lead_agent_type
        self._agent_names = agent_names
        self._logger = logger or logging.getLogger(__name__)
        self._clock = clock
        self._recent: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._recent_lock = threading.Lock()
        # (request task, agent type, lead) to record, in order; None stops the writer.
        self._writes: queue.Queue[tuple[str, str, bool] | None] = queue.Queue(maxsize=WRITE_QUEUE_LIMIT)
        self._queue_full_logged_at: float | None = None
        self._dropped = 0
        self._writer: threading.Thread | None = None
        self._names: Mapping[str, str] = {}
        self._names_read_at: float | None = None
        self._names_lock = threading.Lock()

    # -- recording (the team view's loop and the request path) ------------------------------

    def observe(self, report: AgentReport) -> None:
        """A member's record changed: record each request it names for its type, once."""

        if report.exiting or not report.agent_type:
            return
        for request in report.requests:
            self._enqueue(request, report.agent_type, report.lead)

    def request_sent(self, active_task_id: str) -> None:
        """The gateway handed this request to the team lead: the lead worked on it first."""

        self._enqueue(active_task_id, self._lead_agent_type, True)

    def _enqueue(self, request: str, agent_type: str, lead: bool) -> None:
        pair = (request, agent_type)
        with self._recent_lock:
            if pair in self._recent:
                self._recent.move_to_end(pair)
                return
            self._recent[pair] = None
            while len(self._recent) > RECENT_PAIRS_LIMIT:
                self._recent.popitem(last=False)
        try:
            self._writes.put_nowait((request, agent_type, lead))
        except queue.Full:
            self._queue_full(pair)

    def _queue_full(self, pair: tuple[str, str]) -> None:
        """A pair that does not fit is forgotten (a later record change names it again) and logged once a minute."""

        with self._recent_lock:
            self._recent.pop(pair, None)
            self._dropped += 1
            now = self._clock()
            if self._queue_full_logged_at is not None and now - self._queue_full_logged_at < QUEUE_FULL_LOG_SECONDS:
                return
            self._queue_full_logged_at, dropped, self._dropped = now, self._dropped, 0
        self._logger.warning(
            "The agent trail's write queue is full; pairs are left out until it drains. "
            "event=gateway_request_agent_queue_full dropped=%s limit=%s",
            dropped,
            WRITE_QUEUE_LIMIT,
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Keep the writer running until ``stop`` or cancellation, then let it finish what it was handed."""

        self.start()
        try:
            await stop.wait()
        finally:
            await asyncio.to_thread(self.close)

    def start(self) -> None:
        """Start the one writer thread (idempotent); handed writes wait in order until it runs."""

        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._write_all, name="gateway_request_agents", daemon=True)
            self._writer.start()

    def close(self, timeout: float = 5.0) -> None:
        """Stop the writer after the writes already handed to it."""

        writer = self._writer
        if writer is None:
            return
        try:
            self._writes.put(None, timeout=timeout)
        except queue.Full:
            self._logger.warning("The agent trail's writer did not drain before the gateway stopped; pairs still "
                                 "waiting are left out. event=gateway_request_agent_writer_not_drained")
            return
        writer.join(timeout)
        self._writer = None

    def _write_all(self) -> None:
        while True:
            item = self._writes.get()
            if item is None:
                return
            self._write(*item)

    def _write(self, request: str, agent_type: str, lead: bool) -> None:
        """Record one pair, trying again after each wait in ``retry_seconds`` (module docstring)."""

        for attempt, wait in enumerate((*self._retry_seconds, None), start=1):
            try:
                session_key = self._repository.record_request_agent(
                    active_task_id=request, agent_type=agent_type, lead=lead
                )
            except ValueError as error:
                # Final: the pair stays remembered, so the same record does not log this again.
                self._logger.warning(
                    "An agent type cannot be recorded for a request and is left out of its trail. "
                    "event=gateway_request_agent_refused task_id=%s agent_type=%s error=%s",
                    request,
                    agent_type,
                    error,
                )
                return
            except Exception:
                if wait is not None:
                    self._sleep(wait)
                    continue
                with self._recent_lock:
                    self._recent.pop((request, agent_type), None)  # a later change of the record may name it again
                self._logger.warning(
                    "Could not record which agent worked on a request after %s tries; its trail leaves it out. "
                    "event=gateway_request_agent_record_failed task_id=%s agent_type=%s",
                    attempt,
                    request,
                    agent_type,
                    exc_info=True,
                )
                return
            break
        if session_key is None:
            # Already recorded, or no request of this gateway.
            self._logger.debug(
                "A reported request changed no thread. event=gateway_request_agent_unchanged task_id=%s agent_type=%s",
                request,
                agent_type,
            )

    # -- reading (page handlers' threads) ------------------------------------------------------

    def working(self, running_tasks: Mapping[str, str | None]) -> dict[str, tuple[WorkingAgent, ...]]:
        """The agents working now on each listed thread's running request (session key -> its task)."""

        sessions_by_task = {task: session for session, task in running_tasks.items() if task}
        if not sessions_by_task:
            return {}
        found: dict[str, dict[str, WorkingAgent]] = {}
        for report in self._live_reports():
            if report.exiting:
                continue
            for request in report.requests:
                session = sessions_by_task.get(request)
                if session is not None:
                    found.setdefault(session, {}).setdefault(
                        report.agent_type, WorkingAgent(report.agent_type, self.name(report.agent_type, report.lead),
                                                        report.lead)
                    )
        # The team lead first, then the others by name, so the page's words do not shuffle; at most
        # WORKING_AGENTS_MAX, the page contract's limit.
        return {
            session: tuple(sorted(agents.values(), key=lambda agent: (not agent.lead, agent.name.casefold())))[
                :WORKING_AGENTS_MAX
            ]
            for session, agents in found.items()
        }

    def name(self, agent_type: str, lead: bool) -> str:
        """The name the page shows for an agent type (``agent_name``, with the catalog read at most once a minute)."""

        return agent_name(agent_type, lead, self._catalog_names())

    def names(self, agents: Iterable[tuple[str, bool]]) -> dict[str, str]:
        """The shown name of each (agent type, lead) given."""

        return {agent_type: self.name(agent_type, lead) for agent_type, lead in agents}

    def _catalog_names(self) -> Mapping[str, str]:
        with self._names_lock:
            now = self._clock()
            if self._names_read_at is None or now - self._names_read_at >= NAMES_FRESH_SECONDS:
                try:
                    self._names = dict(self._agent_names())
                except Exception:
                    self._logger.warning(
                        "Could not read the agent catalog's display names; the thread list shows agent types. "
                        "event=gateway_agent_names_unreadable",
                        exc_info=True,
                    )
                self._names_read_at = now
            return self._names


__all__ = ["AgentReport", "GatewayRequestAgents", "TEAM_LEAD_NAME", "WorkingAgent", "agent_name"]
