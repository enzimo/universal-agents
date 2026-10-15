"""Keys and sharing choices from onboarding take effect at gateway start.

Onboarding (web and ``--base-env-file`` imports) asks for the Exa and Brave
Search keys, and (web) whether each service's account and the model keys are
shared with people the administrator adds (``access_control.account_sharing``,
owner's decisions 2026-10-08). The keys belong in the credential store as
linked accounts for ``http:exa`` and ``http:brave-search``, never in
``team.env`` or the environment, and the choices in the access store.
Onboarding cannot store either itself: the secure tool broker, which owns the
credential store, is not running yet, and the choices need the person's team
roles, which only the running gateway knows. So:

1. Onboarding writes them to a private hand-over file
   (``InstanceLayout.onboarding_account_keys_path`` under ``state/``, which no
   agent can reach), 0600 and atomically, with the person who onboarded. This
   happens in both ``launch.secrets_source`` modes.
2. At start the gateway (``OnboardingAccountKeyHandover``) looks up that
   person's current team roles. It records the sharing choices when the
   person is an administrator, once: they leave the file at once, so a
   restart never undoes a later ``/accounts`` change. This needs no broker.
3. It then sends the keys on the broker's gateway-only route
   (``OnboardingAccountKeysV1``), each for the team when the team shares the
   service and as the person's own when it keeps it private (asked of the
   access store, so a choice made since onboarding counts). The broker links
   each one as the account link form does, and stores the value in that
   owner's credential collection.
4. Linked keys leave the file; the file is deleted once nothing is left. A key
   the broker could not link stays in the file for the next start, and every
   failure is logged with an ``event=`` key (never a value). Administrators get
   an Inbox notice for the team's keys, and the person one for their own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from access_catalog.catalog import CatalogSource, ToolCatalog
from access_control.account_sharing import (
    AccountSharingStore,
    SharingNotChosen,
    TeamAccountSharing,
    fixed_sharing_text,
    starting_overview,
)
from access_control.people import PERSON_ROLE_ADMIN, account_sharing_refusal
from assistant_gateway.application.ports import GatewaySecureBroker, GatewaySecureBrokerRefusal
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    AccessNoticeV1,
    OnboardingAccountKeysResultV1,
    OnboardingAccountKeysV1,
    OnboardingAccountKeyV1,
    OnboardingModelKeyV1,
    AccountSharing,
    LinkOwnerKind,
    MODEL_KEYS_SHARING_SUBJECT,
    ToolDefinitionV1,
)
from id_generation import new_id
from model_providers.endpoints import PROVIDER_ENDPOINT_KINDS, WELL_KNOWN_PROVIDERS, EndpointKind
from runtime_settings.instance_layout import InstanceLayout
from secure_tools.chat_commands import link_command

LOGGER = logging.getLogger(__name__)

HANDOVER_SCHEMA_VERSION = 1
# Waits before each try while the broker starts next to the gateway.
DEFAULT_RETRY_DELAYS_SECONDS = (0.0, 5.0, 10.0, 20.0, 40.0, 60.0)


@dataclass(frozen=True)
class OnboardingKeyField:
    """One key onboarding asks for: its answer, the tool it links, and its former environment name."""

    answer_key: str
    tool_id: str
    label: str
    former_env_name: str


# The keys onboarding asks for. The former environment names let an imported
# env file (a base env file or an upload) hand its keys over too, instead of
# carrying them into team.env, where startup would refuse them
# (runtime_settings.services.REMOVED_LINKED_KEY_ENV_VARS).
ONBOARDING_KEY_FIELDS: tuple[OnboardingKeyField, ...] = (
    OnboardingKeyField("exa_api_key", "http:exa", "Exa", "EXA_API_KEY"),
    OnboardingKeyField("brave_search_api_key", "http:brave-search", "Brave", "BRAVE_SEARCH_API_KEY"),
)


class OnboardingAccountKeysFileV1(BaseModel):
    """The hand-over file: who onboarded, tool keys by tool id, model keys by address, sharing choices.

    Model keys (the provider key onboarding tested, the embedding key, the
    xAI key for voice) become the team's keys for ``/auth model`` instead of
    ``team.env`` lines, which would override every key saved later.
    ``sharing`` holds the administrator's choices by subject (tool ids and
    ``model_keys``) until the gateway records them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = HANDOVER_SCHEMA_VERSION
    team_id: str = Field(min_length=1)
    person_id: str = Field(min_length=1)
    keys: dict[str, str] = Field(default_factory=dict, repr=False)
    model_keys: dict[str, OnboardingModelKeyV1] = Field(default_factory=dict, repr=False)
    sharing: dict[str, AccountSharing] = Field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.keys and not self.model_keys and not self.sharing


def keys_from_answers(answers: Mapping[str, Any]) -> dict[str, str]:
    """The onboarding keys the person typed, by tool id (empty answers left out)."""

    return {
        field.tool_id: value
        for field in ONBOARDING_KEY_FIELDS
        if (value := str(answers.get(field.answer_key) or "").strip())
    }


def onboarding_sharing_services() -> list[dict[str, Any]]:
    """The Secrets page's rows: every shipped service that uses an account, with the sharing it starts with.

    Rows come from the tool catalog, never a list in the page script, so a
    new service appears on the page by itself. A fixed row carries the plain
    reason it cannot change.
    """

    tools = CatalogSource(None).current().tools
    return [
        {
            "subject": row.subject,
            "display_name": row.display_name,
            "description": tools[row.subject].description,
            "sharing": row.sharing.value,
            "fixed": row.fixed,
            "note": fixed_sharing_text(tools[row.subject]) if row.fixed else "",
        }
        for row in starting_overview(tools.values())
    ]


def sharing_from_answers(answers: Mapping[str, Any]) -> dict[str, AccountSharing]:
    """The sharing choices onboarding checked (``normalize_onboarding_answers``), by subject."""

    raw = answers.get("account_sharing") or {}
    return {str(subject): AccountSharing(value) for subject, value in dict(raw).items()}


def model_keys_from_answers(config: Any, answers: Mapping[str, Any]) -> tuple[OnboardingModelKeyV1, ...]:
    """The model keys typed in onboarding, for the endpoints the new configuration uses.

    ``model_token`` is the selected provider's key (tested on the page before
    the person could finish); ``embedding_api_key`` belongs to
    ``embedding.api_base``; ``xai_api_key`` is the xAI key Grok voice uses.
    A key whose endpoint the configuration does not name is logged and left out.
    """

    keys: list[OnboardingModelKeyV1] = []
    token = str(answers.get("model_token") or "").strip()
    kind = PROVIDER_ENDPOINT_KINDS.get(str(config.llm.provider or "").strip().lower())
    if token and kind is not None:
        base_url = config.llm.litellm_api_base if kind is EndpointKind.OPENAI_COMPATIBLE else WELL_KNOWN_PROVIDERS[kind].base_url
        _add_model_key(keys, base_url, kind, str(config.llm.model_id or ""), token, what="model")
    embedding_key = str(answers.get("embedding_api_key") or "").strip()
    if embedding_key:
        _add_model_key(
            keys, config.embedding.api_base, EndpointKind.OPENAI_COMPATIBLE, str(config.embedding.model_id or ""),
            embedding_key, what="embedding",
        )
    xai_key = str(answers.get("xai_api_key") or "").strip()
    if xai_key:
        _add_model_key(keys, WELL_KNOWN_PROVIDERS[EndpointKind.XAI].base_url, EndpointKind.XAI, "", xai_key, what="xai")
    return tuple(keys)


def _add_model_key(
    keys: list[OnboardingModelKeyV1], base_url: Any, kind: EndpointKind, model_id: str, value: str, *, what: str
) -> None:
    try:
        keys.append(OnboardingModelKeyV1(address=str(base_url or ""), kind=kind, model_id=model_id, value=value))
    except ValidationError:
        LOGGER.warning(
            "An onboarding key has no usable endpoint address in the configuration and was not kept. "
            "event=onboarding_model_key_without_endpoint key=%s",
            what,
        )


def keys_from_env_values(values: Mapping[str, str]) -> dict[str, str]:
    """The onboarding keys in an imported env file, by tool id."""

    return {
        field.tool_id: value
        for field in ONBOARDING_KEY_FIELDS
        if (value := str(values.get(field.former_env_name) or "").strip())
    }


def _layout(instance_root: str | Path) -> InstanceLayout:
    return InstanceLayout.at(instance_root)


def read_handover(instance_root: str | Path) -> OnboardingAccountKeysFileV1 | None:
    """The waiting hand-over, or None when there is none or it cannot be read (logged)."""

    path = _layout(instance_root).onboarding_account_keys_path
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        LOGGER.error(
            "The onboarding key hand-over file cannot be opened. event=onboarding_keys_file_unreadable "
            "path=%s error=%s",
            path,
            type(exc).__name__,
        )
        return None
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return OnboardingAccountKeysFileV1.model_validate(json.load(handle))
    except (OSError, ValueError, ValidationError) as exc:
        LOGGER.error(
            "The onboarding key hand-over file is not valid; it is kept for inspection. "
            "event=onboarding_keys_file_invalid path=%s error=%s",
            path,
            type(exc).__name__,
        )
        return None


def _write_atomically(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` as a new 0600 file that replaces the old one in one step."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_handover(
    instance_root: str | Path,
    *,
    team_id: str,
    person_id: str,
    keys: Mapping[str, str],
    model_keys: Sequence[OnboardingModelKeyV1] = (),
    sharing: Mapping[str, AccountSharing] | None = None,
) -> Path | None:
    """Add keys and sharing choices to the hand-over file for ``person_id``; the path, or None when there is nothing.

    What already waits for the same person and team is kept unless a new
    value replaces it; a file left by another onboarding is replaced.
    """

    if not keys and not model_keys and not sharing:
        return None
    path = _layout(instance_root).onboarding_account_keys_path
    existing = read_handover(instance_root)
    kept = existing if existing is not None and (existing.team_id, existing.person_id) == (team_id, person_id) else None
    handover = OnboardingAccountKeysFileV1(
        team_id=team_id,
        person_id=person_id,
        keys={**(kept.keys if kept else {}), **keys},
        model_keys={**(kept.model_keys if kept else {}), **{key.address: key for key in model_keys}},
        sharing={**(kept.sharing if kept else {}), **dict(sharing or {})},
    )
    _write_atomically(path, handover.model_dump_json())
    LOGGER.info(
        "Onboarding keys and choices wait for the gateway. event=onboarding_keys_handover_written "
        "tool_ids=%s model_key_addresses=%s sharing_subjects=%s person_id=%s",
        ",".join(sorted(handover.keys)),
        ",".join(sorted(handover.model_keys)),
        ",".join(sorted(handover.sharing)),
        person_id,
    )
    return path


def _save(instance_root: str | Path, handover: OnboardingAccountKeysFileV1) -> None:
    """Write what is left of the hand-over, or delete the file when nothing is."""

    path = _layout(instance_root).onboarding_account_keys_path
    if handover.empty:
        path.unlink(missing_ok=True)
        LOGGER.info("The onboarding key hand-over is done; the file was deleted. event=onboarding_keys_handover_deleted")
        return
    _write_atomically(path, handover.model_dump_json())


def _keep_only(
    instance_root: str | Path,
    handover: OnboardingAccountKeysFileV1,
    tool_ids: Sequence[str],
    addresses: Sequence[str] = (),
) -> None:
    """Leave only the keys of ``tool_ids`` and model keys of ``addresses`` in the file, or delete it."""

    remaining = {tool: value for tool, value in handover.keys.items() if tool in set(tool_ids)}
    remaining_models = {address: key for address, key in handover.model_keys.items() if address in set(addresses)}
    _save(instance_root, handover.model_copy(update={"keys": remaining, "model_keys": remaining_models}))


def _labels(tool_ids: Sequence[str]) -> str:
    names = [field.label for field in ONBOARDING_KEY_FIELDS if field.tool_id in tool_ids]
    names += [tool for tool in tool_ids if tool not in {field.tool_id for field in ONBOARDING_KEY_FIELDS}]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def linked_notice(team_id: str, tool_ids: Sequence[str], *, now: datetime) -> AccessNoticeV1:
    """The administrators' Inbox notice for keys that were linked."""

    several = len(tool_ids) > 1
    title = f"{_labels(tool_ids)} {'keys' if several else 'key'} from onboarding {'were' if several else 'was'} linked for the team"
    commands = ", ".join(link_command(tool, LinkOwnerKind.TEAM) for tool in tool_ids)
    summary = (
        f"The {'keys' if several else 'key'} typed during onboarding {'are' if several else 'is'} now in the "
        f"credential store as the team's {'accounts' if several else 'account'} for {', '.join(tool_ids)}. "
        "Agents use them for web search unless a person links their own. "
        f"To change one, use {commands}."
    )
    return AccessNoticeV1(
        notice_id=new_id("access_notice"),
        team_id=team_id,
        kind=AccessNoticeKind.ONBOARDING_KEYS_LINKED,
        recipient_roles=(PERSON_ROLE_ADMIN,),
        title=title,
        summary=summary,
        created_at_utc=now,
    )


def model_keys_saved_notice(team_id: str, addresses: Sequence[str], *, now: datetime) -> AccessNoticeV1:
    """The administrators' Inbox notice for model keys from onboarding that the broker saved."""

    hosts = ", ".join(address.split("://", 1)[-1] for address in addresses)
    return AccessNoticeV1(
        notice_id=new_id("access_notice"),
        team_id=team_id,
        kind=AccessNoticeKind.ONBOARDING_KEYS_LINKED,
        recipient_roles=(PERSON_ROLE_ADMIN,),
        title=f"Model keys from onboarding were saved for the team ({hosts})",
        summary=(
            f"The model keys typed and tested during onboarding are now the team's keys for {hosts}. "
            "Agents use them without a restart. To replace or remove one, use /auth model."
        ),
        created_at_utc=now,
    )


def own_keys_notice(team_id: str, person_id: str, tool_ids: Sequence[str], *, now: datetime) -> AccessNoticeV1:
    """The onboarded person's Inbox notice for keys saved as their own account (the team keeps the service private)."""

    several = len(tool_ids) > 1
    commands = ", ".join(link_command(tool) for tool in tool_ids)
    return AccessNoticeV1(
        notice_id=new_id("access_notice"),
        team_id=team_id,
        kind=AccessNoticeKind.ONBOARDING_KEYS_LINKED,
        recipient_person_id=person_id,
        title=f"{_labels(tool_ids)} {'keys' if several else 'key'} from onboarding {'were' if several else 'was'} saved as your own",
        summary=(
            f"You made {'these services' if several else 'this service'} private, so the "
            f"{'keys' if several else 'key'} typed during onboarding {'are' if several else 'is'} your own "
            f"{'accounts' if several else 'account'} for {', '.join(tool_ids)}: agents use "
            f"{'them' if several else 'it'} only for your work, and people you add link their own. "
            f"To change one, use {commands}; /accounts shows whose accounts agents use."
        ),
        created_at_utc=now,
    )


class OnboardingAccountKeyHandover:
    """At gateway start, record the onboarding sharing choices and hand the waiting keys to the broker."""

    def __init__(
        self,
        *,
        instance_root: str | Path,
        team_id: str,
        broker: GatewaySecureBroker,
        roles_of: Callable[[str], tuple[str, ...] | None],
        send_notice: Callable[[AccessNoticeV1], None],
        store: Callable[[], AccountSharingStore],
        catalog: Callable[[], ToolCatalog],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._root = instance_root
        self._team_id = team_id
        self._broker = broker
        self._roles_of = roles_of
        self._send_notice = send_notice
        self._store = store
        self._catalog = catalog
        self._now = now
        self._retry_delays = tuple(retry_delays)
        self._logger = logger or LOGGER

    def _roles(self, handover: OnboardingAccountKeysFileV1) -> tuple[str, ...] | None:
        """The onboarded person's current roles, or None (logged) when nothing can be done for them now."""

        if handover.team_id != self._team_id:
            self._logger.error(
                "The onboarding keys are for another team; they were not linked. "
                "event=onboarding_keys_team_mismatch file_team_id=%s team_id=%s",
                handover.team_id,
                self._team_id,
            )
            return None
        roles = self._roles_of(handover.person_id)
        if not roles:
            self._logger.error(
                "The person who onboarded is not an active team member, so their keys and choices wait. "
                "event=onboarding_keys_person_inactive person_id=%s",
                handover.person_id,
            )
            return None
        return tuple(roles)

    def _sharing(self) -> TeamAccountSharing:
        return TeamAccountSharing(self._store(), self._team_id, now=self._now)

    def _record_sharing(
        self, handover: OnboardingAccountKeysFileV1, roles: tuple[str, ...]
    ) -> OnboardingAccountKeysFileV1 | None:
        """Record the onboarding choices once and take them out of the file; None (logged) when they could not be.

        Keys wait while the choices cannot be recorded, so a key meant to be
        private is never linked for the team.
        """

        if not handover.sharing:
            return handover
        refusal = account_sharing_refusal(actor_roles=roles)
        if refusal is not None:
            self._logger.error(
                "The onboarding sharing choices were not recorded: the person who onboarded is not an "
                "administrator. They stay in the file. event=onboarding_sharing_refused person_id=%s",
                handover.person_id,
            )
            return handover
        try:
            sharing = self._sharing()
            tools = self._catalog().tools
            for subject, choice in sorted(handover.sharing.items()):
                self._record_one(sharing, tools, subject, choice, handover.person_id)
        except Exception:  # noqa: BLE001 - the access store may be unavailable; everything waits for the next start
            self._logger.exception(
                "The onboarding sharing choices could not be recorded; they and the keys wait for the next start. "
                "event=onboarding_sharing_not_recorded person_id=%s",
                handover.person_id,
            )
            return None
        self._logger.info(
            "Recorded the onboarding sharing choices. event=onboarding_sharing_recorded subjects=%s person_id=%s",
            ",".join(f"{subject}={choice.value}" for subject, choice in sorted(handover.sharing.items())),
            handover.person_id,
        )
        remaining = handover.model_copy(update={"sharing": {}})
        _save(self._root, remaining)
        return remaining

    def _record_one(
        self,
        sharing: TeamAccountSharing,
        tools: Mapping[str, ToolDefinitionV1],
        subject: str,
        choice: AccountSharing,
        person_id: str,
    ) -> None:
        if subject == MODEL_KEYS_SHARING_SUBJECT:
            sharing.choose_model_keys(choice, by=person_id)
            return
        tool = tools.get(subject)
        if tool is None:
            self._logger.warning(
                "An onboarding sharing choice names a service the catalog does not have; it was skipped. "
                "event=onboarding_sharing_unknown_service subject=%s",
                subject,
            )
            return
        try:
            sharing.choose_tool(tool, choice, by=person_id)
        except SharingNotChosen:
            self._logger.warning(
                "An onboarding sharing choice names a service whose sharing is fixed; it was skipped. "
                "event=onboarding_sharing_fixed_service subject=%s",
                subject,
            )

    def _request(self, handover: OnboardingAccountKeysFileV1, roles: tuple[str, ...]) -> OnboardingAccountKeysV1:
        """The broker request: each key for the team while the team shares the service, otherwise as the person's own."""

        sharing = self._sharing()
        tools = self._catalog().tools

        def owner(tool_id: str) -> LinkOwnerKind:
            tool = tools.get(tool_id)
            if tool is not None and not sharing.team_account_used(tool):
                return LinkOwnerKind.PERSON
            return LinkOwnerKind.TEAM

        return OnboardingAccountKeysV1(
            team_id=self._team_id,
            person_id=handover.person_id,
            person_roles=roles,
            keys=tuple(
                OnboardingAccountKeyV1(tool_id=tool, owner_kind=owner(tool), value=value)
                for tool, value in sorted(handover.keys.items())
            ),
            model_keys=tuple(key for _address, key in sorted(handover.model_keys.items())),
        )

    async def _send(self, request: OnboardingAccountKeysV1, stop_event: asyncio.Event) -> dict[str, Any] | None:
        """Send the request, retrying while the broker starts; None when it never answered or refused."""

        for attempt, delay in enumerate(self._retry_delays, start=1):
            if delay and await _stopped_within(stop_event, delay):
                return None
            try:
                return await self._broker.link_onboarding_account_keys(request)
            except GatewaySecureBrokerRefusal as exc:
                self._logger.error(
                    "The broker refused the onboarding keys; the file is kept for the next start. "
                    "event=onboarding_keys_refused status=%s reason_code=%s",
                    exc.status,
                    exc.reason_code or "-",
                )
                return None
            except Exception as exc:  # noqa: BLE001 - the broker may still be starting
                self._logger.warning(
                    "The broker did not take the onboarding keys yet. event=onboarding_keys_broker_retry "
                    "attempt=%s error_type=%s",
                    attempt,
                    type(exc).__name__,
                )
        self._logger.error(
            "The broker never took the onboarding keys; the file is kept for the next start. "
            "event=onboarding_keys_broker_unreachable attempts=%s",
            len(self._retry_delays),
        )
        return None

    async def run(self, stop_event: asyncio.Event) -> None:
        handover = read_handover(self._root)
        if handover is None:
            return
        roles = self._roles(handover)
        if roles is None:
            return
        recorded = await asyncio.to_thread(self._record_sharing, handover, roles)
        if recorded is None or (not recorded.keys and not recorded.model_keys):
            return
        if not self._broker.available:
            self._logger.error(
                "The secure tool broker is not set up, so the onboarding keys wait in their file. "
                "event=onboarding_keys_broker_unavailable"
            )
            return
        request = await asyncio.to_thread(self._request, recorded, roles)
        payload = await self._send(request, stop_event)
        if payload is None:
            return
        await self._finish(recorded, request, OnboardingAccountKeysResultV1.model_validate(payload))

    async def _finish(
        self, handover: OnboardingAccountKeysFileV1, request: OnboardingAccountKeysV1, result: OnboardingAccountKeysResultV1
    ) -> None:
        """Log failures, keep only what failed, and tell people what was saved."""

        for failure in result.failed:
            self._logger.error(
                "An onboarding key was not linked; it is kept for the next start. event=onboarding_key_not_linked "
                "tool_id=%s reason_code=%s message=%s",
                failure.tool_id,
                failure.reason_code,
                failure.message,
            )
        for failure in result.model_keys_failed:
            self._logger.error(
                "An onboarding model key was not saved; it is kept for the next start. "
                "event=onboarding_model_key_not_saved address=%s reason_code=%s message=%s",
                failure.tool_id,
                failure.reason_code,
                failure.message,
            )
        _keep_only(
            self._root,
            handover,
            [failure.tool_id for failure in result.failed],
            [failure.tool_id for failure in result.model_keys_failed],
        )
        if result.model_keys_saved:
            await asyncio.to_thread(
                self._send_notice, model_keys_saved_notice(self._team_id, result.model_keys_saved, now=self._now())
            )
            self._logger.info(
                "Onboarding model keys were saved for the team. event=onboarding_model_keys_saved addresses=%s "
                "person_id=%s",
                ",".join(result.model_keys_saved),
                handover.person_id,
            )
        owners = {key.tool_id: key.owner_kind for key in request.keys}
        team = [tool for tool in result.linked if owners.get(tool) is LinkOwnerKind.TEAM]
        own = [tool for tool in result.linked if owners.get(tool) is LinkOwnerKind.PERSON]
        if team:
            await asyncio.to_thread(self._send_notice, linked_notice(self._team_id, team, now=self._now()))
            self._logger.info(
                "Onboarding keys were linked for the team. event=onboarding_keys_linked tool_ids=%s person_id=%s",
                ",".join(team),
                handover.person_id,
            )
        if own:
            await asyncio.to_thread(
                self._send_notice, own_keys_notice(self._team_id, handover.person_id, own, now=self._now())
            )
            self._logger.info(
                "Onboarding keys were linked as the person's own. event=onboarding_keys_linked_own tool_ids=%s "
                "person_id=%s",
                ",".join(own),
                handover.person_id,
            )


async def _stopped_within(stop_event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


__all__ = [
    "ONBOARDING_KEY_FIELDS",
    "OnboardingAccountKeyHandover",
    "OnboardingAccountKeysFileV1",
    "OnboardingKeyField",
    "keys_from_answers",
    "keys_from_env_values",
    "linked_notice",
    "onboarding_sharing_services",
    "own_keys_notice",
    "read_handover",
    "sharing_from_answers",
    "write_handover",
]
