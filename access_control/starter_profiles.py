"""Starter profiles: the team lead's standing access for one person's work (access plan section 9.5).

A profile is a short list of grants to the team lead's agent type, given by a
person within their own authority, so they sit at the root of every hand-down
chain the team lead makes. The grants serve that person's work only (decision
D13). Applying a profile first revokes the grants an earlier profile made for
the same person (recognized by their reason), so switching profiles never
leaves both in place; grants people gave by hand are untouched. Applying the
profile already in place changes nothing unless its grants for that person
differ (a grant marked ``if_person_may_give`` follows what the person may give
now, such as model choice after they became an administrator).

Each choice is recorded with its source, because "manual" leaves no grants to
recognize. ``authorization.starter_profile`` is the first user's default: the
gateway applies it at start (``CHOSEN_IN_CONFIGURATION``) until that person
chooses one themselves (``CHOSEN_BY_PERSON``), and from then on their choice
stays across restarts. Every other person chooses their own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml

from access_control.decisions import AccessService
from access_control.entities import PersonPrincipal, agent_type_id
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    DelegationMode,
    GrantStatus,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)

logger = logging.getLogger(__name__)

PROFILES_FILE = Path(__file__).resolve().parents[1] / "ua_definitions" / "access" / "starter_profiles.yaml"
REASON_PREFIX = "Starter profile: "
CHOSEN_BY_PERSON = "person"
CHOSEN_IN_CONFIGURATION = "configuration"


@dataclass(frozen=True)
class ProfileGrant:
    target: TargetRefV1  # a tool set or one tool
    level: AccessLevel
    delegation_depth: int
    delegation_mode: DelegationMode
    # Given only when the person may give it themselves (the access service
    # decides), left out otherwise; e.g. model choice, which only
    # administrators hold.
    if_person_may_give: bool = False


@dataclass(frozen=True)
class StarterProfile:
    name: str
    display_name: str
    description: str
    grants: tuple[ProfileGrant, ...]


@lru_cache(maxsize=1)
def starter_profiles() -> dict[str, StarterProfile]:
    data = yaml.safe_load(PROFILES_FILE.read_text(encoding="utf-8")) or {}
    profiles: dict[str, StarterProfile] = {}
    for name, raw in (data.get("profiles") or {}).items():
        profiles[str(name)] = StarterProfile(
            name=str(name),
            display_name=str(raw["display_name"]),
            description=" ".join(str(raw.get("description") or "").split()),
            grants=tuple(_profile_grant(item) for item in raw.get("grants") or ()),
        )
    return profiles


def _profile_grant(item: dict) -> ProfileGrant:
    if bool(item.get("tool_set")) == bool(item.get("tool")):
        raise ValueError(f"A starter profile grant names exactly one tool_set or tool: {item!r}")
    target = (
        TargetRefV1(kind=TargetKind.TOOL_SET, id=str(item["tool_set"]))
        if item.get("tool_set")
        else TargetRefV1(kind=TargetKind.TOOL, id=str(item["tool"]))
    )
    return ProfileGrant(
        target=target,
        level=AccessLevel(str(item["level"])),
        delegation_depth=int(item.get("delegation_depth") or 0),
        delegation_mode=DelegationMode(str(item.get("delegation_mode") or "automatic")),
        if_person_may_give=bool(item.get("if_person_may_give")),
    )


def _profile_grants(service: AccessService, lead_subject: SubjectRefV1, person_id: str) -> list[GrantV1]:
    return [
        grant
        for grant in service.store.for_subjects(service.team_id, [lead_subject], statuses=(GrantStatus.ACTIVE,))
        if grant.reason.startswith(REASON_PREFIX) and grant.for_person_id == person_id
    ]


def _wanted_grants(
    service: AccessService, person: PersonPrincipal, profile: StarterProfile, lead: SubjectRefV1, moment: datetime
) -> list[GrantV1]:
    """The profile's grants for this person's work, leaving out those they may not give."""

    actor = SubjectRefV1(kind=SubjectKind.USER, id=person.person_id)
    wanted: list[GrantV1] = []
    for item in profile.grants:
        if service.person_owned(item.target.id):
            # A person's own tool (such as their history clean-up) is opened
            # only by an explicit choice of that person, never by a profile.
            raise ValueError(f"Starter profile {profile.name} names {item.target.id}, which only its person opens.")
        spec = GrantV1(
            grant_id="new",
            team_id=service.team_id,
            subject=lead,
            target=item.target,
            level=item.level,
            delegation_depth=item.delegation_depth,
            delegation_mode=item.delegation_mode,
            granted_by=actor,
            reason=f"{REASON_PREFIX}{profile.name}",
            created_at_utc=moment,
            for_person_id=person.person_id,
        )
        if item.if_person_may_give and service.grant_refusal(person, spec) is not None:
            logger.info(
                "A starter profile grant was left out: the person may not give it. "
                "event=starter_profile_grant_left_out profile=%s person_id=%s target=%s",
                profile.name,
                person.person_id,
                item.target.id,
            )
            continue
        wanted.append(spec)
    return wanted


def _grant_key(grant: GrantV1) -> tuple[str, str, str, int, str]:
    """What makes a profile grant the same one (ids, times and which profile gave it aside).

    The profile name (in the reason) is left out so a grant two profiles share
    stays when the person switches; it keeps the reason it was first given with.
    """

    return (
        grant.target.kind.value,
        grant.target.id,
        grant.level.value,
        grant.delegation_depth,
        grant.delegation_mode.value,
    )


def applied_profile(service: AccessService, person_id: str) -> str | None:
    """The profile chosen for one person's work, or None when none was chosen yet."""

    choice = service.store.profile_choice(service.team_id, person_id)
    return choice[0] if choice else None


def chosen_by_person(service: AccessService, person_id: str) -> bool:
    """True when the person chose their profile themselves, so the configured default no longer applies."""

    choice = service.store.profile_choice(service.team_id, person_id)
    return choice is not None and choice[1] == CHOSEN_BY_PERSON


def apply_starter_profile(
    service: AccessService,
    person: PersonPrincipal,
    name: str,
    *,
    lead_agent_type: str,
    source: str = CHOSEN_BY_PERSON,
    now: datetime | None = None,
) -> list[GrantV1]:
    """Give the team lead the profile's grants and record the choice; returns the grants now in place."""

    profile = starter_profiles().get(name)
    if profile is None:
        raise ValueError(f"Unknown starter profile {name!r}; choose one of {', '.join(sorted(starter_profiles()))}.")
    lead = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(service.team_id, lead_agent_type))
    moment = now or service.clock()
    wanted = _wanted_grants(service, person, profile, lead, moment)
    existing = _profile_grants(service, lead, person.person_id)
    wanted_keys = {_grant_key(spec) for spec in wanted}
    existing_keys = {_grant_key(grant) for grant in existing}
    # Grants both profiles give stay, so what the team lead handed on from
    # them is not cut off. A wanted grant that other access already covers
    # (one entry per tool, access_control.grant_entries) is not given again,
    # so the check at every startup changes nothing.
    stale = [grant for grant in existing if _grant_key(grant) not in wanted_keys]
    kept = [grant for grant in existing if _grant_key(grant) in wanted_keys]
    missing = [spec for spec in wanted if _grant_key(spec) not in existing_keys]
    missing = [spec for spec in missing if spec.grant_id not in service.plan_grants([spec], ending=[g.grant_id for g in stale]).covered]
    if not stale and not missing:
        service.store.record_profile_choice(service.team_id, person.person_id, profile.name, source=source, at=moment)
        return existing
    actor = SubjectRefV1(kind=SubjectKind.USER, id=person.person_id)
    for grant in stale:
        service.revoke(grant.grant_id, actor=actor)
    created = kept + [service.grant_by_person(person, spec) for spec in missing]
    service.store.record_profile_choice(service.team_id, person.person_id, profile.name, source=source, at=moment)
    logger.info(
        "Starter profile applied. event=starter_profile_applied profile=%s team_id=%s by=%s source=%s grants=%s",
        profile.name,
        service.team_id,
        person.person_id,
        source,
        len(created),
    )
    return created


__all__ = [
    "CHOSEN_BY_PERSON",
    "CHOSEN_IN_CONFIGURATION",
    "REASON_PREFIX",
    "StarterProfile",
    "applied_profile",
    "apply_starter_profile",
    "chosen_by_person",
    "starter_profiles",
]
