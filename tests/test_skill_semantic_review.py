"""The selected skill review runs in its staging turn, on the turn's loop, and fails closed with a reason.

Every review here runs as a sync tool call of a real model turn
(``tests.task_work_support.run_in_turn``), as staging does, with the real
Strands agent and inert model responses. Failures are the real Strands and
SDK exceptions a provider or the per-call key binding raises. Staging and
apply share one check (``validate_report``) bound to the review settings in
force; a staging refusal names its reason and fix, and saves no plan.
"""

import asyncio
import json
import logging
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from strands.models import Model
from strands.types.exceptions import ContextWindowOverflowException, EventLoopException, ModelThrottledException

from custom_agents import access_runtime
from custom_agents import skill_semantic_review as semantic
from custom_agents.model_keys import (
    ModelKeyNotAllowedError,
    ModelKeyRejectedError,
    ResolvedKey,
    own_model_needed_message,
    rejection_message,
)
from custom_agents.provider_error_runtime import ModelStreamTimeoutError
from custom_agents.sdk.strands.turn_tool_stop import stop_tool_calls
from custom_agents import skill_installation
from custom_agents.governance.authorized_operations import canonical_json_sha256, model_digest
from custom_agents.skill_installation import SkillInstallationService, resolve_skill_installation_paths
from custom_agents.skill_repository import build_skill_tree_manifest
from custom_agents.turn_registry import end_turn, start_turn
from custom_messages.contracts.model_keys_v1 import KeySource
from custom_tools.runtime_context import current_task_work, task_work
from model_providers.endpoints import EndpointKind, ModelEndpoint
from runtime_settings.skill_assessment import SkillSemanticReviewSettings
from tests.change_approval_support import TASK_ID, agreement, use_signed_contexts
from tests.task_work_support import run_tool_in_turn, turn_agent, with_turn_bookkeeping
from tests.test_skill_installation import (
    REPOSITORY_URL,
    _agent,
    _patch_acquisition,
    _repository,
    _safe_scan_result,
    _write_config,
)

SECRET = "sk-ant-review-secret-must-not-be-recorded"
ENDPOINT = ModelEndpoint(address="https://models.example.invalid", kind=EndpointKind.ANTHROPIC, env_vars=("ANTHROPIC_API_KEY",))


class ReviewModel(Model):
    """An inert provider: answers with a SemanticDecision, or fails the way it is told to."""

    def __init__(self, *, output=None, error=None, stop_reason="tool_use", before_answer=None):
        self.calls = []
        self.tool_names = []
        self.loops = []
        self.output = output
        self.error = error
        self.stop_reason = stop_reason
        self.before_answer = before_answer

    def update_config(self, **config):
        pass

    def get_config(self):
        return {"model_id": "offline-review", "api_key": SECRET}

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("Use the SDK formatting tool, not a second provider path")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.loops.append(asyncio.get_running_loop())
        self.calls.append(deepcopy(messages))
        self.tool_names.append([tool["name"] for tool in tool_specs or []])
        if self.before_answer is not None:
            await self.before_answer()
        if self.error is not None:
            raise self.error
        yield {"messageStart": {"role": "assistant"}}
        if self.stop_reason != "tool_use":
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "A partial answer."}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": self.stop_reason}}
            return
        source = json.loads(messages[0]["content"][0]["text"])
        output = self.output or {
            "complete": True,
            "inspected_files": [f["path"] for f in source["untrusted_files"]],
            "findings": [], "summary": "Reviewed the supplied inert files.",
        }
        yield {"contentBlockStart": {"start": {"toolUse": {"name": "SemanticDecision", "toolUseId": f"review-{len(self.calls)}"}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(output)}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


@pytest.fixture
def artifact(tmp_path):
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: example-skill\ndescription: Formats a greeting.\n---\nReturn a greeting.\n")
    (root / ".hidden.txt").write_text("Hidden supporting data.")
    return root


def selected(**settings):
    return SkillSemanticReviewSettings(mode="task_model", **settings)


def review(root, model, *, manifest=None, person_model=None, **settings):
    """``assess_skill`` from the tool thread of a real turn of an agent whose team model is ``model``."""

    agent = turn_agent(_strands_model=model)
    manifest = manifest or build_skill_tree_manifest(root)

    def tool():
        if person_model is not None:
            current_task_work().person_model = person_model
        return semantic.assess_skill(agent, root, manifest, selected(**settings))

    return run_tool_in_turn(agent, "review-task", tool)


def test_review_contains_every_file_offers_no_executable_tool_and_runs_on_the_turns_loop(artifact):
    model = ReviewModel()
    agent = turn_agent(_strands_model=model)
    manifest = build_skill_tree_manifest(artifact)
    seen = {}

    def tool():
        seen["turn_loop"] = current_task_work().turn_owner.get_loop()
        return semantic.assess_skill(agent, artifact, manifest, selected())

    report = run_tool_in_turn(agent, "review-task", tool)

    assert report["status"] == "completed", report
    assert report["reason"] == "assessed" and report["detail"] == "" and report["error_type"] == ""
    assert set(report["decision"]["inspected_files"]) == {"SKILL.md", ".hidden.txt"}
    assert model.tool_names == [["SemanticDecision"]]
    assert len(model.calls[0]) == 1
    assert model.loops == [seen["turn_loop"]]
    assert agent._owned_tasks == set()
    assert SECRET not in json.dumps(report)


def test_off_finishes_with_the_configuration_choice_and_no_model(artifact):
    model = ReviewModel()
    report = semantic.assess_skill(
        SimpleNamespace(_strands_model=model), artifact, build_skill_tree_manifest(artifact), SkillSemanticReviewSettings()
    )
    assert (report["status"], report["reason"]) == ("not_configured", "disabled")
    assert report["configure"]["choices"] == ["off", "task_model"]
    assert not model.calls


def test_a_selected_review_without_a_model_is_incomplete_never_not_configured(artifact):
    report = review(artifact, None)
    assert (report["status"], report["reason"]) == ("incomplete", "task_model_unavailable")


def test_a_selected_review_outside_a_running_turn_never_uses_the_team_model(artifact):
    team = ReviewModel()
    agent = turn_agent(_strands_model=team)
    manifest = build_skill_tree_manifest(artifact)

    no_task = semantic.assess_skill(agent, artifact, manifest, selected())
    with task_work("review-task"):
        no_turn = semantic.assess_skill(agent, artifact, manifest, selected())

    assert (no_task["status"], no_task["reason"]) == ("incomplete", "task_model_unavailable")
    assert (no_turn["status"], no_turn["reason"]) == ("incomplete", "turn_finished")
    assert not team.calls


@pytest.mark.asyncio
async def test_a_staging_thread_that_outlives_its_stopped_turn_uses_no_model(artifact):
    """Finding F1's trigger: end_turn put the team model back while the task's handling is still open."""

    personal, team = ReviewModel(), ReviewModel()
    agent = turn_agent(_strands_model=team)
    manifest = build_skill_tree_manifest(artifact)
    turn_cancelled = threading.Event()
    outcome = {}

    def lingering_tool():
        turn_cancelled.wait(5)
        outcome["report"] = semantic.assess_skill(agent, artifact, manifest, selected())

    async def turn():
        start_turn(agent, "review-task")
        current_task_work().person_model = SimpleNamespace(
            model=personal, profile=SimpleNamespace(model_id="person-review"), provider="test"
        )
        try:
            await asyncio.to_thread(lingering_tool)
        finally:
            end_turn(agent)

    with task_work("review-task") as work:
        owner = asyncio.create_task(turn())
        await asyncio.sleep(0.05)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        turn_cancelled.set()
        while "report" not in outcome:
            await asyncio.sleep(0.01)
        assert work.handling_finished is False

    assert (outcome["report"]["status"], outcome["report"]["reason"]) == ("incomplete", "turn_finished")
    assert not personal.calls and not team.calls


def test_the_persons_own_model_is_used_instead_of_the_team_model(artifact):
    team, personal = ReviewModel(error=RuntimeError("the team model must not be called")), ReviewModel()
    bundle = SimpleNamespace(model=personal, profile=SimpleNamespace(model_id="person-review"), provider="test")
    report = review(artifact, team, person_model=bundle)
    assert report["status"] == "completed", report
    assert (report["model_id"], report["provider"]) == ("person-review", "test")
    assert personal.calls and not team.calls


def test_each_review_has_an_independent_conversation(artifact):
    model = ReviewModel()
    assert review(artifact, model)["status"] == "completed"
    (artifact / ".hidden.txt").write_text("Second independent input.")
    assert review(artifact, model)["status"] == "completed"
    assert len(model.calls) == 2
    assert all(len(messages) == 1 for messages in model.calls)
    assert "Hidden supporting data." not in json.dumps(model.calls[1])


@pytest.mark.parametrize("output, reason", [
    ({"complete": False, "inspected_files": ["SKILL.md", ".hidden.txt"], "findings": [], "summary": "Partial."},
     "incomplete_file_coverage"),
    ({"complete": True, "inspected_files": ["SKILL.md"], "findings": [], "summary": "Missing hidden file."},
     "incomplete_file_coverage"),
    ({"complete": True, "inspected_files": ["SKILL.md", "SKILL.md"], "findings": [], "summary": "Duplicated file."},
     "incomplete_file_coverage"),
    ({"complete": True, "inspected_files": ["SKILL.md", ".hidden.txt"], "summary": "Elsewhere.",
      "findings": [{"file_path": "other.md", "severity": "LOW", "title": "Note", "detail": "Outside the files."}]},
     "finding_outside_reviewed_files"),
])
def test_incomplete_coverage_or_outside_findings_never_pass(artifact, output, reason):
    report = review(artifact, ReviewModel(output=output))
    assert (report["status"], report["reason"]) == ("incomplete", reason)
    assert report["decision"] is None


def test_unreadable_oversized_or_changed_input_does_not_reach_the_model(artifact):
    model = ReviewModel()
    (artifact / "binary").write_bytes(b"\xff\x00")
    assert (review(artifact, model)["reason"]) == "non_text_content"
    (artifact / "binary").write_bytes(b"text with a NUL \x00")
    assert (review(artifact, model)["reason"]) == "non_text_content"
    (artifact / "binary").unlink()
    (artifact / "large.txt").write_text("data" * 1024)
    oversized = review(artifact, model, max_input_bytes=1024)
    assert (oversized["status"], oversized["reason"], oversized["detail"]) == (
        "incomplete", "input_too_large", "max_input_bytes=1024"
    )
    manifest = build_skill_tree_manifest(artifact)
    (artifact / "large.txt").write_text("changed after the scan")
    assert review(artifact, model, manifest=manifest)["reason"] == "input_changed"
    (artifact / "large.txt").unlink()
    assert review(artifact, model, manifest=manifest)["reason"] == "input_changed"
    assert not model.calls


def test_medium_findings_block_without_changing_the_static_verdict(artifact):
    output = {"complete": True, "inspected_files": ["SKILL.md", ".hidden.txt"],
              "findings": [{"file_path": "SKILL.md", "severity": "MEDIUM", "title": "Unexpected capability",
                            "detail": "The requested behavior exceeds its stated purpose."}], "summary": "Needs review."}
    report = review(artifact, ReviewModel(output=output))
    assert (report["status"], report["reason"]) == ("blocked", "findings")


def _status_error(cls, status_code, message):
    request = httpx.Request("POST", "https://models.example.invalid/v1/messages")
    return cls(message, response=httpx.Response(status_code, request=request), body=None)


def _failures():
    """(model, reason, detail, error_type): each a real exception or stop the provider path produces."""

    invalid_output = {"complete": True, "inspected_files": [], "summary": "Bad severity.",
                      "findings": [{"file_path": "SKILL.md", "severity": "EXTREME", "title": "t", "detail": "d"}]}
    stream_timeout = ModelStreamTimeoutError(
        limit="idle", limit_seconds=1.0, elapsed_seconds=1.5, events_received=0, model_id="offline-review"
    )
    team_key = ResolvedKey(address=ENDPOINT.address, value=SECRET, source=KeySource.STORED)
    return {
        "token limit": (ReviewModel(stop_reason="max_tokens"), "model_did_not_finish", "max_tokens",
                        "MaxTokensReachedException"),
        # Three invalid decisions reach the reviewer's turn limit: the same cause and fix as no decision.
        "turn limit": (ReviewModel(output=invalid_output), "model_output_invalid", "limit_turns", ""),
        "provider filter": (ReviewModel(stop_reason="content_filtered"), "model_did_not_finish", "content_filtered",
                            ""),
        "no decision": (ReviewModel(stop_reason="end_turn"), "model_output_invalid", "", "StructuredOutputException"),
        "context window": (ReviewModel(error=ContextWindowOverflowException("prompt is too long")),
                           "input_too_large_for_model", "", "ContextWindowOverflowException"),
        "stream watchdog": (ReviewModel(error=stream_timeout), "model_timed_out", "", "ModelStreamTimeoutError"),
        "client timeout": (ReviewModel(error=httpx.ReadTimeout("The read operation timed out")),
                           "model_timed_out", "", "ReadTimeout"),
        "unreachable": (ReviewModel(error=httpx.ConnectError("All connection attempts failed")),
                        "model_unreachable", "", "ConnectError"),
        "busy": (ReviewModel(error=RuntimeError("Error code: 429 - rate limit exceeded")), "model_busy", "",
                 "RuntimeError"),
        "provider refused the key": (
            ReviewModel(error=_status_error(anthropic.AuthenticationError, 401, f"invalid x-api-key {SECRET}")),
            "model_key_rejected", "", "AuthenticationError"),
        "key binding: provider refused the key": (
            ReviewModel(error=ModelKeyRejectedError(ENDPOINT, team_key, 401)), "model_key_rejected",
            rejection_message(ENDPOINT.host, KeySource.STORED, None), "ModelKeyRejectedError"),
        "key binding: own model needed": (ReviewModel(error=ModelKeyNotAllowedError(ENDPOINT)), "model_key_refused",
                                          own_model_needed_message(), "ModelKeyNotAllowedError"),
        "anything else": (ReviewModel(error=RuntimeError(f"provider said {SECRET}")), "model_call_failed", "",
                          "RuntimeError"),
    }


@pytest.mark.parametrize("case", sorted(_failures()))
def test_each_failure_is_incomplete_with_its_reason_and_no_provider_text(artifact, caplog, case):
    model, reason, detail, error_type = _failures()[case]
    caplog.set_level(logging.WARNING)

    report = review(artifact, model)

    assert (report["status"], report["reason"], report["detail"], report["error_type"]) == (
        "incomplete", reason, detail, error_type
    )
    assert report["decision"] is None
    for text in (json.dumps(report), caplog.text):
        assert SECRET not in text
        assert "provider said" not in text and "x-api-key" not in text


def test_a_throttled_model_wrapped_by_strands_is_busy():
    # Strands retries ModelThrottledException with back-off delays of seconds,
    # so this case is classified directly, wrapped as the event loop wraps it.
    reason, detail, error_type = semantic._classify_failure(EventLoopException(ModelThrottledException("Throttled")))
    assert (reason, detail, error_type) == ("model_busy", "", "ModelThrottledException")


@pytest.mark.asyncio
async def test_a_stop_during_the_model_call_cancels_it_and_the_stopped_turn_waits_for_it(artifact, monkeypatch):
    from custom_agents import turn_registry

    monkeypatch.setattr(turn_registry, "TURN_POLL_SECONDS", 0.01)
    streaming = asyncio.Event()
    seen = {}

    async def wait_until_cancelled():
        streaming.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            seen["stream_cancelled"] = True
            raise

    model = ReviewModel(before_answer=wait_until_cancelled)
    agent = turn_agent(_strands_model=model)
    manifest = build_skill_tree_manifest(artifact)

    def tool():
        seen["report"] = semantic.assess_skill(agent, artifact, manifest, selected())

    async def turn():
        tool_stop = start_turn(agent, "review-task")
        thread = asyncio.create_task(asyncio.to_thread(tool))
        try:
            await streaming.wait()
            seen["running"] = [call.tool_name for call in tool_stop.running()]
            # The turn's own stop cleanup (invoke_with_tool_stop): it waits for the review.
            await stop_tool_calls(tool_stop, logging.getLogger("tests.review_stop"), reason="stopped by the test")
            seen["after_stop"] = (seen.get("stream_cancelled"), tool_stop.running(), set(agent._owned_tasks))
        finally:
            end_turn(agent)
            await thread

    with task_work("review-task"):
        await asyncio.create_task(turn())

    assert seen["running"] == [semantic.REVIEW_WORK_NAME]
    assert seen["after_stop"] == (True, (), set())
    assert (seen["report"]["status"], seen["report"]["reason"], seen["report"]["error_type"]) == (
        "incomplete", "turn_finished", "TurnEnded"
    )


def test_a_stop_as_the_model_answers_still_refuses(artifact):
    async def stop_the_turn():
        current_task_work().tool_stop.request("stopped as the model answered")

    report = review(artifact, ReviewModel(before_answer=stop_the_turn))
    assert (report["status"], report["reason"]) == ("incomplete", "turn_finished")


@pytest.fixture
def installation(tmp_path, monkeypatch):
    use_signed_contexts(monkeypatch)
    config, path = _write_config(tmp_path)
    config.workspace.skill_installation.semantic_review.mode = "task_model"
    agent = with_turn_bookkeeping(_agent(config, path))
    agent._strands_model = ReviewModel()
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    yield agent, SkillInstallationService(agent)
    access_runtime._runtime_access.cache_clear()


def stage_in_turn(agent, service):
    return run_tool_in_turn(agent, TASK_ID, lambda: service.stage(repository_url=REPOSITORY_URL))


def test_protected_plan_binds_static_and_semantic_reports_and_receipt(installation):
    agent, service = installation
    plan = stage_in_turn(agent, service)
    assert plan["scan"]["mode"] == "static_no_llm"
    assert plan["semantic_review"]["status"] == "completed"
    installed = service.apply(plan["change_id"], agreement())
    assert installed["status"] == "installed"
    assert installed["semantic_review"]["report_sha256"] == plan["semantic_review"]["report_sha256"]


def test_changed_semantic_report_refuses_application(installation):
    agent, service = installation
    plan = stage_in_turn(agent, service)
    report = Path(plan["semantic_review"]["report_path"])
    with pytest.raises(PermissionError):
        report.write_text("{}")
    report.chmod(0o600)  # Simulate operator-level damage to protected evidence.
    report.write_text("{}")
    with pytest.raises(PermissionError, match="Semantic review evidence"):
        service.apply(plan["change_id"], agreement())
    assert not Path(plan["destination_path"]).exists()


def test_changed_review_configuration_requires_a_new_plan(installation):
    agent, service = installation
    plan = stage_in_turn(agent, service)
    agent.skill_installation_config["semantic_review"]["mode"] = "off"
    with pytest.raises(PermissionError, match="settings changed"):
        service.apply(plan["change_id"], agreement())


def _refused_log_line(caplog) -> str:
    return next(r.getMessage() for r in caplog.records if "event=skill_semantic_review_refused" in r.getMessage())


def _protected_review(agent) -> tuple[str, dict]:
    """The change id and protected report of the one review staging wrote."""

    (report,) = resolve_skill_installation_paths(agent).reports.glob("*.semantic.json")
    return report.name.removesuffix(".semantic.json"), json.loads(report.read_text())


def _nothing_proposed(agent) -> bool:
    paths = resolve_skill_installation_paths(agent)
    return not list(paths.plans.glob("*.json")) and not list(paths.installed.iterdir())


def test_failed_selected_review_never_creates_an_installable_plan(installation, caplog):
    agent, service = installation
    agent._strands_model = ReviewModel(error=RuntimeError(f"synthetic provider failure {SECRET}"))
    caplog.set_level(logging.INFO)

    with pytest.raises(PermissionError) as refused:
        stage_in_turn(agent, service)

    assert "did not pass (model_call_failed)" in str(refused.value)
    assert "runtime log names the error type" in str(refused.value)
    assert "skill_scans" in str(refused.value)  # the person's readable copy of the report
    assert _nothing_proposed(agent)
    change_id, review = _protected_review(agent)
    line = _refused_log_line(caplog)
    for field in (
        f"change_id={change_id}", "skill_name=reviewed-skill",
        f"artifact_tree_sha256={review['artifact_tree_sha256']}", f"task_id={TASK_ID}", "mode=task_model",
        "status=incomplete", "reason=model_call_failed", "error_type=RuntimeError", "model_id=offline-review",
    ):
        assert field in line, (field, line)
    assert SECRET not in caplog.text and SECRET not in str(refused.value)


def test_staging_without_a_model_refuses_with_its_reason(installation, caplog):
    agent, service = installation
    agent._strands_model = None
    caplog.set_level(logging.INFO)

    with pytest.raises(PermissionError, match=r"did not pass \(task_model_unavailable\)"):
        stage_in_turn(agent, service)

    assert _nothing_proposed(agent)
    assert "reason=task_model_unavailable" in _refused_log_line(caplog)


def test_off_stages_and_applies_on_the_local_checks_and_logs_the_outcome(tmp_path, monkeypatch, caplog):
    use_signed_contexts(monkeypatch)
    config, path = _write_config(tmp_path)
    agent = _agent(config, path)
    agent._strands_model = ReviewModel()
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    service = SkillInstallationService(agent)
    caplog.set_level(logging.INFO)
    try:
        plan = service.stage(repository_url=REPOSITORY_URL)
        installed = service.apply(plan["change_id"], agreement())
    finally:
        access_runtime._runtime_access.cache_clear()

    assert plan["semantic_review"]["status"] == "not_configured"
    assert installed["status"] == "installed"
    assert not agent._strands_model.calls
    line = next(r.getMessage() for r in caplog.records if "event=skill_semantic_review_finished" in r.getMessage())
    assert "mode=off status=not_configured reason=disabled" in line and f"change_id={plan['change_id']}" in line


@pytest.mark.parametrize("old_reason", ["task_model_unavailable", "disabled"])
def test_apply_refuses_a_model_free_report_while_the_review_is_selected(installation, old_reason):
    """Finding F1, apply side: a plan whose report says no model ran never installs under task_model."""

    agent, service = installation
    plan = stage_in_turn(agent, service)
    paths = resolve_skill_installation_paths(agent)
    report_path = Path(plan["semantic_review"]["report_path"])
    report = json.loads(report_path.read_text())
    # The PR head's shape: bound to task_model, but no model ran.
    report.update(status="not_configured", reason=old_reason, decision=None, model_id="", provider="")
    report_path.chmod(0o600)  # Simulate operator-level damage to protected evidence.
    report_path.write_text(json.dumps(report))
    store = service._plan_store(paths)
    stored = store.load(plan["change_id"])
    evidence = stored.semantic_review.model_copy(
        update={"status": "not_configured", "report_sha256": canonical_json_sha256(report)}
    )
    forged = stored.model_copy(update={"semantic_review": evidence})
    store.save(forged.model_copy(update={"plan_digest": model_digest(forged)}))

    with pytest.raises(PermissionError, match="Semantic review evidence"):
        service.apply(plan["change_id"], agreement())
    assert not Path(plan["destination_path"]).exists()


@pytest.mark.asyncio
async def test_staging_that_outlives_its_stopped_turn_proposes_nothing(installation, monkeypatch):
    """Finding F1's trigger through stage(): the turn stopped during the scan, which then finished."""

    agent, service = installation
    scanning, resume = threading.Event(), threading.Event()
    outcome = {}

    def slow_scan(*args, **kwargs):
        scanning.set()
        resume.wait(5)
        return _safe_scan_result()

    monkeypatch.setattr(skill_installation, "scan_skill_with_skillspector", slow_scan)

    def staging_tool():
        try:
            service.stage(repository_url=REPOSITORY_URL)
        except PermissionError as exc:
            outcome["refusal"] = str(exc)

    async def turn():
        start_turn(agent, TASK_ID)
        try:
            await asyncio.to_thread(staging_tool)
        finally:
            end_turn(agent)

    with task_work(TASK_ID):
        owner = asyncio.create_task(turn())
        await asyncio.wait_for(asyncio.to_thread(scanning.wait, 5), 6)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        resume.set()
        for _ in range(500):
            if "refusal" in outcome:
                break
            await asyncio.sleep(0.01)

    assert "did not pass (turn_finished)" in outcome["refusal"]
    assert not agent._strands_model.calls
    assert _nothing_proposed(agent)


@pytest.mark.asyncio
async def test_a_stop_during_the_review_through_stage_proposes_nothing(installation, monkeypatch):
    """C04(b): the turn's own stop cleanup cancels the review's model call; staging refuses and saves no plan."""

    from custom_agents import turn_registry

    monkeypatch.setattr(turn_registry, "TURN_POLL_SECONDS", 0.01)
    agent, service = installation
    streaming = asyncio.Event()
    seen = {}

    async def wait_until_cancelled():
        streaming.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            seen["stream_cancelled"] = True
            raise

    agent._strands_model = ReviewModel(before_answer=wait_until_cancelled)

    def staging_tool():
        try:
            service.stage(repository_url=REPOSITORY_URL)
        except PermissionError as exc:
            seen["refusal"] = str(exc)

    async def turn():
        tool_stop = start_turn(agent, TASK_ID)
        thread = asyncio.create_task(asyncio.to_thread(staging_tool))
        try:
            await asyncio.wait_for(streaming.wait(), 10)
            await stop_tool_calls(tool_stop, logging.getLogger("tests.review_stop"), reason="stopped by the test")
            seen["after_stop"] = (seen.get("stream_cancelled"), tool_stop.running(), set(agent._owned_tasks))
        finally:
            end_turn(agent)
            await thread

    with task_work(TASK_ID):
        await asyncio.create_task(turn())

    assert seen["after_stop"] == (True, (), set())
    assert "did not pass (turn_finished)" in seen["refusal"]
    assert _nothing_proposed(agent)


def test_static_rejection_never_sends_skill_contents_to_the_model(installation, monkeypatch):
    from custom_agents import skill_installation
    from custom_agents.skill_repository import SkillScanRejected

    agent, service = installation
    def reject(*args, **kwargs):
        raise SkillScanRejected("Synthetic mandatory local rejection")

    monkeypatch.setattr(skill_installation, "scan_skill_with_skillspector", reject)
    with pytest.raises(SkillScanRejected, match="mandatory local rejection"):
        stage_in_turn(agent, service)
    assert not agent._strands_model.calls


def test_one_gate_binds_the_settings_and_passes_a_model_free_report_only_when_off(artifact):
    manifest = build_skill_tree_manifest(artifact)
    paths = {entry.path for entry in manifest.entries}
    off, on = SkillSemanticReviewSettings(), selected()

    model_free = semantic.assess_skill(SimpleNamespace(), artifact, manifest, off)
    completed = review(artifact, ReviewModel())
    semantic.validate_report(model_free, manifest.tree_sha256, paths, off)
    semantic.validate_report(completed, manifest.tree_sha256, paths, on)

    # A review made under one setting never passes under another.
    for report, settings in ((model_free, on), (completed, off), (completed, selected(max_input_bytes=2048))):
        with pytest.raises(ValueError, match="other review settings"):
            semantic.validate_report(report, manifest.tree_sha256, paths, settings)
    # Under task_model only a completed review passes; the PR head accepted these model-free reports.
    bound = semantic.configuration_digest(on)
    for reason in ("task_model_unavailable", "disabled"):
        with pytest.raises(ValueError, match="blocked or incomplete"):
            semantic.validate_report(
                {**model_free, "configuration_sha256": bound, "reason": reason}, manifest.tree_sha256, paths, on
            )
    with pytest.raises(ValueError, match="blocked or incomplete"):
        semantic.validate_report(review(artifact, None), manifest.tree_sha256, paths, on)
    # Under off only the model-free report itself passes.
    with pytest.raises(ValueError, match="model-free"):
        semantic.validate_report(
            {**model_free, "decision": completed["decision"]}, manifest.tree_sha256, paths, off
        )


def test_a_refusal_names_its_reason_detail_fix_and_report():
    passing = {semantic.AssessmentReason.DISABLED, semantic.AssessmentReason.ASSESSED}
    assert set(semantic.REASON_HINTS) == set(semantic.AssessmentReason) - passing
    path = Path("/workspace/skill_scans/skill_install_0123456789ab.json")

    too_large = semantic.refusal_message({"reason": "input_too_large", "detail": "max_input_bytes=1024"}, path)
    invalid = semantic.refusal_message({"reason": "model_output_invalid", "detail": "limit_turns"}, path)
    key = semantic.refusal_message({"reason": "model_key_refused", "detail": own_model_needed_message()}, None)
    unknown = semantic.refusal_message({"reason": "not-a-reason"}, path)
    mismatched = semantic.refusal_message({"reason": "assessed"}, path)

    assert "(input_too_large, max_input_bytes=1024)" in too_large and "raise that limit" in too_large
    assert "(model_output_invalid, limit_turns)" in invalid and "follows structured output" in invalid
    assert "(model_key_refused)" in key and own_model_needed_message() in key
    assert "protected runtime reports" in key
    assert "(unknown)" in unknown and "does not match" in unknown
    assert "(assessed)" in mismatched and "does not match" in mismatched
    for message in (too_large, invalid, unknown, mismatched):
        assert str(path) in message and "local static checks passed" in message
