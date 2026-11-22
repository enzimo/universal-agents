"""Trusted-code diff descriptions and review rows for proposed web API tool changes.

When TeamLead proposes replacing an existing proposal-written web API tool
(``AccessProposalV1.tool_changes``), trusted code compares the current catalog
definition read from ``control/access/tools/proposed-*.yaml`` against the
proposed replacement and writes every difference in its own words:

- hosts added (with ``new_host``, optional ``private_address``, and
  ``credential_to_new_host`` whenever active links will remain valid and send
  their stored credentials to the added hosts immediately) and hosts removed;
- routes added, removed, re-levelled, modified, or reordered (route matching
  uses first match, so order affects access levels);
- account credential mode and injection changes, and team gate credential
  injection changes;
- which existing links (team account, people's accounts, team gate credential)
  must be linked again with their exact ``/link`` commands;
- setup changes (prerequisites and external steps, with added/changed steps
  shown verbatim under the ``setup_steps`` flag);
- display name and description changes.

Both ``ProposalCompiler`` (authorization on) and ``apply_by_agreement``
(authorization off) use these rows so the reviewer or person always sees the
same trusted description of every change.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from access_catalog.instance_tools import private_host_names
from access_control.requests import LEVEL_WORDS
from custom_messages.contracts.access_v2 import (
    AccountLinkStatus,
    AccountLinkV1,
    AccountMode,
    AccountSharing,
    CredentialInjectionV1,
    HttpRouteV1,
    InjectionPlacement,
    LinkOwnerKind,
    LinkPurpose,
    OwnerScope,
    ReviewItemV1,
    ToolDefinitionV1,
    injections_match,
)
from custom_messages.contracts.tool_setup_spec_v1 import ExternalSetupStepV1
from secure_tools.chat_commands import link_command

_ROW_TEXT_LIMIT = 1600
_DESCRIBED_TOOL_FIELDS = frozenset(
    {
        "schema_version",
        "tool_id",
        "kind",
        "display_name",
        "description",
        "hosts",
        "routes",
        "operations",
        "account",
        "credential_injections",
        "gate_injections",
        "setup",
        "link_owners",
        "default_sharing",
        "owner_scope",
    }
)
_OWNER_SCOPE_WORDS = {
    OwnerScope.TEAM: "team",
    OwnerScope.PERSON: "each person's own",
    OwnerScope.ADMINISTRATORS: "administrators only",
    OwnerScope.PLATFORM: "platform",
}


def _bounded(text: str) -> str:
    if len(text) <= _ROW_TEXT_LIMIT:
        return text
    return text[: _ROW_TEXT_LIMIT - 1].rstrip() + "…"


def describe_injections(injections: Iterable[CredentialInjectionV1]) -> str:
    """Plain-language summary of credential or gate injections in stable order."""

    items = sorted(
        injections,
        key=lambda item: (
            item.placement.value,
            item.name.lower() if item.placement is InjectionPlacement.HEADER else item.name,
            item.field,
            item.template,
        ),
    )
    if not items:
        return "none"
    parts: list[str] = []
    for item in items:
        template = f", format {item.template}" if item.template != "{value}" else ""
        parts.append(f"{item.placement.value} {item.name} (field {item.field}{template})")
    return ", ".join(parts)


@dataclass(frozen=True)
class LinkImpact:
    """How a proposed tool change affects currently active links for the tool."""

    surviving_team_account: bool = False
    surviving_person_accounts: int = 0
    surviving_gate: bool = False
    outdated_team_account: bool = False
    outdated_person_accounts: int = 0
    outdated_gate: bool = False
    unused_account_links: int = 0
    unused_gate_link: bool = False

    @property
    def has_surviving_links(self) -> bool:
        return self.surviving_team_account or self.surviving_person_accounts > 0 or self.surviving_gate

    @property
    def has_outdated_links(self) -> bool:
        return self.outdated_team_account or self.outdated_person_accounts > 0 or self.outdated_gate


def _is_matching_link(link: AccountLinkV1, expected: tuple[CredentialInjectionV1, ...]) -> bool:
    return injections_match(link.injections, expected)


def assess_link_impact(
    proposed: ToolDefinitionV1, active_links: Iterable[AccountLinkV1]
) -> LinkImpact:
    """Classify each active link on ``proposed.tool_id`` as surviving, outdated, or no longer used."""

    active = [link for link in active_links if link.status is AccountLinkStatus.ACTIVE]
    gate_links = [link for link in active if link.purpose is LinkPurpose.GATE]
    account_links = [link for link in active if link.purpose is LinkPurpose.ACCOUNT]

    if not proposed.gate_injections:
        surviving_gate = False
        outdated_gate = False
        unused_gate = bool(gate_links)
    else:
        surviving_gate = any(_is_matching_link(link, proposed.gate_injections) for link in gate_links)
        outdated_gate = any(not _is_matching_link(link, proposed.gate_injections) for link in gate_links)
        unused_gate = False

    if proposed.account is not AccountMode.LINKED_CREDENTIAL:
        return LinkImpact(
            surviving_gate=surviving_gate,
            outdated_gate=outdated_gate,
            unused_account_links=len(account_links),
            unused_gate_link=unused_gate,
        )

    matching = [link for link in account_links if _is_matching_link(link, proposed.credential_injections)]
    outdated = [link for link in account_links if not _is_matching_link(link, proposed.credential_injections)]
    return LinkImpact(
        surviving_team_account=any(link.owner_kind is LinkOwnerKind.TEAM for link in matching),
        surviving_person_accounts=sum(1 for link in matching if link.owner_kind is LinkOwnerKind.PERSON),
        surviving_gate=surviving_gate,
        outdated_team_account=any(link.owner_kind is LinkOwnerKind.TEAM for link in outdated),
        outdated_person_accounts=sum(1 for link in outdated if link.owner_kind is LinkOwnerKind.PERSON),
        outdated_gate=outdated_gate,
        unused_gate_link=unused_gate,
    )


@dataclass(frozen=True)
class ToolChangeDiff:
    """Structured differences between a web API tool's current and proposed definitions."""

    tool_id: str
    current_name: str
    proposed_name: str
    added_hosts: tuple[str, ...] = ()
    removed_hosts: tuple[str, ...] = ()
    added_routes: tuple[HttpRouteV1, ...] = ()
    removed_routes: tuple[HttpRouteV1, ...] = ()
    changed_routes: tuple[tuple[HttpRouteV1, HttpRouteV1], ...] = ()
    routes_reordered: bool = False
    account_mode_changed: bool = False
    account_injections_changed: bool = False
    gate_injections_changed: bool = False
    added_or_changed_steps: tuple[ExternalSetupStepV1, ...] = ()
    removed_step_ids: tuple[str, ...] = ()
    steps_reordered: bool = False
    added_prerequisites: tuple[str, ...] = ()
    removed_prerequisites: tuple[str, ...] = ()
    display_name_changed: bool = False
    description_changed: bool = False
    link_owners_changed: bool = False
    default_sharing_changed: bool = False
    owner_scope_changed: bool = False
    other_changed_fields: tuple[str, ...] = ()

    @property
    def policy_changed(self) -> bool:
        return bool(
            self.link_owners_changed
            or self.default_sharing_changed
            or self.owner_scope_changed
            or self.other_changed_fields
        )

    @property
    def routes_changed(self) -> bool:
        return bool(self.added_routes or self.removed_routes or self.changed_routes or self.routes_reordered)

    @property
    def setup_changed(self) -> bool:
        return bool(
            self.added_or_changed_steps
            or self.removed_step_ids
            or self.steps_reordered
            or self.added_prerequisites
            or self.removed_prerequisites
        )

    @property
    def has_changes(self) -> bool:
        return any(
            (
                self.added_hosts,
                self.removed_hosts,
                self.routes_changed,
                self.account_mode_changed,
                self.account_injections_changed,
                self.gate_injections_changed,
                self.setup_changed,
                self.display_name_changed,
                self.description_changed,
                self.policy_changed,
            )
        )


def _diff_routes(
    current: tuple[HttpRouteV1, ...], proposed: tuple[HttpRouteV1, ...]
) -> tuple[
    tuple[HttpRouteV1, ...],
    tuple[HttpRouteV1, ...],
    tuple[tuple[HttpRouteV1, HttpRouteV1], ...],
    bool,
]:
    current_by_name = {route.name: route for route in current}
    proposed_by_name = {route.name: route for route in proposed}
    added = tuple(route for route in proposed if route.name not in current_by_name)
    removed = tuple(route for route in current if route.name not in proposed_by_name)
    changed = tuple(
        (current_by_name[route.name], route)
        for route in proposed
        if route.name in current_by_name and current_by_name[route.name] != route
    )
    common_old = tuple(route.name for route in current if route.name in proposed_by_name)
    common_new = tuple(route.name for route in proposed if route.name in current_by_name)
    return added, removed, changed, common_old != common_new


def _diff_setup(
    current: ToolDefinitionV1, proposed: ToolDefinitionV1
) -> tuple[tuple[ExternalSetupStepV1, ...], tuple[str, ...], bool, tuple[str, ...], tuple[str, ...]]:
    old_steps = current.setup.external_steps if current.setup else ()
    new_steps = proposed.setup.external_steps if proposed.setup else ()
    old_by_id = {step.step_id: step for step in old_steps}
    new_by_id = {step.step_id: step for step in new_steps}
    added_or_changed = tuple(
        step for step in new_steps if step.step_id not in old_by_id or old_by_id[step.step_id] != step
    )
    removed_ids = tuple(step.step_id for step in old_steps if step.step_id not in new_by_id)
    reordered = tuple(s.step_id for s in old_steps if s.step_id in new_by_id) != tuple(
        s.step_id for s in new_steps if s.step_id in old_by_id
    )
    old_prereqs = current.setup.prerequisites if current.setup else ()
    new_prereqs = proposed.setup.prerequisites if proposed.setup else ()
    added_prereqs = tuple(item for item in new_prereqs if item not in old_prereqs)
    removed_prereqs = tuple(item for item in old_prereqs if item not in new_prereqs)
    return added_or_changed, removed_ids, reordered, added_prereqs, removed_prereqs


def _other_changed_fields(current: ToolDefinitionV1, proposed: ToolDefinitionV1) -> tuple[str, ...]:
    """Any ``ToolDefinitionV1`` fields outside the explicitly described aspects whose values differ."""

    old_dump = current.model_dump(mode="json")
    new_dump = proposed.model_dump(mode="json")
    keys = (set(old_dump) | set(new_dump)) - _DESCRIBED_TOOL_FIELDS
    return tuple(sorted(key for key in keys if old_dump.get(key) != new_dump.get(key)))


def diff_tool_definition(current: ToolDefinitionV1, proposed: ToolDefinitionV1) -> ToolChangeDiff:
    """Compare ``current`` and ``proposed`` web API tool definitions."""

    current_hosts = set(current.hosts)
    proposed_hosts = set(proposed.hosts)
    added_hosts = tuple(host for host in proposed.hosts if host not in current_hosts)
    removed_hosts = tuple(host for host in current.hosts if host not in proposed_hosts)
    added_routes, removed_routes, changed_routes, routes_reordered = _diff_routes(current.routes, proposed.routes)
    (
        added_or_changed_steps,
        removed_step_ids,
        steps_reordered,
        added_prerequisites,
        removed_prerequisites,
    ) = _diff_setup(current, proposed)

    return ToolChangeDiff(
        tool_id=proposed.tool_id,
        current_name=current.display_name,
        proposed_name=proposed.display_name,
        added_hosts=added_hosts,
        removed_hosts=removed_hosts,
        added_routes=added_routes,
        removed_routes=removed_routes,
        changed_routes=changed_routes,
        routes_reordered=routes_reordered,
        account_mode_changed=current.account is not proposed.account,
        account_injections_changed=not injections_match(
            current.credential_injections, proposed.credential_injections
        ),
        gate_injections_changed=not injections_match(current.gate_injections, proposed.gate_injections),
        added_or_changed_steps=added_or_changed_steps,
        removed_step_ids=removed_step_ids,
        steps_reordered=steps_reordered,
        added_prerequisites=added_prerequisites,
        removed_prerequisites=removed_prerequisites,
        display_name_changed=current.display_name != proposed.display_name,
        description_changed=current.description != proposed.description,
        link_owners_changed=current.link_owners != proposed.link_owners,
        default_sharing_changed=current.default_sharing != proposed.default_sharing,
        owner_scope_changed=current.owner_scope is not proposed.owner_scope,
        other_changed_fields=_other_changed_fields(current, proposed),
    )


def _people_count_words(count: int) -> str:
    return "1 person's account" if count == 1 else f"{count} people's accounts"


def _surviving_links_words(impact: LinkImpact) -> str:
    parts: list[str] = []
    if impact.surviving_team_account:
        parts.append("the team's account")
    if impact.surviving_person_accounts:
        parts.append(_people_count_words(impact.surviving_person_accounts))
    if impact.surviving_gate:
        parts.append("the team's gate credential")
    return ", ".join(parts)


def _overview_row(index: int, diff: ToolChangeDiff) -> ReviewItemV1:
    aspects: list[str] = []
    if diff.added_hosts or diff.removed_hosts:
        aspects.append("hosts")
    if diff.routes_changed:
        aspects.append("routes")
    if diff.account_mode_changed or diff.account_injections_changed:
        aspects.append("account credentials")
    if diff.gate_injections_changed:
        aspects.append("team gate credentials")
    if diff.setup_changed:
        aspects.append("setup")
    if diff.display_name_changed or diff.description_changed:
        aspects.append("details")
    if diff.policy_changed:
        aspects.append("account and ownership settings")
    return ReviewItemV1(
        item_id=f"change-{index}",
        section="tool",
        text=_bounded(
            f"Change web API tool {diff.current_name} ({diff.tool_id}): updates {', '.join(aspects)}."
        ),
    )


def _host_rows(index: int, diff: ToolChangeDiff, impact: LinkImpact) -> list[ReviewItemV1]:
    rows: list[ReviewItemV1] = []
    if diff.added_hosts:
        flags = ["new_host"]
        if private_host_names(diff.added_hosts):
            flags.append("private_address")
        label = "host" if len(diff.added_hosts) == 1 else "hosts"
        text = f"Add {label} to {diff.proposed_name} ({diff.tool_id}): {', '.join(diff.added_hosts)}."
        if impact.has_surviving_links:
            flags.append("credential_to_new_host")
            target_word = "this new host" if len(diff.added_hosts) == 1 else "these new hosts"
            text += (
                f" Existing stored credentials ({_surviving_links_words(impact)}) will be sent to "
                f"{target_word} immediately once approved, without linking again."
            )
        rows.append(
            ReviewItemV1(
                item_id=f"change-{index}-hosts-added",
                section="tool",
                text=_bounded(text),
                flags=tuple(flags),
            )
        )
    if diff.removed_hosts:
        label = "host" if len(diff.removed_hosts) == 1 else "hosts"
        rows.append(
            ReviewItemV1(
                item_id=f"change-{index}-hosts-removed",
                section="tool",
                text=_bounded(
                    f"Remove {label} from {diff.proposed_name} ({diff.tool_id}): {', '.join(diff.removed_hosts)}."
                ),
            )
        )
    return rows


def _route_row(
    index: int, current: ToolDefinitionV1, proposed: ToolDefinitionV1, diff: ToolChangeDiff
) -> list[ReviewItemV1]:
    if not diff.routes_changed:
        return []
    parts: list[str] = []
    for route in diff.added_routes:
        parts.append(f"added {route.name} ({route.method} {route.path}, needs {LEVEL_WORDS[route.level]})")
    for route in diff.removed_routes:
        parts.append(f"removed {route.name} ({route.method} {route.path}, was {LEVEL_WORDS[route.level]})")
    for old_route, new_route in diff.changed_routes:
        if old_route.method == new_route.method and old_route.path == new_route.path:
            parts.append(
                f"re-levelled {new_route.name} ({new_route.method} {new_route.path}): "
                f"{LEVEL_WORDS[old_route.level]} -> {LEVEL_WORDS[new_route.level]}"
            )
        else:
            parts.append(
                f"changed {new_route.name}: {old_route.method} {old_route.path} ({LEVEL_WORDS[old_route.level]}) -> "
                f"{new_route.method} {new_route.path} ({LEVEL_WORDS[new_route.level]})"
            )
    if diff.routes_reordered:
        old_order = ", ".join(route.name for route in current.routes)
        new_order = ", ".join(route.name for route in proposed.routes)
        parts.append(f"reordered routes (first match wins): {old_order} -> {new_order}")
    return [
        ReviewItemV1(
            item_id=f"change-{index}-routes",
            section="tool",
            text=_bounded(f"Routes changed for {diff.proposed_name} ({diff.tool_id}): {'; '.join(parts)}."),
        )
    ]


def _credential_rows(
    index: int, current: ToolDefinitionV1, proposed: ToolDefinitionV1, diff: ToolChangeDiff
) -> list[ReviewItemV1]:
    rows: list[ReviewItemV1] = []
    if diff.account_mode_changed or diff.account_injections_changed:
        if current.account is AccountMode.NONE and proposed.account is AccountMode.LINKED_CREDENTIAL:
            text = (
                f"Account credentials added for {diff.proposed_name} ({diff.tool_id}): now requires a linked "
                f"account ({describe_injections(proposed.credential_injections)})."
            )
        elif current.account is AccountMode.LINKED_CREDENTIAL and proposed.account is AccountMode.NONE:
            text = (
                f"Account credentials removed from {diff.proposed_name} ({diff.tool_id}): no longer uses a "
                f"linked account (was {describe_injections(current.credential_injections)})."
            )
        else:
            text = (
                f"Account credential injections changed for {diff.proposed_name} ({diff.tool_id}): "
                f"was {describe_injections(current.credential_injections)}; "
                f"now {describe_injections(proposed.credential_injections)}."
            )
        rows.append(
            ReviewItemV1(item_id=f"change-{index}-account-credentials", section="tool", text=_bounded(text))
        )

    if diff.gate_injections_changed:
        gate_cmd = link_command(diff.tool_id, LinkOwnerKind.TEAM, purpose=LinkPurpose.GATE)
        if not current.gate_injections and proposed.gate_injections:
            text = (
                f"Team gate credential added for {diff.proposed_name} ({diff.tool_id}): "
                f"{describe_injections(proposed.gate_injections)}. An administrator must link it with "
                f"{gate_cmd} before the tool can be used."
            )
        elif current.gate_injections and not proposed.gate_injections:
            text = (
                f"Team gate credential removed from {diff.proposed_name} ({diff.tool_id}) "
                f"(was {describe_injections(current.gate_injections)})."
            )
        else:
            text = (
                f"Team gate credential injections changed for {diff.proposed_name} ({diff.tool_id}): "
                f"was {describe_injections(current.gate_injections)}; "
                f"now {describe_injections(proposed.gate_injections)}."
            )
        rows.append(
            ReviewItemV1(item_id=f"change-{index}-gate-credentials", section="tool", text=_bounded(text))
        )
    return rows


def _relink_row(index: int, diff: ToolChangeDiff, impact: LinkImpact) -> list[ReviewItemV1]:
    outdated_parts: list[str] = []
    if impact.outdated_team_account:
        team_cmd = link_command(diff.tool_id, LinkOwnerKind.TEAM, purpose=LinkPurpose.ACCOUNT)
        outdated_parts.append(f"the team's account ({team_cmd})")
    if impact.outdated_person_accounts:
        person_cmd = link_command(diff.tool_id, LinkOwnerKind.PERSON, purpose=LinkPurpose.ACCOUNT)
        outdated_parts.append(f"{_people_count_words(impact.outdated_person_accounts)} ({person_cmd})")
    if impact.outdated_gate:
        gate_cmd = link_command(diff.tool_id, LinkOwnerKind.TEAM, purpose=LinkPurpose.GATE)
        outdated_parts.append(f"the team's gate credential ({gate_cmd})")

    notes: list[str] = []
    if outdated_parts:
        notes.append(
            f"Links that must be made again for {diff.proposed_name} ({diff.tool_id}): "
            f"{', '.join(outdated_parts)}. Existing links will be refused until linked again."
        )
    elif diff.account_injections_changed or diff.gate_injections_changed:
        notes.append(f"No existing links for {diff.proposed_name} ({diff.tool_id}) need to be made again.")
    if impact.unused_account_links:
        count_word = "1 existing account link" if impact.unused_account_links == 1 else f"{impact.unused_account_links} existing account links"
        notes.append(f"{count_word} will no longer be used because the tool no longer uses a linked account.")
    if impact.unused_gate_link:
        notes.append("The existing team gate link will no longer be used because the tool no longer declares gate injections.")
    if not notes:
        return []
    return [
        ReviewItemV1(
            item_id=f"change-{index}-relink",
            section="tool",
            text=_bounded(" ".join(notes)),
        )
    ]


def _setup_rows(index: int, proposed: ToolDefinitionV1, diff: ToolChangeDiff) -> list[ReviewItemV1]:
    if not diff.setup_changed:
        return []
    parts: list[str] = []
    if diff.added_prerequisites:
        parts.append(f"added prerequisites: {', '.join(diff.added_prerequisites)}")
    if diff.removed_prerequisites:
        parts.append(f"removed prerequisites: {', '.join(diff.removed_prerequisites)}")
    if diff.added_or_changed_steps:
        parts.append(
            f"added or updated steps: {', '.join(step.step_id for step in diff.added_or_changed_steps)}"
        )
    if diff.removed_step_ids:
        parts.append(f"removed steps: {', '.join(diff.removed_step_ids)}")
    if diff.steps_reordered:
        parts.append("reordered setup steps")
    rows = [
        ReviewItemV1(
            item_id=f"change-{index}-setup",
            section="tool",
            text=_bounded(f"Setup changed for {diff.proposed_name} ({diff.tool_id}): {'; '.join(parts)}."),
        )
    ]
    for number, step in enumerate(diff.added_or_changed_steps, start=1):
        who = "an administrator" if step.who == "administrator" else "the person"
        link = f" at {step.url}" if step.url else ""
        check = f" How to tell it worked: {step.verify}" if step.verify else ""
        rows.append(
            ReviewItemV1(
                item_id=f"change-{index}-step-{number}",
                section="tool",
                text=_bounded(
                    f"Setup step {number} for {proposed.display_name}, done by {who}{link}: \"{step.title}\". "
                    f"{step.instructions}{check}"
                ),
                flags=("setup_steps",),
            )
        )
    return rows


def _details_row(
    index: int, current: ToolDefinitionV1, proposed: ToolDefinitionV1, diff: ToolChangeDiff
) -> list[ReviewItemV1]:
    if not (diff.display_name_changed or diff.description_changed):
        return []
    parts: list[str] = []
    if diff.display_name_changed:
        parts.append(f"display name {current.display_name!r} -> {proposed.display_name!r}")
    if diff.description_changed:
        parts.append(f"description updated to {proposed.description!r}" if proposed.description else "description cleared")
    return [
        ReviewItemV1(
            item_id=f"change-{index}-details",
            section="tool",
            text=_bounded(f"Details changed for {diff.proposed_name} ({diff.tool_id}): {'; '.join(parts)}."),
        )
    ]


def _link_owners_words(owners: tuple[str, ...]) -> str:
    if len(owners) == 1:
        return f"{owners[0]} only"
    return " then ".join(owners)


def _sharing_words(sharing: AccountSharing | None) -> str:
    return sharing.value if sharing is not None else "team default"


def _policy_row(
    index: int, current: ToolDefinitionV1, proposed: ToolDefinitionV1, diff: ToolChangeDiff
) -> list[ReviewItemV1]:
    if not diff.policy_changed:
        return []
    parts: list[str] = []
    if diff.link_owners_changed:
        parts.append(
            f"whose account is used: {_link_owners_words(current.link_owners)} -> "
            f"{_link_owners_words(proposed.link_owners)}"
        )
    if diff.default_sharing_changed:
        parts.append(
            f"default account sharing: {_sharing_words(current.default_sharing)} -> "
            f"{_sharing_words(proposed.default_sharing)}"
        )
    if diff.owner_scope_changed:
        parts.append(
            f"owner scope: {_OWNER_SCOPE_WORDS[current.owner_scope]} -> "
            f"{_OWNER_SCOPE_WORDS[proposed.owner_scope]}"
        )
    if diff.other_changed_fields:
        parts.append(f"other settings: {', '.join(diff.other_changed_fields)}")
    return [
        ReviewItemV1(
            item_id=f"change-{index}-settings",
            section="tool",
            text=_bounded(
                f"Account and ownership settings changed for {diff.proposed_name} ({diff.tool_id}): "
                f"{'; '.join(parts)}."
            ),
        )
    ]


def tool_change_rows(
    index: int,
    *,
    current: ToolDefinitionV1,
    proposed: ToolDefinitionV1,
    active_links: Iterable[AccountLinkV1] = (),
) -> tuple[ToolChangeDiff, list[ReviewItemV1]]:
    """Compute the diff and trusted-code review rows for one ``tool_changes`` item."""

    diff = diff_tool_definition(current, proposed)
    if not diff.has_changes:
        return diff, []
    impact = assess_link_impact(proposed, active_links)
    rows: list[ReviewItemV1] = [
        _overview_row(index, diff),
        *_host_rows(index, diff, impact),
        *_route_row(index, current, proposed, diff),
        *_credential_rows(index, current, proposed, diff),
        *_relink_row(index, diff, impact),
        *_setup_rows(index, proposed, diff),
        *_details_row(index, current, proposed, diff),
        *_policy_row(index, current, proposed, diff),
    ]
    return diff, rows


__all__ = [
    "LinkImpact",
    "ToolChangeDiff",
    "assess_link_impact",
    "describe_injections",
    "diff_tool_definition",
    "tool_change_rows",
]
