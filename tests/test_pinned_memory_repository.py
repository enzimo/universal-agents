from __future__ import annotations

from pathlib import Path

import pytest

from custom_agents.pinned_memory_runtime import (
    PinnedMemoryActor,
    PinnedMemoryCommand,
    PinnedMemoryRequestContext,
    PinnedMemoryService,
    PinnedMemorySource,
)
from storage.pinned_memory_repository import (
    PinnedMemoryConflictError,
    SqlitePinnedMemoryRepository,
)


def _repo(tmp_path: Path) -> SqlitePinnedMemoryRepository:
    return SqlitePinnedMemoryRepository(str(tmp_path / "pinned_memory.db"))


def _context() -> PinnedMemoryRequestContext:
    return PinnedMemoryRequestContext(
        team_id="team_test",
        canonical_user_id="user_123",
        request_timezone="America/Chicago",
        source=PinnedMemorySource(
            conversation_id="conversation_1",
            task_id="task_1",
            gateway_session_key="session_1",
            channel_name="cli",
            chat_id="cli_user",
        ),
        actor=PinnedMemoryActor(kind="user", id="user_123", display_name="Test User"),
    )


def test_pinned_memory_repository_reuses_deleted_ids_and_enforces_capacity(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path)

    first = repository.create_coremem(
        team_id="team_test", fact_text="Fact one", capacity=2
    )
    second = repository.create_coremem(
        team_id="team_test", fact_text="Fact two", capacity=2
    )

    assert first.memory_id == 1
    assert second.memory_id == 2
    with pytest.raises(ValueError, match="capacity is full"):
        repository.create_coremem(
            team_id="team_test", fact_text="Fact three", capacity=2
        )

    assert repository.delete_coremem(team_id="team_test", memory_id=1) is True
    reused = repository.create_coremem(
        team_id="team_test", fact_text="Replacement fact", capacity=2
    )

    assert reused.memory_id == 1
    assert [row.memory_id for row in repository.list_coremem(team_id="team_test")] == [
        1,
        2,
    ]


def test_pinned_memory_replace_reports_missing_post_mutation_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _repo(tmp_path)
    repository.create_coremem(
        team_id="team_test",
        fact_text="Original fact",
        capacity=2,
    )
    original_get_row = repository._get_row
    calls = 0

    def row_disappears_after_update(conn, **scope):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_get_row(conn, **scope)
        return None

    monkeypatch.setattr(repository, "_get_row", row_disappears_after_update)

    with (
        caplog.at_level("ERROR"),
        pytest.raises(
            RuntimeError,
            match="replace did not produce an active row",
        ),
    ):
        repository.replace_coremem(
            team_id="team_test",
            memory_id=1,
            fact_text="Updated fact",
        )

    assert "event=pinned_memory_post_mutation_row_missing" in caplog.text
    assert "memory_id=1" in caplog.text


def test_pinned_memory_repository_source_key_is_idempotent_per_user(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path)

    first = repository.create_usermem(
        canonical_user_id="user_123",
        fact_text="Initial onboarding context",
        capacity=10,
        source_key="onboarding:user_context:v1",
    )
    second = repository.create_usermem(
        canonical_user_id="user_123",
        fact_text="Changed retry content must not duplicate or overwrite the fact",
        capacity=10,
        source_key="onboarding:user_context:v1",
    )
    other_user = repository.create_usermem(
        canonical_user_id="user_456",
        fact_text="Another user's onboarding context",
        capacity=10,
        source_key="onboarding:user_context:v1",
    )

    assert first.memory_id == second.memory_id
    assert second.fact_text == "Initial onboarding context"
    assert second.source_key == "onboarding:user_context:v1"
    assert other_user.source_key == "onboarding:user_context:v1"
    assert len(repository.list_usermem(canonical_user_id="user_123")) == 1


def test_pinned_memory_repository_undo_restore_conflicts_when_id_reused(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path)
    repository.create_coremem(
        team_id="team_test", fact_text="Original fact", capacity=2
    )
    repository.delete_coremem(team_id="team_test", memory_id=1)
    delete_event = repository.get_latest_event(
        bank="coremem",
        team_id="team_test",
        memory_id=1,
        event_type="delete",
    )
    assert delete_event is not None

    reused = repository.create_coremem(
        team_id="team_test", fact_text="Reused ID fact", capacity=2
    )

    assert reused.memory_id == 1
    with pytest.raises(ValueError, match="already in use"):
        repository.undo_memory_event(delete_event.event_id)


def test_pinned_memory_service_rejects_stale_edit_and_delete_digests(
    tmp_path: Path,
) -> None:
    service = PinnedMemoryService(_repo(tmp_path))
    context = _context()
    created = service.execute(
        PinnedMemoryCommand(
            bank="usermem",
            action="create",
            fact_text="Prefer concise release notes.",
        ),
        context=context,
    )
    stale_digest = created["fact"]["record_digest"]
    current = service.execute(
        PinnedMemoryCommand(
            bank="usermem",
            action="replace",
            memory_id=1,
            fact_text="Prefer concise release notes with links.",
        ),
        context=context,
    )

    with pytest.raises(PinnedMemoryConflictError, match="changed after it was loaded"):
        service.execute(
            PinnedMemoryCommand(
                bank="usermem",
                action="replace",
                memory_id=1,
                fact_text="Overwrite a newer edit.",
                expected_record_digest=stale_digest,
            ),
            context=context,
        )
    with pytest.raises(PinnedMemoryConflictError, match="changed after it was loaded"):
        service.execute(
            PinnedMemoryCommand(
                bank="usermem",
                action="delete",
                memory_id=1,
                expected_record_digest=stale_digest,
            ),
            context=context,
        )

    shown = service.execute(
        PinnedMemoryCommand(bank="usermem", action="show", memory_id=1),
        context=context,
    )
    assert shown["fact"]["fact_text"] == "Prefer concise release notes with links."
    assert shown["fact"]["record_digest"] == current["fact"]["record_digest"]


def test_pinned_memory_service_event_ids_undo_and_id_first_aliases(
    tmp_path: Path,
) -> None:
    service = PinnedMemoryService(_repo(tmp_path))
    context = _context()

    created = service.execute_command(
        '/coremem add "Team reviews runbooks" tags="Ops Team,deploy,deploy"',
        bank="coremem",
        context=context,
    )
    create_event = service.repository.get_latest_event(
        bank="coremem",
        team_id="team_test",
        memory_id=1,
        event_type="create",
    )
    shown = service.execute_command("/coremem 1 show", bank="coremem", context=context)
    replaced = service.execute_command(
        '/coremem 1 replace "Team reviews runbooks weekly"',
        bank="coremem",
        context=context,
    )
    replace_event = service.repository.get_latest_event(
        bank="coremem",
        team_id="team_test",
        memory_id=1,
        event_type="replace",
    )

    assert create_event is not None
    assert replace_event is not None
    assert f"Undo: /coremem undo {create_event.event_id}" in created
    assert "Team reviews runbooks" in shown
    assert f"Undo: /coremem undo {replace_event.event_id}" in replaced
    assert service.repository.list_coremem(team_id="team_test")[0].tags == [
        "deploy",
        "ops-team",
    ]

    undo_replace = service.execute_command(
        f"/coremem undo {replace_event.event_id}", bank="coremem", context=context
    )

    assert "undo recorded" in undo_replace
    assert (
        service.repository.list_coremem(team_id="team_test")[0].fact_text
        == "Team reviews runbooks"
    )

    deleted = service.execute_command(
        "/coremem 1 delete", bank="coremem", context=context
    )
    delete_event = service.repository.get_latest_event(
        bank="coremem",
        team_id="team_test",
        memory_id=1,
        event_type="delete",
    )

    assert delete_event is not None
    assert f"Undo: /coremem undo {delete_event.event_id}" in deleted
    assert service.repository.list_coremem(team_id="team_test") == []

    undo_delete = service.execute_command(
        f"/coremem undo {delete_event.event_id}", bank="coremem", context=context
    )

    assert "undo recorded" in undo_delete
    assert (
        service.repository.list_coremem(team_id="team_test")[0].fact_text
        == "Team reviews runbooks"
    )


def test_pinned_memory_service_requires_canonical_user_for_usermem(
    tmp_path: Path,
) -> None:
    service = PinnedMemoryService(_repo(tmp_path))
    context = PinnedMemoryRequestContext(team_id="team_test", canonical_user_id=None)

    result = service.execute_command(
        '/usermem add "Likes concise replies"', bank="usermem", context=context
    )

    assert "Use /user first" in result
