"""Installed CLIs end to end (owner's decision 2026-09-28), with a Gitea ``tea``-like program.

Real Cedar, SQLite, the real proposal tool, applier, catalog and broker gate;
the program is a tiny shell script (no real binaries are downloaded) and its
download is served by a local HTTP server standing in for https. Covered:

- the review card says, in trusted code's words, which program runs, its pinned
  digest and download host, whose account and how the credential reaches it,
  each command's level, and the setup step; flags a new program, a download,
  full commands;
- approving downloads the program into ``team/tools/tea/``, checks it and
  writes the catalog file; a digest mismatch applies nothing; a program an
  agent built into the workspace is used when it matches, and a mismatch or a
  link there is refused;
- the gate decides by the command's level (unlisted commands need full), the
  broker refuses blocked commands and flags, and uses only the owners the tool
  allows;
- a run gets the person's credential in an environment variable and a file,
  never in the argument list, and its output never shows it;
- the run uses a checked private copy: a program changed in the workspace
  after approval, or replaced by a link, is refused and logged;
- the program runs in the Landlock sandbox: it cannot read another person's
  credential or the broker's files; without Landlock it is refused with a
  plain message and an event log;
- ``set_up_tools`` plans the CLI's setup step and its account link.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands.interrupt import InterruptException

from access_catalog.catalog import CatalogSource
from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller
from access_control.cli_binaries import ProgramRefused, fetch_https, read_within_deadline, workspaces_root
from access_control.decisions import HandDownRefused
from access_control.proposals import ACCESS_PROPOSAL_KIND, ProposalCompiler, ProposalRefused, proposal_summary
from access_control.requests import AccessRequests
from custom_agents.tool_setup_runs import plan_run
from custom_messages.contracts.access_v2 import (
    AccessProposalV1,
    AccessRequestStatus,
    AccessReviewChoice,
    AccountLinkV1,
    LinkOwnerKind,
    ProposedCliToolV1,
    SubjectKind,
    SubjectRefV1,
)
from custom_tools import code_sandbox
from custom_tools.providers.access_proposals import propose_access_change
from custom_tools.runtime_context import set_agent_ref
from secure_tool_broker import installed_clis as broker_clis
from secure_tool_broker.access_gate import REASON_ACCOUNT_LINK_MISSING
from secure_tool_broker.cli_passthrough import CommandRefused, check_installed_command_line
from secure_tools.installed_clis import installed_cli_provider
from secure_tools.providers import ToolContext
from tests.broker_access_support import agent_context
from tests.test_access_proposals import world  # noqa: F401 - pytest fixture
from tests.test_access_request_tools import BINARY, OWNER, TEAM, Context

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

SECRET = "gitea-token-8f3c2a91d7e4b605"
OTHER_SECRET = "someone-elses-token-5d1e77aa"
PROGRAM = b"""#!/bin/sh
case "$1" in
  repo) echo "repos: acme/site"; echo "token=$TEA_TOKEN"; cat "$HOME/.config/tea/token"; echo "args=$*" ;;
  peek) if cat "$2" 2>/dev/null; then echo READ-OK; else echo READ-DENIED; fi ;;
  write) if echo x > "$2" 2>/dev/null; then echo WRITE-OK; else echo WRITE-DENIED; fi ;;
  *) echo "unknown $1" ;;
esac
"""
DIGEST = hashlib.sha256(PROGRAM).hexdigest()
SOURCE = "https://dl.gitea.com/tea/0.9.2/tea-0.9.2-linux-amd64"


def tea(**updates) -> dict:
    cli = {
        "tool_id": "cli:tea",
        "display_name": "Gitea CLI (tea)",
        "description": "Lists and changes the team's Gitea repositories and issues.",
        "installed_cli": {
            "binary_path": "team/tools/tea/tea",
            "sha256": DIGEST,
            "version": "0.9.2",
            "source_url": SOURCE,
            "commands": {"repo.list": "read", "peek": "read", "write": "read", "issue.create": "write", "repo.delete": "full"},
            "blocked_commands": {"login.add": "stores credentials outside the broker"},
            "blocked_flags": ["--token"],
            "allowed_hosts": ["gitea.example.com"],
        },
        "credential_injections": [
            {"placement": "env", "name": "TEA_TOKEN", "field": "token"},
            {"placement": "file", "name": ".config/tea/token", "field": "token"},
        ],
        "link_owners": ["person"],
        "setup": {"external_steps": [{
            "step_id": "create_token",
            "title": "Create a Gitea token",
            "instructions": "In Gitea, open Settings, then Applications, and generate a token that can read repositories.",
            "url": "https://gitea.example.com/user/settings/applications",
        }]},
    }
    cli.update(updates)
    return cli


@dataclass
class Served:
    port: int
    files: dict[str, bytes]

    def download(self, url: str) -> bytes:
        """The https download, served over plain HTTP by this test's local server."""

        local = url.replace("https://dl.gitea.com", f"http://127.0.0.1:{self.port}")
        with urllib.request.urlopen(local, timeout=5) as response:
            return response.read()


@pytest.fixture
def served():
    files: dict[str, bytes] = {"/tea/0.9.2/tea-0.9.2-linux-amd64": PROGRAM}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            body = files.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *args):  # noqa: D401 - quiet
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Served(server.server_address[1], files)
    server.shutdown()


def propose(context: Context, cli: dict) -> dict:
    return json.loads(asyncio.run(propose_access_change(
        title="Add the Gitea CLI",
        reason="List and triage the team's Gitea issues.",
        tool_context=context,
        clis=[cli],
        # TeamArchitect keeps it and hands it on to WebResearchAgent's tasks ("access follows the work").
        grants=[{"agent_type": "TeamArchitect", "target": {"kind": "tool", "id": "cli:tea"}, "level": "read",
                 "delegation_depth": 1}],
    )))


def approve(world_tuple, cli: dict, served: Served):
    lead, requests, _gate, _instance, service = world_tuple
    set_agent_ref(lead)
    context = Context("tooluse-tea")
    with pytest.raises(InterruptException):
        propose(context, cli)
    [paused] = context.raised
    request = service.store.get_request(paused["access_request_id"])
    requests().appliers[ACCESS_PROPOSAL_KIND].download = served.download
    return request, lambda: requests().decide(request.request_id, OWNER, AccessReviewChoice.APPROVE, kept_item_ids=("grant-0",))


def program_path(instance: Path) -> Path:
    return workspaces_root(instance) / "team" / "tools" / "tea" / "tea"


def person_link(service, person_id: str = OWNER.person_id, key: str = "gitea_me") -> AccountLinkV1:
    return service.store.create_link(AccountLinkV1(
        link_id=f"account_link_tea_{person_id}",
        team_id=TEAM,
        tool_id="cli:tea",
        owner_kind=LinkOwnerKind.PERSON,
        owner_id=person_id,
        credential_key=key,
        created_by=SubjectRefV1(kind=SubjectKind.USER, id=person_id),
        created_at_utc=datetime.now(timezone.utc),
    ))


def worker_call(gate, argv: list[str]):
    return gate.check_broker_action(
        BrokerCaller(CALLER_MANAGER), "tea", "exec", {"argv": argv},
        agent_context(OWNER.person_id, agent_id="agent-web", named_agent_type="WebResearchAgent", roles=("owner",),
                      task_id="task-web"),
    )


# -- the review card ---------------------------------------------------------------------


def test_the_tea_card_says_what_runs_whose_account_and_each_level(tmp_path: Path) -> None:
    proposal = AccessProposalV1(title="Add the Gitea CLI", reason="Triage issues.", clis=(ProposedCliToolV1(**tea()),))

    compiled = ProposalCompiler(catalog=CatalogSource(tmp_path).current(), team_id=TEAM, holds=lambda *_: True, account_sharing=_no_sharing_choices()).compile(
        proposal, person=None, task_id=None
    )

    rows = {row.item_id: row for row in compiled.rows}
    assert set(rows["cli-0"].flags) == {"new_program", "download_host"}
    assert f"team/tools/tea/tea with SHA-256 {DIGEST}" in rows["cli-0"].text
    assert "version 0.9.2 downloaded from dl.gitea.com" in rows["cli-0"].text
    assert "the environment variable TEA_TOKEN and the file ~/.config/tea/token, never on its command line" in rows["cli-0"].text
    assert "each person's own account" in rows["cli-0"].text and "not enforced" in rows["cli-0"].text
    assert rows["cli-0-commands"].flags == ("full_access",)
    assert "Full (needs full access; asked for each time otherwise): repo delete" in rows["cli-0-commands"].text
    assert "Any other command needs full." in rows["cli-0-commands"].text and "--token" in rows["cli-0-commands"].text
    assert "Create a Gitea token" in rows["cli-0-step-1"].text
    assert proposal_summary(compiled.rows).startswith("Access proposal: add 1 new tool")


def test_a_cli_named_like_a_built_in_provider_is_refused(tmp_path: Path) -> None:
    clash = tea(tool_id="cli:github", installed_cli={**tea()["installed_cli"], "binary_path": "team/tools/github/gh"})
    proposal = AccessProposalV1(title="x", reason="y", clis=(ProposedCliToolV1(**clash),))

    with pytest.raises(ProposalRefused, match="already"):
        ProposalCompiler(catalog=CatalogSource(tmp_path).current(), team_id=TEAM, holds=lambda *_: True, account_sharing=_no_sharing_choices()).compile(
            proposal, person=None, task_id=None
        )


# -- approving -------------------------------------------------------------------------


def test_approving_downloads_checks_and_installs_the_program(world, served) -> None:  # noqa: F811
    _lead, _requests, gate, instance, service = world
    request, decide = approve(world, tea(), served)
    rows = {row.item_id: row for row in request.change.items}
    assert "new_program" in rows["cli-0"].flags

    decided = decide()

    assert decided.applied_at_utc is not None
    assert program_path(instance).read_bytes() == PROGRAM and os.access(program_path(instance), os.X_OK)
    assert (instance / "control" / "access" / "tools" / "proposed-cli-tea.yaml").is_file()
    assert "tea" in gate.catalog.cli_policies and gate.catalog.tools["cli:tea"].link_owners == ("person",)


def test_a_download_that_does_not_match_applies_nothing(world, served) -> None:  # noqa: F811
    _lead, _requests, _gate, instance, service = world
    served.files["/tea/0.9.2/tea-0.9.2-linux-amd64"] = PROGRAM + b"# tampered\n"
    request, decide = approve(world, tea(), served)

    with pytest.raises(Exception, match="not the approved one"):
        decide()

    assert not program_path(instance).exists()
    assert not (instance / "control" / "access" / "tools" / "proposed-cli-tea.yaml").exists()
    assert service.store.get_request(request.request_id).status is AccessRequestStatus.WAITING


def test_a_program_agents_built_into_the_workspace_is_used_when_it_matches(world, served) -> None:  # noqa: F811
    _lead, _requests, _gate, instance, _service = world
    built = tea(installed_cli={**tea()["installed_cli"], "source_url": None})
    program_path(instance).parent.mkdir(parents=True)
    program_path(instance).write_bytes(PROGRAM)

    _request, decide = approve(world, built, served)

    assert decide().applied_at_utc is not None


@pytest.mark.parametrize("problem", ["changed", "link"])
def test_a_built_program_that_changed_or_is_a_link_is_refused(world, served, tmp_path: Path, problem: str) -> None:  # noqa: F811
    _lead, _requests, _gate, instance, _service = world
    built = tea(installed_cli={**tea()["installed_cli"], "source_url": None})
    program_path(instance).parent.mkdir(parents=True)
    if problem == "changed":
        program_path(instance).write_bytes(PROGRAM + b"# changed\n")
    else:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.write_bytes(PROGRAM)
        program_path(instance).symlink_to(elsewhere)
    _request, decide = approve(world, built, served)

    with pytest.raises(Exception, match="not the reviewed one|cannot be used"):
        decide()

    assert not (instance / "control" / "access" / "tools" / "proposed-cli-tea.yaml").exists()


def test_downloads_are_https_only() -> None:
    with pytest.raises(ProgramRefused, match="only over https"):
        fetch_https("http://dl.gitea.com/tea")


def _trickling_server(seconds_per_byte: float, body: bytes):
    """A server that sends its body one byte at a time: every read gets data, so no socket timeout trips."""

    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        conn, _address = listener.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n")
        try:
            for byte in body:
                time.sleep(seconds_per_byte)
                conn.sendall(bytes([byte]))
        except OSError:
            pass  # the reader stopped
        conn.close()
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{listener.getsockname()[1]}/tea"


def test_a_download_that_trickles_in_stops_at_its_total_deadline() -> None:
    # The socket timeout bounds each read only (2026-10-17 review): this server
    # never pauses longer than 0.2 seconds, so only a total deadline stops it.
    url = _trickling_server(0.2, b"x" * 40)
    with urllib.request.urlopen(url, timeout=1) as response:
        with pytest.raises(ProgramRefused, match="stopped after 1 seconds in total because it was too slow"):
            read_within_deadline(response, host="dl.gitea.com", deadline_seconds=1)

    quick = _trickling_server(0.0, b"tea program")
    with urllib.request.urlopen(quick, timeout=1) as response:
        assert read_within_deadline(response, host="dl.gitea.com", deadline_seconds=5) == b"tea program"


def test_each_download_runs_between_two_claim_renewals(world, served, monkeypatch) -> None:  # noqa: F811
    _lead, requests, _gate, instance, _service = world
    request, decide = approve(world, tea(), served)
    applier = requests().appliers[ACCESS_PROPOSAL_KIND]
    fetch = applier.download
    steps: list[str] = []
    renew = AccessRequests.renew_claim

    def renew_claim(self, claim):  # noqa: ANN001 - patched method
        steps.append("renew")
        return renew(self, claim)

    def download(url: str) -> bytes:
        steps.append("download")
        return fetch(url)

    monkeypatch.setattr(AccessRequests, "renew_claim", renew_claim)
    applier.download = download

    decide()

    assert steps == ["renew", "download", "renew"]
    assert program_path(instance).read_bytes() == PROGRAM


def test_a_decision_that_loses_its_claim_during_a_download_installs_nothing(world, served) -> None:  # noqa: F811
    _lead, requests, _gate, instance, service = world
    request, decide = approve(world, tea(), served)
    applier = requests().appliers[ACCESS_PROPOSAL_KIND]
    fetch = applier.download

    def taken_over_meanwhile(url: str) -> bytes:
        data = fetch(url)
        # Meanwhile the claim ran out and another decision took the request.
        with service.store._connect() as conn:
            conn.execute(
                "UPDATE access_request_claims SET claim_id = 'decision_other' WHERE request_id = ?",
                (request.request_id,),
            )
            conn.commit()
        return data

    applier.download = taken_over_meanwhile
    with pytest.raises(HandDownRefused, match="taken over"):
        decide()

    assert not program_path(instance).exists(), "a decision that lost the request writes nothing"
    assert not (instance / "control" / "access" / "tools" / "proposed-cli-tea.yaml").exists()
    assert service.store.get_request(request.request_id).status is AccessRequestStatus.WAITING


# -- deciding and checking commands -------------------------------------------------------


def test_the_gate_decides_by_command_level_and_uses_only_allowed_owners(world, served) -> None:  # noqa: F811
    _lead, _requests, gate, _instance, service = world
    _request, decide = approve(world, tea(), served)
    decide()

    missing = worker_call(gate, ["repo", "list"])
    assert missing.reason_code == REASON_ACCOUNT_LINK_MISSING, "a person-only tool never falls back to a team link"
    person_link(service)
    allowed = worker_call(gate, ["repo", "list"])
    assert allowed.allowed and allowed.resolution.level.value == "read"
    assert allowed.account_link.owner_kind is LinkOwnerKind.PERSON
    write = worker_call(gate, ["issue", "create", "--title", "x"])
    assert not write.allowed and write.resolution.level.value == "write"
    unlisted = worker_call(gate, ["admin", "user", "delete"])
    assert not unlisted.allowed and unlisted.resolution.level.value == "full"


def test_blocked_commands_and_flags_are_refused(world, served) -> None:  # noqa: F811
    _lead, _requests, gate, _instance, _service = world
    _request, decide = approve(world, tea(), served)
    decide()
    policy = gate.catalog.cli_policies["tea"]

    with pytest.raises(CommandRefused, match="stores credentials outside the broker"):
        check_installed_command_line(policy, ["login", "add"])
    with pytest.raises(CommandRefused, match="--token"):
        check_installed_command_line(policy, ["repo", "list", "--token", "x"])
    assert check_installed_command_line(policy, ["repo", "list"]).argv == ["repo", "list"]


# -- running --------------------------------------------------------------------------------


@pytest.fixture
def installed(world, served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    """tea approved and linked for the person; credentials resolved from a stand-in store."""

    _lead, _requests, gate, instance, service = world
    _request, decide = approve(world, tea(), served)
    decide()
    link = person_link(service)
    broker_dir = tmp_path / "broker_credentials"
    (broker_dir / "users" / "user-2").mkdir(parents=True)
    other = broker_dir / "users" / "user-2" / "gitea.json"
    other.write_text(json.dumps({"token": OTHER_SECRET}), encoding="utf-8")
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(instance))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(broker_dir))
    values = {("gitea_me", "token"): SECRET}
    monkeypatch.setattr(
        broker_clis,
        "resolve_secret_field",
        lambda key, field, scope: SimpleNamespace(value=values[(key, field)], secret=True),
    )
    calls: list = []
    real_run = broker_clis.subprocess.run

    def spy(argv, **kwargs):
        calls.append((list(argv), dict(kwargs["env"])))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(broker_clis.subprocess, "run", spy)
    provider = installed_cli_provider(gate.catalog, "tea")
    context = ToolContext(canonical_user_id=OWNER.person_id, team_id=TEAM, account_link=link)

    def run(argv: list[str]):
        return provider.execute(provider.validate("exec", {"argv": argv}), context)

    return SimpleNamespace(run=run, calls=calls, instance=instance, other=other, gate=gate)


def test_a_run_gets_the_credential_in_env_and_file_never_in_argv_or_output(installed) -> None:
    result = installed.run(["repo", "list"])

    assert result.status == "success", result
    out = result.content["stdout"]
    assert "repos: acme/site" in out and "token=[REDACTED]" in out
    assert SECRET not in json.dumps(result.content)
    [(argv, env)] = installed.calls
    assert SECRET not in " ".join(argv) and env["TEA_TOKEN"] == SECRET
    assert "code_sandbox_exec" in " ".join(argv), "the program runs inside the Landlock launcher"
    assert result.content["level"] == "read" and result.content["command"] == "repo.list"


def test_the_program_cannot_read_another_persons_credential_or_the_brokers_files(installed) -> None:
    control = installed.instance / "control" / "access" / "tools" / "proposed-cli-tea.yaml"

    other = installed.run(["peek", str(installed.other)])
    broker = installed.run(["peek", str(control)])
    workspace = installed.run(["write", str(installed.instance / "workspaces" / "team" / "tools" / "tea" / "tea")])

    assert "READ-DENIED" in other.content["stdout"] and OTHER_SECRET not in json.dumps(other.content)
    assert "READ-DENIED" in broker.content["stdout"]
    assert "WRITE-DENIED" in workspace.content["stdout"], "it writes only its private run folder"


def test_a_program_changed_after_approval_is_refused_and_logged(installed, caplog) -> None:
    program_path(installed.instance).write_bytes(PROGRAM + b"echo stolen\n")

    with caplog.at_level(logging.ERROR, logger="secure_tool_broker.installed_clis"):
        result = installed.run(["repo", "list"])

    assert result.status == "error" and "not the approved one" in result.error
    assert "Propose the update with its new digest" in result.error
    assert "event=installed_cli_program_refused tool_id=cli:tea" in caplog.text
    assert f"approved_sha256={DIGEST}" in caplog.text and "actual_sha256=" in caplog.text
    assert installed.calls == [], "nothing ran"


def test_a_program_replaced_by_a_link_is_refused(installed, tmp_path: Path) -> None:
    path = program_path(installed.instance)
    target = tmp_path / "same-program"
    target.write_bytes(PROGRAM)
    path.unlink()
    path.symlink_to(target)

    result = installed.run(["repo", "list"])

    assert result.status == "error" and "cannot be used" in result.error
    assert installed.calls == []


def test_without_landlock_the_cli_is_refused_with_a_plain_message(installed, monkeypatch, caplog) -> None:
    monkeypatch.setattr(code_sandbox, "landlock_abi", lambda: 0)

    with caplog.at_level(logging.ERROR, logger="secure_tool_broker.installed_clis"):
        result = installed.run(["repo", "list"])

    assert result.status == "error" and result.content["error_code"] == "installed_cli_sandbox_unavailable"
    assert "runs only inside the sandbox" in result.error
    assert "event=installed_cli_sandbox_unavailable tool_id=cli:tea" in caplog.text
    assert installed.calls == []


# -- set_up_tools -----------------------------------------------------------------------------


def test_set_up_tools_plans_the_clis_setup_step_and_account_link(world, served) -> None:  # noqa: F811
    _lead, _requests, gate, _instance, _service = world
    _request, decide = approve(world, tea(), served)
    decide()
    report = {"tool_id": "cli:tea", "next_steps": [{
        "step": "account_link", "who": "person", "fix_with": "setup_page", "kind": "account_link", "owner": "person",
    }]}

    plan = plan_run([report], gate.catalog, is_admin=False)

    assert [(piece.kind, piece.step_id) for piece in plan.pieces] == [("external_step", "create_token"), ("account_link", None)]
    assert plan.pieces[1].depends_on == (plan.pieces[0].piece_id,)


def _no_sharing_choices():
    """A team that has made no sharing choices: every service has its starting sharing."""

    from access_control.account_sharing import TeamAccountSharing

    class _NoChoices:
        def account_sharing(self, team_id, subject):
            return None

        def team_account_sharing(self, team_id):
            return []

        def set_account_sharing(self, record):
            raise AssertionError("compiling a proposal records no choice")

    return TeamAccountSharing(_NoChoices(), TEAM)
