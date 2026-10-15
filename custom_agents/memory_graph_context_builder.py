"""Render bounded graph-backed memory context for prompt injection."""

from __future__ import annotations

from typing import Any, Dict, List


SECTION_LABELS = {
    "preferences": "User preference",
    "communication_style": "User communication style",
    "standing_decisions": "User standing decision",
    "stable_facts": "User stable fact",
    "accepted_decisions": "Team decision",
    "conventions": "Team convention",
    "durable_lessons": "Durable lesson",
    "known_constraints": "Agent constraint",
}


def render_memory_graph_markdown(result: Dict[str, Any], *, max_chars: int = 2200, max_claims: int = 8) -> str:
    claims: List[Dict[str, Any]] = list(result.get("claims") or [])[: max(1, max_claims)]
    if not claims:
        return ""

    lines = ["## Retrieved Memory Facts"]
    current_length = len("\n".join(lines))
    evidence_by_claim = dict(result.get("evidence_by_claim_id") or {})
    episodes_by_id = dict(result.get("episodes_by_id") or {})

    for claim in claims:
        metadata = dict(claim.get("metadata") or {})
        section = str(metadata.get("section") or "").strip()
        label = SECTION_LABELS.get(section, "Retrieved memory")
        if section == "durable_lessons":
            subject_kind = str(metadata.get("subject_kind") or "").strip()
            if subject_kind == "agent_role":
                label = "Agent lesson"
            elif subject_kind == "team":
                label = "Team lesson"
        primary = f"- {label}: {claim.get('fact_text')}"
        block = [primary]

        evidence_rows = list(evidence_by_claim.get(claim.get("claim_id")) or [])
        if evidence_rows:
            first_evidence = evidence_rows[0]
            episode = episodes_by_id.get(first_evidence.get("episode_id")) or {}
            conversation_id = episode.get("conversation_id")
            turn_index = episode.get("turn_index")
            detail = f"Evidence: {first_evidence.get('evidence_kind')}."
            if conversation_id and turn_index is not None:
                detail = f"Evidence: {first_evidence.get('evidence_kind')} in conversation {conversation_id}, turn {turn_index}."
            block.append(f"  {detail}")

        if claim.get("valid_from_utc"):
            block.append(f"  Valid from: {claim.get('valid_from_utc')}")

        candidate = "\n".join(lines + block)
        if len(candidate) > max_chars and len(lines) > 1:
            break
        if len(candidate) > max_chars:
            return ""
        lines.extend(block)
        current_length = len("\n".join(lines))
        if current_length >= max_chars:
            break

    return "\n".join(lines).strip()
