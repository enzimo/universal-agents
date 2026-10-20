from __future__ import annotations

import logging
from pathlib import Path

from custom_messages.enums import ObjectiveHorizon
from custom_messages.records.objectives import ObjectiveRecord
from storage.config import resolve_storage_settings
from storage.objective_repository import ObjectiveView, create_objective_repository
from storage.sqlite_connection import connect_sqlite
from tests.postgres_test_helpers import patch_fake_postgres


def _settings(tmp_path: Path):
    return resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )


def test_objective_repository_scopes_same_objective_id_by_owner(tmp_path: Path) -> None:
    repo = create_objective_repository(_settings(tmp_path), logger=logging.getLogger("test"))
    for owner_id, title in [
        ("manager_agent", "First manager objective"),
        ("manager_agent_b", "Second manager objective"),
    ]:
        repo.insert_objective(
            ObjectiveRecord(
                objective_id="objective_id_shared",
                title=title,
                desired_outcome=title,
                why="Owner scoping test.",
                horizon=ObjectiveHorizon.FINITE,
                owner_agent_id=owner_id,
            )
        )

    first = repo.get_objective("objective_id_shared", owner_agent_id="manager_agent")
    second = repo.get_objective("objective_id_shared", owner_agent_id="manager_agent_b")

    assert first is not None
    assert second is not None
    assert first.title == "First manager objective"
    assert second.title == "Second manager objective"


def test_objective_repository_backfills_and_queries_canonical_user_ownership(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    repo = create_objective_repository(settings, logger=logging.getLogger("test"))
    objective = ObjectiveRecord(
        objective_id="objective_id_user_owned",
        title="User-owned Objective",
        desired_outcome="Remain reachable after manager replacement.",
        why="Canonical user ownership is durable authority.",
        horizon=ObjectiveHorizon.LONG_HORIZON,
        owner_agent_id="legacy_manager_runtime_id",
        owner_canonical_user_id="usr_owner",
        team_id="team_1",
        owner_named_agent_type="TeamArchitect",
    )
    repo.insert_objective(objective)
    with connect_sqlite(settings.sqlite_path or "") as conn:
        conn.execute(
            "UPDATE objectives SET owner_canonical_user_id=NULL "
            "WHERE objective_id=?",
            (objective.objective_id,),
        )

    restored_repo = create_objective_repository(
        settings,
        logger=logging.getLogger("test.backfill"),
    )
    restored = restored_repo.get_objective_for_user(
        objective.objective_id,
        owner_canonical_user_id="usr_owner",
        team_id="team_1",
    )
    other_team = restored_repo.get_objective_for_user(
        objective.objective_id,
        owner_canonical_user_id="usr_owner",
        team_id="team_2",
    )
    rows = restored_repo.list_objectives_for_user(
        owner_canonical_user_id="usr_owner",
        team_id="team_1",
    )

    assert restored is not None
    assert restored.objective_id == objective.objective_id
    assert other_team is None
    assert [item.objective_id for item in rows] == [objective.objective_id]


def test_objective_repository_accepts_postgres_when_driver_is_available(tmp_path: Path, monkeypatch) -> None:
    settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    patch_fake_postgres(monkeypatch)

    repo = create_objective_repository(settings, logger=logging.getLogger("test"))
    assert repo is not None


def test_objective_listing_view_shows_the_teams_and_one_persons_objectives(tmp_path: Path) -> None:
    """The view filter in the query, so the limit counts only Objectives in view."""

    repo = create_objective_repository(_settings(tmp_path), logger=logging.getLogger("test"))
    # Oldest first: another person's Objective is the newest.
    owners = {"objective_a": "usr_a", "objective_team": None, "objective_blank": "", "objective_b": "usr_b"}
    for minute, (objective_id, owner) in enumerate(owners.items()):
        repo.insert_objective(
            ObjectiveRecord(
                objective_id=objective_id,
                title=objective_id,
                desired_outcome="Listing view test.",
                why="Each task sees the team's Objectives and its person's.",
                horizon=ObjectiveHorizon.FINITE,
                owner_agent_id="manager_agent",
                owner_canonical_user_id=owner,
                team_id="team_1",
                updated_at=f"2026-10-16T10:0{minute}:00+00:00",
            )
        )

    def listed(view: ObjectiveView | None, limit: int = 20) -> set[str]:
        rows = repo.list_objectives(limit=limit, owner_agent_id="manager_agent", view=view)
        return {row.objective_id for row in rows}

    assert listed(ObjectiveView(person_id="usr_a")) == {"objective_a", "objective_team", "objective_blank"}
    assert listed(ObjectiveView(person_id=None)) == {"objective_team", "objective_blank"}
    assert listed(None) == set(owners)  # trusted runtime code only
    # Another person's newer Objective does not take a place within the limit.
    assert listed(ObjectiveView(person_id="usr_a"), limit=2) == {"objective_blank", "objective_team"}
