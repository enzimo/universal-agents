from __future__ import annotations

import ast
from fnmatch import fnmatchcase
from pathlib import Path
import re
import shlex
import tomllib

from packaging.requirements import Requirement
import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = PROJECT_ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict:
    return yaml.load(
        (WORKFLOW_ROOT / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


CHECK_WORKFLOWS = (
    "tests.yml",
    "security-quality.yml",
    "dependency-audit.yml",
    "personal-details.yml",
)


@pytest.mark.parametrize("name", CHECK_WORKFLOWS)
def test_checks_run_only_for_release_candidates_or_by_hand(name: str) -> None:
    triggers = _workflow(name)["on"]

    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v*.*.*-rc.*"]}


@pytest.mark.parametrize(
    ("tag", "starts"),
    [
        ("v0.10.0-rc.1", True),
        ("v10.20.30-rc.12", True),
        ("v0.10.0", False),
        ("v0.10.0-beta.1", False),
        ("v0.9.9-nightly.202610240317", False),
    ],
)
def test_only_release_candidate_tags_start_the_checks(tag: str, starts: bool) -> None:
    (pattern,) = _workflow("tests.yml")["on"]["push"]["tags"]

    assert fnmatchcase(tag, pattern) is starts


def test_security_quality_installs_required_rust_components() -> None:
    workflow = _workflow("security-quality.yml")
    steps = workflow["jobs"]["security-quality"]["steps"]
    toolchain = next(step for step in steps if step.get("name") == "Ensure pinned Rust toolchain")
    assert "--component rustfmt,clippy" in toolchain["run"]
    assert "--no-self-update" in toolchain["run"]
    assert "rustup default" not in toolchain["run"]


def test_security_coverage_uses_hermetic_profile() -> None:
    # Live NATS tests run in the separate job that starts a broker.
    steps = _workflow("security-quality.yml")["jobs"]["security-quality"]["steps"]
    coverage = next(step for step in steps if step.get("name") == "Coverage for security-sensitive regressions")
    assert "-m pytest -m 'not live and not external'" in coverage["run"]


def test_security_quality_commands_reference_existing_python_inputs() -> None:
    """Refactors must move CI inputs with the modules and tests they check."""
    steps = _workflow("security-quality.yml")["jobs"]["security-quality"]["steps"]
    checked = []
    for step in steps:
        for token in shlex.split(step.get("run", "").replace("\\\n", " ")):
            if token.startswith("--include="):
                for pattern in token.removeprefix("--include=").split(","):
                    assert list(PROJECT_ROOT.glob(pattern)), pattern
                    checked.append(pattern)
            elif token.endswith((".py", "/")) and not token.startswith(("-", "$")):
                assert (PROJECT_ROOT / token).exists(), token
                checked.append(token)
    assert checked


def test_ruff_correctness_baseline_is_repository_owned() -> None:
    config = tomllib.loads((PROJECT_ROOT / "ruff.toml").read_text(encoding="utf-8"))

    assert config["lint"]["select"] == ["E4", "E7", "E9", "F"]


def test_cognitive_complexity_advisory_is_repository_owned() -> None:
    config = tomllib.loads(
        (PROJECT_ROOT / "complexipy.toml").read_text(encoding="utf-8")
    )
    requirements = (PROJECT_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    lock = (PROJECT_ROOT / "requirements-dev.lock.txt").read_text(encoding="utf-8")
    workflow = _workflow("security-quality.yml")
    steps = workflow["jobs"]["security-quality"]["steps"]
    checkout = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    advisory = next(
        step
        for step in steps
        if step.get("name") == "Cognitive complexity advisory for changed functions"
    )

    assert config["max-complexity-allowed"] == 15
    assert config["ignore-complexity"] is True
    assert config["check-script"] is False
    assert "tests" not in config["paths"]
    requirement = Requirement(next(line for line in requirements.splitlines() if line.startswith("complexipy")))
    locked = re.search(r"^complexipy==([^\s\\]+)", lock, re.MULTILINE)
    assert locked and locked[1] in requirement.specifier
    assert checkout["with"]["fetch-depth"] == "0"
    assert advisory["continue-on-error"] == "true"
    assert "env" not in advisory
    assert advisory["run"] == "python scripts/report_cognitive_complexity.py origin/main"


def test_postgres_live_job_controls_pull_retries_and_cleanup() -> None:
    workflow = _workflow("tests.yml")
    job = workflow["jobs"]["postgres-live"]
    steps = job["steps"]
    start = next(
        step for step in steps if step.get("name") == "Start PostgreSQL with pull retries"
    )
    wait = next(step for step in steps if step.get("name") == "Wait for PostgreSQL")

    assert "services" not in job
    assert "for attempt in 1 2 3" in start["run"]
    assert "docker pull postgres:16-alpine" in start["run"]
    assert "python .github/runner/start_service.py postgres" in start["run"]
    assert 'docker exec "$UA_CI_POSTGRES" pg_isready' in wait["run"]
    assert 'touch "$UA_CI_CACHE/docker-used"' in start["run"]


def test_photon_sidecar_ci_uses_the_pinned_node_24_runtime() -> None:
    workflow = _workflow("tests.yml")
    steps = workflow["jobs"]["photon-sidecar"]["steps"]
    setup_node = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    )
    install = next(
        step for step in steps if step.get("name") == "Install Photon sidecar dependencies"
    )
    test = next(step for step in steps if step.get("name") == "Test Photon sidecar")

    assert setup_node["uses"] == (
        "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020"
    )
    assert setup_node["with"] == {
        "node-version": "24.20.0",
        "package-manager-cache": "false",
    }
    assert install["working-directory"] == (
        "assistant_gateway/interactive_apps/photon_sidecar"
    )
    assert install["run"] == "npm ci --no-audit --no-fund"
    assert test["working-directory"] == install["working-directory"]
    assert test["run"] == "npm test"


def test_photon_sidecar_dependency_audit_blocks_moderate_findings() -> None:
    workflow = _workflow("dependency-audit.yml")
    steps = workflow["jobs"]["photon-sidecar-audit"]["steps"]
    setup_node = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    )
    audit = next(
        step
        for step in steps
        if step.get("name") == "Audit Photon sidecar production dependencies"
    )

    assert setup_node["with"]["node-version"] == "24.20.0"
    assert setup_node["with"]["package-manager-cache"] == "false"
    assert audit["working-directory"] == (
        "assistant_gateway/interactive_apps/photon_sidecar"
    )
    assert audit["run"] == "npm audit --omit=dev --audit-level=moderate"


def test_rust_dependency_audit_blocks_vulnerabilities_yanks_and_unsoundness() -> None:
    steps = _workflow("dependency-audit.yml")["jobs"]["cargo-audit"]["steps"]
    install = next(step for step in steps if step.get("name") == "Install pinned Rust audit tool")
    audit = next(step for step in steps if step.get("name") == "Audit Rust dependencies and maintenance")

    assert install["run"] == "cargo install --locked cargo-audit --version 0.22.2"
    assert audit["run"] == "cargo audit --deny yanked --deny unsound"


def test_runtime_install_job_uses_a_clean_environment_and_only_runtime_lock() -> None:
    job = _workflow("tests.yml")["jobs"]["runtime-install"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.12", "3.13"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "python -m venv .ci-runtime" in commands
    assert "--require-hashes -r requirements.lock.txt" in commands
    assert "requirements-dev" not in commands
    assert ".ci-runtime/bin/python -m pip check" in commands
    assert ".ci-runtime/bin/python scripts/verify_runtime_install.py" in commands


def test_jobs_require_their_documented_runners_and_bounded_execution() -> None:
    for path in WORKFLOW_ROOT.glob("*.yml"):
        for name, job in _workflow(path.name)["jobs"].items():
            assert int(job["timeout-minutes"]) <= 90
            for step in job["steps"]:
                assert "cache" not in step.get("with", {}), (path, name)
            # Public docs are the sole hosted-runner exception. Other jobs must
            # still refuse public repositories and require the local hooks.
            if path.name == "publish-docs.yml":
                assert job["runs-on"] == "ubuntu-24.04", (path, name)
                assert "repository.private" not in job.get("if", "")
                assert not any("UA_CI_" in step.get("run", "") for step in job["steps"])
                continue
            runners = ([job["runs-on"]] if isinstance(job["runs-on"], list) else
                       [entry["runner"] for entry in job["strategy"]["matrix"]["include"]])
            for runner in runners:
                assert "self-hosted" in runner, (path, name)
                assert "ua-linux" in runner or "ua-macos" in runner, (path, name)
            assert "github.event.repository.private" not in job["if"]
            assert "pull_request" in job["if"]
            assert "UA_CI_HOOK_VERSION" in job["steps"][0]["run"]
            assert "UA_CI_PUBLIC_JOB_POLICY_VERSION" in job["steps"][0]["run"]


def test_live_nats_uses_job_identity_and_dynamic_loopback_port() -> None:
    steps = _workflow("tests.yml")["jobs"]["nats-live"]["steps"]
    start = next(s for s in steps if s.get("name") == "Start NATS with JetStream")
    assert start["run"] == "python .github/runner/start_service.py nats"


def test_evidence_storage_cannot_skip_tests_or_hide_verification_failures() -> None:
    job = _workflow("tests.yml")["jobs"]["fast"]
    steps = job["steps"]
    canary = next(s for s in steps if s.get("name") == "Run reviewed GitHub skill-installation canary")
    tests = next(s for s in steps if s.get("name") == "Run hermetic test profile")
    upload = next(s for s in steps if s.get("id") == "canary-evidence")
    warning = next(s for s in steps if s.get("name") == "Report unavailable evidence archive")

    assert "continue-on-error" not in job
    for verification in (canary, tests):
        assert "continue-on-error" not in verification
        assert "if" not in verification
        assert steps.index(verification) < steps.index(upload)
    assert upload["continue-on-error"] == "true"
    assert upload["if"] == "always()"
    assert upload["with"]["retention-days"] == "7"
    assert warning["if"] == "always() && steps.canary-evidence.outcome == 'failure'"
    assert "::warning::" in warning["run"]


def test_postgres_live_job_runs_every_postgres_test() -> None:
    steps = _workflow("tests.yml")["jobs"]["postgres-live"]["steps"]
    run_tests = next(step for step in steps if step.get("name") == "Run every PostgreSQL test")

    assert run_tests["run"].startswith("python -m pytest tests -m postgres ")


def _is_postgres_mark(node: ast.expr) -> bool:
    return ast.unparse(node) == "pytest.mark.postgres"


def _marks_include_postgres(marks: ast.expr | None) -> bool:
    if marks is None:
        return False
    items = marks.elts if isinstance(marks, (ast.List, ast.Tuple)) else [marks]
    return any(_is_postgres_mark(item) for item in items)


def _reads_test_database_address(function: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and ast.unparse(node.func) in {"os.environ.get", "os.getenv"}
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "LIVE_TEST_POSTGRES_DSN"
        for node in ast.walk(function)
    )


def test_every_postgres_test_is_marked_for_the_postgres_job() -> None:
    """The PostgreSQL CI job selects tests by the postgres marker.

    A Postgres variant marked only live is skipped by the fast job (no database)
    and never selected by the PostgreSQL job, so it silently never runs.
    """

    unmarked = []
    for path in sorted((PROJECT_ROOT / "tests").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_marked = any(
            isinstance(node, ast.Assign)
            and any(ast.unparse(target) == "pytestmark" for target in node.targets)
            and _marks_include_postgres(node.value)
            for node in tree.body
        )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and ast.unparse(node.func) == "pytest.param"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "postgres"
            ):
                marks = next((kw.value for kw in node.keywords if kw.arg == "marks"), None)
                if not _marks_include_postgres(marks):
                    unmarked.append(f"{path.name}:{node.lineno} pytest.param('postgres')")
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
                and _reads_test_database_address(node)
                and not module_marked
                and not any(_is_postgres_mark(decorator) for decorator in node.decorator_list)
            ):
                unmarked.append(f"{path.name}:{node.lineno} {node.name}")

    assert unmarked == []
