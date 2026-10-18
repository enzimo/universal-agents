import logging
from pathlib import Path

import pytest

from assistant_gateway.ports.repositories import (
    DeliveryRepository,
    IdentityRepository,
    RequestRepository,
    SessionRepository,
    TranscriptRepository,
)
from assistant_gateway.store import GatewayStateStore
from tests.postgres_test_helpers import patch_fake_postgres
from storage.config import resolve_storage_settings
from storage.gateway_state_repository import create_gateway_state_repository


def test_gateway_state_repository_roundtrips_sessions_search_and_conversations(
    tmp_path: Path,
) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/gateway_state.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_gateway_state_repository(settings, logger=logging.getLogger("test"))

    session = repo.create_session(
        session_key="session_alpha",
        session_alias="general",
        conversation_id="conv_alpha",
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
        thread_id="thread_1",
    )
    repo.upsert_session(
        session_key=session.session_key,
        conversation_id="conv_alpha",
        last_task_id="task_1",
        manager_agent_id="manager_1",
    )
    repo.register_conversation_session(
        session_key=session.session_key,
        conversation_id="conv_alpha_followup",
        last_task_id="task_2",
    )
    repo.record_request(
        correlation_id="corr_alpha",
        session_key=session.session_key,
        request_kind="interactive",
        request_text="Plan the alpha migration",
        conversation_id="conv_alpha",
        followup_to_task_id=None,
    )

    loaded = repo.get_session("session_alpha")
    mapped = repo.get_session_by_conversation_id("conv_alpha_followup")
    previews = repo.list_sessions_with_preview(limit=5, offset=0)
    conversations = repo.list_session_conversations("session_alpha", limit=5)
    matches = repo.search_sessions("alpha")

    assert loaded is not None
    assert loaded.last_task_id == "task_1"
    assert loaded.chat_id == "chat_1"
    assert mapped is not None
    assert mapped.session_key == "session_alpha"
    assert repo.get_session_by_alias("general") is not None
    assert (
        repo.get_active_session_for_channel(
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_1",
            thread_id="thread_1",
        )
        is not None
    )
    assert len(previews) == 1
    assert previews[0].latest_request_text == "Plan the alpha migration"
    assert len(conversations) == 2
    assert conversations[0].conversation_id == "conv_alpha_followup"
    assert matches
    assert matches[0].matched_request_text == "Plan the alpha migration"
    request = repo.get_request("corr_alpha")
    assert request is not None
    assert request.active_task_id is None
    repo.update_request_coordination(
        "corr_alpha",
        status="waiting_for_permission",
        active_task_id="task_active_alpha",
    )
    request = repo.get_request("corr_alpha")
    assert request is not None
    assert request.status == "waiting_for_permission"
    assert request.active_task_id == "task_active_alpha"
    assert repo.set_session_status("session_alpha", "archived") is True
    assert repo.get_session("session_alpha").status == "archived"
    assert repo.set_session_status("missing_session", "archived") is False


def test_gateway_state_repository_roundtrips_deliveries_queries_and_scheduled_targets(
    tmp_path: Path,
) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/gateway_state.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_gateway_state_repository(settings, logger=logging.getLogger("test"))

    repo.create_session(
        session_key="session_alpha",
        conversation_id="conv_alpha",
        manager_agent_id="manager_1",
    )
    delivery_id = repo.add_deferred_delivery(
        session_key="session_alpha",
        origin_kind="cron",
        payload={"hello": "world"},
        rendered_text="Deferred result",
        available_after="2000-01-01T00:00:00+00:00",
        correlation_id="corr_delivery",
    )
    repo.upsert_pending_query(
        session_key="session_alpha",
        correlation_id="corr_query",
        task_query_id="task_query_1",
        parent_task_id="task_1",
        conversation_id="conv_alpha",
        query_text="Which project?",
        query_payload_json='{"message_class":"TaskQuery","message":"Which project?"}',
    )
    repo.upsert_scheduled_target(
        scheduled_job_id="schedule_1",
        session_key="session_alpha",
        conversation_id="conv_alpha",
    )

    due = repo.get_due_deliveries()
    pending = repo.get_pending_query("session_alpha")
    target = repo.get_scheduled_target_by_conversation_id("conv_alpha")

    assert len(due) == 1
    assert due[0].delivery_id == delivery_id
    assert pending is not None
    assert pending.task_query_id == "task_query_1"
    assert (
        pending.query_payload_json
        == '{"message_class":"TaskQuery","message":"Which project?"}'
    )
    assert target is not None
    assert target.scheduled_job_id == "schedule_1"

    repo.mark_delivery_delivered(delivery_id)
    repo.clear_pending_query("session_alpha")

    assert repo.list_pending_deliveries() == []
    assert repo.get_pending_query("session_alpha") is None
    assert repo.remove_scheduled_target("schedule_1") is True
    assert repo.get_scheduled_target("schedule_1") is None


def test_gateway_state_repository_roundtrips_gateway_users_bindings_tokens_and_codes(
    tmp_path: Path,
) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/gateway_state.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_gateway_state_repository(settings, logger=logging.getLogger("test"))

    user = repo.create_gateway_user(
        canonical_user_id="user_alpha",
        display_name="Alpha",
        timezone="America/Chicago",
        metadata={"source": "test"},
    )
    membership = repo.create_team_membership(
        canonical_user_id=user.canonical_user_id,
        team_id="team_ua",
        role="member",
    )
    binding = repo.create_channel_binding(
        canonical_user_id=user.canonical_user_id,
        channel_name="telegram",
        external_user_id="tg_123",
    )
    token = repo.create_web_user_token(
        token_hash="hash_alpha",
        canonical_user_id=user.canonical_user_id,
        label="browser",
    )
    invite = repo.create_web_user_invite(
        invite_hash="invite_hash_alpha",
        canonical_user_id=user.canonical_user_id,
        label="initial browser",
        expires_at_utc="2999-01-01T00:00:00+00:00",
        created_by_canonical_user_id="user_admin",
    )
    link = repo.create_link_code(
        link_code_hash="code_hash_alpha",
        canonical_user_id=user.canonical_user_id,
        expires_at_utc="2999-01-01T00:00:00+00:00",
        created_by_channel="telegram",
    )

    loaded = repo.get_gateway_user("user_alpha")
    active = repo.get_active_channel_binding(
        channel_name="telegram", external_user_id="tg_123"
    )
    resolved_token = repo.resolve_web_user_token(token_hash="hash_alpha")
    consumed_invite = repo.consume_web_user_invite(
        invite_hash="invite_hash_alpha",
        team_id="team_ua",
    )
    consumed_link = repo.consume_link_code(link_code_hash="code_hash_alpha")

    assert loaded is not None
    assert loaded.metadata == {"source": "test"}
    assert membership.team_id == "team_ua"
    assert repo.list_team_memberships("user_alpha", status="active")[0].role == "member"
    assert active is not None
    assert active.binding_id == binding.binding_id
    assert [item.canonical_user_id for item in repo.list_gateway_users()] == [
        "user_alpha"
    ]
    active_bindings = repo.list_channel_bindings_for_user("user_alpha", status="active")
    assert [item.binding_id for item in active_bindings] == [binding.binding_id]
    assert resolved_token is not None
    assert resolved_token.token_id == token.token_id
    assert consumed_invite is not None
    assert consumed_invite.invite_id == invite.invite_id
    assert consumed_invite.consumed_at_utc is not None
    assert (
        repo.consume_web_user_invite(invite_hash="invite_hash_alpha", team_id="team_ua")
        is None
    )
    assert consumed_link is not None
    assert consumed_link.canonical_user_id == link.canonical_user_id
    assert repo.consume_link_code(link_code_hash="code_hash_alpha") is None

    replacement_user = repo.create_gateway_user(
        canonical_user_id="user_beta",
        display_name="Beta",
        timezone="UTC",
    )
    replacement = repo.replace_channel_binding(
        canonical_user_id=replacement_user.canonical_user_id,
        channel_name="telegram",
        external_user_id="tg_123",
    )
    replaced_active = repo.get_active_channel_binding(
        channel_name="telegram", external_user_id="tg_123"
    )

    assert replaced_active is not None
    assert replaced_active.binding_id == replacement.binding_id
    assert replaced_active.canonical_user_id == "user_beta"
    assert repo.revoke_web_user_token(token.token_id) is True
    assert repo.resolve_web_user_token(token_hash="hash_alpha") is None


def test_a_link_code_two_uses_read_as_unused_is_given_to_only_one(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/gateway_state.db"}},
        base_dir=str(tmp_path),
    )
    first = create_gateway_state_repository(settings, logger=logging.getLogger("test"))
    second = create_gateway_state_repository(settings, logger=logging.getLogger("test"))
    first.create_gateway_user(canonical_user_id="user_alpha", display_name="Alpha", timezone="UTC")
    first.create_link_code(
        link_code_hash="code_hash_alpha",
        canonical_user_id="user_alpha",
        expires_at_utc="2999-01-01T00:00:00+00:00",
        created_by_channel="telegram",
    )
    open_connection = first._open_connection
    used_by_second: list[object] = []

    class SecondUseAfterRead:
        """The first use's connection: right after it reads the code as unused, the second use takes it."""

        def __init__(self) -> None:
            self._conn = open_connection()

        def __enter__(self) -> "SecondUseAfterRead":
            self._conn.__enter__()
            return self

        def __exit__(self, *exc: object) -> object:
            return self._conn.__exit__(*exc)

        def execute(self, sql: str, params: object = ()) -> object:
            cursor = self._conn.execute(sql, params)
            if "FROM gateway_user_link_codes" in sql and not used_by_second:
                rows = cursor.fetchall()
                used_by_second.append(second.consume_link_code(link_code_hash="code_hash_alpha"))
                return _Rows(rows)
            return cursor

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

    first._open_connection = SecondUseAfterRead  # type: ignore[method-assign]

    assert first.consume_link_code(link_code_hash="code_hash_alpha") is None
    assert used_by_second and used_by_second[0] is not None, "the second use got the code"


class _Rows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def fetchone(self) -> object:
        return self._rows[0] if self._rows else None


def test_gateway_state_repository_roundtrips_per_user_appearance(
    tmp_path: Path,
) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/gateway_state.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_gateway_state_repository(settings, logger=logging.getLogger("test"))
    repo.create_gateway_user(
        canonical_user_id="user_alpha",
        display_name="Alpha",
        timezone="UTC",
    )
    repo.create_gateway_user(
        canonical_user_id="user_beta",
        display_name="Beta",
        timezone="UTC",
    )

    created = repo.upsert_gateway_user_appearance(
        canonical_user_id="user_alpha",
        appearance={"color_mode": "dark", "polygon_seed": 4147},
    )
    updated = repo.upsert_gateway_user_appearance(
        canonical_user_id="user_alpha",
        appearance={"color_mode": "light", "polygon_seed": 8675309},
    )

    assert created.appearance == {"color_mode": "dark", "polygon_seed": 4147}
    assert updated.appearance == {"color_mode": "light", "polygon_seed": 8675309}
    assert updated.created_at_utc == created.created_at_utc
    assert repo.get_gateway_user_appearance("user_beta") is None
    assert repo.delete_gateway_user_appearance("user_beta") is False
    assert repo.delete_gateway_user_appearance("user_alpha") is True
    assert repo.get_gateway_user_appearance("user_alpha") is None
    with pytest.raises(ValueError, match="active gateway user"):
        repo.upsert_gateway_user_appearance(
            canonical_user_id="missing_user",
            appearance={"color_mode": "dark"},
        )


def test_gateway_state_repository_pending_web_token_requires_owner_scoped_activation(
    tmp_path: Path,
) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/gateway_state.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_gateway_state_repository(settings, logger=logging.getLogger("test"))
    user = repo.create_gateway_user(
        canonical_user_id="user_pending",
        display_name="Pending",
        timezone="UTC",
    )
    pending = repo.create_web_user_token(
        token_hash="pending_hash",
        canonical_user_id=user.canonical_user_id,
        label="Initial browser",
        status="pending",
        expires_at_utc="2999-01-01T00:00:00+00:00",
    )

    assert pending.status == "pending"
    assert repo.resolve_web_user_token(token_hash="pending_hash") is None
    assert (
        repo.activate_web_user_token(
            token_id=pending.token_id,
            canonical_user_id="another_user",
            expires_at_utc=None,
        )
        is None
    )

    active = repo.activate_web_user_token(
        token_id=pending.token_id,
        canonical_user_id=user.canonical_user_id,
        expires_at_utc=None,
    )

    assert active is not None
    assert active.status == "active"
    assert active.expires_at_utc is None
    assert repo.resolve_web_user_token(token_hash="pending_hash") is not None
    assert (
        repo.revoke_web_user_token(pending.token_id, canonical_user_id="another_user")
        is False
    )
    assert (
        repo.revoke_web_user_token(
            pending.token_id,
            canonical_user_id=user.canonical_user_id,
            required_status="pending",
        )
        is False
    )
    assert (
        repo.revoke_web_user_token(
            pending.token_id, canonical_user_id=user.canonical_user_id
        )
        is True
    )
    assert repo.resolve_web_user_token(token_hash="pending_hash") is None

    expired = repo.create_web_user_token(
        token_hash="expired_pending_hash",
        canonical_user_id=user.canonical_user_id,
        status="pending",
        expires_at_utc="2020-01-01T00:00:00+00:00",
    )
    assert (
        repo.activate_web_user_token(
            token_id=expired.token_id,
            canonical_user_id=user.canonical_user_id,
            expires_at_utc=None,
            now_utc="2020-01-01T00:00:01+00:00",
        )
        is None
    )


def test_gateway_state_repository_accepts_postgres_when_driver_is_available(
    tmp_path: Path, monkeypatch
) -> None:
    settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    patch_fake_postgres(monkeypatch)

    repo = create_gateway_state_repository(settings, logger=logging.getLogger("test"))
    assert repo is not None


def test_gateway_state_repository_satisfies_domain_scoped_ports(
    tmp_path: Path,
) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/gateway_state.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_gateway_state_repository(
        settings,
        logger=logging.getLogger("test"),
    )

    assert isinstance(repo, IdentityRepository)
    assert isinstance(repo, SessionRepository)
    assert isinstance(repo, RequestRepository)
    assert isinstance(repo, DeliveryRepository)
    assert isinstance(repo, TranscriptRepository)


def test_gateway_state_store_exposes_restricted_domain_repository_views(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))

    assert isinstance(store.identity_repository, IdentityRepository)
    assert isinstance(store.session_repository, SessionRepository)
    assert isinstance(store.request_repository, RequestRepository)
    assert isinstance(store.delivery_repository, DeliveryRepository)
    assert isinstance(store.transcript_repository, TranscriptRepository)
    assert not hasattr(store.session_repository, "create_gateway_user")
    assert not hasattr(store.identity_repository, "create_session")
    with store.identity_unit_of_work() as unit_of_work:
        assert not hasattr(unit_of_work.identity, "create_session")
    with store.request_unit_of_work() as unit_of_work:
        assert not hasattr(unit_of_work.request, "create_gateway_user")
        assert not hasattr(unit_of_work.session, "add_deferred_delivery")
    with store.delivery_unit_of_work() as unit_of_work:
        assert not hasattr(unit_of_work.delivery, "create_gateway_user")
        assert not hasattr(unit_of_work.request, "create_session")
        assert not hasattr(unit_of_work.session, "create_gateway_user")
    with store.transcript_unit_of_work() as unit_of_work:
        assert not hasattr(unit_of_work.transcript, "create_gateway_user")
        assert not hasattr(unit_of_work.request, "create_session")
        assert not hasattr(unit_of_work.delivery, "create_gateway_user")


def test_create_session_rolls_back_all_session_tables_on_mapping_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/gateway_state.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_gateway_state_repository(
        settings,
        logger=logging.getLogger("test"),
    )

    def fail_mapping(**_kwargs: object) -> None:
        raise RuntimeError("mapping write failed")

    monkeypatch.setattr(repo, "register_conversation_session", fail_mapping)

    with pytest.raises(RuntimeError, match="mapping write failed"):
        repo.create_session(
            session_key="session_atomic",
            conversation_id="conversation_atomic",
            manager_agent_id="manager_1",
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_1",
        )

    assert repo.get_session("session_atomic") is None
    assert (
        repo.get_active_session_for_channel(
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_1",
        )
        is None
    )


def test_identity_unit_of_work_requires_commit_and_rolls_back_on_failure(
    tmp_path: Path,
) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/gateway_state.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_gateway_state_repository(
        settings,
        logger=logging.getLogger("test"),
    )

    with repo.identity_unit_of_work() as unit_of_work:
        unit_of_work.identity.create_gateway_user(
            canonical_user_id="user_uncommitted",
            display_name="Uncommitted",
            timezone="UTC",
        )

    assert repo.get_gateway_user("user_uncommitted") is None

    with pytest.raises(RuntimeError, match="audit failed"):
        with repo.identity_unit_of_work() as unit_of_work:
            unit_of_work.identity.create_gateway_user(
                canonical_user_id="user_rolled_back",
                display_name="Rolled Back",
                timezone="UTC",
            )
            unit_of_work.identity.create_team_membership(
                canonical_user_id="user_rolled_back",
                team_id="team_1",
            )
            raise RuntimeError("audit failed")

    assert repo.get_gateway_user("user_rolled_back") is None
    assert repo.list_team_memberships("user_rolled_back") == []

    with repo.identity_unit_of_work() as unit_of_work:
        unit_of_work.identity.create_gateway_user(
            canonical_user_id="user_committed",
            display_name="Committed",
            timezone="UTC",
        )
        unit_of_work.identity.create_team_membership(
            canonical_user_id="user_committed",
            team_id="team_1",
        )
        unit_of_work.commit()

    assert repo.get_gateway_user("user_committed") is not None
    assert len(repo.list_team_memberships("user_committed")) == 1
