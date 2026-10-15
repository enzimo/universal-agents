from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from assistant_gateway.application.identity_service import (
    GatewayIdentityService,
    GatewayIdentitySettings,
)
from assistant_gateway.store import GatewayStateStore
from tests.people_directory_support import people_directory


class _FixedClock:
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


class _DeterministicSecrets:
    def __init__(self) -> None:
        self._token_number = 0

    def hash_secret(self, raw_secret: str) -> str:
        return f"hashed:{raw_secret}"

    def new_web_user_token(self) -> str:
        self._token_number += 1
        suffix = "" if self._token_number == 1 else f"_{self._token_number}"
        return f"uauser_deterministic{suffix}"

    def new_web_user_invite(self) -> str:
        return "uainvite_deterministic"

    def new_link_code(self) -> str:
        return "LINKCODE1"


class _RoleAuthorizer:
    def can_create_user(
        self,
        *,
        actor_canonical_user_id: str,
        team_id: str,
        roles: tuple[str, ...],
    ) -> bool:
        del actor_canonical_user_id, team_id
        return "admin" in roles

    def can_list_users(
        self,
        *,
        actor_canonical_user_id: str,
        team_id: str,
        roles: tuple[str, ...],
    ) -> bool:
        del actor_canonical_user_id, team_id
        return "admin" in roles

    def can_create_web_invite(
        self,
        *,
        actor_canonical_user_id: str,
        target_canonical_user_id: str | None,
        team_id: str,
        roles: tuple[str, ...],
    ) -> bool:
        del actor_canonical_user_id, target_canonical_user_id, team_id
        return "admin" in roles

    def role_change_refusal(self, **request: object) -> str | None:
        from access_control.people import role_change_refusal

        return role_change_refusal(**request)  # type: ignore[arg-type]

    def status_change_refusal(self, **request: object) -> str | None:
        from access_control.people import status_change_refusal

        return status_change_refusal(**request)  # type: ignore[arg-type]

    def removal_refusal(self, **request: object) -> str | None:
        from access_control.people import removal_refusal

        return removal_refusal(**request)  # type: ignore[arg-type]


def _service(tmp_path: Path) -> tuple[GatewayIdentityService, GatewayStateStore]:
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    with store.identity_unit_of_work() as unit_of_work:
        unit_of_work.identity.create_gateway_user(
            canonical_user_id="admin_1",
            display_name="Admin",
            timezone="America/Chicago",
        )
        unit_of_work.identity.create_team_membership(
            canonical_user_id="admin_1",
            team_id="team_1",
            role="admin",
        )
        unit_of_work.identity.create_channel_binding(
            canonical_user_id="admin_1",
            channel_name="telegram",
            external_user_id="tg_admin",
        )
        unit_of_work.commit()
    directory, _people = people_directory(
        tmp_path, store.identity_repository, team_id="team_1", clock=_FixedClock()
    )
    service = GatewayIdentityService(
        repository=store.identity_repository,
        unit_of_work_factory=store,
        authorizer=_RoleAuthorizer(),
        secrets=_DeterministicSecrets(),
        clock=_FixedClock(),
        people_directory=directory,
        settings=GatewayIdentitySettings(
            enabled=True,
            allow_newuser_registration=True,
            web_user_tokens_enabled=True,
            web_user_token_ttl_days=7,
            web_user_invite_ttl_seconds=900,
            link_code_ttl_seconds=600,
            default_timezone="UTC",
            team_id="team_1",
            web_chat_url="https://agents.example.test/webchat",
        ),
    )
    return service, store


def test_identity_service_registers_and_resolves_users_without_gateway(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    actor = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_admin",
    )

    result = service.register_user_for_channel(
        actor=actor,
        display_name="New User",
        channel_name="telegram",
        external_user_id="tg_new",
        chat_scope="",
        thread_id=None,
    )

    assert result.text.startswith("Registered user ")
    binding = store.identity_repository.get_active_channel_binding(
        channel_name="telegram",
        external_user_id="tg_new",
    )
    assert binding is not None
    resolved = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_new",
    )
    assert resolved is not None
    assert resolved.user.display_name == "New User"
    assert resolved.has_role("team_1", "member")


def test_identity_service_owns_token_and_link_code_workflows(
    tmp_path: Path,
) -> None:
    service, _store = _service(tmp_path)
    actor = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_admin",
    )

    token_text = service.create_web_token(
        resolved_user=actor,
        label="browser",
        channel_name="telegram",
    )
    linked_token_user = service.resolve_web_token("uauser_deterministic")
    link_text = service.create_link_code(
        resolved_user=actor,
        channel_name="telegram",
    )
    linked_channel_text = service.link_code_for_channel(
        code="LINKCODE1",
        channel_name="signal",
        external_user_id="+15551234567",
        chat_scope="",
        thread_id=None,
    )

    assert "uauser_deterministic" in token_text
    assert linked_token_user is not None
    assert linked_token_user.user.canonical_user_id == "admin_1"
    assert "LINKCODE1" in link_text
    assert "Expires: Jul 27, 2026 at 7:10:00 AM CDT" in link_text
    assert linked_channel_text == (
        "Linked signal identity +15551234567 to user admin_1."
    )


def test_identity_service_renders_user_timestamps_in_requester_timezone(
    tmp_path: Path,
) -> None:
    service, _store = _service(tmp_path)
    actor = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_admin",
    )
    assert actor is not None

    listed = service.render_user_list(actor=actor)
    token_text = service.create_web_token(
        resolved_user=actor,
        label="browser",
        channel_name="telegram",
    )

    assert "last_seen: " in listed
    assert "last_seen_at_utc" not in listed
    assert "+00:00" not in listed
    assert "CDT" in listed
    assert "Expires: Aug 3, 2026 at 7:00:00 AM CDT" in token_text


def test_identity_service_owns_invite_and_permanent_token_enrollment(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    actor = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_admin",
    )
    assert actor is not None

    invite_text = service.create_web_invite(
        actor=actor,
        target=None,
        display_name="Alice",
        label="Initial browser",
        create_new=True,
        channel_name="telegram",
        chat_scope="",
    )
    invited = service.consume_web_invite("uainvite_deterministic")

    assert invite_text.text.startswith("Registered web-only user.")
    assert "https://agents.example.test/webchat" in invite_text.text
    assert "Invite code: uainvite_deterministic" in invite_text.text
    assert invited is not None
    assert invited.user.display_name == "Alice"
    assert invited.web_invite_label == "Initial browser"
    assert invited.has_role("team_1", "member")
    assert service.consume_web_invite("uainvite_deterministic") is None

    first_pending = service.issue_enrollment_token(
        canonical_user_id=invited.user.canonical_user_id,
        label=invited.web_invite_label or "",
        pending_expires_at_utc="2026-07-27T13:00:00+00:00",
    )
    assert first_pending is not None
    assert first_pending.record.status == "pending"
    assert service.resolve_web_token(first_pending.raw_token) is None

    replacement = service.issue_enrollment_token(
        canonical_user_id=invited.user.canonical_user_id,
        label="Alice laptop",
        pending_expires_at_utc="2026-07-27T13:00:00+00:00",
        replacement_token_id=first_pending.record.token_id,
    )
    assert replacement is not None
    assert replacement.record.status == "pending"
    assert (
        store.identity_repository.activate_web_user_token(
            token_id=first_pending.record.token_id,
            canonical_user_id=invited.user.canonical_user_id,
            expires_at_utc=None,
            now_utc="2026-07-27T12:30:00+00:00",
        )
        is None
    )

    activated = service.activate_enrollment_token(
        canonical_user_id=invited.user.canonical_user_id,
        token_id=replacement.record.token_id,
    )
    resolved = service.resolve_web_token(replacement.raw_token)

    assert activated is not None
    assert activated.status == "active"
    assert activated.expires_at_utc == "2026-08-03T12:00:00+00:00"
    assert resolved is not None
    assert resolved.user.canonical_user_id == invited.user.canonical_user_id

    disabled_text = service.set_user_enabled_status(
        actor=actor,
        target=invited.user.canonical_user_id,
        enabled=False,
        channel_name="telegram",
        chat_scope="",
    )
    assert disabled_text.text.startswith("Disabled Alice (")
    assert not service.is_web_chat_user_active(invited.user.canonical_user_id)
    assert service.resolve_web_token(replacement.raw_token) is None


def test_identity_service_rejects_member_web_invites(tmp_path: Path) -> None:
    service, _store = _service(tmp_path)
    admin = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_admin",
    )
    assert admin is not None
    service.register_user_for_channel(
        actor=admin,
        display_name="Member",
        channel_name="telegram",
        external_user_id="tg_member",
        chat_scope="",
        thread_id=None,
    )
    member = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_member",
    )

    result = service.create_web_invite(
        actor=member,
        target="admin_1",
        display_name=None,
        label="Forbidden",
        create_new=False,
        channel_name="telegram",
        chat_scope="",
    )

    assert result.text == "Only an admin can create Web Chat invitations."
    assert result.person_id is None


def test_identity_service_rolls_back_web_token_when_audit_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service(tmp_path)
    actor = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_admin",
    )
    assert actor is not None

    def fail_audit(**_kwargs: object) -> str:
        raise RuntimeError("audit failed")

    monkeypatch.setattr(
        store._repository,  # noqa: SLF001 - verifies the concrete transaction.
        "record_gateway_user_event",
        fail_audit,
    )

    with pytest.raises(RuntimeError, match="audit failed"):
        service.create_web_token(
            resolved_user=actor,
            label="browser",
            channel_name="telegram",
        )

    assert service.resolve_web_token("uauser_deterministic") is None


def test_identity_service_rolls_back_web_only_user_when_audit_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service(tmp_path)
    actor = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_admin",
    )
    assert actor is not None

    def fail_audit(**_kwargs: object) -> str:
        raise RuntimeError("audit failed")

    monkeypatch.setattr(
        store._repository,  # noqa: SLF001 - verifies the concrete transaction.
        "record_gateway_user_event",
        fail_audit,
    )

    with pytest.raises(RuntimeError, match="audit failed"):
        service.create_web_invite(
            actor=actor,
            target=None,
            display_name="Rolled Back",
            label="Initial browser",
            create_new=True,
            channel_name="telegram",
            chat_scope="",
        )

    assert [user.canonical_user_id for user in store.list_gateway_users()] == [
        "admin_1"
    ]
    assert (
        store.consume_web_user_invite(
            invite_hash="hashed:uainvite_deterministic",
            team_id="team_1",
            now_utc="2026-07-27T12:00:01+00:00",
        )
        is None
    )


def test_identity_service_rolls_back_invite_consumption_when_audit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service(tmp_path)
    actor = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_admin",
    )
    assert actor is not None
    service.create_web_invite(
        actor=actor,
        target=None,
        display_name="Alice",
        label="Initial browser",
        create_new=True,
        channel_name="telegram",
        chat_scope="",
    )

    def fail_audit(**_kwargs: object) -> str:
        raise RuntimeError("audit failed")

    monkeypatch.setattr(
        store._repository,  # noqa: SLF001 - verifies the concrete transaction.
        "record_gateway_user_event",
        fail_audit,
    )

    with pytest.raises(RuntimeError, match="audit failed"):
        service.consume_web_invite("uainvite_deterministic")

    monkeypatch.undo()
    assert service.consume_web_invite("uainvite_deterministic") is not None


def test_identity_service_rolls_back_pending_token_replacement_when_audit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service(tmp_path)
    actor = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_admin",
    )
    assert actor is not None
    service.create_web_invite(
        actor=actor,
        target=None,
        display_name="Alice",
        label="Initial browser",
        create_new=True,
        channel_name="telegram",
        chat_scope="",
    )
    invited = service.consume_web_invite("uainvite_deterministic")
    assert invited is not None
    pending = service.issue_enrollment_token(
        canonical_user_id=invited.user.canonical_user_id,
        label="Initial browser",
        pending_expires_at_utc="2026-07-27T13:00:00+00:00",
    )
    assert pending is not None

    def fail_audit(**_kwargs: object) -> str:
        raise RuntimeError("audit failed")

    monkeypatch.setattr(
        store._repository,  # noqa: SLF001 - verifies the concrete transaction.
        "record_gateway_user_event",
        fail_audit,
    )

    with pytest.raises(RuntimeError, match="audit failed"):
        service.issue_enrollment_token(
            canonical_user_id=invited.user.canonical_user_id,
            label="Replacement",
            pending_expires_at_utc="2026-07-27T13:00:00+00:00",
            replacement_token_id=pending.record.token_id,
        )

    monkeypatch.undo()
    activated = service.activate_enrollment_token(
        canonical_user_id=invited.user.canonical_user_id,
        token_id=pending.record.token_id,
    )
    assert activated is not None
    assert service.resolve_web_token(pending.raw_token) is not None
    assert service.resolve_web_token("uauser_deterministic_2") is None


def test_identity_service_rolls_back_token_activation_when_audit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service(tmp_path)
    actor = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_admin",
    )
    assert actor is not None
    service.create_web_invite(
        actor=actor,
        target=None,
        display_name="Alice",
        label="Initial browser",
        create_new=True,
        channel_name="telegram",
        chat_scope="",
    )
    invited = service.consume_web_invite("uainvite_deterministic")
    assert invited is not None
    pending = service.issue_enrollment_token(
        canonical_user_id=invited.user.canonical_user_id,
        label="Initial browser",
        pending_expires_at_utc="2026-07-27T13:00:00+00:00",
    )
    assert pending is not None

    def fail_audit(**_kwargs: object) -> str:
        raise RuntimeError("audit failed")

    monkeypatch.setattr(
        store._repository,  # noqa: SLF001 - verifies the concrete transaction.
        "record_gateway_user_event",
        fail_audit,
    )

    with pytest.raises(RuntimeError, match="audit failed"):
        service.activate_enrollment_token(
            canonical_user_id=invited.user.canonical_user_id,
            token_id=pending.record.token_id,
        )

    monkeypatch.undo()
    activated = service.activate_enrollment_token(
        canonical_user_id=invited.user.canonical_user_id,
        token_id=pending.record.token_id,
    )
    assert activated is not None
    assert service.resolve_web_token(pending.raw_token) is not None


def test_identity_service_rolls_back_pending_token_revocation_when_audit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service(tmp_path)
    actor = service.resolve_canonical_user(
        channel_name="telegram",
        external_user_id="tg_admin",
    )
    assert actor is not None
    service.create_web_invite(
        actor=actor,
        target=None,
        display_name="Alice",
        label="Initial browser",
        create_new=True,
        channel_name="telegram",
        chat_scope="",
    )
    invited = service.consume_web_invite("uainvite_deterministic")
    assert invited is not None
    pending = service.issue_enrollment_token(
        canonical_user_id=invited.user.canonical_user_id,
        label="Initial browser",
        pending_expires_at_utc="2026-07-27T13:00:00+00:00",
    )
    assert pending is not None

    def fail_audit(**_kwargs: object) -> str:
        raise RuntimeError("audit failed")

    monkeypatch.setattr(
        store._repository,  # noqa: SLF001 - verifies the concrete transaction.
        "record_gateway_user_event",
        fail_audit,
    )

    with pytest.raises(RuntimeError, match="audit failed"):
        service.revoke_pending_enrollment_token(
            canonical_user_id=invited.user.canonical_user_id,
            token_id=pending.record.token_id,
        )

    monkeypatch.undo()
    assert service.revoke_pending_enrollment_token(
        canonical_user_id=invited.user.canonical_user_id,
        token_id=pending.record.token_id,
    )
    assert (
        service.activate_enrollment_token(
            canonical_user_id=invited.user.canonical_user_id,
            token_id=pending.record.token_id,
        )
        is None
    )
