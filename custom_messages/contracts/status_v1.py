"""Status findings: what the framework flags to administrators on the Status page.

A finding is current state, not an event: it says what is wrong or unusual
now, what it affects and what to do, and it disappears once fixed. Each
process reports its own findings in this one shape (agents in their status
heartbeat, the launcher through ``EnvironmentOverridesV1``), and the gateway
gathers them with its own checks into a ``StatusReportV1``
(``assistant_gateway.status_page``). Access Inbox notices stay what they
are, one-off messages; the gateway sends one when new problems appear,
linking to the page.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StatusContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StatusSeverity(StrEnum):
    """How much a finding matters.

    ``problem``: something does not work now. ``warning``: it works, but not
    as configured or expected. ``note``: a deliberate choice worth knowing.
    """

    PROBLEM = "problem"
    WARNING = "warning"
    NOTE = "note"


class StatusFindingV1(StatusContractModel):
    """One finding. ``finding_id`` is stable while the condition lasts, so a page and a notice can tell it is the same one."""

    finding_id: str = Field(min_length=1, max_length=200)
    severity: StatusSeverity
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)
    action: str = Field(default="", max_length=1000)
    source: str = Field(default="gateway", max_length=120)
    since: datetime | None = None
    details: str = Field(default="", max_length=2000)


class StatusReportV1(StatusContractModel):
    """Everything the gateway found, and the sources it could not ask."""

    checked_at: datetime
    findings: tuple[StatusFindingV1, ...] = ()
    unavailable_sources: tuple[str, ...] = ()


class OverrideOrigin(StrEnum):
    """Where the launcher found an overriding variable.

    ``env_file``: the instance's env file (``team.env``), which the launcher
    reads and passes only to the processes it starts, so a shell opened with
    ``docker exec`` does not show it. ``container_environment``: the
    container's own environment. ``secret_file``: the file a ``<NAME>_FILE``
    variable names (set in either of the other two).
    """

    ENV_FILE = "env_file"
    CONTAINER_ENVIRONMENT = "container_environment"
    SECRET_FILE = "secret_file"


class OverrideSourceV1(StatusContractModel):
    """One place an overriding variable came from: names and paths only, never values."""

    origin: OverrideOrigin
    variable: str = Field(min_length=1, max_length=200)
    path: str = Field(default="", max_length=1000)


class EnvironmentOverridesV1(StatusContractModel):
    """What the launcher saw in the environment at start: variable names only, never values.

    ``setting_variables`` hold a value that differs from what the saved
    instance configuration would give, so they override a saved setting.
    ``model_key_variables`` are set model key variables, which override the
    keys saved with ``/auth model``. ``sources`` says where each one came
    from, so the Status page names the exact file or environment to change
    (an operator looking in the wrong place sees nothing and the warning looks
    wrong).
    """

    written_at: datetime
    setting_variables: tuple[str, ...] = ()
    model_key_variables: tuple[str, ...] = ()
    sources: dict[str, tuple[OverrideSourceV1, ...]] = Field(default_factory=dict)


__all__ = [
    "EnvironmentOverridesV1",
    "OverrideOrigin",
    "OverrideSourceV1",
    "StatusFindingV1",
    "StatusReportV1",
    "StatusSeverity",
]
