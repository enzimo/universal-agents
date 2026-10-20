"""One-line change summaries (ChangeProposalV1.summary) for chat and the Web Chat card.

Covers: runtime settings summaries built from the plan's before and after
values (unchanged values left out, restart wording from the plan), access
proposal counts with their warnings, the summary contract (one bounded line),
and the restart summary. The end-to-end tests of each change service assert
their own summaries where the request is stored.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from access_control.proposals import proposal_summary
from custom_agents.runtime_restart_service import (
    RuntimeRestartImpactSnapshot,
    RuntimeRestartTaskState,
    _restart_summary,
)
from custom_agents.runtime_settings_summary import NOTHING_CHANGES, summarize_runtime_settings
from custom_messages.contracts.access_v2 import (
    CHANGE_SUMMARY_MAX_CHARS,
    ChangeProposalV1,
    ReviewItemV1,
    summary_line,
)
from custom_messages.contracts.common_v1 import utc_now
from custom_messages.contracts.runtime_v1 import (
    RESTART_SCOPE_ALL,
    RESTART_SCOPE_GATEWAY,
    RESTART_SCOPE_MANAGER,
    RESTART_SCOPE_NONE,
)

LEAD = "TeamArchitect"


def change(path: str, before: object, after: object, agent_type: str | None = LEAD) -> SimpleNamespace:
    return SimpleNamespace(path=path, previous_value=before, value=after, target_agent_type=agent_type)


def llm(name: str, before: object, after: object, agent_type: str = LEAD) -> SimpleNamespace:
    return change(f"current_agent.llm.{name}", before, after, agent_type)


def test_a_model_switch_reads_as_one_sentence_from_before_and_after_values() -> None:
    # The owner's reported request: provider, model and input kinds, one of them unchanged.
    summary = summarize_runtime_settings(
        [
            llm("provider", "litellm", "openrouter"),
            llm("model_id", "old/model", "deepseek/deepseek-v4.1-flash"),
            llm("supports_image_input", True, True),
            llm("supports_file_input", True, False),
        ],
        RESTART_SCOPE_NONE,
    )

    assert summary == "Switch TeamArchitect to deepseek/deepseek-v4.1-flash via openrouter (files off). Takes effect now."


def test_unchanged_values_are_left_out_so_a_kept_provider_is_not_mentioned() -> None:
    summary = summarize_runtime_settings(
        [llm("provider", "litellm", "litellm"), llm("model_id", "a", "openrouter/anthropic/claude-sonnet")],
        RESTART_SCOPE_NONE,
    )

    assert summary == "Switch TeamArchitect to openrouter/anthropic/claude-sonnet. Takes effect now."
    assert "litellm" not in summary


def test_a_plan_where_nothing_changes_says_so() -> None:
    assert summarize_runtime_settings([llm("supports_image_input", True, True)], RESTART_SCOPE_NONE) == NOTHING_CHANGES


@pytest.mark.parametrize(
    ("scope", "timing"),
    [
        (RESTART_SCOPE_NONE, "Takes effect now."),
        (RESTART_SCOPE_MANAGER, "Takes effect after a manager restart."),
        (RESTART_SCOPE_GATEWAY, "Takes effect after a gateway restart."),
        (RESTART_SCOPE_ALL, "Takes effect after a full restart."),
    ],
)
def test_the_sentence_ends_with_when_the_plan_takes_effect(scope: str, timing: str) -> None:
    summary = summarize_runtime_settings([llm("model_id", "a", "b", "WebResearchAgent")], scope)

    assert summary == f"Switch WebResearchAgent to b. {timing}"


def test_model_settings_other_agents_read_at_their_next_turn_say_so_instead_of_now() -> None:
    summary = summarize_runtime_settings(
        [llm("context_window_tokens", 800000, 1050000, "InternalImprovementAgent")], RESTART_SCOPE_NONE, next_turn=True
    )

    assert summary == (
        "Set InternalImprovementAgent's context window to 1050000. Takes effect at each running agent's next turn."
    )
    # A restart a plan really needs still wins.
    assert summarize_runtime_settings([llm("temperature", 0.2, 0.7)], RESTART_SCOPE_MANAGER, next_turn=True).endswith(
        "Takes effect after a manager restart."
    )


def test_provider_only_input_kinds_only_and_tuning_changes_have_their_own_words() -> None:
    assert (
        summarize_runtime_settings([llm("provider", "litellm", "openrouter")], RESTART_SCOPE_NONE)
        == "Switch TeamArchitect to the openrouter provider. Takes effect now."
    )
    assert (
        summarize_runtime_settings(
            [llm("supports_image_input", False, True), llm("supports_audio_input", True, False)], RESTART_SCOPE_NONE
        )
        == "Turn images on and audio off for TeamArchitect. Takes effect now."
    )
    assert (
        summarize_runtime_settings(
            [llm("temperature", 0.2, 0.7), llm("context_window_tokens", None, 128000)], RESTART_SCOPE_NONE
        )
        == "Set TeamArchitect's temperature to 0.7; set TeamArchitect's context window to 128000. Takes effect now."
    )


def test_team_defaults_and_other_settings_name_whose_setting_changes() -> None:
    summary = summarize_runtime_settings(
        [
            change("llm.model_id", "a", "b", None),
            change("workspace_default.llm.supports_file_input", None, True, None),
            change("heartbeat.enabled", False, True, None),
            change("team.startup_agents.count", 1, 2, "Coder"),
        ],
        RESTART_SCOPE_ALL,
    )

    assert summary == (
        "Switch the team default model to b; turn files on for the workspace default model; "
        "turn the heartbeat on; set team startup agents count for Coder to 2. Takes effect after a full restart."
    )


def test_long_text_is_named_not_quoted_and_a_long_plan_counts_the_rest() -> None:
    soul = summarize_runtime_settings([change("team_lead.soul", "old", "x" * 400)], RESTART_SCOPE_NONE)
    many = summarize_runtime_settings(
        [change(f"delivery.urgent_keyword_{index}", "", f"word-{index}-" + "k" * 30, None) for index in range(12)],
        RESTART_SCOPE_NONE,
    )

    assert soul == "Update TeamArchitect's SOUL.md. Takes effect now."
    assert many.endswith("more setting changes. Takes effect now.")
    assert len(many) <= CHANGE_SUMMARY_MAX_CHARS


def test_a_summary_is_one_bounded_line() -> None:
    base = {"kind": "runtime_settings", "change_id": "c-1", "plan_digest": "d" * 64, "title": "t", "effect": ("e",)}

    assert summary_line("  two\nlines   here ") == "two lines here"
    assert summary_line("x" * 400).endswith("…") and len(summary_line("x" * 400)) == CHANGE_SUMMARY_MAX_CHARS
    with pytest.raises(ValidationError):
        ChangeProposalV1(**base, summary="first line\nsecond line")
    with pytest.raises(ValidationError):
        ChangeProposalV1(**base)  # every change carries its summary


def test_an_access_proposal_is_counted_by_section_with_its_warnings() -> None:
    rows = (
        ReviewItemV1(item_id="t", section="tool", text="Add Weather API", flags=("new_host",)),
        ReviewItemV1(item_id="a1", section="access", text="Coder: read", keepable=True),
        ReviewItemV1(item_id="a2", section="access", text="Coder: full", flags=("full_access",), keepable=True),
        ReviewItemV1(item_id="k", section="account", text="Link Weather API"),
    )

    assert proposal_summary(rows) == (
        "Access proposal: add 1 new tool, give 2 accesses, link 1 credential "
        "(includes a new host, full access). Takes effect when approved."
    )


def _restart_plan(*states: str) -> SimpleNamespace:
    tasks = tuple(RuntimeRestartTaskState(task_id=f"task_{index}", state=state) for index, state in enumerate(states))
    snapshot = RuntimeRestartImpactSnapshot(
        captured_at_utc=utc_now(),
        scope=RESTART_SCOPE_MANAGER,
        affected_processes=("manager",),
        manager_agent_id="agent_team_lead",
        manager_current_task_id="",
        manager_tracked_top_level_task_count=len(tasks),
        manager_runnable_top_level_queue_depth=0,
        manager_waiting_top_level_task_count=0,
        manager_tasks=tasks,
        known_team_agents=(),
    )
    return SimpleNamespace(impact_snapshot=snapshot)


def test_a_restart_summary_says_what_restarts_and_how_much_work_it_interrupts() -> None:
    busy = _restart_plan("in_progress", "waiting")

    assert _restart_summary(busy, "manager") == "Restart manager; 2 tasks may be interrupted or delayed."
    assert _restart_summary(_restart_plan(), "manager, gateway") == (
        "Restart manager, gateway; no tasks are running, queued or waiting."
    )
