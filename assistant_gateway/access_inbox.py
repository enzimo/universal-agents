"""The gateway's access Inbox: reviews of waiting access requests, and notices.

Agents store requests and notices in the access store (``access_control``);
the gateway is where people see and decide them:

- **Reviews.** Who decides follows ``AccessRequests.may_decide`` (decision
  D13): a request for work for a person who holds the access is decided only
  by that person; one they lack access for (``person_lacks_access``), system
  work, and staged changes go to whoever could give it (administrators for
  team targets), and a change's owner (a file sent into their folder) may
  decide it too. The person the task works for, and a person who asked,
  also see it. Each card lists every row to approve; grant rows carry a
  "Keep for later tasks" box (off: this task only). Deciding creates the
  grants with the reviewer's own authority, and changes the gateway applies
  at the decision (file deliveries, proposals) are applied then.
- **Announcements.** Each new waiting request is announced once in a chat:
  the task's own conversation when the person there can decide, otherwise the
  instance's proactive session when its owner can. Otherwise it waits in the
  Inbox. A staged change is announced with its one-line summary
  (``headline``); the Inbox keeps the full effect list. The announcement
  carries the request id and its review choices, so Telegram and Discord
  show them as buttons; a press decides here as the linked person
  (``assistant_gateway.application.access_reviews``). Web Chat records the
  request id on the transcript message and shows a card that reads the
  request (``review``) and decides through the same ``decide``. Other
  channels show the Inbox link.
- **One decision at a time.** A decision claims the request first
  (``AccessRequests.decide``), so pressing a button twice, or deciding in
  chat and on the web card at once, decides once; the second press is told
  someone is deciding it. Each maintenance pass recovers claims whose
  decision stopped without finishing (``recover_unfinished_decisions``).
- **Waking the agent.** After a decision the gateway sends
  ``AccessRequestResolvedV1`` to the requesting agent. When the transport is
  down the message is sent by the next maintenance pass
  (``AccessRepository.unsent_resolutions``).
- **Notices** are silent (decision D3): listed here, read-marked per person,
  never pushed to a chat.

The Access page (``assistant_gateway.access_map``) is built here too, on the
same access service and membership lookup, so the gateway runs one Cedar
evaluator for people's decisions.

Nothing here trusts a person's roles from the browser: they come from active
team memberships in the gateway's identity store at each call.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from access_catalog.catalog import CatalogSource, ToolCatalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import PersonPrincipal, agent_role_resolver
from access_control.people import effective_roles
from access_control.inbox import StoredAccessInbox
from access_control.requests import (
    ACCESS_PROPOSAL_CHANGE,
    LEVEL_WORDS,
    SAVED_BEFORE_WORK_IDS,
    AccessRequests,
    ChangeApplier,
    before_work_ids,
)
from assistant_gateway.tool_setup_guide import GatewaySetupGuide
from access_control.grant_entries import lasting_order
from assistant_gateway.access_choice_effects import ChoiceWords, chat_effect_lines, choice_effects, default_variant
from assistant_gateway.access_map import LIFETIME_WORDS as MAP_LIFETIME_WORDS, GatewayAccessMap
from assistant_gateway.access_setup import MembershipRoles
from assistant_gateway.personal_setup import GatewayPersonalSetup, PersonalSetupSteps
from assistant_gateway.application.access_reviews import (
    ACCESS_REVIEW_CHOICE_LABELS,
    WEB_CHANGE_CHOICE_LABELS,
    WEB_PROPOSAL_CHOICE_LABELS,
    WEB_REVIEW_CHOICE_LABELS,
)
from assistant_gateway.application.access_inbox_ports import NoticeQuery
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.domain.message_types import ACCESS_REVIEW_MESSAGE_TYPE
from custom_messages.contracts.access_v2 import (
    AccessRequestKind,
    AccessNoticeKind,
    AccessNoticeV1,
    AccessRequestResolvedV1,
    AccessRequestStatus,
    AccessRequestV1,
    AccessReviewChoice,
    SubjectKind,
    ToolKind,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import RequesterType
from access_control.data_folders import DataFolderRegistry, protected_locations
from assistant_gateway.file_sharing import FILE_DELIVERY_KIND, GatewayFileSharing
from access_control.proposals import ACCESS_PROPOSAL_KIND, ProposalApplier
from access_control.tool_setup_states import ToolSetupStates
from mcp_control.registry import MCPConnectionStore, resolve_mcp_connection_paths
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.services import web_service_urls
from id_generation import new_id
from storage.access_repository import AccessRepository
from storage.config import StorageSettings

MAINTENANCE_INTERVAL_SECONDS = 5.0
LIFETIME_WORDS = {
    "one_time": "one use, in this task",
    "task": "for this task",
    "until": "until a set time",
    "until_revoked": "until someone removes it",
}


@dataclass(frozen=True)
class GatewayAccessInboxSettings:
    team_id: str
    instance_name: str
    response_route: str
    lead_agent_type: str
    inbox_url: str  # the web chat page; the Inbox view is ?view=inbox
    instance_root: Any
    storage_settings: StorageSettings
    cedar_binary: str | None
    # With authorization off the Access page shows only that it is off.
    authorization_enabled: bool
    # Where no data folder may reach (access_control.data_folders).
    protected_locations: tuple[Any, ...] = ()
    # Configured addresses of the instance's own services (the Crawl4AI web tool).
    service_urls: Any = None


class GatewayAccessRequestsSource:
    """The gateway's access service, opened on first use, with the current tool catalog.

    Shared by the Inbox and the ``/access`` chat commands so the gateway runs
    one Cedar evaluator for people's access decisions.
    """

    def __init__(self, settings: GatewayAccessInboxSettings) -> None:
        self._settings = settings
        self._catalogs = CatalogSource(settings.instance_root, service_urls=settings.service_urls)
        self._service: AccessService | None = None
        # Changes the gateway applies when a person approves them (file
        # deliveries, access proposals); see access_control.requests.ChangeApplier.
        self.appliers: dict[str, ChangeApplier] = {}

    def requests(self) -> AccessRequests:
        if self._service is None:
            self._service = AccessService(
                store=AccessRepository(self._settings.storage_settings),
                catalog=self._catalogs.current(),
                cedar=CedarAccessClient(binary=self._settings.cedar_binary),
                team_id=self._settings.team_id,
                agent_role_of=agent_role_resolver(self._settings.lead_agent_type),
                lead_agent_type=self._settings.lead_agent_type,
            )
        self._service.catalog = self._catalogs.current()
        return AccessRequests(
            service=self._service,
            inbox=StoredAccessInbox(self._service.store),
            lead_agent_type=self._settings.lead_agent_type,
            appliers=dict(self.appliers),
        )

    @property
    def catalogs(self) -> CatalogSource:
        return self._catalogs

    def setup_states(self) -> ToolSetupStates:
        """People's last known tool setup states (access_control.tool_setup_states); both modes."""

        return ToolSetupStates(AccessRepository(self._settings.storage_settings))

    def data_folders(self) -> DataFolderRegistry:
        """The team's data folders (decision D16); works in both authorization modes."""

        return DataFolderRegistry(AccessRepository(self._settings.storage_settings), self._settings.team_id)

    def send_notice(self, notice: AccessNoticeV1) -> None:
        """Store a notice for the Inbox; works in both authorization modes (no access service needed)."""

        StoredAccessInbox(AccessRepository(self._settings.storage_settings)).send_notice(notice)

    def send_notice_once(self, notice: AccessNoticeV1) -> bool:
        """Store a notice unless one with its id exists; True when stored (the Status page's problem notices)."""

        return AccessRepository(self._settings.storage_settings).create_notice_if_absent(notice)

    def close(self) -> None:
        if self._service is not None:
            self._service.cedar.close()
            self._service = None


def access_review_url(inbox_url: str | None, request_id: str | None = None) -> str:
    """The web chat's Inbox view, opened at one request when given."""

    base = inbox_url or "/webchat"
    anchor = f"#access_request={quote(request_id, safe='')}" if request_id else ""
    return f"{base}?view=inbox{anchor}"


class GatewayAccessInbox:
    def __init__(
        self,
        *,
        settings: GatewayAccessInboxSettings,
        memberships: MembershipRoles,
        manager_client: Any,
        session_repository: Any,
        proactive_session: Callable[[], Any],
        emit_delivery_event: Callable[[GatewayDeliveryEvent], Awaitable[None]],
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._memberships = memberships
        self._manager_client = manager_client
        self._sessions = session_repository
        self._proactive_session = proactive_session
        self._emit = emit_delivery_event
        self._logger = logger or logging.getLogger(__name__)
        self._source = GatewayAccessRequestsSource(settings)
        self.file_sharing = GatewayFileSharing(
            requests=self.requests,
            person=self.person,
            workspaces_dir=InstanceLayout.at(settings.instance_root).workspaces_dir
            if settings.instance_root
            else Path("workspaces"),
            team_people=lambda: memberships.team_people(settings.team_id),
            display_name=memberships.display_name,
            authorization_enabled=settings.authorization_enabled,
        )
        self._source.appliers[FILE_DELIVERY_KIND] = self.file_sharing
        self.proposals = ProposalApplier(
            requests=self.requests,
            instance_root=Path(settings.instance_root) if settings.instance_root else Path("."),
            catalogs=self._source.catalogs,
            data_folders=self.data_folders,
            protected=tuple(settings.protected_locations),
            person=self.person,
            # Proposals install MCP servers from the instance's registry (plan 7d).
            mcp_store=self._mcp_store if settings.instance_root else None,
        )
        self._source.appliers[ACCESS_PROPOSAL_KIND] = self.proposals
        # Set by the gateway: opens and watches the account link forms an
        # approved proposal asks for (assistant_gateway.application.tool_setup).
        self._tool_setups: Any = None
        # Approved proposals whose decision notice waits until their account
        # link forms end, so the waiting task resumes with the links made.
        # Memory only: after a restart the maintenance loop sends the notice.
        self._held_resolutions: set[str] = set()
        self.access_map = GatewayAccessMap(
            requests=self.requests,
            person=self.person,
            display_name=memberships.display_name,
            lead_agent_type=settings.lead_agent_type,
            authorization_enabled=settings.authorization_enabled,
            file_sharing=self.file_sharing,
            data_folders=self.data_folders,
            protected_locations=tuple(settings.protected_locations),
            schedule_name=self._schedule_name,
        )

    @property
    def tool_setups(self) -> Any:
        return self._tool_setups

    @tool_setups.setter
    def tool_setups(self, service: Any) -> None:
        """The gateway's setup service; it reads step words and workflow links through a guide made here."""

        self._tool_setups = service
        if service is not None:
            service.guide = GatewaySetupGuide(
                catalogs=self._source.catalogs,
                storage_settings=self._settings.storage_settings,
                team_id=self._settings.team_id,
                site_url=self._settings.inbox_url,
            )

    # -- the access service -----------------------------------------------------

    def _schedule_name(self, schedule_id: str) -> str:
        """A scheduled job's readable name for the Access page ("" when it has none or is gone)."""

        from storage.schedule_repository import create_schedule_repository

        try:
            row = create_schedule_repository(self._settings.storage_settings, logger=self._logger).get_schedule_row(
                schedule_id
            )
        except Exception:  # noqa: BLE001 - the card shows the schedule's id instead
            self._logger.warning(
                "Could not read a schedule's name for the Access page. event=access_map_schedule_name_failed "
                "schedule_id=%s",
                schedule_id,
                exc_info=True,
            )
            return ""
        return str(row[2] or "") if row else ""

    def requests(self) -> AccessRequests:
        return self._source.requests()

    def setup_states(self) -> ToolSetupStates:
        """People's last known tool setup states, for the gateway's setup pages."""

        return self._source.setup_states()

    def data_folders(self) -> DataFolderRegistry:
        return self._source.data_folders()

    def send_notice(self, notice: AccessNoticeV1) -> None:
        self._source.send_notice(notice)

    def display_name(self, person_id: str) -> str:
        """The person's name as the gateway knows it; the id when there is none."""

        return self._memberships.display_name(person_id)

    def send_notice_once(self, notice: AccessNoticeV1) -> bool:
        return self._source.send_notice_once(notice)

    def access_store(self) -> AccessRepository:
        """The access store (both authorization modes), for trusted gateway work such as the onboarding hand-over."""

        return AccessRepository(self._settings.storage_settings)

    def catalog(self) -> ToolCatalog:
        """The current tool catalog (built-in tools, the instance's web API tools and MCP connections)."""

        return self._source.catalogs.current()

    @functools.cached_property
    def personal_setup(self) -> GatewayPersonalSetup:
        """A newly added person's first-time setup (``assistant_gateway.personal_setup``) on this access store.

        Built on first use: it needs the identity repository's event log,
        which only the gateway's own memberships carry.
        """

        return GatewayPersonalSetup(
            steps=PersonalSetupSteps(store=self.access_store, catalog=self.catalog, team_id=self._settings.team_id),
            events=self._memberships.user_events,
            emit=self._emit,
        )

    def _mcp_store(self) -> MCPConnectionStore:
        return MCPConnectionStore(resolve_mcp_connection_paths(self._settings.instance_root))

    @property
    def protected_locations(self) -> tuple[Any, ...]:
        return self._settings.protected_locations

    def close(self) -> None:
        self._source.close()

    def person(self, person_id: str) -> PersonPrincipal | None:
        roles = self._memberships.roles_of(person_id, self._settings.team_id)
        return PersonPrincipal(person_id, self._settings.team_id, roles) if roles else None

    @staticmethod
    def _can_decide(requests: AccessRequests, person: PersonPrincipal, request: AccessRequestV1) -> bool:
        return requests.may_decide(person, request)

    # -- presenting -------------------------------------------------------------

    def review_url(self, request_id: str | None = None) -> str:
        return access_review_url(self._settings.inbox_url, request_id)

    @staticmethod
    def title(requests: AccessRequests, request: AccessRequestV1) -> str:
        if request.change is not None:
            if request.requested_by_person_id:
                return request.change.title
            return f"{request.requester_agent_type} asks to make a change: {request.change.title}"
        words = LEVEL_WORDS[request.level]
        name = requests.target_name(request.target)
        recipient = requests.subject_name(request.subject)
        if recipient != request.requester_agent_type:
            return f"{request.requester_agent_type} asks to give {recipient} {words} access to {name}"
        return f"{request.requester_agent_type} asks for {words} access to {name}"

    @classmethod
    def headline(cls, requests: AccessRequests, request: AccessRequestV1) -> str:
        """One line for chat and the chat card: the change's trusted summary, or the access asked for.

        A staged change leads with the summary its staging service wrote from
        the plan (``ChangeProposalV1.summary``); the Inbox keeps the full
        effect list. A person's own request (a file delivery) names them in
        its summary already.
        """

        if request.change is not None:
            if request.requested_by_person_id:
                return request.change.summary
            return f"{request.requester_agent_type} asks for approval: {request.change.summary}"
        return f"{cls.title(requests, request)} ({LIFETIME_WORDS[request.lifetime.value]})."

    def _decided_by_name(self, request: AccessRequestV1) -> str | None:
        decider = request.decided_by
        if decider is None:
            return None
        if decider.kind is SubjectKind.USER:
            return self._memberships.display_name(decider.id)
        return decider.id

    def _work_words(self, request: AccessRequestV1, viewer_id: str) -> str:
        if not request.person_id:
            return "work for nobody (system work)"
        if request.person_id == viewer_id:
            return "your work"
        return f"{self._memberships.display_name(request.person_id)}'s work"

    def review_rows(self, requests: AccessRequests, request: AccessRequestV1) -> list[dict[str, Any]]:
        """Each thing to approve, one row each; grant rows may be kept for later tasks.

        A staged change or proposal brings its own rows. An access request is
        one row; it is keepable when it is for a task and asks for longer than
        the task (without a task the access lasts as asked, with no box).
        """

        if request.change is not None:
            if request.change.items:
                return [item.model_dump(mode="json") for item in request.change.items]
            return [
                {"item_id": f"effect-{index}", "section": "access", "text": line, "flags": [], "keepable": False}
                for index, line in enumerate(request.change.effect)
            ]
        text = (
            f"{requests.subject_name(request.subject)}: {LEVEL_WORDS[request.level]} access to "
            f"{requests.target_name(request.target)}"
        )
        flags = ["full_access"] if request.level.value == "full" else []
        tool = requests.service.catalog.tools.get(request.target.id.partition("#")[0])
        if tool is not None and tool.kind is ToolKind.CODE:
            flags.append("runs_code")
        keepable = bool(request.work_id) and request.lifetime.value in {"until", "until_revoked"}
        # The team lead may be allowed to hand what it asks for on to workers.
        can_hand_on = request.requester_agent_type == self._settings.lead_agent_type
        return [
            {
                "item_id": "access",
                "section": "access",
                "text": text,
                "flags": flags,
                "keepable": keepable,
                "can_hand_on": can_hand_on,
            }
        ]

    @staticmethod
    def effect_lines(requests: AccessRequests, request: AccessRequestV1) -> list[str]:
        """What approving does: a change's effect, or what a code tool lets agents do.

        Code tools say it in their catalog description; for ``code:unconfined``
        that is running code with the runtime's own rights, so a person sees it
        before approving (decision D9).
        """

        if request.change is not None:
            return list(request.change.effect)
        if before_work_ids(request):
            return [SAVED_BEFORE_WORK_IDS]
        lines = GatewayAccessInbox.entry_lines(requests, request)
        tool_id = request.target.id.partition("#")[0]
        tool = requests.service.catalog.tools.get(tool_id)
        if tool is not None and tool.kind is ToolKind.CODE and tool.description:
            lines.append(tool.description)
        return lines

    @staticmethod
    def entry_lines(requests: AccessRequests, request: AccessRequestV1) -> list[str]:
        """What approving does to the agent's access on the same tool (one entry per tool).

        Said only while the request waits: afterwards the entry has changed.
        """

        if request.status is not AccessRequestStatus.WAITING:
            return []
        holder, replaced, kept = requests.access_beside(request)
        if holder is None:
            return []
        recipient = requests.subject_name(holder)
        tool = requests.target_name(request.target)
        lines = []
        if replaced:
            held = max(replaced, key=lasting_order)
            lines.append(
                f"{recipient} now has {LEVEL_WORDS[held.level]} access to {tool} "
                f"{MAP_LIFETIME_WORDS[held.lifetime]}. Approving replaces it."
            )
        if kept:
            held = max(kept, key=lasting_order)
            lines.append(
                f"{recipient} keeps its {LEVEL_WORDS[held.level]} access to {tool} "
                f"{MAP_LIFETIME_WORDS[held.lifetime]}; approving adds this beside it."
            )
        return lines

    @staticmethod
    def review_choices(request: AccessRequestV1, *, web: bool = False) -> list[AccessReviewChoice]:
        """What a person deciding this request may choose.

        The web card has a "Keep for later tasks" box, so it offers Approve,
        Approve reading only and Deny. Chat buttons have no box: "for this
        task" and "keep" are separate buttons there, and a change or proposal
        is approved as a whole.
        """

        more_than_read = request.level.value != "read"
        if before_work_ids(request):
            return [AccessReviewChoice.DENY]
        if request.change is not None:
            approve = AccessReviewChoice.APPROVE if web else AccessReviewChoice.APPROVE_ALL
            return [approve, AccessReviewChoice.DENY]
        if web:
            return [
                choice
                for choice in (AccessReviewChoice.APPROVE, AccessReviewChoice.APPROVE_READ, AccessReviewChoice.DENY)
                if more_than_read or choice is not AccessReviewChoice.APPROVE_READ
            ]
        choices = [AccessReviewChoice.APPROVE_TASK] if request.work_id else []
        choices.append(AccessReviewChoice.APPROVE)
        if more_than_read:
            choices.append(AccessReviewChoice.APPROVE_READ)
        return [*choices, AccessReviewChoice.DENY]

    @staticmethod
    def _web_labels(request: AccessRequestV1) -> dict[str, str]:
        if request.change is None:
            return WEB_REVIEW_CHOICE_LABELS
        return WEB_PROPOSAL_CHOICE_LABELS if request.change.kind == ACCESS_PROPOSAL_CHANGE else WEB_CHANGE_CHOICE_LABELS

    def choice_words(self, requests: AccessRequests, request: AccessRequestV1, viewer_id: str) -> ChoiceWords:
        """The names the effects of this request's choices are said with (``access_choice_effects``)."""

        mine = bool(request.person_id) and request.person_id == viewer_id
        name = self._memberships.display_name(request.person_id) if request.person_id else "this person"
        return ChoiceWords(
            lead=self._settings.lead_agent_type,
            tool=requests.target_name(request.target),
            whose="you" if mine else (name or "this person"),
            person_lacks_you=mine if request.person_lacks_access else None,
        )

    def keep_help(self, request: AccessRequestV1, viewer_id: str) -> str:
        """What the Keep for later tasks box does for this request ("access follows the work")."""

        whose = "you" if request.person_id and request.person_id == viewer_id else (
            self._memberships.display_name(request.person_id) if request.person_id else "this person"
        )
        lead = self._settings.lead_agent_type
        if request.requester_agent_type == lead:
            return f"Off: only for this task. On: {lead} keeps it whenever it works for {whose}, until it is removed."
        if request.schedule_id:
            return "Off: only for this run. On: this scheduled job keeps it for its later runs, until it is removed."
        return (
            f"Off: only for this task. On: {lead} keeps it for {whose} and gives it to {request.requester_agent_type} "
            "and its other workers in later tasks, until it is removed."
        )

    def review_view(
        self, requests: AccessRequests, request: AccessRequestV1, *, can_decide: bool, viewer_id: str = ""
    ) -> dict[str, Any]:
        choices = self.review_choices(request, web=True)
        labels = self._web_labels(request)
        rows = self.review_rows(requests, request)
        return {
            "request_id": request.request_id,
            "kind": request.kind.value,
            "change_kind": request.change.kind if request.change is not None else None,
            "effect": self.effect_lines(requests, request),
            "rows": rows,
            # What each choice does, per state of the card's boxes (access_choice_effects).
            "choice_effects": choice_effects(request, self.choice_words(requests, request, viewer_id)),
            "choice_effects_default": default_variant(request),
            "keep_help": self.keep_help(request, viewer_id) if request.kind is AccessRequestKind.ACCESS else "",
            "kept_item_ids": list(request.kept_item_ids),
            "title": self.title(requests, request),
            "summary": self.headline(requests, request),
            "agent_type": request.requester_agent_type,
            "requested_by": self._memberships.display_name(request.requested_by_person_id)
            if request.requested_by_person_id
            else None,
            "for_whose_work": self._work_words(request, viewer_id),
            "working_for_you": bool(request.person_id) and request.person_id == viewer_id,
            "working_for_name": self._memberships.display_name(request.person_id) if request.person_id else "",
            "recipient": requests.subject_name(request.subject),
            "target": request.target.id,
            "target_name": requests.target_name(request.target),
            "level": request.level.value,
            "level_words": LEVEL_WORDS[request.level],
            "lifetime": request.lifetime.value,
            "lifetime_words": LIFETIME_WORDS[request.lifetime.value],
            "expires_at_utc": request.expires_at_utc.isoformat() if request.expires_at_utc else None,
            "reason": request.reason,
            "working_for": request.person_id,
            "person_lacks_access": request.person_lacks_access,
            "task_id": request.task_id,
            "status": request.status.value,
            "created_at_utc": request.created_at_utc.isoformat(),
            # The stored decision, so a chat card shows it after a reload or a
            # decision made on another device.
            "decision": request.decision.value if request.decision is not None else None,
            "decided_by_name": self._decided_by_name(request),
            "decided_at_utc": request.decided_at_utc.isoformat() if request.decided_at_utc else None,
            "can_decide": can_decide,
            "choices": [{"choice": c.value, "label": labels[c.value]} for c in choices] if can_decide else [],
        }

    @staticmethod
    def notice_view(notice: AccessNoticeV1, read: bool) -> dict[str, Any]:
        return {
            "notice_id": notice.notice_id,
            "kind": notice.kind.value,
            "title": notice.title,
            "summary": notice.summary,
            "grant_id": notice.grant_id,
            "created_at_utc": notice.created_at_utc.isoformat(),
            "read": read,
        }

    # -- reading and deciding (called from web handlers) -------------------------

    def _waiting_reviews(self, requests: AccessRequests, person: PersonPrincipal) -> list[tuple[AccessRequestV1, bool]]:
        """Waiting requests the person may see, each with whether they may decide it."""

        visible = []
        for request in requests.service.store.list_requests(self._settings.team_id, status=AccessRequestStatus.WAITING):
            can_decide = self._can_decide(requests, person, request)
            if can_decide or person.person_id in {request.person_id, request.requested_by_person_id}:
                visible.append((request, can_decide))
        return visible

    def _inbox(self, person_id: str, notices: NoticeQuery) -> dict[str, Any]:
        person = self.person(person_id)
        if person is None:
            return {"reviews": [], "notices": [], "notices_total": 0, "notices_all_total": 0, "unread_notices": 0}
        requests = self.requests()
        reviews = [
            self.review_view(requests, request, can_decide=can_decide, viewer_id=person_id)
            for request, can_decide in self._waiting_reviews(requests, person)
        ]
        page = requests.service.store.notice_page(
            self._settings.team_id,
            person_id=person_id,
            roles=_notice_roles(person),
            unread_only=notices.unread_only,
            offset=notices.offset,
            limit=notices.limit,
        )
        return {
            "reviews": reviews,
            "notices": [self.notice_view(notice, read) for notice, read in page.notices],
            "notices_offset": notices.offset,
            "notices_total": page.total,
            "notices_all_total": page.all_total,
            "unread_notices": page.unread,
        }

    async def inbox(self, person_id: str, notices: NoticeQuery = NoticeQuery()) -> dict[str, Any]:
        return await asyncio.to_thread(self._inbox, person_id, notices)

    def _counts(self, person_id: str) -> dict[str, int]:
        person = self.person(person_id)
        if person is None:
            return {"decidable_requests": 0, "unread_notices": 0}
        requests = self.requests()
        unread = requests.service.store.notice_page(
            self._settings.team_id, person_id=person_id, roles=_notice_roles(person),
            unread_only=True, offset=0, limit=0,
        ).unread
        decidable = sum(1 for _request, can_decide in self._waiting_reviews(requests, person) if can_decide)
        return {"decidable_requests": decidable, "unread_notices": unread}

    async def counts(self, person_id: str) -> dict[str, int]:
        """What the Settings cog's badge counts: requests the person can decide and their unread notices."""

        return await asyncio.to_thread(self._counts, person_id)

    def _review(self, person_id: str, request_id: str) -> dict[str, Any] | None:
        person = self.person(person_id)
        if person is None:
            return None
        requests = self.requests()
        request = requests.service.store.get_request(request_id)
        if request is None or request.team_id != self._settings.team_id:
            return None
        # Who may see one request: the Inbox's rule (whoever may decide it, the
        # person the task works for, and a person who asked), plus whoever
        # decided it, so their chat card keeps showing the outcome.
        decided_by = request.decided_by.id if request.decided_by is not None else None
        linked = person_id in {request.person_id, request.requested_by_person_id, decided_by}
        waiting = request.status is AccessRequestStatus.WAITING
        # The access service (one Cedar evaluator, one decision at a time) is
        # asked only when its answer matters: whether this person may decide a
        # waiting request, or may see one they have no link to. A chat reload
        # reads every card again, and asking for each decided card queued them
        # all behind one another (250 cards took 15 seconds).
        may_decide = self._can_decide(requests, person, request) if waiting or not linked else False
        if not linked and not may_decide:
            return None
        return self.review_view(requests, request, can_decide=may_decide and waiting, viewer_id=person_id)

    async def review(self, person_id: str, request_id: str) -> dict[str, Any] | None:
        """One request as this person may see it, in any status (the chat card); None when hidden or missing."""

        return await asyncio.to_thread(self._review, person_id, request_id)

    def _decide(
        self,
        person_id: str,
        request_id: str,
        choice: str,
        note: str,
        keep: bool | None,
        kept_item_ids: tuple[str, ...],
        keep_hours: int | None = None,
        hand_on: bool | None = None,
    ) -> tuple[AccessRequests, AccessRequestV1]:
        person = self.person(person_id)
        if person is None:
            raise HandDownRefused("You are not an active member of this team.")
        try:
            decision = AccessReviewChoice(str(choice or "").strip())
        except ValueError as exc:
            raise HandDownRefused("Choose approve, approve_read, approve_task, approve_all or deny.") from exc
        requests = self.requests()
        return requests, requests.decide(
            request_id,
            person,
            decision,
            note=str(note or "").strip()[:2000],
            keep=keep,
            kept_item_ids=kept_item_ids,
            keep_hours=keep_hours,
            hand_on=hand_on,
        )

    async def decide(
        self,
        person_id: str,
        request_id: str,
        choice: str,
        note: str = "",
        *,
        keep: bool | None = None,
        kept_item_ids: tuple[str, ...] = (),
        keep_hours: int | None = None,
        hand_on: bool | None = None,
    ) -> dict[str, Any]:
        """Decide a waiting request as the person, then wake the waiting agent.

        ``keep`` is the web card's "Keep for later tasks" box for an access
        request, ``keep_hours`` how long kept access lasts (None: until
        removed), ``hand_on`` the "may hand it on" box on the team lead's own
        requests (None when there is no box, as in chat: yes), and
        ``kept_item_ids`` the rows ticked on a proposal.
        """

        requests, finished = await asyncio.to_thread(
            self._decide, person_id, request_id, choice, note, keep, tuple(kept_item_ids), keep_hours, hand_on
        )
        links, forms = await self._open_account_links(person_id, finished)
        if forms and finished.requester_agent_id is not None:
            self._hold_resolution(finished, forms)
        else:
            await self.send_resolution(finished)
        view = self.review_view(requests, finished, can_decide=False, viewer_id=person_id)
        view["follow_up_links"] = links
        return view

    def _hold_resolution(self, request: AccessRequestV1, forms: list[Any]) -> None:
        """Tell the waiting agent only after the proposal's account link forms end.

        The task paused inside ``propose_access_change``; resuming it once the
        approver used (or closed, or let expire) each form lets the tool
        report which links exist, so TeamLead does not call a tool that is
        still refused for a missing account link.
        """

        self._held_resolutions.add(request.request_id)

        async def release() -> None:
            self._held_resolutions.discard(request.request_id)
            current = await asyncio.to_thread(self.requests().service.store.get_request, request.request_id)
            await self.send_resolution(current or request)

        self.tool_setups.after_proposal_links(request_id=request.request_id, forms=forms, then=release)

    async def _open_account_links(
        self, person_id: str, request: AccessRequestV1
    ) -> tuple[list[dict[str, str]], list[Any]]:
        """Credential forms an approved proposal asked for, opened for the approver.

        Returns the links for the card and the opened forms, which the
        gateway watches (``_hold_resolution``). The links are also left as a
        notice, so a decision made in chat can be finished from the Inbox. A
        form that cannot open is reported, never hidden: the tool is refused
        with account_link_missing until someone links it (``/link`` opens the
        form again).
        """

        pending = self.proposals.account_links.pop(request.request_id, [])
        if not pending:
            return [], []
        person = self.person(person_id)
        links: list[dict[str, str]] = []
        forms: list[Any] = []
        lines: list[str] = []
        for item in pending:
            tool = self.requests().service.catalog.tools.get(item.tool_id)
            name = tool.display_name if tool else item.tool_id
            try:
                if self.tool_setups is None or person is None:
                    raise RuntimeError("account link forms are not available in this gateway")
                form = await self.tool_setups.open_proposal_link(
                    request_id=f"{request.request_id}:{item.tool_id}",
                    person_id=person_id,
                    person_roles=tuple(person.roles),
                    tool_id=item.tool_id,
                    owner=item.owner,
                    reason=item.purpose,
                )
            except Exception as exc:  # noqa: BLE001 - the proposal stands; the form can be opened with /link
                self._logger.warning(
                    "Could not open a credential form after a proposal. event=proposal_account_link_failed "
                    "request_id=%s tool_id=%s error=%s",
                    request.request_id,
                    item.tool_id,
                    exc,
                )
                lines.append(f"{name}: the form could not open ({exc}); use /link {item.tool_id}.")
                continue
            links.append({"label": f"Choose the credential for {name}", "url": form.form_url})
            forms.append((form, item.tool_id, item.owner))
            lines.append(f"{name}: {form.form_url}")
        await asyncio.to_thread(
            self.requests().inbox.send_notice,
            AccessNoticeV1(
                notice_id=new_id("access_notice"),
                team_id=self._settings.team_id,
                kind=AccessNoticeKind.ACCOUNT_LINK_NEEDED,
                recipient_person_id=person_id,
                title="Choose the credentials for the tools you approved",
                summary=" ".join(lines)[:4000],
                created_at_utc=self.requests().service.clock(),
            ),
        )
        return links, forms

    async def decide_review(self, person_id: str, request_id: str, choice: str) -> str:
        """Decide from a chat button (``AccessReviewDecider``); the new status."""

        return str((await self.decide(person_id, request_id, choice))["status"])

    async def request_status(self, request_id: str) -> str | None:
        """The request's current status, or None when there is no such request in this team."""

        def read() -> str | None:
            request = self.requests().service.store.get_request(request_id)
            if request is None or request.team_id != self._settings.team_id:
                return None
            return request.status.value

        return await asyncio.to_thread(read)

    def _mark_notices_read(self, person_id: str, notice_ids: tuple[str, ...] | None) -> dict[str, int]:
        person = self.person(person_id)
        if person is None:
            return {"marked": 0, "unread_notices": 0}
        requests = self.requests()
        store, roles = requests.service.store, _notice_roles(person)
        marked = store.mark_notices_read(
            self._settings.team_id, person_id=person_id, roles=roles, notice_ids=notice_ids,
            at=requests.service.clock(),
        )
        unread = store.notice_page(
            self._settings.team_id, person_id=person_id, roles=roles, unread_only=True, offset=0, limit=0
        ).unread
        return {"marked": marked, "unread_notices": unread}

    async def mark_notices_read(self, person_id: str, notice_ids: tuple[str, ...] | None) -> dict[str, int]:
        """Mark the person's notices read (these ids, or all when None); how many changed and how many stay unread."""

        return await asyncio.to_thread(self._mark_notices_read, person_id, notice_ids)

    def _revoke(self, person_id: str, grant_id: str) -> list[str]:
        person = self.person(person_id)
        if person is None:
            raise HandDownRefused("You are not an active member of this team.")
        return self.requests().service.revoke_by_person(person, grant_id)

    async def revoke_grant(self, person_id: str, grant_id: str) -> list[str]:
        """One-click revoke from a notice (the person's own authority decides)."""

        return await asyncio.to_thread(self._revoke, person_id, grant_id)

    # -- messages ---------------------------------------------------------------

    async def send_resolution(self, request: AccessRequestV1) -> bool:
        """Tell the waiting agent its request was decided; retried by maintenance."""

        if request.status is AccessRequestStatus.WITHDRAWN:
            return True
        if request.requester_agent_id is None:
            # A person asked (a file delivery); no agent waits for a message.
            await asyncio.to_thread(self.requests().service.store.mark_resolution_sent, request.request_id)
            return True
        if not getattr(self._manager_client, "connected", False):
            self._logger.warning(
                "Access decision saved while the transport is down; the agent is told later. "
                "event=access_resolution_deferred request_id=%s agent_id=%s",
                request.request_id,
                request.requester_agent_id,
            )
            return False
        message = AccessRequestResolvedV1(
            message_id=f"access_res:{request.request_id}",
            team_id=request.team_id,
            request_id=request.request_id,
        )
        envelope = MessageEnvelope(
            msg_content=message,
            msg_header=MessageHeader(
                from_agent_id=self._settings.response_route,
                to_routing_path=request.requester_agent_id,
                response_routing_key=self._settings.response_route,
                idempotency_key=message.message_id,
                agent_signature="",
            ),
            msg_meta=MessageMeta(
                from_agent_name=f"{self._settings.instance_name}_gateway",
                conversation_id=request.conversation_id,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=False,
            ),
        )
        await self._manager_client.publish_durable(envelope, routing_key=request.requester_agent_id)
        await asyncio.to_thread(self.requests().service.store.mark_resolution_sent, request.request_id)
        self._logger.info(
            "Told the agent about its access decision. event=access_resolution_sent request_id=%s agent_id=%s status=%s",
            request.request_id,
            request.requester_agent_id,
            request.status.value,
        )
        return True

    def _announcement_session(self, requests: AccessRequests, request: AccessRequestV1) -> Any | None:
        """Where to tell people: see the module docstring."""

        candidates = []
        if request.conversation_id:
            candidates.append(self._sessions.get_session_by_conversation_id(request.conversation_id))
        candidates.append(self._proactive_session())
        for session in candidates:
            owner = str(getattr(session, "owner_canonical_user_id", "") or "") if session is not None else ""
            if not owner or getattr(session, "status", "") != "active":
                continue
            person = self.person(owner)
            if person is not None and self._can_decide(requests, person, request):
                return session
        return None

    async def announce(self, request: AccessRequestV1) -> bool:
        requests = self.requests()
        session = await asyncio.to_thread(self._announcement_session, requests, request)
        if session is None:
            self._logger.info(
                "No chat to announce this access request in; it waits in the Inbox. "
                "event=access_review_inbox_only request_id=%s",
                request.request_id,
            )
            return False
        url = self.review_url(request.request_id)
        if request.change is not None:
            # One line from the change's trusted summary; the full effect list
            # stays in the Inbox (Web Chat shows it under the card's Details).
            text = (
                f"{self.headline(requests, request)}\n"
                f"The task waits for your decision. Details are in the Inbox: {url}"
            )
        else:
            lacks = " The person this task works for needs this access too." if request.person_lacks_access else ""
            about = "".join(f"{line}\n" for line in self.effect_lines(requests, request))
            # Chat buttons cannot show a hover or a card, so say what each one does.
            owner = str(getattr(session, "owner_canonical_user_id", "") or "")
            buttons = chat_effect_lines(
                request,
                self.choice_words(requests, request, owner),
                ACCESS_REVIEW_CHOICE_LABELS,
                [choice.value for choice in self.review_choices(request)],
            )
            what = ("What each button does:\n" + "".join(f"- {line}\n" for line in buttons)) if buttons else ""
            text = (
                f"{self.title(requests, request)} ({LIFETIME_WORDS[request.lifetime.value]}).\n"
                f"{about}Why: {request.reason}{lacks}\n"
                f"{what}"
                f"The task waits for your decision. Review it in the Inbox: {url}"
            )
        await self._emit(
            GatewayDeliveryEvent(
                text=text,
                session_key=session.session_key,
                channel_name=session.channel_name,
                user_id=session.user_id,
                chat_id=session.chat_id,
                thread_id=session.thread_id,
                conversation_id=session.conversation_id,
                scheduled_job_id=None,
                correlation_id=None,
                original_message_id=None,
                message_type=ACCESS_REVIEW_MESSAGE_TYPE,
                review_url=url,
                access_request_id=request.request_id,
                review_choices=tuple(choice.value for choice in self.review_choices(request)),
            )
        )
        return True

    async def run_once(self) -> None:
        requests = self.requests()
        store = requests.service.store
        # A decision whose process ended keeps its request claimed until the
        # claim runs out; this ends what it left behind, and the request waits
        # for a decision again (AccessRepository.recover_unfinished_decisions).
        # A failure here must not skip the announcements and wake messages.
        try:
            await asyncio.to_thread(
                store.recover_unfinished_decisions, self._settings.team_id, requests.service.clock()
            )
        except Exception:  # noqa: BLE001 - retried on the next pass
            self._logger.exception(
                "Could not recover unfinished access decisions. event=access_decision_recovery_failed team_id=%s",
                self._settings.team_id,
            )
        for request in await asyncio.to_thread(store.claim_unannounced, self._settings.team_id):
            try:
                await self.announce(request)
            except Exception:  # noqa: BLE001 - the review stays in the Inbox
                self._logger.exception(
                    "Could not announce an access request. event=access_review_announce_failed request_id=%s",
                    request.request_id,
                )
        for request in await asyncio.to_thread(store.unsent_resolutions, self._settings.team_id):
            if request.request_id in self._held_resolutions:
                continue
            try:
                await self.send_resolution(request)
            except Exception:  # noqa: BLE001 - retried on the next pass
                self._logger.exception(
                    "Could not tell an agent about its access decision. event=access_resolution_failed request_id=%s",
                    request.request_id,
                )

    async def run(self, stop_event: asyncio.Event, *, interval: float = MAINTENANCE_INTERVAL_SECONDS) -> None:
        while not stop_event.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - keep the loop alive; each pass logs its own failure
                self._logger.exception("Access Inbox maintenance failed. event=access_inbox_maintenance_failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


def _notice_roles(person: PersonPrincipal) -> tuple[str, ...]:
    return effective_roles(person.roles)


def _inbox_url(base_url: str | None) -> str:
    """The web chat page for Inbox links: absolute when the public origin is
    HTTPS or loopback HTTP, otherwise relative (the link still works inside
    the web chat)."""

    from urllib.parse import urlparse

    base = str(base_url or "").rstrip("/")
    parsed = urlparse(base)
    host = str(parsed.hostname or "").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")
    if base and (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)):
        return f"{base}/webchat"
    return "/webchat"


def access_inbox_settings(config: Any) -> GatewayAccessInboxSettings:
    return GatewayAccessInboxSettings(
        team_id=config.manager.team_id,
        instance_name=config.instance_name,
        response_route=config.manager.response_queue_name,
        lead_agent_type=config.manager.named_agent_type,
        inbox_url=_inbox_url(config.internal_website_base_url),
        instance_root=config.instance_root_path,
        storage_settings=config.manager_storage_settings,
        cedar_binary=config.authorization.cedar_binary_path,
        authorization_enabled=bool(config.authorization.enabled),
        protected_locations=protected_locations(
            config.instance_root_path,
            instance_config=config.instance_root_path / "instance_config.yaml",
            broker_credentials=getattr(config, "broker_credentials_path", None),
        ),
        service_urls=web_service_urls(config.web_services) if hasattr(config, "web_services") else None,
    )


def build_gateway_access_inbox(
    config: Any,
    *,
    identity_repository: Any,
    manager_client: Any,
    session_repository: Any,
    proactive_sessions: Any,
    event_output: Any,
    logger: logging.Logger | None = None,
) -> GatewayAccessInbox:
    return GatewayAccessInbox(
        settings=access_inbox_settings(config),
        memberships=MembershipRoles(identity_repository),
        manager_client=manager_client,
        session_repository=session_repository,
        proactive_session=proactive_sessions.resolve_session,
        emit_delivery_event=event_output.emit,
        logger=logger,
    )


__all__ = [
    "ACCESS_REVIEW_MESSAGE_TYPE",
    "GatewayAccessInbox",
    "GatewayAccessInboxSettings",
    "GatewayAccessRequestsSource",
    "access_inbox_settings",
    "access_review_url",
    "build_gateway_access_inbox",
]
