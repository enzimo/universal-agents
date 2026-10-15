"""The sections a transfer file carries, in import order.

Order matters: a section may rely on the ones before it (settings first, then
control files, people before access, objectives before the schedules that
advance them). Sections are added phase by phase
(``plans/team_transfer_plan.md`` in the architecture repository).
"""

from __future__ import annotations

from team_transfer.access import AccessSection
from team_transfer.broker_credentials import BrokerCredentialsSection
from team_transfer.control import ControlSection
from team_transfer.integrations import McpSection, ProceduresSection
from team_transfer.knowledge import KnowledgeSection
from team_transfer.objectives import ObjectivesSection
from team_transfer.operator_secrets import OperatorSecretsSection
from team_transfer.people import PeopleSection
from team_transfer.pinned_memory import PinnedMemorySection
from team_transfer.schedules import SchedulesSection
from team_transfer.sections import TransferSection
from team_transfer.settings import SettingsSection
from team_transfer.skills import SkillsSection
from team_transfer.team_tools import TeamToolsSection

TRANSFER_SECTIONS: tuple[TransferSection, ...] = (
    SettingsSection(),
    OperatorSecretsSection(),
    ControlSection(),
    SkillsSection(),
    TeamToolsSection(),
    McpSection(),
    ProceduresSection(),
    PeopleSection(),
    AccessSection(),
    ObjectivesSection(),
    SchedulesSection(),
    PinnedMemorySection(),
    KnowledgeSection(),
    # Last: imported after the first start (team import-credentials).
    BrokerCredentialsSection(),
)

__all__ = ["TRANSFER_SECTIONS"]
