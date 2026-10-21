"""The Status page: what the framework flags to administrators about itself, now.

Why: many conditions that change how the framework behaves were only in logs
or only visible in one process, for example an environment variable that
silently overrides a saved setting or a saved model key, a provider that
rejected a key, or an MCP server that stopped answering. The Status page
gathers them in one place, as current state: a finding disappears once its
condition is gone.

Sources, each producing ``StatusFindingV1`` items:

- the launcher's list of environment overrides
  (``InstanceLayout.environment_overrides_path``, written by
  ``docker.settings_environment.write_environment_overrides``; names only);
- the broker's model key status (rejections; configured endpoints with no
  key from anywhere), plus the configured endpoints
  (``assistant_gateway.model_key_endpoints``);
- the agents' own findings in their status heartbeat
  (``custom_agents.status_findings``), and whether TeamLead is reporting;
- people's model settings in the access store (``access_control.person_settings``):
  active people whose work may not use the team's model keys and who have no
  model of their own yet (a warning: their requests do not run), and the
  people whose work goes to their own model provider (a note: conversations
  leave through an address the team did not configure);
- the gateway's own configuration (authorization off, and checks between a
  person's separate requests off, are deliberate choices, shown as notes).

A source that cannot be asked is listed in ``unavailable_sources`` and, for
the broker, shown as a problem: silence must not look like health.
``notify_new_problems`` sends administrators one Inbox notice per new set of
problems, linking to the page; the page itself never changes anything. A
notice records what administrators were told and is not withdrawn when its
problems go away; the page shows the current state. So the loop's first check
waits for the gateway's start-up work that changes what it would find
(``StatusPage.run``), instead of reporting a condition that start-up is about
to fix.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Sequence

from pydantic import ValidationError

from access_control.people import PERSON_ROLE_ADMIN
from access_control.person_settings import PersonSettings
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.model_key_endpoints import configured_model_endpoints
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    AccessNoticeV1,
    PersonStanding,
    TeamPersonV1,
)
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.model_keys_v1 import (
    ConnectionPurposeV1,
    KeySource,
    ModelEndpointStatusV1,
    ModelKeyEndpointUseV1,
    ModelKeysStatusV1,
    StoredKeyState,
)
from custom_messages.contracts.person_settings_v1 import PersonSettingsV1
from custom_messages.contracts.status_v1 import (
    EnvironmentOverridesV1,
    OverrideOrigin,
    OverrideSourceV1,
    StatusFindingV1,
    StatusReportV1,
    StatusSeverity,
)
from model_providers.endpoints import (
    EndpointKind,
    display_host,
    embedding_endpoint,
    openai_compatible_endpoint,
    well_known_endpoint,
)
from storage.access_repository import AccessRepository

LOGGER = logging.getLogger(__name__)

STATUS_PAGE_PATH = "/webchat?view=status"
# How often the gateway checks for new problems to tell administrators about.
NOTIFY_INTERVAL_SECONDS = 300.0
_SEVERITY_ORDER = {StatusSeverity.PROBLEM: 0, StatusSeverity.WARNING: 1, StatusSeverity.NOTE: 2}
BROKER_SOURCE = "secure tool broker"
ACCESS_STORE_SOURCE = "access store"
LAUNCHER_SOURCE = "launcher"
GATEWAY_SOURCE = "gateway"


class StatusPage:
    """Builds the Status report from every source; reads only, never repairs."""

    def __init__(
        self,
        *,
        config: GatewayInstanceConfig,
        model_key_status: Callable[[], Awaitable[dict[str, Any]]],
        agent_statuses: Callable[[], Sequence[AgentStatus]],
        send_notice_once: Callable[[AccessNoticeV1], bool],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        open_access_store: Callable[[], Any] | None = None,
        stopped_receivers: Callable[[], Sequence[Any]] = lambda: (),
    ) -> None:
        self._config = config
        # People and their model settings; the gateway's own manager storage unless a test gives one.
        self._open_access_store = open_access_store or (lambda: AccessRepository(config.manager_storage_settings))
        self._model_key_status = model_key_status
        self._agent_statuses = agent_statuses
        self._send_notice_once = send_notice_once
        self._now = now
        # The gateway's own receive loops that stopped (custom_transport.nats_pull).
        self._stopped_receivers = stopped_receivers

    async def report(self) -> StatusReportV1:
        # File and database reads run in worker threads: the gateway's loop
        # also runs the chat apps (website_handlers.off_loop).
        findings: list[StatusFindingV1] = []
        unavailable: list[str] = []
        overrides = await asyncio.to_thread(self._environment_overrides)
        findings.extend(_override_findings(overrides))
        findings.extend(await self._model_key_findings(overrides, unavailable))
        findings.extend(self._agent_findings())
        findings.extend(self._receiver_findings())
        findings.extend(await asyncio.to_thread(self._person_findings, unavailable))
        findings.extend(self._configuration_findings())
        return StatusReportV1(
            checked_at=self._now(), findings=tuple(_ordered(_unique(findings))), unavailable_sources=tuple(unavailable)
        )

    async def notify_new_problems(self) -> None:
        """One Inbox notice to administrators per set of problems (the set's digest is the notice id)."""

        report = await self.report()
        problems = [item for item in report.findings if item.severity is StatusSeverity.PROBLEM]
        if not problems:
            return
        digest = hashlib.sha256("\n".join(sorted(item.finding_id for item in problems)).encode("utf-8")).hexdigest()
        stored = await asyncio.to_thread(
            self._send_notice_once,
            AccessNoticeV1(
                notice_id=f"status_problems:{digest[:32]}",
                team_id=self._config.manager.team_id,
                kind=AccessNoticeKind.STATUS_PROBLEMS,
                recipient_roles=(PERSON_ROLE_ADMIN,),
                title=f"{len(problems)} problem(s) on the Status page",
                summary="\n".join(f"- {item.title}" for item in problems)
                + f"\n\nOpen the Status page ({STATUS_PAGE_PATH}) for what each one affects and what to do.",
                created_at_utc=self._now(),
            ),
        )
        if stored:
            LOGGER.warning(
                "Told administrators about problems on the Status page. event=status_problems_notice problems=%s",
                ",".join(item.finding_id for item in problems),
            )

    async def run(self, stop_event: asyncio.Event, *, first_check_after: Sequence[asyncio.Task[Any]] = ()) -> None:
        """Check for new problems every ``NOTIFY_INTERVAL_SECONDS`` until the gateway stops.

        The first check waits for ``first_check_after``: start-up work that
        changes what the check finds. The gateway passes its onboarding key
        hand-over, which saves the model keys typed during onboarding
        (``assistant_gateway.onboarding_account_keys``). Checked alongside it,
        every new instance's first start told administrators "No key is saved"
        for a key saved a moment later, and that notice stayed (module
        docstring). The wait ends however the work ends; a hand-over that fails
        logs its own error, and the check then reports what is still missing.
        It lasts at most the hand-over's wait for the broker, and only on a
        start where onboarding left keys waiting.
        """

        if first_check_after:
            await asyncio.wait(first_check_after)
        while not stop_event.is_set():
            try:
                await self.notify_new_problems()
            except Exception:  # noqa: BLE001 - a failed check is logged; the loop keeps watching
                LOGGER.exception("The Status page check failed. event=status_check_failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=NOTIFY_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue

    # -- Sources ----------------------------------------------------------------------

    def _environment_overrides(self) -> EnvironmentOverridesV1 | None:
        path = self._config.layout.environment_overrides_path
        if not path.is_file():
            return None
        try:
            return EnvironmentOverridesV1.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            LOGGER.warning(
                "The launcher's list of environment overrides is not readable. event=environment_overrides_unreadable "
                "path=%s error=%s",
                path,
                exc,
            )
            return None

    async def _model_key_findings(
        self, overrides: EnvironmentOverridesV1 | None, unavailable: list[str]
    ) -> list[StatusFindingV1]:
        try:
            status = ModelKeysStatusV1.model_validate(await self._model_key_status())
        except (RuntimeError, OSError, ValidationError) as exc:
            unavailable.append(BROKER_SOURCE)
            return [
                StatusFindingV1(
                    finding_id="broker_unreachable",
                    severity=StatusSeverity.PROBLEM,
                    title="The secure tool broker did not answer",
                    summary=(
                        "Agents cannot use tools that need an account, MCP servers, or saved model keys while it "
                        "does not answer."
                    ),
                    action="Check the broker's log; restarting the container starts it again.",
                    source=GATEWAY_SOURCE,
                    since=self._now(),
                    details=str(exc)[:2000],
                )
            ]
        by_address = {endpoint.address: endpoint for endpoint in status.endpoints}
        environment = set(overrides.model_key_variables) if overrides is not None else set()
        uses = configured_model_endpoints(self._config)
        # The saved endpoint, not the gateway's start-up copy: it switches without a restart.
        chat_api_base = next(
            (
                use.base_url
                for use in uses
                if use.kind is EndpointKind.OPENAI_COMPATIBLE and use.purpose is ConnectionPurposeV1.CHAT
            ),
            None,
        )
        findings = [
            finding
            for use in uses
            if (finding := _missing_key(use, by_address.get(use.address), environment, chat_api_base)) is not None
        ]
        findings.extend(_rejection(endpoint) for endpoint in status.endpoints if _rejected_now(endpoint))
        return findings

    def _agent_findings(self) -> list[StatusFindingV1]:
        statuses = list(self._agent_statuses())
        findings = [finding for status in statuses for finding in status.status_findings]
        if not any(status.is_team_lead for status in statuses):
            findings.append(
                StatusFindingV1(
                    finding_id="team_lead_not_reporting",
                    severity=StatusSeverity.PROBLEM,
                    title="TeamLead is not reporting",
                    summary=(
                        "No status from TeamLead arrived recently, so messages may not be answered and agents' own "
                        "problems are missing from this page."
                    ),
                    action="Check that the manager process is running (its log, or restart the container).",
                    source=GATEWAY_SOURCE,
                )
            )
        return findings

    def _person_findings(self, unavailable: list[str]) -> list[StatusFindingV1]:
        team_id = self._config.manager.team_id
        try:
            store = self._open_access_store()
            people = store.team_people(team_id)
            settings = PersonSettings(store, team_id).everyone()
        except Exception as exc:  # noqa: BLE001 - listed as unavailable, never shown as healthy
            LOGGER.warning(
                "The Status page could not read people's model settings. event=status_person_settings_unreadable "
                "error=%s",
                exc,
            )
            unavailable.append(ACCESS_STORE_SOURCE)
            return []
        return person_settings_findings(people, settings)

    def _receiver_findings(self) -> list[StatusFindingV1]:
        from custom_transport.dead_letters import stopped_receiver_finding

        return [stopped_receiver_finding(receiver, source="Gateway") for receiver in self._stopped_receivers()]

    def _configuration_findings(self) -> list[StatusFindingV1]:
        findings = [] if self._config.authorization.enabled else [_AUTHORIZATION_OFF]
        if not self._config.agent.request_conflict_checks:
            findings.append(_REQUEST_CONFLICT_CHECKS_OFF)
        return findings


_AUTHORIZATION_OFF = StatusFindingV1(
    finding_id="authorization_off",
    severity=StatusSeverity.NOTE,
    title="Access checks are turned off",
    summary=(
        "Agents use every tool without access requests; the sandbox, the broker and protected folders "
        "still apply. Changes are applied with a person's agreement in chat instead of an Inbox approval."
    ),
    action="Turn them on with authorization.enabled: true in instance_config.yaml, then restart.",
    source=GATEWAY_SOURCE,
)
_REQUEST_CONFLICT_CHECKS_OFF = StatusFindingV1(
    finding_id="request_conflict_checks_off",
    severity=StatusSeverity.NOTE,
    title="Checks between a person's separate requests are turned off",
    summary=(
        "When two of a person's requests change the same record, such as a personal knowledge page, the later "
        "change is saved over the earlier one instead of being refused. Changes made from a record that is out "
        "of date are still refused."
    ),
    action="Turn them on with agent.request_conflict_checks: true in instance_config.yaml, then restart.",
    source=GATEWAY_SOURCE,
)


def person_settings_findings(people: Iterable[TeamPersonV1], settings: Iterable[PersonSettingsV1]) -> list[StatusFindingV1]:
    """People who cannot use agents until they set up their own model, and people whose work uses their own."""

    active = {person.person_id for person in people if person.standing is PersonStanding.ACTIVE}
    records = [record for record in settings if record.person_id in active]
    waiting = sorted(record.person_id for record in records if not record.may_use_team_keys and record.own_model is None)
    own = sorted(
        (record.person_id, f"{record.own_model.model_id} at {display_host(record.own_model.address)}")
        for record in records
        if record.own_model is not None
    )
    findings: list[StatusFindingV1] = []
    if waiting:
        findings.append(
            StatusFindingV1(
                finding_id="people_without_own_model:" + ",".join(waiting)[:150],
                severity=StatusSeverity.WARNING,
                title=f"{len(waiting)} person(s) cannot use agents until they set up their own model",
                summary=(
                    "Their work may not use the team's model keys, and they have no model of their own yet, so "
                    "their requests do not run: " + ", ".join(waiting) + "."
                ),
                action=(
                    "They set up their model with /auth model me, or an administrator lets them use the team's "
                    "keys with /user team-keys <id> on."
                ),
                source=GATEWAY_SOURCE,
            )
        )
    if own:
        findings.append(
            StatusFindingV1(
                finding_id="people_with_own_model",
                severity=StatusSeverity.NOTE,
                title=f"{len(own)} person(s) use their own model provider",
                summary=(
                    "Agents send all work for these people, with its conversation, to the model they saved: "
                    + "; ".join(f"{person}: {model}" for person, model in own)
                    + "."
                ),
                action="Nothing to do. People change or remove their model with /auth model me.",
                source=GATEWAY_SOURCE,
            )
        )
    return findings


def _override_findings(overrides: EnvironmentOverridesV1 | None) -> list[StatusFindingV1]:
    if overrides is None:
        return []
    findings = [
        StatusFindingV1(
            finding_id=f"environment_overrides_setting:{name}",
            severity=StatusSeverity.WARNING,
            title=f"{name} overrides a saved setting",
            summary=(
                f"{name} is set in {_where(overrides, name)}, with a value that differs from the saved instance "
                "configuration. It wins until the container restarts, also over changes saved since."
            ),
            action=f"{_how_to_remove(overrides, name)} Leave it if the override is intended.",
            source=LAUNCHER_SOURCE,
            since=overrides.written_at,
        )
        for name in overrides.setting_variables
    ]
    findings.extend(
        StatusFindingV1(
            finding_id=f"environment_overrides_model_key:{name}",
            severity=StatusSeverity.WARNING,
            title=f"{name} overrides the model key saved with /auth model",
            summary=(
                f"{name} is set in {_where(overrides, name)}, so agents send it instead of the key saved for that "
                "endpoint, and a key saved with /auth model has no effect."
            ),
            action=f"To use saved keys: {_how_to_remove(overrides, name)}",
            source=LAUNCHER_SOURCE,
            since=overrides.written_at,
        )
        for name in overrides.model_key_variables
    )
    return findings


# A shell opened with docker exec gets the container's environment, never the env file's values.
_ENV_FILE_NOTE = "a shell opened with docker exec does not show its values"


def _where(overrides: EnvironmentOverridesV1, name: str) -> str:
    """The places the launcher found ``name``, in plain words (from an older launcher: both possibilities)."""

    sources = overrides.sources.get(name, ())
    if not sources:
        return "team.env or the container environment"
    return " and ".join(_place(source) for source in sources)


def _place(source: OverrideSourceV1) -> str:
    if source.origin is OverrideOrigin.ENV_FILE:
        return f"the instance's env file {source.path or 'team.env'} (the launcher reads it; {_ENV_FILE_NOTE})"
    if source.origin is OverrideOrigin.SECRET_FILE:
        return f"the secret file {source.path}, named by {source.variable}"
    return "the container's own environment (docker run -e, the compose file's environment, or the image)"


def _how_to_remove(overrides: EnvironmentOverridesV1, name: str) -> str:
    steps = []
    for source in overrides.sources.get(name, ()):
        if source.origin is OverrideOrigin.ENV_FILE:
            steps.append(f"delete the {source.variable} line from {source.path or 'team.env'}")
        elif source.origin is OverrideOrigin.SECRET_FILE:
            steps.append(f"remove {source.variable} from where it is set")
        else:
            steps.append(f"remove {source.variable} from the container's environment")
    if not steps:
        steps.append(f"remove {name} (and {name}_FILE) from team.env or the container environment")
    text = "; ".join(steps)
    return f"{text[0].upper()}{text[1:]}, then restart the container."


def _missing_key(
    use: ModelKeyEndpointUseV1,
    endpoint: ModelEndpointStatusV1 | None,
    environment: set[str],
    chat_api_base: str | None,
) -> StatusFindingV1 | None:
    """A configured endpoint with no stored key and no environment variable that gives one."""

    if endpoint is not None and endpoint.state is not StoredKeyState.MISSING:
        return None
    reported_environment = endpoint is not None and any(
        item.source is KeySource.ENVIRONMENT for item in endpoint.sources
    )
    if use.kind is not EndpointKind.OPENAI_COMPATIBLE:
        model_endpoint = well_known_endpoint(use.kind)
    elif use.purpose is ConnectionPurposeV1.EMBEDDING:
        model_endpoint = embedding_endpoint(use.base_url, chat_api_base=chat_api_base)
    else:
        model_endpoint = openai_compatible_endpoint(use.base_url)
    if reported_environment or environment & set(model_endpoint.env_vars):
        return None
    host = display_host(use.address)
    return StatusFindingV1(
        finding_id=f"model_key_missing:{use.address}",
        severity=StatusSeverity.PROBLEM,
        title=f"No key is saved for {host}",
        summary=(
            f"{use.label or 'This endpoint'} ({use.model_id or 'no model'}) is in the configuration, but agents "
            "have no key for it, so its requests fail."
        ),
        action="An administrator saves its key with /auth model (or ticks “This endpoint needs no key”).",
        source=GATEWAY_SOURCE,
    )


def _rejected_now(endpoint: ModelEndpointStatusV1) -> bool:
    rejection = endpoint.last_rejection
    if rejection is None:
        return False
    return endpoint.saved_at is None or rejection.rejected_at >= endpoint.saved_at


def _rejection(endpoint: ModelEndpointStatusV1) -> StatusFindingV1:
    rejection = endpoint.last_rejection
    assert rejection is not None
    from_environment = rejection.source is KeySource.ENVIRONMENT
    whose = f"the key in {rejection.env_var}" if from_environment else "the saved key"
    return StatusFindingV1(
        finding_id=f"model_key_rejected:{endpoint.address}",
        severity=StatusSeverity.PROBLEM,
        title=f"{display_host(endpoint.address)} rejected {whose}",
        summary=(
            f"Requests to {display_host(endpoint.address)} fail until the key is replaced"
            + (f" (HTTP {rejection.status_code})." if rejection.status_code else ".")
        ),
        action=(
            f"Update {rejection.env_var} in team.env (or its secret file) and restart the container."
            if from_environment
            else "An administrator saves a working key with /auth model; no restart is needed."
        ),
        source=rejection.caller,
        since=rejection.rejected_at,
        details=f"model {rejection.model_id or '-'}, provider {rejection.provider or '-'}",
    )


def _unique(findings: Iterable[StatusFindingV1]) -> list[StatusFindingV1]:
    """One finding per id (several agents may report the same condition); the first is kept."""

    seen: dict[str, StatusFindingV1] = {}
    for finding in findings:
        seen.setdefault(finding.finding_id, finding)
    return list(seen.values())


def _ordered(findings: list[StatusFindingV1]) -> list[StatusFindingV1]:
    return sorted(findings, key=lambda item: (_SEVERITY_ORDER[item.severity], item.title.casefold()))


__all__ = ["NOTIFY_INTERVAL_SECONDS", "STATUS_PAGE_PATH", "StatusPage"]
