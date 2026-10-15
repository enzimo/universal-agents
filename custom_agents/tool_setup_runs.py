"""The setup run behind ``set_up_tools``: what is missing, in what order, and how it ended.

Owner's decision 2026-09-28: when a request needs a tool that is not ready,
TeamLead calls ``set_up_tools`` straight away instead of asking the person
whether to start. The approval cards and setup pages are the consent points;
asking first is a wasted round trip. Trusted code then does the whole routine:

1. **Plan** (``plan_run``): from the setup check of each tool (access for
   TeamLead and the person, and, when a worker does the work, TeamLead's
   right to hand it on to the worker's tasks: workers keep no access of their
   own, "access follows the work"; the person's sign-in or linked
   account; the team's sign-in app; ``custom_tools.tool_setup_check``) and the
   tool's catalog ``setup`` data (``ToolSetupSpecV1``: external steps and
   prerequisite tools), list the pieces. Nothing here is written per tool:
   every piece comes from catalog metadata. A piece only an administrator can
   do, for a person who is not one, stops the run before anything is asked,
   with a plain explanation (``Plan.stops``).
2. **Order**: a sign-in waits for the same tool's team sign-in app, a sign-in
   or account link waits for the tool's external steps (such as creating a
   token first), and the pages and steps of a tool wait for those of its
   prerequisite tools. Access requests go out at once.
3. **Workflow** (``workflow_proposal``): two or more pieces get one access
   workflow with a step per piece, TeamLead's own extra steps, and a final
   work step; one piece gets
   none. A run with an external step always gets one, because the workflow
   page is where the person marks the step done.
4. **Results** (``record_page_result``, ``record_access_decision``): a
   finished page or a granted request completes its piece; an expired page,
   or one the gateway could not watch after a restart, is offered again up to
   ``TOOL_SETUP_OFFER_LIMIT`` pages; a closed page, a denial, or a page that
   cannot be offered again stops the run (``settle``).
5. **Outcome** (``outcome``): one compact line per tool (ready or not, and
   what happened) for TeamLead.

The run is stored (``storage.tool_setup_run_repository``) so it survives
restarts; ``custom_agents.tool_setup_run_runtime`` drives it. A run grants
nothing: access comes only from grants people approve, and every tool call
is decided again when it is made.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from custom_messages.contracts.access_workflow_v2 import utc_now
from custom_messages.contracts.credentials_v1 import (
    TOOL_SETUP_ACCOUNT_LINK,
    TOOL_SETUP_COMPLETED,
    TOOL_SETUP_DECLINED,
    TOOL_SETUP_EXPIRED,
    TOOL_SETUP_EXTERNAL_STEP,
    TOOL_SETUP_OFFER_LIMIT,
    TOOL_SETUP_OWNER_PERSON,
    TOOL_SETUP_OWNER_TEAM,
    TOOL_SETUP_SIGN_IN,
    TOOL_SETUP_TEAM_SIGN_IN_APP,
    ToolSetupResultV1,
)
from custom_messages.contracts.tool_setup_spec_v1 import EXTERNAL_STEP_BY_ADMINISTRATOR
from secure_tools.auth_methods import tool_sign_in

# The interrupt that pauses TeamLead's turn, and the task blocker it records.
TOOL_SETUP_RUN_KIND = "tool_setup_run"
TOOL_SETUP_RUN_RESPONSE_KIND = "tool_setup_run_finished"

PIECE_ACCESS = "access"
PAGE_KINDS = frozenset({TOOL_SETUP_SIGN_IN, TOOL_SETUP_ACCOUNT_LINK, TOOL_SETUP_TEAM_SIGN_IN_APP, TOOL_SETUP_EXTERNAL_STEP})
PieceKind = Literal["access", "sign_in", "account_link", "team_sign_in_app", "external_step"]

PENDING = "pending"
WAITING = "waiting"
DONE = "done"
SKIPPED = "skipped"
DECLINED = "declined"
DENIED = "denied"
FAILED = "failed"
PieceStatus = Literal["pending", "waiting", "done", "skipped", "declined", "denied", "failed"]
STOPPING = frozenset({DECLINED, DENIED, FAILED})
SETTLED = frozenset({DONE, SKIPPED})

RUN_WAITING = "waiting"
RUN_FINISHED = "finished"
RUN_STOPPED = "stopped"
RunStatus = Literal["waiting", "finished", "stopped"]

# Why a page may be offered again: it expired, or nobody watched it after a gateway restart.
REOFFER_REASONS = frozenset({"tool_setup_page_not_watched"})
WORK_STEP_ID = "do_the_work"
# Workflow event codes for what trusted code, not the agent, did on a step.
ACCESS_REQUESTED_CODE = "access_requested"
ACCESS_APPROVED_CODE = "access_approved"


class RunPieceV1(BaseModel):
    """One thing a run waits for: an access request, a setup page or an external step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    piece_id: str
    tool_id: str
    kind: PieceKind
    owner: Literal["person", "team"] = TOOL_SETUP_OWNER_PERSON
    step_id: str | None = None  # an external step's id in the catalog setup data
    title: str = ""  # an external step's approved title
    depends_on: tuple[str, ...] = ()
    workflow_step_id: str | None = None
    status: PieceStatus = PENDING
    request_id: str | None = None  # access
    setup_request_id: str | None = None  # pages and external steps
    attempt: int = 0
    account_label: str | None = None
    note: str = ""


class ToolSetupRunV1(BaseModel):
    """One ``set_up_tools`` call and the state of each piece (module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    team_id: str
    person_id: str
    person_roles: tuple[str, ...] = ()
    task_id: str
    agent_id: str
    agent_type: str
    tool_ids: tuple[str, ...]
    level: str = "read"
    worker_type: str = ""
    purpose: str = ""
    workflow_id: str | None = None
    # The person was told about the workflow in chat, once (tool_setup_run_runtime).
    workflow_announced: bool = False
    interrupt_id: str | None = None
    status: RunStatus = RUN_WAITING
    stop_reason: str = ""
    pieces: tuple[RunPieceV1, ...] = ()
    notes: tuple[str, ...] = ()
    version: int = Field(default=1, ge=1)
    created_at_utc: datetime = Field(default_factory=utc_now)
    updated_at_utc: datetime = Field(default_factory=utc_now)

    def piece(self, piece_id: str) -> RunPieceV1 | None:
        return next((item for item in self.pieces if item.piece_id == piece_id), None)

    def with_piece(self, piece: RunPieceV1) -> "ToolSetupRunV1":
        pieces = tuple(piece if item.piece_id == piece.piece_id else item for item in self.pieces)
        return self.model_copy(update={"pieces": pieces})


def run_id_for(team_id: str, task_id: str, tool_use_id: str) -> str:
    """A stable id for one tool call, so a resumed call finds its run."""

    digest = hashlib.sha256(f"{team_id}\n{task_id}\n{tool_use_id}".encode("utf-8")).hexdigest()
    return f"tool_setup_run_{digest[:24]}"


def service_name(tool_id: str, catalog_name: str = "") -> str:
    """The service's own name ("GitHub"), not the catalog's tool name ("GitHub (gh)")."""

    sign_in = tool_sign_in(tool_id)
    return sign_in.display_name if sign_in else (catalog_name or tool_id)


# -- 1. plan ------------------------------------------------------------------------


@dataclass
class Plan:
    """What a run waits for, what stops it before it starts, and notes for TeamLead."""

    pieces: list[RunPieceV1] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


# Gaps only an administrator can fix for someone else (the check says who).
_ADMIN_STEPS = frozenset({"person_access", "agent_blocked"})
_NOTE_FIXES = frozenset({"try_the_tool"})


def _page_piece(tool_id: str, kind: str, owner: str) -> RunPieceV1:
    return RunPieceV1(piece_id=f"{kind}:{owner}:{tool_id}", tool_id=tool_id, kind=kind, owner=owner)


class _Planner:
    """Builds the pieces of one run from the checks, in setup order."""

    def __init__(self, catalog: Any, *, is_admin: bool) -> None:
        self.catalog = catalog
        self.is_admin = is_admin
        self.plan = Plan()
        self.setup_pieces: dict[str, list[str]] = {}  # tool -> its page and external piece ids

    def add_report(self, report: dict[str, Any]) -> None:
        tool_id = report["tool_id"]
        fixes = list(report.get("next_steps") or [])
        if any(fix.get("step") == "unknown_tool" for fix in fixes):
            self.plan.unknown.append(tool_id)
            return
        pages: list[RunPieceV1] = []
        for fix in fixes:
            piece = self._piece_for(tool_id, fix)
            if piece is None:
                continue
            if piece.kind in PAGE_KINDS:
                pages.append(piece)
            else:
                self.plan.pieces.append(piece)
        externals = self._external_pieces(tool_id) if pages else []
        self._order_pages(tool_id, externals, pages)

    def _piece_for(self, tool_id: str, fix: dict[str, Any]) -> RunPieceV1 | None:
        step, how = fix.get("step"), fix.get("fix_with")
        if step in _ADMIN_STEPS or how in {"tell_person", "tell_person_and_stop"}:
            if not fix.get("alternative_to"):
                self.plan.stops.append(str(fix.get("say") or fix.get("note") or f"{tool_id} needs an administrator."))
            return None
        if fix.get("alternative_to"):
            return None
        if how in _NOTE_FIXES:
            self.plan.notes.append(f"{tool_id}: {fix.get('note', 'could not be checked; try the tool.')}")
            return None
        if how == "request_access":
            return RunPieceV1(piece_id=f"{PIECE_ACCESS}:{tool_id}", tool_id=tool_id, kind=PIECE_ACCESS)
        if how == "setup_page":
            return _page_piece(tool_id, str(fix["kind"]), str(fix.get("owner") or TOOL_SETUP_OWNER_PERSON))
        return None

    def _external_pieces(self, tool_id: str) -> list[RunPieceV1]:
        tool = self.catalog.tools.get(tool_id)
        steps = tool.setup.external_steps if tool is not None and tool.setup else ()
        pieces = []
        for step in steps:
            admin_step = step.who == EXTERNAL_STEP_BY_ADMINISTRATOR
            if admin_step and not self.is_admin:
                self.plan.stops.append(
                    f"Setting up {service_name(tool_id, tool.display_name)} needs a team administrator first: "
                    f"{step.title}."
                )
                continue
            pieces.append(
                RunPieceV1(
                    piece_id=f"{TOOL_SETUP_EXTERNAL_STEP}:{tool_id}:{step.step_id}",
                    tool_id=tool_id,
                    kind=TOOL_SETUP_EXTERNAL_STEP,
                    owner=TOOL_SETUP_OWNER_TEAM if admin_step else TOOL_SETUP_OWNER_PERSON,
                    step_id=step.step_id,
                    title=step.title,
                )
            )
        return pieces

    def _order_pages(self, tool_id: str, externals: list[RunPieceV1], pages: list[RunPieceV1]) -> None:
        """External steps first, then the team app, then the sign-in or link; prerequisites before all."""

        tool = self.catalog.tools.get(tool_id)
        earlier = [
            piece_id
            for prerequisite in (tool.setup.prerequisites if tool is not None and tool.setup else ())
            for piece_id in self.setup_pieces.get(prerequisite, [])
        ]
        ordered: list[RunPieceV1] = []
        external_ids = [piece.piece_id for piece in externals]
        app_ids = [piece.piece_id for piece in pages if piece.kind == TOOL_SETUP_TEAM_SIGN_IN_APP]
        for piece in externals:
            ordered.append(piece.model_copy(update={"depends_on": tuple(earlier)}))
        for piece in pages:
            after = list(earlier) + external_ids
            if piece.kind == TOOL_SETUP_SIGN_IN:
                after += app_ids
            ordered.append(piece.model_copy(update={"depends_on": tuple(after)}))
        self.plan.pieces.extend(ordered)
        self.setup_pieces[tool_id] = [piece.piece_id for piece in ordered]


def plan_run(reports: Iterable[dict[str, Any]], catalog: Any, *, is_admin: bool) -> Plan:
    """The pieces of a run from the checks of each tool, in setup order (module docstring)."""

    planner = _Planner(catalog, is_admin=is_admin)
    for report in reports:
        planner.add_report(report)
    return planner.plan


# -- 3. the workflow -----------------------------------------------------------------


def _slug(text: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in text.lower())[:40].strip("_") or "tool"


def _step_id(piece: RunPieceV1) -> str:
    tool = _slug(piece.tool_id.split(":", 1)[-1])
    if piece.kind == PIECE_ACCESS:
        return f"access_{tool}"
    if piece.kind == TOOL_SETUP_EXTERNAL_STEP:
        return f"{tool}_{piece.step_id}"[:64]
    owner = "_team" if piece.kind == TOOL_SETUP_ACCOUNT_LINK and piece.owner == TOOL_SETUP_OWNER_TEAM else ""
    return f"{piece.kind}_{tool}{owner}"


def piece_title(piece: RunPieceV1, name: str) -> str:
    """What the person sees for a piece, in plain words."""

    if piece.kind == PIECE_ACCESS:
        return f"Access to {name}"
    if piece.kind == TOOL_SETUP_SIGN_IN:
        return f"Sign in to {name}"
    if piece.kind == TOOL_SETUP_TEAM_SIGN_IN_APP:
        return f"Set up the team's {name} sign-in app"
    if piece.kind == TOOL_SETUP_EXTERNAL_STEP:
        return f"{name}: {piece.title}"
    return f"Link the team's account to {name}" if piece.owner == TOOL_SETUP_OWNER_TEAM else f"Link your account to {name}"


def needs_workflow(pieces: Iterable[RunPieceV1]) -> bool:
    """Two or more pieces, or any external step (marked done on the workflow page)."""

    pieces = list(pieces)
    return len(pieces) >= 2 or any(piece.kind == TOOL_SETUP_EXTERNAL_STEP for piece in pieces)


def _piece_step(piece: RunPieceV1, name: str, level: str, depends_on: list[str]) -> dict[str, Any]:
    title = piece_title(piece, name)
    step: dict[str, Any] = {
        "step_id": _step_id(piece),
        "title": title[:160],
        "depends_on": depends_on,
        "success_criterion": f"{title[:150]}: done.",
    }
    if piece.kind == PIECE_ACCESS:
        return {
            **step,
            "title": f"Access to {name}",
            "reason": f"The work needs {level} access to {name}.",
            "kind": "capability",
            "tool_access": [{"tool_id": piece.tool_id, "level": level}],
            "success_criterion": f"{level} access to {name} is approved.",
        }
    setup = {"tool_id": piece.tool_id, "kind": piece.kind, "owner": piece.owner}
    if piece.step_id:
        setup["step_id"] = piece.step_id
    return {
        **step,
        "reason": f"{name} needs this before it can be used for the person.",
        "kind": "setup" if piece.kind in {TOOL_SETUP_EXTERNAL_STEP, TOOL_SETUP_TEAM_SIGN_IN_APP} else "account",
        "tool_setup": [setup],
    }


def workflow_proposal(
    run: ToolSetupRunV1,
    names: dict[str, str],
    *,
    title: str,
    goal: str,
    target_agent_type: str,
    extra_steps: Iterable[dict[str, Any]] = (),
) -> tuple[dict[str, Any], ToolSetupRunV1]:
    """The workflow to publish for a run, and the run with each piece's step (module docstring)."""

    steps: dict[str, dict[str, Any]] = {}
    step_of: dict[str, str] = {}
    for piece in run.pieces:
        step_id = _step_id(piece)
        step_of[piece.piece_id] = step_id
        depends = sorted({step_of[item] for item in piece.depends_on if item in step_of} - {step_id})
        if step_id in steps:  # worker access shares its tool's access step
            continue
        steps[step_id] = _piece_step(piece, names.get(piece.tool_id, piece.tool_id), run.level, depends)
    extra = [dict(step) for step in extra_steps]
    work = {
        "step_id": WORK_STEP_ID,
        "title": "Do the requested work",
        "reason": goal[:2000] or "The work the person asked for, once everything is set up.",
        "kind": "work",
        "depends_on": [*steps, *(step["step_id"] for step in extra if step.get("step_id"))],
        "success_criterion": "The person's request is answered.",
    }
    proposal = {
        "title": title[:300],
        "goal": goal[:2000] or title[:2000],
        "target_agent_type": target_agent_type,
        "steps": [*steps.values(), *extra, work],
    }
    pieces = tuple(item.model_copy(update={"workflow_step_id": step_of[item.piece_id]}) for item in run.pieces)
    return proposal, run.model_copy(update={"pieces": pieces})


def workflow_notice(run: ToolSetupRunV1, names: dict[str, str], url: str) -> str:
    """The one chat message that tells the person about the setup workflow, with its address.

    Trusted code publishes the workflow, so trusted code tells the person: the
    setup pages and access cards only show their own part, and a channel such
    as Telegram shows nothing of a card but its text.
    """

    titles: list[str] = []
    for piece in run.pieces:
        title = piece_title(piece, names.get(piece.tool_id, piece.tool_id))
        if title not in titles:
            titles.append(title)
    purpose = run.purpose.strip().rstrip(".")
    goal = f"{purpose[0].lower()}{purpose[1:]}" if purpose else "do this"
    return (
        f"I need {len(titles)} things before I can {goal}: {'; '.join(titles)}. I'll ask for each one here and "
        f"carry on by myself when they're done. Follow along here: {url}"
    )


# -- 4. results ----------------------------------------------------------------------


def openable(run: ToolSetupRunV1) -> list[RunPieceV1]:
    """Pieces not started yet whose prerequisites are done (access requests start at the call)."""

    if run.status != RUN_WAITING:
        return []
    settled = {piece.piece_id for piece in run.pieces if piece.status in SETTLED}
    return [
        piece
        for piece in run.pieces
        if piece.status == PENDING and piece.kind != PIECE_ACCESS and all(item in settled for item in piece.depends_on)
    ]


def piece_for_setup(run: ToolSetupRunV1, setup_request_id: str) -> RunPieceV1 | None:
    return next((piece for piece in run.pieces if piece.setup_request_id == setup_request_id), None)


def piece_for_request(run: ToolSetupRunV1, request_id: str) -> RunPieceV1 | None:
    return next((piece for piece in run.pieces if piece.request_id == request_id), None)


def record_page_result(run: ToolSetupRunV1, result: ToolSetupResultV1) -> tuple[ToolSetupRunV1, bool]:
    """Apply how a page ended; True when the piece should get a fresh page (module docstring)."""

    piece = piece_for_setup(run, result.setup_request_id)
    if piece is None or piece.status != WAITING:
        return run, False
    attempt = max(piece.attempt, result.attempt)
    if result.status == TOOL_SETUP_COMPLETED:
        changed = piece.model_copy(update={"status": DONE, "account_label": result.account_label, "attempt": attempt})
        return run.with_piece(changed), False
    if result.status == TOOL_SETUP_DECLINED:
        return run.with_piece(piece.model_copy(update={"status": DECLINED, "note": result.message[:500]})), False
    offer_again = result.status == TOOL_SETUP_EXPIRED or (result.reason_code or "") in REOFFER_REASONS
    if offer_again and attempt < TOOL_SETUP_OFFER_LIMIT and not result.needs_administrator:
        return run.with_piece(piece.model_copy(update={"status": PENDING, "attempt": attempt})), True
    note = result.message[:500] or "The page could not be used."
    return run.with_piece(piece.model_copy(update={"status": FAILED, "attempt": attempt, "note": note})), False


def record_access_decision(run: ToolSetupRunV1, request_id: str, status: str) -> ToolSetupRunV1:
    """Apply a decision on one of the run's access requests (granted, denied or withdrawn)."""

    piece = piece_for_request(run, request_id)
    if piece is None or piece.status not in {WAITING, PENDING}:
        return run
    new = {"granted": DONE, "denied": DENIED}.get(status, FAILED)
    return run.with_piece(piece.model_copy(update={"status": new}))


def settle(run: ToolSetupRunV1) -> ToolSetupRunV1:
    """Stop at the first closed page or denial; finish when every piece is done."""

    if run.status != RUN_WAITING:
        return run
    stopped = next((piece for piece in run.pieces if piece.status in STOPPING), None)
    if stopped is not None:
        return run.model_copy(update={"status": RUN_STOPPED, "stop_reason": stopped.piece_id})
    if all(piece.status in SETTLED for piece in run.pieces):
        return run.model_copy(update={"status": RUN_FINISHED})
    return run


# -- 5. the outcome --------------------------------------------------------------------


def _piece_line(piece: RunPieceV1, name: str, worker_type: str) -> str:
    what = piece_title(piece, name)
    if piece.kind == PIECE_ACCESS:
        # Workers get it for each task from TeamLead ("access follows the work").
        handed = f" and hand it on to {worker_type}'s tasks" if worker_type else ""
        return {
            DONE: f"TeamLead may use {name}{handed}.",
            SKIPPED: f"TeamLead asks for access to {name} itself when it starts.",
            DENIED: f"The person denied access to {name}; do not ask again.",
            FAILED: f"The access request for {name} was withdrawn.",
        }.get(piece.status, f"Access to {name} is still waiting for a decision.")
    if piece.status == DONE:
        return f"Signed in to {name} as {piece.account_label}." if piece.account_label else f"{what}: done."
    if piece.status == DECLINED:
        return f"{what}: the person closed it without finishing; do not ask again."
    if piece.status == FAILED:
        return f"{what}: not finished ({piece.note or 'the page could not be used'}). The person can do it later with /link {piece.tool_id}."
    return f"{what}: not started because an earlier step did not finish."


def outcome(run: ToolSetupRunV1, names: dict[str, str]) -> dict[str, Any]:
    """The compact per-tool result TeamLead gets (module docstring)."""

    tools = []
    for tool_id in run.tool_ids:
        pieces = [piece for piece in run.pieces if piece.tool_id == tool_id]
        name = names.get(tool_id, tool_id)
        tools.append(
            {
                "tool_id": tool_id,
                "name": name,
                "ready": all(piece.status in SETTLED for piece in pieces),
                "what_happened": [_piece_line(piece, name, run.worker_type) for piece in pieces] or ["Already set up."],
            }
        )
    ready = run.status == RUN_FINISHED
    workflow = (
        {
            "workflow_url": f"/workflows/{run.workflow_id}",
            "workflow": {
                "workflow_id": run.workflow_id,
                # The setup steps are closed by trusted code; the work step is TeamLead's.
                "work_step_id": WORK_STEP_ID,
                "note": "Mark the work step running with update_access_workflow, then complete it when the "
                "person has the answer (or report it failed with why).",
            },
        }
        if run.workflow_id
        else {}
    )
    return {
        "status": "ready" if ready else "not_ready",
        "tools": tools,
        **workflow,
        **({"notes": list(run.notes)} if run.notes else {}),
        "next_step": (
            "Everything is set up: do the work now."
            if ready
            else "Tell the person plainly what is not set up and why, then end the task. Do not ask again for "
            "anything they closed or denied."
        ),
    }


__all__ = [
    "ACCESS_APPROVED_CODE",
    "ACCESS_REQUESTED_CODE",
    "PIECE_ACCESS",
    "Plan",
    "RUN_FINISHED",
    "RUN_STOPPED",
    "RUN_WAITING",
    "RunPieceV1",
    "TOOL_SETUP_RUN_KIND",
    "TOOL_SETUP_RUN_RESPONSE_KIND",
    "ToolSetupRunV1",
    "WORK_STEP_ID",
    "needs_workflow",
    "openable",
    "outcome",
    "piece_for_request",
    "piece_for_setup",
    "piece_title",
    "plan_run",
    "record_access_decision",
    "record_page_result",
    "run_id_for",
    "service_name",
    "settle",
    "workflow_notice",
    "workflow_proposal",
]
