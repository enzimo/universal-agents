"""Stable record identities shared by review, selection, and import receipts."""

from typing import Any

from team_transfer.errors import TransferError


def record_key(section: str, raw: dict[str, Any]) -> str:
    if section == "settings":
        return ".".join(raw["path"])
    if section == "operator_secrets":
        return f"{raw['kind']}/{raw['name']}"
    if section == "control":
        return raw["unit"]
    if section == "skills":
        return f"{raw['kind']}/{raw['name']}"
    if section == "team_tools":
        return ",".join(raw["tool_ids"]) or raw["file_name"]
    if section in {"mcp", "access", "objectives", "schedules"}:
        return f"{raw['kind']}/{raw['record_id']}"
    if section == "procedures":
        return raw["definition_id"]
    if section == "people":
        return raw["person_id"]
    if section == "pinned_memory":
        fact = raw["document"]
        return f"{fact['bank']}/{fact.get('person_id') or 'team'}/{fact['memory_id']}"
    if section == "knowledge":
        # The section's own key, so the review's selection and the section's
        # plan and apply name each row the same way.
        from team_transfer.knowledge import KnowledgeTable
        from team_transfer.knowledge import record_key as knowledge_record_key

        return knowledge_record_key(KnowledgeTable(raw["table"]), raw["row"])
    raise TransferError(f"The section {section} has no record-selection contract.")


def keyed_records(
    section: str, records: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    found = {}
    for raw in records:
        key = record_key(section, raw)
        if key in found:
            raise TransferError(
                f"The section {section} carries duplicate record {key}."
            )
        found[key] = raw
    return found


def destination_identity(section: str, raw: dict[str, Any]) -> tuple[str, ...]:
    """The destination record to preserve, independently of its source ID.

    Re-linking an account allocates a new link ID for the same owner and tool.
    Source IDs still identify selections and receipts, but must not let a later
    import replace that owner's newer sign-in.
    """
    if section == "access":
        from custom_messages.contracts.access_v2 import AccountLinkV1
        from team_transfer.access import AccessKind

        if raw["kind"] == AccessKind.ACCOUNT_LINK:
            link = AccountLinkV1.model_validate(raw["document"])
            return (
                AccessKind.ACCOUNT_LINK.value,
                link.team_id,
                link.tool_id,
                link.owner_kind.value,
                link.owner_id,
            )
    return (record_key(section, raw),)
