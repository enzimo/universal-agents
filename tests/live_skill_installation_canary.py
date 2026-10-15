#!/usr/bin/env python3
"""Install and remove a real GitHub skill through ``apply_change``.

Fetches a public skill from GitHub, scans it with the real SkillSpector
scanner, and runs both ways a team skill change is approved (access plan,
decision L2) against the real Cedar authorizer and the SQLite access store:

1. TeamArchitect holds no access to the team's skills: ``apply_change`` pauses,
   the owner approves the exact install in the Inbox, and the call resumes and
   installs exactly the scanned files. The approval is used once.
2. The owner grants TeamArchitect ``full`` on ``fn:team_skills``: removing the
   skill applies at once, the skill is kept in a recoverable folder, and the
   administrators get a silent notice for each change.

Writes a JSON evidence file; exits non-zero when a check fails.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strands.interrupt import InterruptException

from assistant_gateway.config import GatewayInstanceConfig

# Load the trusted catalog composition root as the agent runtime does before
# services resolve agent definitions.
from custom_agents import agent_catalog as _agent_catalog  # noqa: F401
from custom_agents import access_runtime
from custom_agents.skill_installation import SkillInstallationService, verify_managed_skill_attestation
from custom_agents.skill_uninstallation import SkillUninstallationService
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeKind,
    AccessRequestStatus,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from tests.broker_access_support import context_verify_keys
from tests.change_approval_support import ToolCall, change_agent, decide, owner, run_apply_change, stored_request

DEFAULT_BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
DEFAULT_SCANNER = Path("skillspector")
DEFAULT_REPOSITORY = "https://github.com/openai/skills"
DEFAULT_SKILL_PATH = "skills/.curated/define-goal"
DEFAULT_EVIDENCE = ROOT / "artifacts" / "skill_installation_canary_evidence.json"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _NoPause(ToolCall):
    """A tool context for a change that must apply without waiting for anyone."""

    def interrupt(self, name: str, reason: Any = None) -> Any:
        raise AssertionError(f"unexpected pause for approval: {reason}")


def _resolved_file(path: Path, label: str) -> Path:
    if path.parent == Path("."):
        found = shutil.which(str(path))
        _require(bool(found), f"{label} is not on PATH: {path}")
        path = Path(str(found))
    path = path.expanduser().resolve()
    _require(path.is_file(), f"{label} does not exist: {path}")
    return path


def _lead(config: GatewayInstanceConfig, config_path: Path, binary: Path) -> Any:
    """TeamArchitect working for the instance owner, with authorization on.

    Team messages (the skill catalog refresh) are recorded instead of sent.
    """

    sent: list[Any] = []

    async def send_operational_message(envelope: Any) -> None:
        sent.append(envelope)

    agent = change_agent(
        config,
        config_path,
        authorization_enabled=True,
        skill_installation_config=config.workspace.skill_installation.model_dump(mode="json"),
        skills_dir=str(config.workspace_path / "skills"),
        additional_skill_directories=[],
        plugins=[],
        _send_operational_message=send_operational_message,
        sent_team_messages=sent,
    )
    agent.authorization_config = {"enabled": True, "cedar_binary_path": str(binary)}
    return agent


def _grant_team_skills(agent: Any) -> None:
    """The owner lets TeamArchitect's agent type manage the team's skills."""

    access_runtime.runtime_access(agent).service.grant_by_person(
        owner(agent),
        GrantV1(
            grant_id="new",
            team_id=agent.team_id,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{agent.team_id}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="fn:team_skills"),
            level=AccessLevel.FULL,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=owner(agent).person_id),
            created_at_utc=datetime.now(timezone.utc),
        ),
    )


def _administrator_notices(agent: Any) -> list[Any]:
    store = access_runtime.runtime_access(agent).service.store
    return [notice for notice, _read in store.notices_for(agent.team_id, person_id=owner(agent).person_id, roles=("admin",))]


def _install_with_person_approval(agent: Any, plan: dict[str, Any], checks: dict[str, bool]) -> dict[str, Any]:
    call = ToolCall("tooluse_install")
    try:
        run_apply_change(agent, plan["change_id"], call, reason="Install the reviewed skill for the team")
        raise AssertionError("apply_change did not pause for the owner's approval")
    except InterruptException:
        pass
    request = stored_request(agent, call)
    checks["install_waits_for_the_owner"] = (
        request.status is AccessRequestStatus.WAITING
        and request.change is not None
        and request.change.kind == "team_skill_install"
        and request.change.plan_digest == plan["plan_digest"]
        and not Path(plan["destination_path"]).exists()
    )
    decide(agent, call)
    applied = run_apply_change(agent, plan["change_id"], call)
    result = applied.get("result") or {}
    installed = Path(str(result.get("installed_path") or ""))
    checks["exact_scanned_tree_installed"] = (
        applied.get("status") == "applied"
        and result.get("status") == "installed"
        and (installed / "SKILL.md").is_file()
        and result.get("artifact_tree_sha256") == result.get("installed_tree_sha256")
    )
    checks["approval_recorded_with_its_request"] = (
        applied["approval"]["mode"] == "person_approval"
        and applied["approval"]["access_request_id"] == request.request_id
        and result.get("authority", {}).get("approval") == applied["approval"]
    )
    checks["activation_attestation_verified"] = (
        verify_managed_skill_attestation(agent, installed) == result.get("installed_tree_sha256")
    )
    checks["running_skills_refreshed_and_team_told"] = result.get("runtime_skill_refresh", {}).get("status") == "completed"
    again = run_apply_change(agent, plan["change_id"], call)
    checks["approval_used_once"] = again.get("status") == "refused" and "already applied" in str(again.get("reason"))
    return {"request_id": request.request_id, "applied": applied}


def _remove_with_held_access(agent: Any, skill_name: str, installed_path: Path, checks: dict[str, bool]) -> dict[str, Any]:
    _grant_team_skills(agent)
    removal = SkillUninstallationService(agent).stage(skill_name=skill_name)
    removed = run_apply_change(agent, removal["change_id"], _NoPause("tooluse_removal"), reason="Clean up the check")
    result = removed.get("result") or {}
    checks["removal_applied_at_once_with_held_access"] = (
        removed.get("status") == "applied"
        and removed["approval"]["mode"] == "held_access"
        and removed["approval"]["access_request_id"] is None
    )
    checks["managed_skill_removed_recoverably"] = (
        result.get("status") == "uninstalled"
        and not installed_path.exists()
        and Path(str(result.get("removed_skill_path") or "")).is_dir()
        and result.get("recoverable") is True
    )
    checks["team_told_of_the_removal"] = (
        result.get("runtime_skill_refresh", {}).get("team_notification") == "published"
        and len(agent.sent_team_messages) == 2
    )
    notices = _administrator_notices(agent)
    checks["administrators_told_of_both_changes"] = [notice.kind for notice in notices] == [
        AccessNoticeKind.TEAM_SKILL_CHANGED
    ] * 2
    return {"change_id": removal["change_id"], "receipt_id": result.get("receipt_id")}


def run_canary(
    *, binary: Path, scanner: Path, repository_url: str, skill_path: str, evidence_output: Path
) -> dict[str, Any]:
    binary = _resolved_file(binary, "Cedar authorizer")
    scanner = _resolved_file(scanner, "SkillSpector")
    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {
        "schema_version": 2,
        "canary": "skill_installation",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_request": {"repository_url": repository_url, "skill_path": skill_path},
        "checks": checks,
    }
    # The test gateway key signs the owner's task context; agents trust only it.
    os.environ["ACCESS_CONTEXT_VERIFY_KEYS"] = context_verify_keys()
    access_runtime._runtime_access.cache_clear()
    with TemporaryDirectory(prefix="ua-live-skill-installation-") as temp_dir:
        root = Path(temp_dir) / "instance"
        config = GatewayInstanceConfig.create(
            instance_name="skill-installation-canary",
            instance_root=str(root),
            team_id="team_skill_installation_canary",
            timezone="UTC",
        )
        config.workspace.skill_installation.scanner_executable = str(scanner)
        config_path = config.save(root / "instance_config.yaml")
        agent = _lead(config, config_path, binary)
        try:
            plan = SkillInstallationService(agent).stage(repository_url=repository_url, skill_path=skill_path)
            checks["github_repository_resolved_to_commit"] = bool(re.fullmatch(r"[0-9a-f]{40}", plan["commit_sha"]))
            checks["nested_skill_path_selected_exactly"] = plan["skill_relative_path"] == skill_path
            checks["static_scan_safe_and_complete"] = (
                plan["status"] == "safe_pending_approval"
                and plan["scan"]["mode"] == "static_no_llm"
                and plan["scan"]["severity"] == "LOW"
                and plan["scan"]["recommendation"] == "SAFE"
                and plan["scan"]["score"] <= 20
            )
            checks["scan_report_exposed_before_approval"] = Path(plan["scan_report_artifact"]).is_file()
            _require(all(checks.values()), f"Acquisition or static scan checks failed: {checks}")

            installed = _install_with_person_approval(agent, plan, checks)
            installed_result = installed["applied"]["result"]
            removal = _remove_with_held_access(
                agent, plan["skill_name"], Path(installed_result["installed_path"]), checks
            )
            scan_evidence = dict(plan["scan"])
            scan_evidence.pop("report_path", None)
            evidence.update(
                {
                    "resolved_source": {
                        "repository_url": plan["repository_url"],
                        "commit_sha": plan["commit_sha"],
                        "skill_name": plan["skill_name"],
                        "skill_relative_path": plan["skill_relative_path"],
                        "tree_sha256": plan["artifact_tree_sha256"],
                    },
                    "scan": scan_evidence,
                    "scan_issues": plan["scan_issues"],
                    "install": {
                        "change_id": plan["change_id"],
                        "access_request_id": installed["request_id"],
                        "receipt_id": installed_result.get("receipt_id"),
                        "installed_tree_sha256": installed_result.get("installed_tree_sha256"),
                        "runtime_skill_refresh": installed_result.get("runtime_skill_refresh"),
                    },
                    "removal": removal,
                }
            )
        finally:
            service = access_runtime.runtime_access(agent).service
            if service is not None:
                service.cedar.close()
            access_runtime._runtime_access.cache_clear()

    evidence["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    evidence["passed"] = all(checks.values())
    evidence_output = evidence_output.expanduser().resolve()
    evidence_output.parent.mkdir(parents=True, exist_ok=True)
    evidence_output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _require(evidence["passed"], f"Managed skill checks failed: {checks}")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--scanner", type=Path, default=DEFAULT_SCANNER)
    parser.add_argument("--repository-url", default=DEFAULT_REPOSITORY)
    parser.add_argument("--skill-path", default=DEFAULT_SKILL_PATH)
    parser.add_argument("--evidence-output", type=Path, default=DEFAULT_EVIDENCE)
    args = parser.parse_args()
    evidence = run_canary(
        binary=args.binary,
        scanner=args.scanner,
        repository_url=args.repository_url,
        skill_path=args.skill_path,
        evidence_output=args.evidence_output,
    )
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
