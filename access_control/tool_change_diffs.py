"""Trusted-code diff descriptions and review rows for proposed web API tool changes.

When TeamLead proposes replacing an existing proposal-written web API tool
(``AccessProposalV1.tool_changes``), trusted code compares the current catalog
definition read from ``control/access/tools/proposed-*.yaml`` against the
proposed replacement and writes every difference in its own words:

- hosts added (with ``new_host``, optional ``private_address``, and
  ``credential_to_new_host`` whenever active links will remain valid and send
  their stored credentials to the added hosts immediately) and hosts removed;
- routes added, removed, re-levelled, modified, or reordered (route matching
  uses first match, so order affects access levels), plus active operation
  blocks widened by route removal (``block_widened``) or moved to a different
  method or path (``block_moved``);
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
from datetime import datetime

from access_catalog.instance_tools import private_host_names
from access_control.requests import LEVEL_WORDS
from access_control.stale_blocks import subject_name
from custom_messages.contracts.access_v2 import (
    AccountLinkStatus,
    AccountLinkV1,
    AccountMode,
    AccountSharing,
    CredentialInjectionV1,
    GrantKind,
    GrantStatus,
    GrantV1,
    HttpRouteV1,
    InjectionPlacement,
    LinkOwnerKind,
    LinkPurpose,
    OwnerScope,
    ReviewItemV1,
    TargetKind,
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
    OwnerScope.ADMINISTRATORS: "administrators only",
    OwnerScope.PLATFORM: "platform",
}


def _review_row(item_id: str, text: str, *, flags: tuple[str, ...] = ()) -> ReviewItemV1:
    """Build one review row without ever truncating a difference."""

    if len(text) > _ROW_TEXT_LIMIT:
        raise ValueError(
            f"Review row {item_id!r} is {len(text)} characters, exceeding the {_ROW_TEXT_LIMIT}-character limit."
        )
    return ReviewItemV1(item_id=item_id, section="tool_change", text=text, flags=flags)


def _pack_text_rows(
    item_id_prefix: str,
    header: str,
    entries: list[str],
    *,
    separator: str = ", ",
    suffix: str = ".",
) -> list[ReviewItemV1]:
    """Pack complete ``entries`` across as many rows as needed so no entry is ever cut."""

    rows: list[ReviewItemV1] = []
    chunk: list[str] = []
    for entry in entries:
        candidate = f"{header}{separator.join([*chunk, entry])}{suffix}"
        if chunk and len(candidate) > _ROW_TEXT_LIMIT:
            rows.append(_review_row(f"{item_id_prefix}-{len(rows)}", f"{header}{separator.join(chunk)}{suffix}"))
            chunk = [entry]
        else:
            chunk.append(entry)
    if chunk:
        rows.append(_review_row(f"{item_id_prefix}-{len(rows)}", f"{header}{separator.join(chunk)}{suffix}"))
    return rows


def _describe_one_injection(item: CredentialInjectionV1) -> str:
    template = f", format {item.template}" if item.template != "{value}" else ""
    return f"{item.placement.value} {item.name} (field {item.field}{template})"


def _sorted_injections(injections: Iterable[CredentialInjectionV1]) -> list[CredentialInjectionV1]:
    return sorted(
        injections,
        key=lambda item: (
            item.placement.value,
            item.name.lower() if item.placement is InjectionPlacement.HEADER else item.name,
            item.field,
            item.template,
        ),
    )


def describe_injections(injections: Iterable[CredentialInjectionV1]) -> str:
    """Plain-language summary of credential or gate injections in stable order."""

    items = _sorted_injections(injections)
    if not items:
        return "none"
    return ", ".join(_describe_one_injection(item) for item in items)


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
    added_hosts = tuple(dict.fromkeys(host for host in proposed.hosts if host not in current_hosts))
    removed_hosts = tuple(dict.fromkeys(host for host in current.hosts if host not in proposed_hosts))
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
    return _review_row(
        f"change-{index}",
        f"Change web API tool {diff.current_name} ({diff.tool_id}): updates {', '.join(aspects)}.",
    )


def _host_rows(index: int, diff: ToolChangeDiff, impact: LinkImpact) -> list[ReviewItemV1]:
    """One review row per added or removed host so no host or credential warning can be hidden."""

    rows: list[ReviewItemV1] = []
    for host_index, host in enumerate(diff.added_hosts):
        flags = ["new_host"]
        if private_host_names((host,)):
            flags.append("private_address")
        text = f"Add host to {diff.proposed_name} ({diff.tool_id}): {host}."
        if impact.has_surviving_links:
            flags.append("credential_to_new_host")
            text += (
                f" Existing stored credentials ({_surviving_links_words(impact)}) will be sent to "
                "this new host immediately once approved, without linking again."
            )
        rows.append(_review_row(f"change-{index}-host-added-{host_index}", text, flags=tuple(flags)))
    for host_index, host in enumerate(diff.removed_hosts):
        rows.append(
            _review_row(
                f"change-{index}-host-removed-{host_index}",
                f"Remove host from {diff.proposed_name} ({diff.tool_id}): {host}.",
            )
        )
    return rows


def _reordered_route_rows(
    index: int, current: ToolDefinitionV1, proposed: ToolDefinitionV1, diff: ToolChangeDiff
) -> list[ReviewItemV1]:
    old_names = [route.name for route in current.routes]
    new_names = [route.name for route in proposed.routes]
    combined = (
        f"Routes reordered for {diff.proposed_name} ({diff.tool_id}) (first match wins): "
        f"{', '.join(old_names)} -> {', '.join(new_names)}."
    )
    if len(combined) <= _ROW_TEXT_LIMIT:
        return [_review_row(f"change-{index}-routes-reordered", combined)]
    return [
        *_pack_text_rows(
            f"change-{index}-routes-reordered-before",
            f"Routes reordered for {diff.proposed_name} ({diff.tool_id}) (first match wins), previous order: ",
            old_names,
        ),
        *_pack_text_rows(
            f"change-{index}-routes-reordered-after",
            f"Routes reordered for {diff.proposed_name} ({diff.tool_id}) (first match wins), new order: ",
            new_names,
        ),
    ]


def _route_rows(
    index: int, current: ToolDefinitionV1, proposed: ToolDefinitionV1, diff: ToolChangeDiff
) -> list[ReviewItemV1]:
    """One review row per added, removed, or changed route so no route change can be pushed past a limit."""

    if not diff.routes_changed:
        return []
    rows: list[ReviewItemV1] = []
    route_index = 0
    for route in diff.added_routes:
        rows.append(
            _review_row(
                f"change-{index}-route-{route_index}",
                f"Route added for {diff.proposed_name} ({diff.tool_id}): "
                f"{route.name} ({route.method} {route.path}, needs {LEVEL_WORDS[route.level]}).",
            )
        )
        route_index += 1
    for route in diff.removed_routes:
        rows.append(
            _review_row(
                f"change-{index}-route-{route_index}",
                f"Route removed for {diff.proposed_name} ({diff.tool_id}): "
                f"{route.name} ({route.method} {route.path}, was {LEVEL_WORDS[route.level]}).",
            )
        )
        route_index += 1
    for old_route, new_route in diff.changed_routes:
        if old_route.method == new_route.method and old_route.path == new_route.path:
            text = (
                f"Route re-levelled for {diff.proposed_name} ({diff.tool_id}): "
                f"{new_route.name} ({new_route.method} {new_route.path}): "
                f"{LEVEL_WORDS[old_route.level]} -> {LEVEL_WORDS[new_route.level]}."
            )
        else:
            text = (
                f"Route changed for {diff.proposed_name} ({diff.tool_id}): "
                f"{new_route.name}: {old_route.method} {old_route.path} ({LEVEL_WORDS[old_route.level]}) -> "
                f"{new_route.method} {new_route.path} ({LEVEL_WORDS[new_route.level]})."
            )
        rows.append(_review_row(f"change-{index}-route-{route_index}", text))
        route_index += 1
    if diff.routes_reordered:
        rows.extend(_reordered_route_rows(index, current, proposed, diff))
    return rows


def _split_injection_rows(
    item_id_prefix: str,
    label: str,
    diff: ToolChangeDiff,
    previous: tuple[CredentialInjectionV1, ...],
    proposed: tuple[CredentialInjectionV1, ...],
    *,
    trailing_note: str = "",
) -> list[ReviewItemV1]:
    rows: list[ReviewItemV1] = []
    for sub_index, item in enumerate(_sorted_injections(previous)):
        rows.append(
            _review_row(
                f"{item_id_prefix}-was-{sub_index}",
                f"Previous {label} for {diff.proposed_name} ({diff.tool_id}): {_describe_one_injection(item)}.",
            )
        )
    proposed_sorted = _sorted_injections(proposed)
    for sub_index, item in enumerate(proposed_sorted):
        note = trailing_note if sub_index == len(proposed_sorted) - 1 else ""
        rows.append(
            _review_row(
                f"{item_id_prefix}-now-{sub_index}",
                f"New {label} for {diff.proposed_name} ({diff.tool_id}): {_describe_one_injection(item)}.{note}",
            )
        )
    return rows


def _account_credential_rows(
    index: int, current: ToolDefinitionV1, proposed: ToolDefinitionV1, diff: ToolChangeDiff
) -> list[ReviewItemV1]:
    if not (diff.account_mode_changed or diff.account_injections_changed):
        return []
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
    if len(text) <= _ROW_TEXT_LIMIT:
        return [_review_row(f"change-{index}-account-credentials", text)]
    return _split_injection_rows(
        f"change-{index}-account-credentials",
        "account credential injection",
        diff,
        current.credential_injections,
        proposed.credential_injections,
    )


def _gate_credential_rows(
    index: int, current: ToolDefinitionV1, proposed: ToolDefinitionV1, diff: ToolChangeDiff
) -> list[ReviewItemV1]:
    if not diff.gate_injections_changed:
        return []
    gate_cmd = link_command(diff.tool_id, LinkOwnerKind.TEAM, purpose=LinkPurpose.GATE)
    added_note = f" An administrator must link it with {gate_cmd} before the tool can be used."
    if not current.gate_injections and proposed.gate_injections:
        text = (
            f"Team gate credential added for {diff.proposed_name} ({diff.tool_id}): "
            f"{describe_injections(proposed.gate_injections)}.{added_note}"
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
    if len(text) <= _ROW_TEXT_LIMIT:
        return [_review_row(f"change-{index}-gate-credentials", text)]
    return _split_injection_rows(
        f"change-{index}-gate-credentials",
        "team gate credential injection",
        diff,
        current.gate_injections,
        proposed.gate_injections,
        trailing_note=added_note if not current.gate_injections else "",
    )


def _credential_rows(
    index: int, current: ToolDefinitionV1, proposed: ToolDefinitionV1, diff: ToolChangeDiff
) -> list[ReviewItemV1]:
    return [
        *_account_credential_rows(index, current, proposed, diff),
        *_gate_credential_rows(index, current, proposed, diff),
    ]


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
    return [_review_row(f"change-{index}-relink", " ".join(notes))]


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
    summary_text = f"Setup changed for {diff.proposed_name} ({diff.tool_id}): {'; '.join(parts)}."
    if len(summary_text) <= _ROW_TEXT_LIMIT:
        rows = [_review_row(f"change-{index}-setup", summary_text)]
    else:
        rows = _pack_text_rows(
            f"change-{index}-setup",
            f"Setup changed for {diff.proposed_name} ({diff.tool_id}): ",
            parts,
            separator="; ",
        )
    for number, step in enumerate(diff.added_or_changed_steps, start=1):
        who = "an administrator" if step.who == "administrator" else "the person"
        link = f" at {step.url}" if step.url else ""
        check = f" How to tell it worked: {step.verify}" if step.verify else ""
        rows.append(
            _review_row(
                f"change-{index}-step-{number}",
                f"Setup step {number} for {proposed.display_name}, done by {who}{link}: \"{step.title}\". "
                f"{step.instructions}{check}",
                flags=("setup_steps",),
            )
        )
    return rows


def _description_rows(index: int, proposed: ToolDefinitionV1, diff: ToolChangeDiff) -> list[ReviewItemV1]:
    if not proposed.description:
        return [
            _review_row(
                f"change-{index}-details-description",
                f"Details changed for {diff.proposed_name} ({diff.tool_id}): description cleared.",
            )
        ]
    prefix = f"Description updated for {diff.proposed_name} ({diff.tool_id}) (part {{part}}/{{total}}): "
    chunk_size = _ROW_TEXT_LIMIT - len(prefix.format(part=99, total=99))
    chunks = [
        proposed.description[offset : offset + chunk_size]
        for offset in range(0, len(proposed.description), chunk_size)
    ]
    total = len(chunks)
    return [
        _review_row(
            f"change-{index}-details-description-{part_index - 1}",
            f"{prefix.format(part=part_index, total=total)}{chunk}",
        )
        for part_index, chunk in enumerate(chunks, start=1)
    ]


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
    combined = f"Details changed for {diff.proposed_name} ({diff.tool_id}): {'; '.join(parts)}."
    if len(combined) <= _ROW_TEXT_LIMIT:
        return [_review_row(f"change-{index}-details", combined)]
    rows: list[ReviewItemV1] = []
    if diff.display_name_changed:
        rows.append(
            _review_row(
                f"change-{index}-details-name",
                f"Details changed for {diff.proposed_name} ({diff.tool_id}): "
                f"display name {current.display_name!r} -> {proposed.display_name!r}.",
            )
        )
    if diff.description_changed:
        rows.extend(_description_rows(index, proposed, diff))
    return rows


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
        _review_row(
            f"change-{index}-settings",
            f"Account and ownership settings changed for {diff.proposed_name} ({diff.tool_id}): "
            f"{'; '.join(parts)}.",
        )
    ]


@dataclass(frozen=True)
class MovedBlock:
    """An active operation block whose route keeps its name but changes method or path."""

    block: GrantV1
    previous_route: HttpRouteV1
    proposed_route: HttpRouteV1


def _active_tool_operation_blocks(
    tool_id: str,
    active_blocks: Iterable[GrantV1],
    *,
    now: datetime | None = None,
) -> list[tuple[GrantV1, str]]:
    """Active, unexpired operation blocks targeting ``tool_id``, in stable review order."""

    prefix = f"{tool_id}#"
    matched: list[tuple[GrantV1, str]] = []
    for block in active_blocks:
        if block.kind is not GrantKind.BLOCK or block.status is not GrantStatus.ACTIVE:
            continue
        if now is not None and block.expires_at_utc is not None and block.expires_at_utc <= now:
            continue
        if block.target.kind is not TargetKind.OPERATION or not block.target.id.startswith(prefix):
            continue
        matched.append((block, block.target.id.partition("#")[2]))
    matched.sort(
        key=lambda pair: (
            pair[1],
            pair[0].subject.kind.value,
            pair[0].subject.id,
            pair[0].grant_id,
        )
    )
    return matched


def widened_blocks(
    current: ToolDefinitionV1,
    proposed: ToolDefinitionV1,
    active_blocks: Iterable[GrantV1],
    *,
    now: datetime | None = None,
) -> list[GrantV1]:
    """Active operation blocks on ``current`` whose operation ``proposed`` removes (so they widen to the whole tool)."""

    return [
        block
        for block, operation in _active_tool_operation_blocks(proposed.tool_id, active_blocks, now=now)
        if current.operation(operation) is not None and proposed.operation(operation) is None
    ]


def moved_blocks(
    current: ToolDefinitionV1,
    proposed: ToolDefinitionV1,
    active_blocks: Iterable[GrantV1],
    *,
    now: datetime | None = None,
) -> list[MovedBlock]:
    """Active operation blocks whose route keeps its name in ``proposed`` but changes method or path."""

    current_routes = {f"route:{route.name}": route for route in current.routes}
    proposed_routes = {f"route:{route.name}": route for route in proposed.routes}
    moved: list[MovedBlock] = []
    for block, operation in _active_tool_operation_blocks(proposed.tool_id, active_blocks, now=now):
        old_route = current_routes.get(operation)
        new_route = proposed_routes.get(operation)
        if old_route is None or new_route is None:
            continue
        if old_route.method != new_route.method or old_route.path != new_route.path:
            moved.append(MovedBlock(block=block, previous_route=old_route, proposed_route=new_route))
    return moved


def _widened_block_rows(
    index: int,
    current: ToolDefinitionV1,
    proposed: ToolDefinitionV1,
    diff: ToolChangeDiff,
    active_blocks: Iterable[GrantV1],
) -> list[ReviewItemV1]:
    """One review row per active one-operation block that a route removal or rename widens to the whole tool."""

    rows: list[ReviewItemV1] = []
    for block_index, block in enumerate(widened_blocks(current, proposed, active_blocks)):
        who = subject_name(block.subject)
        operation = block.target.id.partition("#")[2]
        rows.append(
            _review_row(
                f"change-{index}-widened-block-{block_index}",
                f"Block {block.grant_id} on {diff.proposed_name} ({diff.tool_id}) widens to the whole tool: "
                f"it stopped {who} from {operation}, which this change removes. Once approved, it will block "
                f"all of {diff.tool_id} for {who} until an administrator replaces or removes it.",
                flags=("block_widened",),
            )
        )
    return rows


def _moved_block_rows(
    index: int,
    current: ToolDefinitionV1,
    proposed: ToolDefinitionV1,
    diff: ToolChangeDiff,
    active_blocks: Iterable[GrantV1],
) -> list[ReviewItemV1]:
    """One review row per active one-operation block whose route keeps its name but changes method or path."""

    rows: list[ReviewItemV1] = []
    for block_index, item in enumerate(moved_blocks(current, proposed, active_blocks)):
        who = subject_name(item.block.subject)
        operation = item.block.target.id.partition("#")[2]
        old_route = item.previous_route
        new_route = item.proposed_route
        rows.append(
            _review_row(
                f"change-{index}-moved-block-{block_index}",
                f"Block {item.block.grant_id} on {diff.proposed_name} ({diff.tool_id}) now covers "
                f"{new_route.method} {new_route.path} instead of {old_route.method} {old_route.path} for {who}: "
                f"it targets {operation}, whose method or path changes. Once approved, this block will no longer "
                f"stop {old_route.method} {old_route.path}.",
                flags=("block_moved",),
            )
        )
    return rows


def tool_change_rows(
    index: int,
    *,
    current: ToolDefinitionV1,
    proposed: ToolDefinitionV1,
    active_links: Iterable[AccountLinkV1] = (),
    active_blocks: Iterable[GrantV1] = (),
) -> tuple[ToolChangeDiff, list[ReviewItemV1]]:
    """Compute the diff and trusted-code review rows for one ``tool_changes`` item."""

    diff = diff_tool_definition(current, proposed)
    if not diff.has_changes:
        return diff, []
    impact = assess_link_impact(proposed, active_links)
    rows: list[ReviewItemV1] = [
        _overview_row(index, diff),
        *_host_rows(index, diff, impact),
        *_route_rows(index, current, proposed, diff),
        *_widened_block_rows(index, current, proposed, diff, active_blocks),
        *_moved_block_rows(index, current, proposed, diff, active_blocks),
        *_credential_rows(index, current, proposed, diff),
        *_relink_row(index, diff, impact),
        *_setup_rows(index, proposed, diff),
        *_details_row(index, current, proposed, diff),
        *_policy_row(index, current, proposed, diff),
    ]
    return diff, rows


__all__ = [
    "LinkImpact",
    "MovedBlock",
    "ToolChangeDiff",
    "assess_link_impact",
    "describe_injections",
    "diff_tool_definition",
    "moved_blocks",
    "tool_change_rows",
    "widened_blocks",
]
