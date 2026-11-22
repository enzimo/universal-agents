"""Unit tests for proposed web API tool change diffs and ProposalCompiler validation (Checkpoint 6b)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from access_catalog.catalog import CatalogError, load_catalog
from access_catalog.instance_tools import proposed_tool_path, read_proposed_tool_file, tool_definition, write_instance_tool
from access_control.account_sharing import TeamAccountSharing
from access_control.proposals import (
    ProposalCompiler,
    ProposalRefused,
    proposal_summary,
    with_tool_change_baselines,
)
from access_control.tool_change_diffs import tool_change_rows
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessProposalV1,
    AccountLinkV1,
    AccountSharing,
    AccountSharingV1,
    CredentialInjectionV1,
    HttpRouteV1,
    InjectionPlacement,
    LinkOwnerKind,
    LinkPurpose,
    OwnerScope,
    ProposedAccountLinkV1,
    ProposedGrantV1,
    ProposedHttpToolChangeV1,
    ProposedHttpToolV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.tool_setup_spec_v1 import ExternalSetupStepV1, ToolSetupSpecV1
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

NOW = datetime(2026, 11, 22, 1, 0, tzinfo=timezone.utc)
TEAM = "team-test"
ACTOR = SubjectRefV1(kind=SubjectKind.USER, id="admin-1")


def _gitea_tool(**overrides) -> ProposedHttpToolV1:
    base = dict(
        tool_id="http:gitea",
        display_name="Gitea",
        description="Self-hosted Git forge.",
        hosts=("gitea.example.com",),
        routes=(
            HttpRouteV1(name="repos.list", method="GET", path="/api/v1/user/repos", level=AccessLevel.READ),
            HttpRouteV1(name="issues.create", method="POST", path="/api/v1/repos/{owner}/{repo}/issues", level=AccessLevel.WRITE),
        ),
        uses_credential=True,
        credential_injections=(
            CredentialInjectionV1(
                placement=InjectionPlacement.HEADER,
                name="Authorization",
                field="token",
                template="token {value}",
            ),
        ),
    )
    base.update(overrides)
    return ProposedHttpToolV1(**base)


def _gitea_change(**overrides) -> ProposedHttpToolChangeV1:
    tool = _gitea_tool()
    payload = tool.model_dump(mode="python")
    payload.update(overrides)
    return ProposedHttpToolChangeV1(**payload)


def _link(
    tool_id: str = "http:gitea",
    *,
    owner_kind: LinkOwnerKind = LinkOwnerKind.TEAM,
    owner_id: str = TEAM,
    purpose: LinkPurpose = LinkPurpose.ACCOUNT,
    injections: tuple[CredentialInjectionV1, ...] = (
        CredentialInjectionV1(
            placement=InjectionPlacement.HEADER,
            name="Authorization",
            field="token",
            template="token {value}",
        ),
    ),
    link_id: str = "link-1",
) -> AccountLinkV1:
    return AccountLinkV1(
        link_id=link_id,
        team_id=TEAM,
        tool_id=tool_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        purpose=purpose,
        credential_key=f"cred_{link_id}",
        injections=injections,
        created_by=ACTOR,
        created_at_utc=NOW,
    )


def _compiler(tmp_path: Path, instance: Path, *, links: list[AccountLinkV1] | None = None) -> tuple[ProposalCompiler, AccessRepository]:
    store = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )
    for item in links or ():
        store.create_link(item)
    catalog = load_catalog(instance)
    compiler = ProposalCompiler(
        catalog=catalog,
        team_id=TEAM,
        holds=lambda _person, _target, _level: True,
        account_sharing=TeamAccountSharing(store, TEAM),
        instance_root=instance,
        links_for_tool=lambda tool_id: [
            link for link in store.list_links(TEAM, purpose=None) if link.tool_id == tool_id
        ],
    )
    return compiler, store


def _compile_change(
    tmp_path: Path,
    instance: Path,
    change: ProposedHttpToolChangeV1,
    *,
    links: list[AccountLinkV1] | None = None,
    account_links: tuple[ProposedAccountLinkV1, ...] = (),
    grants: tuple[ProposedGrantV1, ...] = (),
):
    compiler, store = _compiler(tmp_path, instance, links=links)
    proposal = AccessProposalV1(
        title="Update Gitea",
        reason="Add gate and update endpoints.",
        tool_changes=(change,),
        account_links=account_links,
        grants=grants,
    )
    pinned = with_tool_change_baselines(proposal, instance_root=instance, catalog=compiler.catalog)
    return compiler.compile(pinned, person=None, task_id="task-1"), store


def test_tool_change_hosts_added_flags_surviving_credentials_and_private_addresses(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    write_instance_tool(instance, _gitea_tool(
        gate_injections=(
            CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-CF-Secret", field="cf_secret"),
        ),
    ))
    gate_link = _link(
        link_id="gate-1",
        purpose=LinkPurpose.GATE,
        injections=(CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-CF-Secret", field="cf_secret"),),
    )
    links = [
        _link(link_id="team-1", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM),
        _link(link_id="alice-1", owner_kind=LinkOwnerKind.PERSON, owner_id="alice"),
        _link(link_id="bob-1", owner_kind=LinkOwnerKind.PERSON, owner_id="bob"),
        gate_link,
    ]
    change = _gitea_change(
        hosts=("gitea.example.com", "gitea-mirror.example.com", "10.0.0.5"),
        gate_injections=(
            CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-CF-Secret", field="cf_secret"),
        ),
    )
    compiled, _ = _compile_change(tmp_path, instance, change, links=links)
    rows = {row.item_id: row for row in compiled.rows}

    assert "change-0" in rows
    hosts_added = rows["change-0-hosts-added"]
    assert set(hosts_added.flags) == {"new_host", "private_address", "credential_to_new_host"}
    assert "gitea-mirror.example.com, 10.0.0.5" in hosts_added.text
    assert "the team's account" in hosts_added.text
    assert "2 people's accounts" in hosts_added.text
    assert "the team's gate credential" in hosts_added.text
    assert "without linking again" in hosts_added.text
    assert "change-0-relink" not in rows

    summary = proposal_summary(compiled.rows)
    assert summary.startswith("Access proposal: change 1 tool (includes ")
    assert "a new host" in summary
    assert "stored credentials sent to a new host" in summary
    assert "a private address" in summary


def test_tool_change_hosts_added_with_changed_account_injections_only_warns_surviving_gate(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    gate_inj = (CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate", field="gate_key"),)
    write_instance_tool(instance, _gitea_tool(gate_injections=gate_inj))
    links = [
        _link(link_id="team-acct", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM),
        _link(link_id="person-acct", owner_kind=LinkOwnerKind.PERSON, owner_id="alice"),
        _link(link_id="team-gate", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, purpose=LinkPurpose.GATE, injections=gate_inj),
    ]
    change = _gitea_change(
        hosts=("gitea.example.com", "api.gitea.example.com"),
        credential_injections=(
            CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-API-Key", field="api_key"),
        ),
        gate_injections=gate_inj,
    )
    compiled, _ = _compile_change(tmp_path, instance, change, links=links)
    rows = {row.item_id: row for row in compiled.rows}

    # Account links become outdated so only the surviving gate link triggers credential_to_new_host.
    hosts_added = rows["change-0-hosts-added"]
    assert "credential_to_new_host" in hosts_added.flags
    assert "the team's gate credential" in hosts_added.text
    assert "the team's account" not in hosts_added.text
    assert "1 person's account" not in hosts_added.text

    # Re-link row names both outdated account links with exact /link commands.
    relink = rows["change-0-relink"]
    assert "the team's account (/link http:gitea team)" in relink.text
    assert "1 person's account (/link http:gitea)" in relink.text
    assert "/link http:gitea gate" not in relink.text


def test_tool_change_hosts_added_without_surviving_links_omits_credential_to_new_host(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    write_instance_tool(instance, _gitea_tool())
    change = _gitea_change(hosts=("gitea.example.com", "gitea2.example.com"))
    compiled, _ = _compile_change(tmp_path, instance, change, links=[])
    rows = {row.item_id: row for row in compiled.rows}

    assert rows["change-0-hosts-added"].flags == ("new_host",)
    assert "without linking again" not in rows["change-0-hosts-added"].text


def test_tool_change_reports_removed_hosts_routes_credentials_gate_setup_and_details(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    old_step = ExternalSetupStepV1(
        step_id="old_step",
        title="Old step",
        instructions="Do the old thing.",
    )
    kept_step = ExternalSetupStepV1(
        step_id="create_token",
        title="Create token",
        instructions="Create a personal access token in Gitea.",
    )
    write_instance_tool(
        instance,
        _gitea_tool(
            hosts=("gitea.example.com", "legacy.example.com"),
            setup=ToolSetupSpecV1(external_steps=(old_step, kept_step), prerequisites=("cli:github",)),
        ),
    )
    updated_step = ExternalSetupStepV1(
        step_id="create_token",
        title="Create scoped token",
        instructions="Create a token with repository scope.",
        url="https://gitea.example.com/user/settings/applications",
        verify="Token appears in the list.",
        who="administrator",
    )
    change = _gitea_change(
        display_name="Gitea Enterprise",
        description="Updated description.",
        hosts=("gitea.example.com",),
        routes=(
            HttpRouteV1(name="issues.create", method="POST", path="/api/v1/repos/{owner}/{repo}/issues", level=AccessLevel.FULL),
            HttpRouteV1(name="pulls.list", method="GET", path="/api/v1/repos/{owner}/{repo}/pulls", level=AccessLevel.READ),
        ),
        gate_injections=(
            CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-CF-Access", field="cf_token"),
        ),
        setup=ToolSetupSpecV1(external_steps=(updated_step,), prerequisites=("http:exa",)),
    )
    compiled, _ = _compile_change(
        tmp_path,
        instance,
        change,
        account_links=(
            ProposedAccountLinkV1(
                tool_id="http:gitea",
                owner="team",
                purpose="Pass the Cloudflare gate.",
                link_purpose=LinkPurpose.GATE,
            ),
        ),
    )
    rows = {row.item_id: row for row in compiled.rows}

    assert "legacy.example.com" in rows["change-0-hosts-removed"].text
    assert "added pulls.list" in rows["change-0-routes"].text
    assert "removed repos.list" in rows["change-0-routes"].text
    assert "re-levelled issues.create" in rows["change-0-routes"].text
    assert "Team gate credential added" in rows["change-0-gate-credentials"].text
    assert "/link http:gitea gate" in rows["change-0-gate-credentials"].text
    assert "No existing links" in rows["change-0-relink"].text
    assert "added prerequisites: http:exa" in rows["change-0-setup"].text
    assert "removed prerequisites: cli:github" in rows["change-0-setup"].text
    assert "removed steps: old_step" in rows["change-0-setup"].text
    assert rows["change-0-step-1"].flags == ("setup_steps",)
    assert "Create scoped token" in rows["change-0-step-1"].text
    assert "display name 'Gitea' -> 'Gitea Enterprise'" in rows["change-0-details"].text
    assert "description updated" in rows["change-0-details"].text
    assert "team's gate credential Gitea Enterprise sends" in rows["account-0"].text


def test_tool_change_detects_route_reordering(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    r1 = HttpRouteV1(name="admin.route", method="GET", path="/api/admin", level=AccessLevel.FULL)
    r2 = HttpRouteV1(name="any.route", method="GET", path="/api/{section}", level=AccessLevel.READ)
    write_instance_tool(instance, _gitea_tool(routes=(r1, r2)))
    change = _gitea_change(routes=(r2, r1))
    compiled, _ = _compile_change(tmp_path, instance, change)
    rows = {row.item_id: row for row in compiled.rows}
    assert "reordered routes" in rows["change-0-routes"].text


def test_tool_change_refuses_unchanged_and_canonical_only_injection_diff(tmp_path: Path, caplog) -> None:
    instance = tmp_path / "instance"
    write_instance_tool(
        instance,
        _gitea_tool(
            credential_injections=(
                CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-First", field="a"),
                CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Second", field="b"),
            )
        ),
    )
    # Same injections reordered with different header case and explicit default template: canonically identical.
    canonical_same = _gitea_change(
        credential_injections=(
            CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="x-second", field="b", template="{value}"),
            CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="x-first", field="a"),
        )
    )
    caplog.set_level("WARNING")
    with pytest.raises(ProposalRefused, match="makes no changes"):
        _compile_change(tmp_path, instance, canonical_same)
    assert "event=proposal_tool_change_refused reason=no_changes" in caplog.text


def test_tool_change_refuses_builtin_operator_unknown_and_changed_baseline(tmp_path: Path, caplog) -> None:
    instance = tmp_path / "instance"
    tools_dir = instance / "control" / "access" / "tools"
    tools_dir.mkdir(parents=True)
    # Operator hand-written file (not proposed-*.yaml).
    (tools_dir / "operator-forge.yaml").write_text(
        "tools:\n"
        "  - tool_id: http:operator_forge\n"
        "    display_name: Operator Forge\n"
        "    hosts: [forge.example.com]\n"
        "    account: none\n",
        encoding="utf-8",
    )
    write_instance_tool(instance, _gitea_tool())
    compiler, _ = _compiler(tmp_path, instance)
    caplog.set_level("WARNING")

    # 1. Built-in tool (http:github) refused.
    builtin_proposal = AccessProposalV1(
        title="Change GitHub",
        reason="Try to edit built-in",
        tool_changes=(
            ProposedHttpToolChangeV1(
                tool_id="http:github",
                display_name="GitHub",
                hosts=("api.github.com",),
                uses_credential=False,
            ),
        ),
    )
    with pytest.raises(ProposalRefused, match="built-in tool"):
        with_tool_change_baselines(builtin_proposal, instance_root=instance, catalog=compiler.catalog)
    assert "reason=builtin_tool" in caplog.text

    # 2. Operator hand-written file refused.
    operator_proposal = AccessProposalV1(
        title="Change operator forge",
        reason="Try to edit hand-written file",
        tool_changes=(
            ProposedHttpToolChangeV1(
                tool_id="http:operator_forge",
                display_name="Operator Forge",
                hosts=("forge.example.com", "forge2.example.com"),
                uses_credential=False,
            ),
        ),
    )
    with pytest.raises(ProposalRefused, match="operator-managed catalog file"):
        with_tool_change_baselines(operator_proposal, instance_root=instance, catalog=compiler.catalog)
    assert "reason=operator_managed_file" in caplog.text

    # 3. Unknown tool refused.
    unknown_proposal = AccessProposalV1(
        title="Change missing tool",
        reason="Does not exist",
        tool_changes=(
            ProposedHttpToolChangeV1(
                tool_id="http:missing",
                display_name="Missing",
                hosts=("missing.example.com",),
                uses_credential=False,
            ),
        ),
    )
    with pytest.raises(ProposalRefused, match="not in the tool catalog"):
        with_tool_change_baselines(unknown_proposal, instance_root=instance, catalog=compiler.catalog)
    assert "reason=unknown_tool" in caplog.text

    # 4. Caller-supplied previous_definition_digest is overwritten by with_tool_change_baselines.
    spoofed = AccessProposalV1(
        title="Add gate",
        reason="Add gate header",
        tool_changes=(
            _gitea_change(
                hosts=("gitea.example.com", "gitea2.example.com"),
                previous_definition_digest="0" * 64,
            ),
        ),
    )
    pinned = with_tool_change_baselines(spoofed, instance_root=instance, catalog=compiler.catalog)
    assert pinned.tool_changes[0].previous_definition_digest != "0" * 64

    # 5. Missing baseline digest refused by compiler.
    with pytest.raises(ProposalRefused, match="missing its baseline digest"):
        compiler.compile(
            AccessProposalV1(
                title="No baseline",
                reason="Missing baseline",
                tool_changes=(_gitea_change(hosts=("gitea.example.com", "gitea2.example.com")),),
            ),
            person=None,
            task_id="task-1",
        )

    # 6. File changed on disk after baseline pinned is refused by compiler.
    path = proposed_tool_path(instance, "http:gitea")
    path.write_text(path.read_text(encoding="utf-8") + "\n# external edit\n", encoding="utf-8")
    with pytest.raises(ProposalRefused, match="changed after the proposal was prepared"):
        compiler.compile(pinned, person=None, task_id="task-1")
    assert "reason=baseline_changed" in caplog.text


def test_tool_change_refuses_host_clash_and_allows_gate_link_even_when_private_or_no_account(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    write_instance_tool(instance, _gitea_tool(uses_credential=False, credential_injections=()))

    # Host clash with built-in http:github is refused.
    clash_change = _gitea_change(
        uses_credential=False,
        credential_injections=(),
        hosts=("gitea.example.com", "api.github.com"),
    )
    with pytest.raises(ProposalRefused, match="api.github.com"):
        _compile_change(tmp_path, instance, clash_change)

    # Gate link on a tool with account=none is accepted when tool_changes adds gate_injections.
    add_gate = _gitea_change(
        uses_credential=False,
        credential_injections=(),
        gate_injections=(
            CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-CF-Secret", field="cf_secret"),
        ),
    )
    compiled, store = _compile_change(
        tmp_path,
        instance,
        add_gate,
        account_links=(
            ProposedAccountLinkV1(
                tool_id="http:gitea",
                owner="team",
                purpose="Pass Cloudflare gate",
                link_purpose=LinkPurpose.GATE,
            ),
        ),
    )
    assert any(row.item_id == "account-0" and "gate credential" in row.text for row in compiled.rows)

    # Gate link is also accepted even when account sharing for the tool is PRIVATE.
    store.set_account_sharing(
        AccountSharingV1(
            team_id=TEAM,
            subject="http:gitea",
            sharing=AccountSharing.PRIVATE,
            changed_by_person_id=ACTOR.id,
            changed_at_utc=NOW,
        )
    )
    compiler = ProposalCompiler(
        catalog=load_catalog(instance),
        team_id=TEAM,
        holds=lambda _person, _target, _level: True,
        account_sharing=TeamAccountSharing(store, TEAM),
        instance_root=instance,
    )
    proposal = with_tool_change_baselines(
        AccessProposalV1(
            title="Add gate on private tool",
            reason="Gate is team infrastructure",
            tool_changes=(add_gate,),
            account_links=(
                ProposedAccountLinkV1(
                    tool_id="http:gitea",
                    owner="team",
                    purpose="Pass Cloudflare gate",
                    link_purpose=LinkPurpose.GATE,
                ),
            ),
        ),
        instance_root=instance,
        catalog=compiler.catalog,
    )
    compiled_private = compiler.compile(proposal, person=None, task_id="task-1")
    assert any(row.item_id == "account-0" for row in compiled_private.rows)

    # Gate link for a tool without gate_injections is refused.
    no_gate_proposal = AccessProposalV1(
        title="Bad gate link",
        reason="Tool has no gate injections",
        account_links=(
            ProposedAccountLinkV1(
                tool_id="http:gitea",
                owner="team",
                purpose="No gate here",
                link_purpose=LinkPurpose.GATE,
            ),
        ),
    )
    with pytest.raises(ProposalRefused, match="does not use a team gate credential"):
        compiler.compile(no_gate_proposal, person=None, task_id="task-1")


def test_proposal_summary_counts_new_and_changed_tools_separately(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    write_instance_tool(instance, _gitea_tool())
    compiler, _ = _compiler(tmp_path, instance)
    proposal = with_tool_change_baselines(
        AccessProposalV1(
            title="Add one tool and change one tool",
            reason="Combined proposal",
            tools=(
                ProposedHttpToolV1(
                    tool_id="http:forge2",
                    display_name="Forge 2",
                    hosts=("forge2.example.com",),
                    uses_credential=False,
                    gate_injections=(
                        CredentialInjectionV1(placement=InjectionPlacement.QUERY, name="gate", field="gate_key"),
                    ),
                ),
            ),
            tool_changes=(_gitea_change(hosts=("gitea.example.com", "gitea3.example.com")),),
            grants=(
                ProposedGrantV1(
                    agent_type="TeamArchitect",
                    target=TargetRefV1(kind=TargetKind.TOOL, id="http:gitea"),
                    level=AccessLevel.WRITE,
                ),
            ),
        ),
        instance_root=instance,
        catalog=compiler.catalog,
    )
    compiled = compiler.compile(proposal, person=None, task_id="task-1")
    summary = proposal_summary(compiled.rows)
    assert "add 1 new tool, change 1 tool, give 1 access" in summary
    rows = {row.item_id: row for row in compiled.rows}
    assert "Uses a team gate credential (query gate (field gate_key))" in rows["tool-0"].text


def test_assess_link_impact_marks_unused_when_account_or_gate_removed(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    gate_inj = (CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate", field="gate_key"),)
    write_instance_tool(instance, _gitea_tool(gate_injections=gate_inj))
    links = [
        _link(link_id="acct-1", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM),
        _link(link_id="gate-1", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, purpose=LinkPurpose.GATE, injections=gate_inj),
    ]
    change = _gitea_change(uses_credential=False, credential_injections=(), gate_injections=())
    compiled, _ = _compile_change(tmp_path, instance, change, links=links)
    rows = {row.item_id: row for row in compiled.rows}
    assert "Account credentials removed" in rows["change-0-account-credentials"].text
    assert "Team gate credential removed" in rows["change-0-gate-credentials"].text
    assert "1 existing account link will no longer be used" in rows["change-0-relink"].text
    assert "existing team gate link will no longer be used" in rows["change-0-relink"].text


@pytest.mark.parametrize(
    ("extra_yaml", "expected_field"),
    [
        ("    link_owners: [team]\n", "link_owners"),
        ("    default_sharing: private\n", "default_sharing"),
        ("    owner_scope: administrators\n", "owner_scope"),
    ],
)
def test_tool_change_refuses_file_with_operator_only_settings(
    tmp_path: Path, caplog, extra_yaml: str, expected_field: str
) -> None:
    """C20: a proposed-*.yaml file edited by an operator with settings ProposedHttpToolV1 cannot express is refused."""

    instance = tmp_path / "instance"
    path = proposed_tool_path(instance, "http:gitea")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "tools:\n"
        "  - tool_id: http:gitea\n"
        "    display_name: Gitea\n"
        "    hosts: [gitea.example.com]\n"
        "    account: linked_credential\n"
        "    credential_injections:\n"
        "      - placement: header\n"
        "        name: Authorization\n"
        "        field: token\n"
        "        template: 'token {value}'\n"
        + extra_yaml,
        encoding="utf-8",
    )
    compiler, _ = _compiler(tmp_path, instance)
    proposal = AccessProposalV1(
        title="Add gate",
        reason="Add Cloudflare gate",
        tool_changes=(
            _gitea_change(
                gate_injections=(
                    CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate", field="gate"),
                )
            ),
        ),
    )
    caplog.set_level("WARNING")
    with pytest.raises(ProposalRefused, match=f"settings a tool change cannot keep.*{expected_field}"):
        with_tool_change_baselines(proposal, instance_root=instance, catalog=compiler.catalog)
    assert f"reason=unsupported_file_settings tool_id=http:gitea fields={expected_field}" in caplog.text


def test_diff_tool_definition_describes_link_owners_default_sharing_and_owner_scope() -> None:
    """C20 belt-and-suspenders: diff_tool_definition never silently drops policy fields."""

    base = tool_definition(_gitea_tool())
    custom = base.model_copy(
        update={
            "link_owners": ("team",),
            "owner_scope": OwnerScope.ADMINISTRATORS,
        }
    )
    diff, rows = tool_change_rows(0, current=custom, proposed=base)
    assert diff.has_changes and diff.policy_changed
    by_id = {row.item_id: row for row in rows}
    assert "account and ownership settings" in by_id["change-0"].text
    assert "whose account is used: team only -> person then team" in by_id["change-0-settings"].text
    assert "owner scope: administrators only -> team" in by_id["change-0-settings"].text

    private_default = base.model_copy(update={"default_sharing": AccountSharing.PRIVATE})
    diff2, rows2 = tool_change_rows(0, current=private_default, proposed=base)
    assert diff2.has_changes and diff2.default_sharing_changed
    by_id2 = {row.item_id: row for row in rows2}
    assert "default account sharing: private -> team default" in by_id2["change-0-settings"].text


def test_read_proposed_tool_file_refuses_tool_sets_and_reached_through(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    path = proposed_tool_path(instance, "http:bad")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "tools:\n"
        "  - tool_id: http:bad\n"
        "    display_name: Bad\n"
        "    hosts: [bad.example.com]\n"
        "    tool_sets: [standard]\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="cannot join tool sets"):
        read_proposed_tool_file(instance, "http:bad")
    path.write_text(
        "tools:\n"
        "  - tool_id: http:bad\n"
        "    display_name: Bad\n"
        "    hosts: [bad.example.com]\n"
        "    reached_through: fn:web\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="cannot name a function tool"):
        read_proposed_tool_file(instance, "http:bad")
