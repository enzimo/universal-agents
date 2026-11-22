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
    mirror_row = rows["change-0-host-added-0"]
    private_row = rows["change-0-host-added-1"]
    assert set(mirror_row.flags) == {"new_host", "credential_to_new_host"}
    assert "gitea-mirror.example.com" in mirror_row.text
    assert set(private_row.flags) == {"new_host", "private_address", "credential_to_new_host"}
    assert "10.0.0.5" in private_row.text
    for host_row in (mirror_row, private_row):
        assert "the team's account" in host_row.text
        assert "2 people's accounts" in host_row.text
        assert "the team's gate credential" in host_row.text
        assert "without linking again" in host_row.text
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
    hosts_added = rows["change-0-host-added-0"]
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

    assert rows["change-0-host-added-0"].flags == ("new_host",)
    assert "without linking again" not in rows["change-0-host-added-0"].text


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

    assert "legacy.example.com" in rows["change-0-host-removed-0"].text
    route_texts = "\n".join(row.text for item_id, row in rows.items() if item_id.startswith("change-0-route-"))
    assert "Route added" in route_texts and "pulls.list" in route_texts
    assert "Route removed" in route_texts and "repos.list" in route_texts
    assert "Route re-levelled" in route_texts and "issues.create" in route_texts
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
    assert "Routes reordered" in rows["change-0-routes-reordered"].text


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


@pytest.mark.parametrize("bad_purpose", ["gate", "account", " Gate ", "ACCOUNT"])
def test_account_link_refuses_literal_gate_or_account_as_purpose_text(
    tmp_path: Path, caplog, bad_purpose: str
) -> None:
    """C12/C22: typing 'gate' or 'account' into account_links[].purpose is refused with a pointer to link_purpose."""

    instance = tmp_path / "instance"
    write_instance_tool(instance, _gitea_tool())
    compiler, _ = _compiler(tmp_path, instance)
    proposal = AccessProposalV1(
        title="Link Gitea",
        reason="Need a gate link",
        account_links=(
            ProposedAccountLinkV1(tool_id="http:gitea", owner="team", purpose=bad_purpose),
        ),
    )
    caplog.set_level("WARNING")
    with pytest.raises(ProposalRefused, match='set link_purpose to "gate"'):
        compiler.compile(proposal, person=None, task_id="task-1")
    assert "event=proposal_account_link_purpose_ambiguous" in caplog.text


def _max_valid_host(prefix: str) -> str:
    """Build a valid 253-character ASCII host name with <=63-char labels."""

    label = "a" * 62
    candidate = f"{prefix}.{label}.{label}.{label}"
    tail_len = 253 - len(candidate) - 1
    assert 1 <= tail_len <= 63
    host = f"{candidate}.{'b' * tail_len}"
    assert len(host) == 253
    return host


def test_tool_change_never_truncates_many_long_hosts_or_60_routes(tmp_path: Path) -> None:
    """F2: long padding hosts or 60 routes never push a new host, credential warning, or full->read change past 1,600 chars."""

    instance = tmp_path / "instance"
    base_routes = tuple(
        HttpRouteV1(
            name=f"route_{idx:02d}_" + ("x" * 60),
            method="POST",
            path=f"/api/v1/resource/{idx:02d}/" + ("seg/" * 15),
            level=AccessLevel.FULL if idx == 59 else AccessLevel.WRITE,
        )
        for idx in range(60)
    )
    write_instance_tool(instance, _gitea_tool(routes=base_routes))
    links = [_link(link_id="team-1", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM)]

    # 1. Six 253-char hosts plus a private target host (>1,500 chars combined) each get their own un-cut row.
    padding_hosts = tuple(_max_valid_host(f"pad{i}") for i in range(6))
    host_change = _gitea_change(
        hosts=("gitea.example.com", *padding_hosts, "10.0.0.99"),
        routes=base_routes,
    )
    compiled_hosts, _ = _compile_change(tmp_path, instance, host_change, links=links)
    host_rows = [row for row in compiled_hosts.rows if row.item_id.startswith("change-0-host-added-")]
    assert len(host_rows) == 7
    assert all("…" not in row.text and len(row.text) <= 1600 for row in host_rows)
    target_row = next(row for row in host_rows if "10.0.0.99" in row.text)
    assert set(target_row.flags) == {"new_host", "private_address", "credential_to_new_host"}
    assert "Existing stored credentials (the team's account)" in target_row.text
    assert "without linking again" in target_row.text

    # 2. All 60 routes changed, with route #59 lowered from full -> read: every route gets its own row.
    lowered_routes = tuple(
        HttpRouteV1(
            name=route.name,
            method=route.method,
            path=route.path,
            level=AccessLevel.READ if idx == 59 else AccessLevel.FULL,
        )
        for idx, route in enumerate(base_routes)
    )
    route_change = _gitea_change(routes=lowered_routes)
    compiled_routes, _ = _compile_change(tmp_path, instance, route_change)
    route_rows = [row for row in compiled_routes.rows if row.item_id.startswith("change-0-route-")]
    assert len(route_rows) == 60
    assert all("…" not in row.text and len(row.text) <= 1600 for row in route_rows)
    last_route = route_rows[-1]
    assert base_routes[59].name in last_route.text
    assert "full -> read" in last_route.text

    # 3. Reordering 60 long-named routes splits cleanly across rows without cutting any route name.
    reordered_change = _gitea_change(routes=(*base_routes[1:], base_routes[0]))
    compiled_reorder, _ = _compile_change(tmp_path, instance, reordered_change)
    reorder_rows = [row for row in compiled_reorder.rows if row.item_id.startswith("change-0-routes-reordered")]
    assert len(reorder_rows) >= 2
    assert all("…" not in row.text and len(row.text) <= 1600 for row in reorder_rows)
    combined_reorder_text = "\n".join(row.text for row in reorder_rows)
    assert base_routes[0].name in combined_reorder_text
    assert base_routes[59].name in combined_reorder_text


def test_tool_change_splits_long_credential_templates_and_descriptions_and_refuses_card_overflow(
    tmp_path: Path,
) -> None:
    """F2: long templates/descriptions split without cutting, and >80 total review rows are refused cleanly."""

    instance = tmp_path / "instance"
    long_injs = tuple(
        CredentialInjectionV1(
            placement=InjectionPlacement.HEADER,
            name=f"X-Long-Header-{idx}",
            field=f"secret_{idx}",
            template=("prefix-" * 80) + "{value}",
        )
        for idx in range(4)
    )
    write_instance_tool(instance, _gitea_tool())
    big_desc = "D" * 1950 + "TAIL-MARKER"
    change = _gitea_change(
        display_name="Gitea Long",
        description=big_desc,
        credential_injections=long_injs,
    )
    compiled, _ = _compile_change(tmp_path, instance, change)
    assert all("…" not in row.text and len(row.text) <= 1600 for row in compiled.rows)
    joined = "\n".join(row.text for row in compiled.rows)
    for inj in long_injs:
        assert inj.name in joined
    assert "TAIL-MARKER" in joined

    # Overflowing the 80-row review card limit refuses cleanly instead of dropping rows.
    write_instance_tool(
        instance,
        ProposedHttpToolV1(
            tool_id="http:forge2",
            display_name="Forge 2",
            hosts=("forge2.example.com",),
            uses_credential=False,
        ),
    )
    routes_50 = tuple(
        HttpRouteV1(name=f"r{idx}", method="GET", path=f"/r/{idx}", level=AccessLevel.READ)
        for idx in range(50)
    )
    compiler, _ = _compiler(tmp_path, instance)
    overflow = with_tool_change_baselines(
        AccessProposalV1(
            title="Too many rows",
            reason="Two large tool changes",
            tool_changes=(
                _gitea_change(routes=routes_50),
                ProposedHttpToolChangeV1(
                    tool_id="http:forge2",
                    display_name="Forge 2",
                    hosts=("forge2.example.com",),
                    routes=routes_50,
                    uses_credential=False,
                ),
            ),
        ),
        instance_root=instance,
        catalog=compiler.catalog,
    )
    with pytest.raises(ProposalRefused, match="review card limit"):
        compiler.compile(overflow, person=None, task_id="task-1")


@pytest.mark.parametrize(
    "bad_host",
    [
        "g\u0456tea.example.com",  # Cyrillic small letter i
        "gitea.ex\u0430mple.com",  # Cyrillic small letter a
        "b\u00fccher.example.com",  # Unicode IDN (must be punycode)
        "gitea\u200b.example.com",  # zero-width space
        "gitea\u200d.example.com",  # zero-width joiner
        "\u202egitea.example.com",  # RTL override
        "a" * 64 + ".example.com",  # label > 63 chars
        ("a" * 60 + ".") * 4 + "b" * 10,  # total > 253 chars (254 chars)
        "-gitea.example.com",
        "gitea-.example.com",
        "_gitea.example.com",
        "gitea_.example.com",
        "gitea..example.com",
        ".gitea.example.com",
        "gitea.example.com..",
        ".",
        "*.example.com",
        "gitea.example.com:8443",
        "gitea.example.com/api",
        "gitea .example.com",
        "",
    ],
)
def test_host_validation_rejects_homoglyphs_zero_width_and_invalid_names(bad_host: str) -> None:
    """F3: ProposedHttpToolV1, ProposedHttpToolChangeV1, and ToolDefinitionV1 allow only exact ASCII host names."""

    from pydantic import ValidationError

    from custom_messages.contracts.access_v2 import ToolDefinitionV1, ToolKind

    with pytest.raises(ValidationError):
        ProposedHttpToolV1(
            tool_id="http:gitea",
            display_name="Gitea",
            hosts=(bad_host,),
            uses_credential=False,
        )
    with pytest.raises(ValidationError):
        ProposedHttpToolChangeV1(
            tool_id="http:gitea",
            display_name="Gitea",
            hosts=(bad_host,),
            uses_credential=False,
        )
    with pytest.raises(ValidationError):
        ToolDefinitionV1(
            tool_id="http:gitea",
            kind=ToolKind.HTTP,
            display_name="Gitea",
            hosts=(bad_host,),
        )


def test_host_validation_accepts_punycode_underscores_trailing_dot_and_boundary_lengths() -> None:
    """F3 + C23: valid ASCII punycode, underscores inside labels, single trailing dot, IPv4, localhost, and limits work."""

    valid_63_label = f"{'a' * 63}.example.com"
    valid_253 = _max_valid_host("ok")
    tool = ProposedHttpToolV1(
        tool_id="http:valid_hosts",
        display_name="Valid Hosts",
        hosts=(
            "LOCALHOST",
            "10.0.0.5",
            "gitea_web",
            "api.example.com.",
            "xn--bcher-kva.example",
            valid_63_label,
            valid_253,
        ),
        uses_credential=False,
    )
    assert tool.hosts == (
        "localhost",
        "10.0.0.5",
        "gitea_web",
        "api.example.com",
        "xn--bcher-kva.example",
        valid_63_label,
        valid_253,
    )


def test_tool_change_removing_or_renaming_blocked_route_adds_widened_block_row_and_flag(tmp_path: Path) -> None:
    """F5 (R4): removing or renaming a route with an active one-operation block warns with block_widened."""

    from datetime import timedelta

    from access_control.stale_blocks import active_operation_blocks_for_tool
    from custom_messages.contracts.access_v2 import GrantKind, GrantLifetime, GrantV1

    instance = tmp_path / "instance"
    write_instance_tool(instance, _gitea_tool())
    store = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )
    # Active block on the route being removed/renamed.
    widened = GrantV1(
        grant_id="blk-issues-create",
        team_id=TEAM,
        kind=GrantKind.BLOCK,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
        target=TargetRefV1(kind=TargetKind.OPERATION, id="http:gitea#route:issues.create"),
        level=AccessLevel.FULL,
        granted_by=ACTOR,
        created_at_utc=NOW,
    )
    # Active block on a route that stays (same name, only re-levelled) -> should NOT be flagged.
    kept = GrantV1(
        grant_id="blk-repos-list",
        team_id=TEAM,
        kind=GrantKind.BLOCK,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
        target=TargetRefV1(kind=TargetKind.OPERATION, id="http:gitea#route:repos.list"),
        level=AccessLevel.FULL,
        granted_by=ACTOR,
        created_at_utc=NOW,
    )
    # Expired block on the removed route -> should NOT be flagged.
    expired = GrantV1(
        grant_id="blk-expired",
        team_id=TEAM,
        kind=GrantKind.BLOCK,
        subject=SubjectRefV1(kind=SubjectKind.TEAM, id=TEAM),
        target=TargetRefV1(kind=TargetKind.OPERATION, id="http:gitea#route:issues.create"),
        level=AccessLevel.FULL,
        lifetime=GrantLifetime.UNTIL,
        expires_at_utc=NOW - timedelta(minutes=1),
        granted_by=ACTOR,
        created_at_utc=NOW - timedelta(hours=1),
    )
    for grant in (widened, kept, expired):
        store.create(grant)

    catalog = load_catalog(instance)
    compiler = ProposalCompiler(
        catalog=catalog,
        team_id=TEAM,
        holds=lambda _person, _target, _level: True,
        account_sharing=TeamAccountSharing(store, TEAM),
        instance_root=instance,
        blocks_for_tool=lambda tool_id: active_operation_blocks_for_tool(store, TEAM, tool_id, now=NOW),
    )
    # Rename issues.create -> issues.open, keep repos.list with higher level.
    change = _gitea_change(
        routes=(
            HttpRouteV1(name="repos.list", method="GET", path="/api/v1/user/repos", level=AccessLevel.WRITE),
            HttpRouteV1(
                name="issues.open",
                method="POST",
                path="/api/v1/repos/{owner}/{repo}/issues",
                level=AccessLevel.WRITE,
            ),
        ),
    )
    proposal = with_tool_change_baselines(
        AccessProposalV1(title="Rename issues route", reason="Match API naming.", tool_changes=(change,)),
        instance_root=instance,
        catalog=catalog,
    )
    compiled = compiler.compile(proposal, person=None, task_id="task-1")
    widened_rows = [row for row in compiled.rows if "block_widened" in row.flags]
    assert len(widened_rows) == 1
    row = widened_rows[0]
    assert row.item_id == "change-0-widened-block-0"
    assert row.flags == ("block_widened",)
    assert "blk-issues-create" in row.text
    assert "WebResearchAgent" in row.text
    assert "route:issues.create" in row.text
    assert "all of http:gitea" in row.text
    assert "blk-repos-list" not in row.text and "blk-expired" not in row.text

    summary = proposal_summary(compiled.rows)
    assert "change 1 tool" in summary
    assert "a restriction that will block a whole tool" in summary


def test_tool_change_compiler_output_renders_under_tool_changes_section_in_inbox_mjs(tmp_path: Path) -> None:
    """F6 (R6): real ProposalCompiler output for tool_changes renders under 'Tool changes' in access_inbox.mjs."""

    import json
    import shutil
    import subprocess

    from access_control.stale_blocks import active_operation_blocks_for_tool
    from custom_messages.contracts.access_v2 import GrantKind, GrantV1

    node = shutil.which("node")
    assert node is not None, "node is required to render access_inbox.mjs"

    instance = tmp_path / "instance"
    write_instance_tool(instance, _gitea_tool())
    store = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )
    store.create_link(_link(link_id="team-1", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM))
    store.create(
        GrantV1(
            grant_id="blk-render-1",
            team_id=TEAM,
            kind=GrantKind.BLOCK,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
            target=TargetRefV1(kind=TargetKind.OPERATION, id="http:gitea#route:issues.create"),
            level=AccessLevel.FULL,
            granted_by=ACTOR,
            created_at_utc=NOW,
        )
    )
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
        blocks_for_tool=lambda tool_id: active_operation_blocks_for_tool(store, TEAM, tool_id, now=NOW),
    )
    change = _gitea_change(
        hosts=("gitea.example.com", "mirror.example.net"),
        routes=(HttpRouteV1(name="repos.list", method="GET", path="/api/v1/user/repos", level=AccessLevel.READ),),
    )
    proposal = with_tool_change_baselines(
        AccessProposalV1(title="Add mirror and drop issues route", reason="Mirror reads only.", tool_changes=(change,)),
        instance_root=instance,
        catalog=catalog,
    )
    compiled = compiler.compile(proposal, person=None, task_id="task-1")
    assert compiled.rows and all(row.section == "tool_change" for row in compiled.rows)

    review_payload = {
        "request_id": "req-real-tool-change",
        "kind": "change",
        "title": f"TeamArchitect asks to make a change: access proposal: {proposal.title}",
        "summary": proposal_summary(compiled.rows),
        "rows": [row.model_dump(mode="json") for row in compiled.rows],
        "reason": proposal.reason,
        "created_at_utc": NOW.isoformat(),
        "can_decide": True,
        "choices": [{"choice": "approve", "label": "Approve"}, {"choice": "deny", "label": "Deny"}],
    }
    inbox_mjs = Path(__file__).resolve().parents[1] / "assistant_gateway" / "onboarding_web" / "assets" / "access_inbox.mjs"
    script = """
const { renderReviewCard } = await import(process.argv[1]);
const review = JSON.parse(process.argv[2]);
class FakeElement extends EventTarget {
  constructor(document, tagName) {
    super();
    Object.assign(this, { ownerDocument: document, tagName: tagName.toUpperCase(), children: [],
      parentNode: null, attributes: new Map(), className: "", ownText: "", hidden: false,
      disabled: false, readOnly: false, value: "", tabIndex: 0 });
  }
  get classList() {
    const names = () => this.className.split(/\\s+/).filter(Boolean);
    return {
      contains: name => names().includes(name),
      toggle: (name, force) => {
        const cur = names();
        const on = force === undefined ? !cur.includes(name) : Boolean(force);
        this.className = [...new Set(on ? [...cur, name] : cur.filter(v => v !== name))].join(" ");
      },
    };
  }
  get textContent() { return this.ownText + this.children.map(c => c.textContent).join(""); }
  set textContent(v) { this.children = []; this.ownText = String(v); }
  setAttribute(k, v) { this.attributes.set(k, String(v)); }
  getAttribute(k) { return this.attributes.has(k) ? this.attributes.get(k) : null; }
  append(...nodes) { for (const n of nodes) { n.parentNode = this; this.children.push(n); } }
}
const doc = { createElement: tag => new FakeElement(doc, tag) };
const descendants = n => n.children.flatMap(c => [c, ...descendants(c)]);
const byClass = (n, cls) => descendants(n).filter(c => c.classList.contains(cls));
const card = renderReviewCard(doc, review, { formatTime: () => "Nov 22, 2026" });
console.log(JSON.stringify({
  sections: byClass(card, "access-review-row-section").map(n => n.textContent),
  rows: byClass(card, "access-review-row-text").map(n => n.textContent),
  flags: byClass(card, "access-review-flag").map(n => n.textContent),
}));
"""
    rendered = subprocess.run(
        [node, "--input-type=module", "-e", script, inbox_mjs.as_uri(), json.dumps(review_payload)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(rendered.stdout)
    assert result["sections"] == ["Tool changes"]
    assert "New tools" not in result["sections"]
    assert result["rows"] == [row.text for row in compiled.rows]
    assert set(result["flags"]) == {"New host", "Credentials to new host", "Block widens to whole tool"}
