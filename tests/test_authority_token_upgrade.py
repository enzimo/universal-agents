"""Tests for Unit 8: first-start authority-token conversion (A5, C03, C05, C26) and signing-key removal (A6)."""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from access_control.authority_verifier import AuthorityTokenVerifier, set_process_verifier
from access_control.ownership import request_context_id, sent_files_access
from access_control.people import PERSON_ROLE_ADMIN
from access_control.task_identity import resolve_task_identity
from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    PersonStanding,
    TeamPersonV1,
    new_person_ref,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3, TaskSnapshotRecord
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.worker_handback_v1 import WorkerTaskHandbackV1
from custom_messages.enums import ProgressState
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import verify_authority_token
from custom_messages.wire.kinds import AUTHORITY_TOKEN_MAX_SECONDS
from custom_messages.wire.models import AuthorityToken
from docker.launch_headless import build_process_env, prepare_authority_tokens_for_launch
from instance_upgrades.authority_tokens import (
    MissingLegacyVerifyKeysError,
    SigningIdentities,
    authority_tokens_upgrade_done,
    has_legacy_authority_records,
    upgrade_authority_tokens,
)
from internal_credentials import InternalCredentialRequest, generate_internal_credentials, internal_credential_names
from runtime_settings.security import (
    REMOVED_SIGNING_KEY_ENV_VARS,
    reject_removed_signing_key_env_vars,
    removed_signing_key_env_problems,
)
from storage.access_repository import AccessRepository
from storage.change_ledger import RequestState
from storage.runtime_snapshot_layout import canonical_json, payload_digest
from storage.runtime_state_repository import create_runtime_state_repository
from tests.broker_access_support import (
    broker_gateway_identity,
    broker_scheduler_identity,
    broker_trust_store,
    forget_task_context_keys,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SQLITE_09_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "upgrade" / "0.9-sqlite.sql"
TEAM = "team_upgrade"


# ---------------------------------------------------------------------------
# Pure 0.9 legacy Ed25519 signer (exact 0.9.x format, no issuer_roles field)
# ---------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class _Legacy09Signer:
    def __init__(self, issuer: str, key_id: str) -> None:
        self.issuer = issuer
        self.key_id = key_id
        self._priv = Ed25519PrivateKey.generate()

    @property
    def env_private_value(self) -> str:
        return f"{self.key_id}:{_b64(self._priv.private_bytes_raw())}"

    @property
    def verify_entry(self) -> dict[str, str]:
        pub = self._priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return {"key_id": self.key_id, "issuer": self.issuer, "public_key": _b64(pub)}

    def sign_context(
        self,
        *,
        context_id: str,
        team_id: str,
        person_id: str | None,
        issued_at: datetime,
        expires_at: datetime,
        person_roles: tuple[str, ...] = (),
        root_task_id: str | None = None,
        conversation_id: str | None = None,
        schedule_id: str | None = None,
    ) -> dict[str, Any]:
        # Exact 0.9 TaskContextV1 JSON shape (no issuer_roles field).
        ctx: dict[str, Any] = {
            "schema_version": 1,
            "context_id": context_id,
            "issuer": self.issuer,
            "team_id": team_id,
            "person_id": person_id,
            "person_roles": list(person_roles),
            "root_task_id": root_task_id,
            "conversation_id": conversation_id,
            "schedule_id": schedule_id,
            "issued_at_utc": issued_at.isoformat().replace("+00:00", "Z"),
            "expires_at_utc": expires_at.isoformat().replace("+00:00", "Z"),
        }
        payload = b"ua-task-context-v1\x00" + json.dumps(ctx, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {"context": ctx, "key_id": self.key_id, "signature": _b64(self._priv.sign(payload))}

    def sign_permit(
        self,
        *,
        permit_id: str,
        team_id: str,
        schedule_id: str,
        person_id: str | None,
        issued_at: datetime,
        expires_at: datetime,
    ) -> dict[str, Any]:
        permit: dict[str, Any] = {
            "schema_version": 1,
            "record_kind": "schedule_permit",
            "permit_id": permit_id,
            "issuer": "scheduler",
            "team_id": team_id,
            "schedule_id": schedule_id,
            "person_id": person_id,
            "issued_at_utc": issued_at.isoformat().replace("+00:00", "Z"),
            "expires_at_utc": expires_at.isoformat().replace("+00:00", "Z"),
        }
        payload = b"ua-schedule-permit-v1\x00" + json.dumps(permit, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {"permit": permit, "key_id": self.key_id, "signature": _b64(self._priv.sign(payload))}


def _make_config(tmp_path: Path, *, team_id: str = TEAM) -> GatewayInstanceConfig:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="upgrade_test",
        instance_root=str(root),
        team_id=team_id,
        timezone="UTC",
    )
    config.layout.control_dir.mkdir(parents=True, exist_ok=True)
    config.save(root / "instance_config.yaml")
    return config


def _init_09_sqlite(db_path: Path) -> None:
    from scripts.upgrade_schema_fixture import is_vector_table

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        for raw_line in SQLITE_09_FIXTURE.read_text(encoding="utf-8").splitlines():
            stmt = raw_line.strip()
            if not stmt or stmt.startswith("--") or is_vector_table(stmt):
                continue
            conn.execute(stmt)
        conn.commit()


def _legacy_person_row(team_id: str, person_id: str, roles: list[str], standing: str, now_iso: str) -> str:
    # 0.9 access_people payload_json had NO person_ref field.
    return json.dumps(
        {
            "schema_version": 1,
            "team_id": team_id,
            "person_id": person_id,
            "standing": standing,
            "roles": roles,
            "changed_at_utc": now_iso,
        }
    )


def _task_record_dict(
    *,
    task_id: str,
    owner_person_id: str | None,
    state: str = "waiting",
    task_context: dict[str, Any] | None = None,
    schedule_run: dict[str, Any] | None = None,
    conversation_id: str = "conv_1",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "state": state,
        "owner_person_id": owner_person_id,
        "task": {
            "task_id": task_id,
            "task_type": "request",
            "problem_statement": f"Handle {task_id}",
        },
        "header": {"from_agent_id": "gateway", "response_routing_key": "user_queue", "agent_signature": ""},
        "meta": {
            "conversation_id": conversation_id,
            "gateway_context": {
                "canonical_user_id": owner_person_id,
                "task_context": task_context,
                "schedule_run": schedule_run,
            },
        },
    }


def _schedule_dict(
    *,
    schedule_id: str,
    name: str,
    owner_id: str | None,
    task_context: dict[str, Any] | None,
    management_class: str = "user",
    system_schedule_kind: str | None = None,
) -> dict[str, Any]:
    return {
        "schedule_id": schedule_id,
        "schedule_name": name,
        "management_class": management_class,
        "system_schedule_kind": system_schedule_kind,
        "payload": {"task_type": "request", "problem_statement": f"Run {name}"},
        "payload_meta": {
            "gateway_context": {
                "canonical_user_id": owner_id,
                "task_context": task_context,
            }
        },
        "interval_ms": 3_600_000,
    }


def _signing_identities(team_id: str) -> SigningIdentities:
    return SigningIdentities(
        gateway=broker_gateway_identity(team_id),
        scheduler=broker_scheduler_identity(team_id),
    )


def _verify_real_token(raw: Any, trust: Any, now: datetime):
    fields = json.loads(raw) if isinstance(raw, str) else raw.model_dump(mode="json", exclude_none=True)
    return verify_authority_token(AuthorityToken.model_validate(fields), fields, trust, valid_at=now, now=now)


# ---------------------------------------------------------------------------
# A5 + C03 + C05 + C26: Full 0.9 SQLite conversion test
# ---------------------------------------------------------------------------


def test_09_sqlite_first_start_conversion_end_to_end(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Starting from tests/fixtures/upgrade/0.9-sqlite.sql + added rows converts every stored
    0.9 context and permit to real protocol 1 authority tokens, preserves request ids and file
    bindings, backfills change_requests.token_digest (C26), reports unverifiable and expired
    records separately to administrators, and is a no-op on second start."""

    forget_task_context_keys()
    try:
        config = _make_config(tmp_path)
        manager_db = Path(config.manager_storage_settings.sqlite_path or "")
        gateway_db = Path(config.state_storage_settings.sqlite_path or "")
        _init_09_sqlite(manager_db)
        _init_09_sqlite(gateway_db)

        gw_signer = _Legacy09Signer("gateway", "gateway-old01")
        sched_signer = _Legacy09Signer("scheduler", "scheduler-old01")
        verify_keys_json = json.dumps([gw_signer.verify_entry, sched_signer.verify_entry])
        old_env = {
            "ACCESS_CONTEXT_VERIFY_KEYS": verify_keys_json,
            "ACCESS_GATEWAY_SIGNING_KEY": gw_signer.env_private_value,
        }

        now = datetime.now(timezone.utc).replace(microsecond=0)
        past_issue = now - timedelta(hours=2)
        end_3d = now + timedelta(days=3)
        end_30d = now + timedelta(days=30)
        expired_end = now - timedelta(minutes=10)

        # Build all 0.9 legacy signed records.
        wait_ctx = gw_signer.sign_context(
            context_id="task_context_wait_ada",
            team_id=TEAM,
            person_id="ada",
            person_roles=("owner",),
            root_task_id="task_wait_ada",
            conversation_id="conv_ada",
            issued_at=past_issue,
            expires_at=end_3d,
        )
        expired_ctx = gw_signer.sign_context(
            context_id="task_context_expired_ada",
            team_id=TEAM,
            person_id="ada",
            root_task_id="task_expired_ada",
            issued_at=now - timedelta(days=8),
            expires_at=expired_end,
        )
        forged_ctx = dict(wait_ctx, signature=_b64(b"x" * 64))

        sched_run_permit = sched_signer.sign_permit(
            permit_id="schedule_permit_run1",
            team_id=TEAM,
            schedule_id="sched_ada_active",
            person_id="ada",
            issued_at=past_issue,
            expires_at=end_3d,
        )
        legacy_sched_run = {
            "permit": sched_run_permit,
            "context_id": "schedule_run_abc123",
            "conversation_id": "conv_sched",
            "started_at_utc": past_issue.isoformat(),
        }

        # Deferred message carrying attachments with SHA-256 message_context_<hex> id (C05).
        from custom_messages.contracts.task_v1 import Task, UserTaskAttachment
        from custom_messages.enums import MessageMetaType

        deferred_task = Task(
            task_id="task_deferred_1",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Read attached report.",
            attachments=[UserTaskAttachment(attachment_id="att_1", sha256="a" * 64, filename="report.pdf", size_bytes=128)],
        )
        file_ctx_id = request_context_id(deferred_task)
        deferred_ctx = gw_signer.sign_context(
            context_id=file_ctx_id,
            team_id=TEAM,
            person_id="ada",
            root_task_id="task_deferred_1",
            conversation_id="conv_files",
            issued_at=past_issue,
            expires_at=end_3d,
        )
        deferred_envelope = {
            "msg_header": {"message_id": "msg_deferred_1", "from_agent_id": "gateway", "agent_signature": ""},
            "msg_meta": {
                "conversation_id": "conv_files",
                "gateway_context": {"canonical_user_id": "ada", "task_context": deferred_ctx},
            },
            "msg_content": deferred_task.model_dump(mode="json"),
        }

        # Handback record.
        handback_ctx = gw_signer.sign_context(
            context_id="task_context_handback_1",
            team_id=TEAM,
            person_id="bo",
            root_task_id="task_hb_1",
            issued_at=past_issue,
            expires_at=end_3d,
        )
        handback_payload = {
            "schema_version": 1,
            "message_class": "WorkerTaskHandbackV1",
            "handback_id": "hb_1",
            "team_id": TEAM,
            "worker_agent_id": "worker_1",
            "worker_named_agent_type": "CodeWritingAgent",
            "evidence": {
                "lease_lost_at_utc": past_issue.isoformat(),
                "reason": "lease lost",
            },
            "handed_back": {
                "task_id": "task_hb_1",
                "disposition": "not_started",
                "prior_state": "ACCEPTED",
                "task": {"task_id": "task_hb_1", "task_type": "request", "problem_statement": "Continue work."},
                "header": {"from_agent_id": "lead_1", "agent_signature": ""},
                "meta": {"gateway_context": {"canonical_user_id": "bo", "task_context": handback_ctx}},
            },
        }

        # Turn action outbox record.
        outbox_ctx = gw_signer.sign_context(
            context_id="task_context_outbox_1",
            team_id=TEAM,
            person_id="ada",
            root_task_id="task_wait_ada",
            issued_at=past_issue,
            expires_at=end_3d,
        )
        outbox_record = {
            "action_id": "act_1",
            "batch_id": "batch_1",
            "source_task_id": "task_wait_ada",
            "action_index": 0,
            "action_type": "delegate_task",
            "payload_json": {"gateway_context": {"canonical_user_id": "ada", "task_context": outbox_ctx}},
        }

        # Schedules:
        # 1) Active vouched schedule with 30-day permit (capped to 7 days).
        sched_ada_ctx = gw_signer.sign_context(
            context_id="task_context_sched_ada",
            team_id=TEAM,
            person_id="ada",
            issued_at=past_issue,
            expires_at=end_3d,
        )
        sched_ada_permit_30d = sched_signer.sign_permit(
            permit_id="schedule_permit_30d",
            team_id=TEAM,
            schedule_id="sched_ada_active",
            person_id="ada",
            issued_at=past_issue,
            expires_at=end_30d,
        )
        # 2) Schedule with EXPIRED saved gateway context and no permit -> still vouched, gets fresh permit.
        sched_bo_expired_ctx = gw_signer.sign_context(
            context_id="task_context_sched_bo_old",
            team_id=TEAM,
            person_id="bo",
            issued_at=now - timedelta(days=10),
            expires_at=expired_end,
        )
        # 3) Schedule with NO saved context, only a valid unexpired permit -> vouched by permit, keeps 3d end.
        sched_ada_permit_only = sched_signer.sign_permit(
            permit_id="schedule_permit_only",
            team_id=TEAM,
            schedule_id="sched_ada_permit_only",
            person_id="ada",
            issued_at=past_issue,
            expires_at=end_3d,
        )
        # 4) System-work maintenance schedule with vouched admin owner + system-work permit.
        sched_maint_ctx = gw_signer.sign_context(
            context_id="task_context_sched_maint",
            team_id=TEAM,
            person_id="ada",
            issued_at=past_issue,
            expires_at=end_3d,
        )
        sched_maint_permit = sched_signer.sign_permit(
            permit_id="schedule_permit_maint",
            team_id=TEAM,
            schedule_id="sched_internal_imp",
            person_id=None,
            issued_at=past_issue,
            expires_at=end_3d,
        )
        # 5) Schedule with forged saved gateway context -> preserved, unvouched, reported.
        sched_forged_ctx = dict(sched_ada_ctx, signature=_b64(b"z" * 64))

        # Populate manager SQLite DB (0.9 schema).
        with sqlite3.connect(str(manager_db)) as conn:
            now_iso = past_issue.isoformat()
            conn.execute(
                "INSERT INTO access_people (team_id, person_id, payload_json, changed_at) VALUES (?, ?, ?, ?)",
                (TEAM, "ada", _legacy_person_row(TEAM, "ada", ["owner"], "active", now_iso), now_iso),
            )
            conn.execute(
                "INSERT INTO access_people (team_id, person_id, payload_json, changed_at) VALUES (?, ?, ?, ?)",
                (TEAM, "bo", _legacy_person_row(TEAM, "bo", ["member"], "active", now_iso), now_iso),
            )
            # Runtime snapshot header + records.
            header = canonical_json(
                {
                    "layout_version": 1,
                    "task_order": ["task_wait_ada", "task_sched_run", "task_expired_ada", "task_forged"],
                    "header": {"schema_version": 3, "task_queue": [], "waiting_top_level_task_ids": ["task_wait_ada"]},
                }
            )
            conn.execute(
                "INSERT INTO active_task_runtime (owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, runtime_state_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("lead_1", "Lead", TEAM, "TeamArchitect", header, now_iso),
            )
            for rec_id, rec_doc in (
                ("task_wait_ada", _task_record_dict(task_id="task_wait_ada", owner_person_id="ada", task_context=wait_ctx, conversation_id="conv_ada")),
                ("task_sched_run", _task_record_dict(task_id="task_sched_run", owner_person_id="ada", schedule_run=legacy_sched_run, state="in_progress")),
                ("task_expired_ada", _task_record_dict(task_id="task_expired_ada", owner_person_id="ada", task_context=expired_ctx)),
                ("task_forged", _task_record_dict(task_id="task_forged", owner_person_id="ada", task_context=forged_ctx)),
            ):
                pjson = canonical_json(rec_doc)
                conn.execute(
                    "INSERT INTO active_task_runtime_records (owner_agent_id, section, record_id, payload_json, payload_digest) VALUES (?, ?, ?, ?, ?)",
                    ("lead_1", "tasks", rec_id, pjson, payload_digest(pjson)),
                )
                conn.execute(
                    "INSERT INTO runtime_task_custody (team_id, task_id, holder_agent_id, holder_named_agent_type, claimed_at) VALUES (?, ?, ?, ?, ?)",
                    (TEAM, rec_id, "lead_1", "TeamArchitect", now_iso),
                )
            outbox_json = canonical_json(outbox_record)
            conn.execute(
                "INSERT INTO active_task_runtime_records (owner_agent_id, section, record_id, payload_json, payload_digest) VALUES (?, ?, ?, ?, ?)",
                ("lead_1", "turn_action_outbox", "act_1", outbox_json, payload_digest(outbox_json)),
            )
            # C26: 0.9 change_requests rows (no token_digest column yet).
            conn.execute(
                "INSERT INTO change_requests (team_id, request_id, person_id, start_position, state, opened_at_utc, closed_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (TEAM, "task_context_wait_ada", "ada", 1, RequestState.OPEN.value, now_iso, ""),
            )
            conn.execute(
                "INSERT INTO change_requests (team_id, request_id, person_id, start_position, state, opened_at_utc, closed_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (TEAM, "task_context_other_person", "bo", 2, RequestState.OPEN.value, now_iso, ""),
            )
            # Deferred inbox.
            conn.execute(
                "INSERT INTO agent_deferred_messages (owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, messages_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("lead_1", "Lead", TEAM, "TeamArchitect", json.dumps([deferred_envelope]), now_iso),
            )
            # Handback.
            conn.execute(
                "INSERT INTO worker_task_handbacks (handback_id, team_id, task_id, worker_agent_id, disposition, status, received_at, updated_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("hb_1", TEAM, "task_hb_1", "worker_1", "not_started", "received", now_iso, now_iso, json.dumps(handback_payload)),
            )
            # Conversation task link.
            conn.execute(
                "INSERT INTO conversation_task_links (conversation_id, task_id, turn_index, owner_canonical_user_id, gateway_context_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("conv_ada", "task_wait_ada", 1, "ada", json.dumps({"canonical_user_id": "ada", "task_context": wait_ctx}), now_iso, now_iso),
            )
            # Schedules.
            for sid, sname, owner, sdoc, rpermit in (
                ("sched_ada_active", "Daily digest", "ada", _schedule_dict(schedule_id="sched_ada_active", name="Daily digest", owner_id="ada", task_context=sched_ada_ctx), json.dumps(sched_ada_permit_30d)),
                ("sched_bo_expired_ctx", "Weekly check", "bo", _schedule_dict(schedule_id="sched_bo_expired_ctx", name="Weekly check", owner_id="bo", task_context=sched_bo_expired_ctx), None),
                ("sched_ada_permit_only", "Permit only", "ada", _schedule_dict(schedule_id="sched_ada_permit_only", name="Permit only", owner_id="ada", task_context=None), json.dumps(sched_ada_permit_only)),
                ("sched_internal_imp", "internal_improvement:team", "ada", _schedule_dict(schedule_id="sched_internal_imp", name="internal_improvement:team", owner_id="ada", task_context=sched_maint_ctx, management_class="system", system_schedule_kind="internal_improvement"), json.dumps(sched_maint_permit)),
                ("sched_forged", "Forged reminder", "ada", _schedule_dict(schedule_id="sched_forged", name="Forged reminder", owner_id="ada", task_context=sched_forged_ctx), None),
            ):
                conn.execute(
                    "INSERT INTO scheduled_messages (schedule_id, payload_json, schedule_json, schedule_name, next_fire_at, created_at, enabled, claim_team_id, owner_canonical_user_id, run_permit_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sid, json.dumps({"task_type": "request", "problem_statement": f"Run {sname}"}), json.dumps(sdoc), sname, end_3d.isoformat(), now_iso, 1, TEAM, owner, rpermit),
                )
            conn.commit()

        # Populate gateway_deliveries in gateway_state.db.
        delivery_ctx = gw_signer.sign_context(
            context_id="task_context_delivery_1",
            team_id=TEAM,
            person_id="ada",
            root_task_id="task_wait_ada",
            issued_at=past_issue,
            expires_at=end_3d,
        )
        delivery_envelope = {
            "msg_header": {"message_id": "msg_deliv_1", "from_agent_id": "lead_1", "agent_signature": ""},
            "msg_meta": {"gateway_context": {"canonical_user_id": "ada", "task_context": delivery_ctx}},
            "msg_content": {"message_class": "Task", "task_id": "task_wait_ada", "task_type": "response", "problem_statement": "Done."},
        }
        with sqlite3.connect(str(gateway_db)) as conn:
            conn.execute(
                "INSERT INTO gateway_deliveries (delivery_id, correlation_id, session_key, origin_kind, payload_json, rendered_text, available_after, owner_canonical_user_id, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("deliv_1", "corr_1", "website:ada:chat", "response", json.dumps(delivery_envelope), "Done.", now.isoformat(), "ada", "pending", past_issue.isoformat()),
            )
            conn.commit()

        assert has_legacy_authority_records(config, env=old_env) is True

        identities = _signing_identities(TEAM)
        with caplog.at_level(logging.INFO, logger="instance_upgrades.authority_tokens"):
            result = upgrade_authority_tokens(config, env=old_env, identities=identities, now=now)

        assert result.already_done is False
        assert result.had_legacy_records is True
        assert result.counts.converted_contexts >= 6
        assert result.counts.converted_schedule_runs == 1
        assert result.counts.converted_permits == 3
        assert result.counts.vouched_schedules == 4
        assert result.counts.fresh_permits_issued == 1  # sched_bo_expired_ctx
        assert result.counts.ledger_digests_backfilled == 1
        assert result.counts.expired == 1  # task_expired_ada
        assert result.counts.unverifiable == 2  # task_forged + sched_forged
        assert result.counts.expiry_capped == 1  # 30d permit capped to 7d
        assert result.notice_stored is True
        assert set(result.removed_keys_still_set) == {"ACCESS_CONTEXT_VERIFY_KEYS", "ACCESS_GATEWAY_SIGNING_KEY"}
        assert authority_tokens_upgrade_done(config) is True
        assert has_legacy_authority_records(config, env=old_env) is False
        assert "event=authority_token_conversion_unverifiable" in caplog.text
        assert "event=authority_token_conversion_expired" in caplog.text
        assert "event=authority_token_conversion_remove_old_keys" in caplog.text
        assert "event=authority_tokens_upgraded" in caplog.text

        # Real trust store & verifier for verifying every converted token!
        trust = broker_trust_store()
        installation = identities.gateway.installation
        access_repo = AccessRepository(config.manager_storage_settings)
        verifier = AuthorityTokenVerifier(trust, people=access_repo, installation=installation)
        ada = access_repo.team_person(TEAM, "ada")
        bo = access_repo.team_person(TEAM, "bo")
        assert ada is not None and bo is not None

        # 1. C26: Restore and re-save converted waiting task with NO process verifier bound!
        set_process_verifier(None)
        runtime_repo = create_runtime_state_repository(storage_settings=config.manager_storage_settings)
        rows = runtime_repo.load_task_runtime_by_owner("lead_1")
        assert len(rows) == 1
        snapshot = RuntimeSnapshotV3.model_validate_json(rows[0].runtime_state_json)
        tasks_by_id = {t.task_id: t for t in snapshot.tasks}
        waiting = tasks_by_id["task_wait_ada"]
        assert waiting.meta.gateway_context is not None
        wait_token = waiting.meta.gateway_context.task_context
        assert wait_token is not None
        verified_wait = _verify_real_token(wait_token, trust, now + timedelta(seconds=5))
        assert verified_wait.token.person == ada.person_ref
        assert verified_wait.token.bound_to.request_id == "task_context_wait_ada"
        assert verified_wait.token.bound_to.id == "task_wait_ada"
        assert verified_wait.token.expires_at == format_timestamp(end_3d)

        # Resolve task identity -> waiting task resumes with exact request id and ada's current roles!
        identity = resolve_task_identity(
            verifier=verifier,
            team_id=TEAM,
            task_context=waiting.meta.gateway_context.task_context,
            schedule_run=waiting.meta.gateway_context.schedule_run,
            schedule_runs=None,
            now=now + timedelta(seconds=5),
        )
        assert identity.person_id == "ada"
        assert identity.context_id == "task_context_wait_ada"
        assert set(identity.person_roles) >= {"owner"}

        # Re-saving the snapshot (e.g. completing/resuming on first start before trust store loads)
        # MUST succeed using the C26 backfilled change_requests.token_digest with verifier=None!
        resumed = waiting.model_copy(update={"state": ProgressState.IN_PROGRESS})
        updated_snapshot = snapshot.model_copy(
            update={"tasks": [resumed if t.task_id == "task_wait_ada" else t for t in snapshot.tasks]}
        )
        runtime_repo.save_task_runtime(
            owner_agent_id="lead_1",
            owner_agent_name="Lead",
            team_id=TEAM,
            owner_named_agent_type="TeamArchitect",
            runtime_state_json=updated_snapshot.model_dump_json(),
            expect_existing=True,
        )
        with sqlite3.connect(str(manager_db)) as conn:
            bo_digest = conn.execute(
                "SELECT token_digest FROM change_requests WHERE team_id = ? AND request_id = ?",
                (TEAM, "task_context_other_person"),
            ).fetchone()[0]
        assert bo_digest == "", "unrelated person's change_requests row must not be touched"

        # Expired and forged tasks were preserved with task_context cleared to None.
        assert tasks_by_id["task_expired_ada"].meta.gateway_context.task_context is None
        assert tasks_by_id["task_forged"].meta.gateway_context.task_context is None

        # Scheduled run task kept its schedule_run_abc123 id with a valid protocol 1 permit.
        sched_run = tasks_by_id["task_sched_run"].meta.gateway_context.schedule_run
        assert sched_run is not None
        assert sched_run.context_id == "schedule_run_abc123"
        _verify_real_token(sched_run.permit, trust, now + timedelta(seconds=5))

        # 2. C05: Converted deferred message with attachments passes sent_files_access!
        with sqlite3.connect(str(manager_db)) as conn:
            deferred_raw = json.loads(
                conn.execute("SELECT messages_json FROM agent_deferred_messages WHERE owner_agent_id = 'lead_1'").fetchone()[0]
            )
        deferred_env = MessageEnvelope.model_validate(deferred_raw[0])
        assert deferred_env.msg_meta.gateway_context is not None
        deferred_identity = resolve_task_identity(
            verifier=verifier,
            team_id=TEAM,
            task_context=deferred_env.msg_meta.gateway_context.task_context,
            schedule_run=deferred_env.msg_meta.gateway_context.schedule_run,
            schedule_runs=None,
            now=now + timedelta(seconds=5),
        )
        assert deferred_identity.context_id == file_ctx_id
        file_decision = sent_files_access(
            person_id="ada",
            task_id="task_deferred_1",
            conversation_id="conv_files",
            sender=deferred_identity,
            message_context_id=file_ctx_id,
            message_conversation_id="conv_files",
        )
        assert file_decision.allowed is True, file_decision

        # 3. Handback validates against WorkerTaskHandbackV1 and carries a verified token.
        with sqlite3.connect(str(manager_db)) as conn:
            hb_raw = conn.execute("SELECT payload_json FROM worker_task_handbacks WHERE handback_id = 'hb_1'").fetchone()[0]
        hb = WorkerTaskHandbackV1.model_validate_json(hb_raw)
        assert hb.handed_back.meta.gateway_context is not None
        hb_identity = resolve_task_identity(
            verifier=verifier,
            team_id=TEAM,
            task_context=hb.handed_back.meta.gateway_context.task_context,
            schedule_run=hb.handed_back.meta.gateway_context.schedule_run,
            schedule_runs=None,
            now=now + timedelta(seconds=5),
        )
        assert hb_identity.person_id == "bo" and hb_identity.context_id == "task_context_handback_1"

        # 4. Gateway delivery validates against MessageEnvelope and carries a verified token.
        with sqlite3.connect(str(gateway_db)) as conn:
            deliv_raw = conn.execute("SELECT payload_json FROM gateway_deliveries WHERE delivery_id = 'deliv_1'").fetchone()[0]
        deliv_env = MessageEnvelope.model_validate_json(deliv_raw)
        assert deliv_env.msg_meta.gateway_context is not None
        deliv_identity = resolve_task_identity(
            verifier=verifier,
            team_id=TEAM,
            task_context=deliv_env.msg_meta.gateway_context.task_context,
            schedule_run=deliv_env.msg_meta.gateway_context.schedule_run,
            schedule_runs=None,
            now=now + timedelta(seconds=5),
        )
        assert deliv_identity.person_id == "ada" and deliv_identity.context_id == "task_context_delivery_1"

        # 5. Schedules: vouched marks, permits, expiry capping, system-work owner permit, forged schedule preserved.
        with sqlite3.connect(str(manager_db)) as conn:
            sched_rows = {
                r[0]: r[1:]
                for r in conn.execute(
                    "SELECT schedule_id, owner_vouched, run_permit_json, owner_permit_json, schedule_json FROM scheduled_messages"
                ).fetchall()
            }
        # sched_ada_active: vouched, 30d permit capped to 7d.
        vouched, rp_json, _op_json, s_json = sched_rows["sched_ada_active"]
        assert vouched == 1 and rp_json is not None
        ScheduledMessage.model_validate_json(s_json)
        ada_permit = _verify_real_token(rp_json, trust, now + timedelta(seconds=5))
        assert ada_permit.token.person == ada.person_ref
        assert ada_permit.token.expires_at == format_timestamp(now + timedelta(seconds=AUTHORITY_TOKEN_MAX_SECONDS))

        # sched_bo_expired_ctx: expired saved gateway context still vouches + gets fresh permit.
        vouched, rp_json, _op_json, s_json = sched_rows["sched_bo_expired_ctx"]
        assert vouched == 1 and rp_json is not None
        parsed_bo_sched = ScheduledMessage.model_validate_json(s_json)
        assert parsed_bo_sched.payload_meta.gateway_context.task_context is None
        bo_permit = _verify_real_token(rp_json, trust, now + timedelta(seconds=5))
        assert bo_permit.token.person == bo.person_ref

        # sched_ada_permit_only: vouched by permit, keeps exact 3d expiry.
        vouched, rp_json, _op_json, _s_json = sched_rows["sched_ada_permit_only"]
        assert vouched == 1 and rp_json is not None
        po_permit = _verify_real_token(rp_json, trust, now + timedelta(seconds=5))
        assert po_permit.token.expires_at == format_timestamp(end_3d)

        # sched_internal_imp: vouched, system-work run_permit (no person) + admin owner_permit.
        vouched, rp_json, op_json, _s_json = sched_rows["sched_internal_imp"]
        assert vouched == 1 and rp_json is not None and op_json is not None
        maint_rp = _verify_real_token(rp_json, trust, now + timedelta(seconds=5))
        maint_op = _verify_real_token(op_json, trust, now + timedelta(seconds=5))
        assert maint_rp.token.schedule_id == "sched_internal_imp"
        assert maint_op.token.person == ada.person_ref
        assert maint_op.token.bound_to.id == "schedule_owner_permit:sched_internal_imp"

        # sched_forged: row preserved intact, unvouched, cleared.
        vouched, rp_json, op_json, s_json = sched_rows["sched_forged"]
        assert vouched == 0 and rp_json is None and op_json is None
        parsed_forged = ScheduledMessage.model_validate_json(s_json)
        assert parsed_forged.schedule_name == "Forged reminder"
        assert parsed_forged.payload_meta.gateway_context.task_context is None

        # 6. Admin Inbox notice reports BOTH unverifiable and expired categories clearly (visible to admin, not member).
        admin_notices = [
            n for n, _read in access_repo.notices_for(TEAM, person_id="ada", roles=("owner", PERSON_ROLE_ADMIN))
            if n.kind is AccessNoticeKind.AUTHORITY_TOKEN_UPGRADE
        ]
        member_notices = [
            n for n, _read in access_repo.notices_for(TEAM, person_id="bo", roles=("member",))
            if n.kind is AccessNoticeKind.AUTHORITY_TOKEN_UPGRADE
        ]
        assert len(admin_notices) == 1
        assert member_notices == []
        notice = admin_notices[0]
        assert notice.recipient_roles == (PERSON_ROLE_ADMIN,)
        assert "Unverifiable records (2)" in notice.summary
        assert "sched_forged" in notice.summary and "Forged reminder" in notice.summary
        assert "task_forged" in notice.summary
        assert "Expired, not converted (1)" in notice.summary
        assert "task_expired_ada" in notice.summary

        # 7. Second start is a complete no-op and changes nothing.
        with sqlite3.connect(str(manager_db)) as conn:
            before_scheds = conn.execute("SELECT schedule_id, owner_vouched, run_permit_json, schedule_json FROM scheduled_messages ORDER BY schedule_id").fetchall()
        second = upgrade_authority_tokens(config, env={}, identities=identities, now=now + timedelta(minutes=5))
        assert second.already_done is True
        with sqlite3.connect(str(manager_db)) as conn:
            after_scheds = conn.execute("SELECT schedule_id, owner_vouched, run_permit_json, schedule_json FROM scheduled_messages ORDER BY schedule_id").fetchall()
        assert after_scheds == before_scheds
    finally:
        set_process_verifier(None)
        forget_task_context_keys()


def test_missing_legacy_keys_stops_conversion_without_writing_marker(tmp_path: Path) -> None:
    """When legacy 0.9 rows exist without old verify keys, conversion refuses without changing rows or writing the marker."""

    config = _make_config(tmp_path)
    manager_db = Path(config.manager_storage_settings.sqlite_path or "")
    _init_09_sqlite(manager_db)
    gw_signer = _Legacy09Signer("gateway", "gateway-missing")
    now = datetime.now(timezone.utc)
    ctx = gw_signer.sign_context(
        context_id="task_context_1",
        team_id=TEAM,
        person_id="ada",
        issued_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=2),
    )
    with sqlite3.connect(str(manager_db)) as conn:
        conn.execute(
            "INSERT INTO scheduled_messages (schedule_id, payload_json, schedule_json, schedule_name, next_fire_at, created_at, enabled, claim_team_id, owner_canonical_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sched_1", "{}", json.dumps(_schedule_dict(schedule_id="sched_1", name="Test", owner_id="ada", task_context=ctx)), "Test", now.isoformat(), now.isoformat(), 1, TEAM, "ada"),
        )
        conn.commit()

    with pytest.raises(MissingLegacyVerifyKeysError) as exc_info:
        upgrade_authority_tokens(config, env={}, identities=_signing_identities(TEAM), now=now)
    assert "ACCESS_CONTEXT_VERIFY_KEYS" in str(exc_info.value)
    assert "--allow-unverifiable" in str(exc_info.value)
    assert not config.layout.authority_tokens_upgrade_path.exists()
    assert has_legacy_authority_records(config) is True

    # Explicit --allow-unverifiable clears the unverifiable context, reports it, and writes the marker.
    res = upgrade_authority_tokens(
        config, env={}, allow_unverifiable=True, identities=_signing_identities(TEAM), now=now
    )
    assert res.counts.unverifiable == 1
    assert res.notice_stored is True
    assert authority_tokens_upgrade_done(config) is True


# ---------------------------------------------------------------------------
# A6: Signing-key removal and startup refusal tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", REMOVED_SIGNING_KEY_ENV_VARS)
def test_removed_signing_key_env_vars_and_file_forms_are_refused(name: str) -> None:
    for key in (name, f"{name}_FILE"):
        problems = removed_signing_key_env_problems({"PATH": "/usr/bin", key: "secret-canary-value"})
        assert len(problems) == 1
        assert key in problems[0]
        assert "secret-canary-value" not in problems[0]
        with pytest.raises(ValueError, match=key):
            reject_removed_signing_key_env_vars({key: "secret-canary-value"})


def test_signing_keys_removed_from_templates_secrets_yaml_and_internal_credentials() -> None:
    request = InternalCredentialRequest(instance_name="fresh", storage_backend="postgres", include_nats_credentials=True)
    generated = generate_internal_credentials(request)
    names = set(internal_credential_names(request))
    for removed in REMOVED_SIGNING_KEY_ENV_VARS:
        assert removed not in generated
        assert removed not in names
    assert set(generated) == names

    example_env = (REPO_ROOT / "team.example.env").read_text(encoding="utf-8")
    secrets_yaml = (REPO_ROOT / "docker" / "secret_env_vars.yaml").read_text(encoding="utf-8")
    for removed in REMOVED_SIGNING_KEY_ENV_VARS:
        assert removed not in example_env
        assert removed not in secrets_yaml


def test_launcher_fresh_install_refuses_removed_keys_and_upgraded_install_converts_before_refusal(
    tmp_path: Path,
) -> None:
    """A6: fresh install refuses removed keys immediately; upgraded install with legacy rows runs
    conversion first (without passing old keys to runtime services), writes marker, and refuses
    on the next start if any removed key is still set."""

    # 1. Fresh 0.10 install (no legacy rows): refuses removed signing keys immediately.
    fresh_config = _make_config(tmp_path / "fresh")
    fresh_cfg_path = fresh_config.config_path
    with pytest.raises(ValueError, match="ACCESS_GATEWAY_SIGNING_KEY"):
        prepare_authority_tokens_for_launch(
            fresh_config,
            fresh_cfg_path,
            {"ACCESS_GATEWAY_SIGNING_KEY": "old-key"},
        )
    assert not fresh_config.layout.authority_tokens_upgrade_path.exists()

    # Clean fresh install writes the marker without needing NATS.
    clean_env: dict[str, str] = {"SECURE_TOOL_BROKER_TOKEN": "stb_1"}
    hook = prepare_authority_tokens_for_launch(fresh_config, fresh_cfg_path, clean_env)
    assert hook is None
    assert authority_tokens_upgrade_done(fresh_config) is True

    # Subsequent start with marker present refuses any removed key or _FILE form via build_process_env.
    with pytest.raises(ValueError, match="ACCESS_SCHEDULER_SIGNING_KEY_FILE"):
        build_process_env(
            fresh_config,
            env_path=None,
            base_env={"ACCESS_SCHEDULER_SIGNING_KEY_FILE": "/run/secrets/sched"},
            allow_legacy_signing_keys=False,
        )

    # 2. Upgraded 0.9 install (marker missing, legacy row present): old keys are stripped from
    #    inherited_env and isolated exclusively to the conversion hook.
    up_config = _make_config(tmp_path / "upgraded")
    manager_db = Path(up_config.manager_storage_settings.sqlite_path or "")
    _init_09_sqlite(manager_db)
    gw_signer = _Legacy09Signer("gateway", "gateway-up")
    now = datetime.now(timezone.utc)
    ctx = gw_signer.sign_context(
        context_id="task_context_up",
        team_id=TEAM,
        person_id="ada",
        issued_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=2),
    )
    with sqlite3.connect(str(manager_db)) as conn:
        conn.execute(
            "INSERT INTO scheduled_messages (schedule_id, payload_json, schedule_json, schedule_name, next_fire_at, created_at, enabled, claim_team_id, owner_canonical_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sched_up", "{}", json.dumps(_schedule_dict(schedule_id="sched_up", name="Up", owner_id="ada", task_context=ctx)), "Up", now.isoformat(), now.isoformat(), 1, TEAM, "ada"),
        )
        conn.commit()

    inherited_env = {
        "SECURE_TOOL_BROKER_TOKEN": "stb_1",
        "ACCESS_GATEWAY_SIGNING_KEY": gw_signer.env_private_value,
        "ACCESS_CONTEXT_VERIFY_KEYS": json.dumps([gw_signer.verify_entry]),
    }
    hook = prepare_authority_tokens_for_launch(up_config, up_config.config_path, inherited_env)
    assert hook is not None
    assert "ACCESS_GATEWAY_SIGNING_KEY" not in inherited_env
    assert "ACCESS_CONTEXT_VERIFY_KEYS" not in inherited_env


# ---------------------------------------------------------------------------
# A5: PostgreSQL conversion test (runs when TEST_POSTGRES_DSN is configured)
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.postgres
def test_09_postgres_first_start_conversion_end_to_end(tmp_path: Path) -> None:
    """A5 on PostgreSQL: converts legacy 0.9 task snapshot, schedule context/permit, handback,
    gateway delivery, and backfills change_requests.token_digest; second start is a no-op."""

    dsn = (os.environ.get("TEST_POSTGRES_DSN") or os.environ.get("LIVE_TEST_POSTGRES_DSN") or "").strip()
    if not dsn:
        pytest.skip("Set TEST_POSTGRES_DSN or LIVE_TEST_POSTGRES_DSN to run the live PostgreSQL upgrade test.")

    from storage.postgres_connection import connect_postgres

    forget_task_context_keys()
    pg_team = "team_pg_upgrade"
    try:
        config = _make_config(tmp_path, team_id=pg_team)
        config.state.storage.backend = "postgres"
        config.gateway.storage.backend = "postgres"
        config.save(config.config_path)

        gw_signer = _Legacy09Signer("gateway", "gateway-pg01")
        sched_signer = _Legacy09Signer("scheduler", "scheduler-pg01")
        old_env = {
            "POSTGRES_DSN": dsn,
            "ACCESS_CONTEXT_VERIFY_KEYS": json.dumps([gw_signer.verify_entry, sched_signer.verify_entry]),
        }

        now = datetime.now(timezone.utc).replace(microsecond=0)
        past = now - timedelta(hours=1)
        end_3d = now + timedelta(days=3)

        wait_ctx = gw_signer.sign_context(
            context_id="task_context_pg_wait",
            team_id=pg_team,
            person_id="ada",
            root_task_id="task_pg_wait",
            conversation_id="conv_pg",
            issued_at=past,
            expires_at=end_3d,
        )
        sched_ctx = gw_signer.sign_context(
            context_id="task_context_pg_sched",
            team_id=pg_team,
            person_id="ada",
            issued_at=past,
            expires_at=end_3d,
        )
        sched_permit = sched_signer.sign_permit(
            permit_id="schedule_permit_pg1",
            team_id=pg_team,
            schedule_id="sched_pg_1",
            person_id="ada",
            issued_at=past,
            expires_at=end_3d,
        )
        forged_ctx = dict(wait_ctx, signature=_b64(b"f" * 64))

        # Initialize minimal 0.9-shaped tables in Postgres.
        pg_settings = config.resolve_manager_storage_settings(env=old_env)
        access_repo = AccessRepository(pg_settings)
        access_repo.replace_people(
            pg_team,
            [
                TeamPersonV1(
                    person_ref=new_person_ref(),
                    team_id=pg_team,
                    person_id="ada",
                    standing=PersonStanding.ACTIVE,
                    roles=("owner",),
                    changed_at_utc=past,
                )
            ],
            at=past,
        )
        with connect_postgres(dsn) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_task_runtime (
                    owner_agent_id TEXT PRIMARY KEY, owner_agent_name TEXT, team_id TEXT,
                    owner_named_agent_type TEXT, runtime_state_json TEXT NOT NULL, updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_task_runtime_records (
                    owner_agent_id TEXT NOT NULL, section TEXT NOT NULL, record_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
                    PRIMARY KEY (owner_agent_id, section, record_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS change_requests (
                    team_id TEXT NOT NULL, request_id TEXT NOT NULL, person_id TEXT NOT NULL DEFAULT '',
                    start_position INTEGER NOT NULL, state TEXT NOT NULL,
                    opened_at_utc TEXT NOT NULL, closed_at_utc TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (team_id, request_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_messages (
                    schedule_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, schedule_json TEXT,
                    schedule_name TEXT, next_fire_at TEXT NOT NULL, created_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, claim_team_id TEXT, owner_canonical_user_id TEXT,
                    run_permit_json TEXT, run_permit_owner TEXT, run_permit_recheck_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_deliveries (
                    delivery_id TEXT PRIMARY KEY, correlation_id TEXT, session_key TEXT NOT NULL,
                    origin_kind TEXT NOT NULL, payload_json TEXT NOT NULL, rendered_text TEXT NOT NULL,
                    available_after TEXT NOT NULL, owner_canonical_user_id TEXT, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, delivered_at TEXT
                )
                """
            )
            # Clean any prior test rows for this team.
            conn.execute("DELETE FROM active_task_runtime_records WHERE owner_agent_id = 'lead_pg'")
            conn.execute("DELETE FROM active_task_runtime WHERE owner_agent_id = 'lead_pg'")
            conn.execute("DELETE FROM change_requests WHERE team_id = %s", (pg_team,))
            conn.execute("DELETE FROM scheduled_messages WHERE claim_team_id = %s", (pg_team,))
            conn.execute("DELETE FROM gateway_deliveries WHERE delivery_id = 'deliv_pg_1'")

            header = canonical_json(
                {
                    "layout_version": 1,
                    "task_order": ["task_pg_wait", "task_pg_forged"],
                    "header": {"schema_version": 3, "task_queue": [], "waiting_top_level_task_ids": ["task_pg_wait"]},
                }
            )
            conn.execute(
                "INSERT INTO active_task_runtime (owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, runtime_state_json, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                ("lead_pg", "LeadPG", pg_team, "TeamArchitect", header, past.isoformat()),
            )
            for rid, rdoc in (
                ("task_pg_wait", _task_record_dict(task_id="task_pg_wait", owner_person_id="ada", task_context=wait_ctx)),
                ("task_pg_forged", _task_record_dict(task_id="task_pg_forged", owner_person_id="ada", task_context=forged_ctx)),
            ):
                pjson = canonical_json(rdoc)
                conn.execute(
                    "INSERT INTO active_task_runtime_records (owner_agent_id, section, record_id, payload_json, payload_digest) VALUES (%s, %s, %s, %s, %s)",
                    ("lead_pg", "tasks", rid, pjson, payload_digest(pjson)),
                )
            conn.execute(
                "INSERT INTO change_requests (team_id, request_id, person_id, start_position, state, opened_at_utc, closed_at_utc) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (pg_team, "task_context_pg_wait", "ada", 1, RequestState.OPEN.value, past.isoformat(), ""),
            )
            conn.execute(
                "INSERT INTO scheduled_messages (schedule_id, payload_json, schedule_json, schedule_name, next_fire_at, created_at, enabled, claim_team_id, owner_canonical_user_id, run_permit_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    "sched_pg_1",
                    json.dumps({"task_type": "request", "problem_statement": "PG Schedule"}),
                    json.dumps(_schedule_dict(schedule_id="sched_pg_1", name="PG Schedule", owner_id="ada", task_context=sched_ctx)),
                    "PG Schedule",
                    end_3d.isoformat(),
                    past.isoformat(),
                    1,
                    pg_team,
                    "ada",
                    json.dumps(sched_permit),
                ),
            )
            deliv_env = {
                "msg_header": {"message_id": "msg_pg_deliv", "from_agent_id": "lead_pg", "agent_signature": ""},
                "msg_meta": {"gateway_context": {"canonical_user_id": "ada", "task_context": wait_ctx}},
                "msg_content": {"message_class": "Task", "task_id": "task_pg_wait", "task_type": "response", "problem_statement": "PG reply"},
            }
            conn.execute(
                "INSERT INTO gateway_deliveries (delivery_id, correlation_id, session_key, origin_kind, payload_json, rendered_text, available_after, owner_canonical_user_id, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("deliv_pg_1", "corr_pg", "website:ada:chat", "response", json.dumps(deliv_env), "PG reply", now.isoformat(), "ada", "pending", past.isoformat()),
            )
            conn.commit()

        identities = _signing_identities(pg_team)
        assert has_legacy_authority_records(config, env=old_env) is True
        result = upgrade_authority_tokens(config, env=old_env, identities=identities, now=now)
        assert result.already_done is False
        assert result.had_legacy_records is True
        assert result.counts.converted_contexts == 3
        assert result.counts.converted_permits == 1
        assert result.counts.vouched_schedules == 1
        assert result.counts.ledger_digests_backfilled == 1
        assert result.counts.unverifiable == 1
        assert result.notice_stored is True
        assert has_legacy_authority_records(config, env=old_env) is False

        trust = broker_trust_store()
        verifier = AuthorityTokenVerifier(trust, people=access_repo, installation=identities.gateway.installation)
        with connect_postgres(dsn) as conn:
            wait_raw = conn.execute(
                "SELECT payload_json FROM active_task_runtime_records WHERE owner_agent_id = 'lead_pg' AND section = 'tasks' AND record_id = 'task_pg_wait'"
            ).fetchone()[0]
            sched_row = conn.execute(
                "SELECT owner_vouched, run_permit_json FROM scheduled_messages WHERE schedule_id = 'sched_pg_1'"
            ).fetchone()
            digest_val = conn.execute(
                "SELECT token_digest FROM change_requests WHERE team_id = %s AND request_id = 'task_context_pg_wait'",
                (pg_team,),
            ).fetchone()[0]
        rec = TaskSnapshotRecord.model_validate_json(wait_raw)
        assert rec.meta.gateway_context is not None
        ident = resolve_task_identity(
            verifier=verifier,
            team_id=pg_team,
            task_context=rec.meta.gateway_context.task_context,
            schedule_run=rec.meta.gateway_context.schedule_run,
            schedule_runs=None,
            now=now + timedelta(seconds=5),
        )
        assert ident.person_id == "ada" and ident.context_id == "task_context_pg_wait"
        assert sched_row[0] == 1 and sched_row[1] is not None
        assert digest_val != ""

        # Second start is a no-op.
        second = upgrade_authority_tokens(config, env={"POSTGRES_DSN": dsn}, identities=identities, now=now + timedelta(minutes=1))
        assert second.already_done is True
    finally:
        forget_task_context_keys()
