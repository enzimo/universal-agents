"""What each review choice does, in plain words (assistant_gateway.access_choice_effects)."""

from __future__ import annotations

from datetime import datetime, timezone

from assistant_gateway.access_choice_effects import ChoiceWords, chat_effect_lines, choice_effects, default_variant
from assistant_gateway.application.access_reviews import ACCESS_REVIEW_CHOICE_LABELS
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestKind,
    AccessRequestV1,
    ChangeProposalV1,
    GrantLifetime,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)

TEAM = "team-1"
WORDS = ChoiceWords(lead="TeamArchitect", tool="Google Workspace", whose="you")


def request(agent: str = "WebResearchAgent", **fields) -> AccessRequestV1:
    values = {
        "request_id": "req-1",
        "team_id": TEAM,
        "requester_agent_id": f"agent-{agent}",
        "requester_agent_type": agent,
        "subject": SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{agent}"),
        "target": TargetRefV1(kind=TargetKind.TOOL, id="cli:google"),
        "level": AccessLevel.WRITE,
        "reason": "Read and draft mail",
        "task_id": "task-1",
        "work_id": "task_context_1",
        "created_at_utc": datetime(2026, 9, 30, tzinfo=timezone.utc),
    }
    values.update(fields)
    return AccessRequestV1(**values)


def test_a_workers_request_says_that_kept_access_goes_to_the_team_lead() -> None:
    effects = choice_effects(request(), WORDS)

    assert effects["approve"]["task"] == (
        "WebResearchAgent can use Google Workspace (read-and-change) for this task only; nothing is kept."
    )
    assert effects["approve"]["kept"].startswith(
        "TeamArchitect keeps read-and-change access to Google Workspace for your later tasks and gives it to "
        "WebResearchAgent and its other workers' tasks without asking"
    )
    assert "(read)" in effects["approve_read"]["task"]
    assert effects["deny"] == {"any": "Nothing is given, and WebResearchAgent is told you said no."}


def test_the_team_leads_own_request_says_whether_workers_get_it() -> None:
    effects = choice_effects(request("TeamArchitect"), ChoiceWords(lead="TeamArchitect", tool="Schedules", whose="Dana"))

    assert "gives it to its workers' tasks without asking" in effects["approve"]["kept"]
    assert "its workers ask each time" in effects["approve"]["kept_no_hand_on"]
    assert "Dana's later tasks" in effects["approve"]["kept"]
    assert "and so can the workers helping with it" in effects["approve"]["task"]


def test_a_scheduled_run_keeps_it_for_the_schedule_and_the_person_lacking_access_is_told() -> None:
    effects = choice_effects(
        request(schedule_id="digest"),
        ChoiceWords(lead="TeamArchitect", tool="Google Workspace", whose="you", person_lacks_you=True),
    )

    assert effects["approve"]["kept"].startswith("This scheduled job keeps read-and-change access")
    assert "for this run only" in effects["approve"]["task"]
    assert effects["approve"]["task"].endswith("You get this access too, because agents never exceed the person they work for.")



def test_the_team_lead_asking_in_a_scheduled_run_keeps_it_itself() -> None:
    effects = choice_effects(request("TeamArchitect", schedule_id="digest"), WORDS)

    assert effects["approve"]["kept"].startswith("TeamArchitect keeps read-and-change access to Google Workspace")

def test_changes_and_the_card_start_and_chat_lines() -> None:
    change = request(
        kind=AccessRequestKind.CHANGE,
        change=ChangeProposalV1(
            kind="runtime_settings", change_id="chg-1", plan_digest="d", title="Switch model", summary="s", effect=("x",)
        ),
    )
    assert choice_effects(change, WORDS)["approve"] == {"any": "Applies exactly what is listed above, once. Nothing else changes."}

    # The card starts with what the agent asked for (its Keep box starts ticked when it asked to keep).
    assert default_variant(request()) == "kept"
    assert default_variant(request(lifetime=GrantLifetime.TASK)) == "task"

    lines = chat_effect_lines(request(), WORDS, ACCESS_REVIEW_CHOICE_LABELS, ["approve_task", "approve", "deny"])
    assert lines[0].startswith("Allow for this task: WebResearchAgent can use Google Workspace")
    assert lines[1].startswith("Allow and keep: TeamArchitect keeps")
    assert lines[2] == "Deny: Nothing is given, and WebResearchAgent is told you said no."
