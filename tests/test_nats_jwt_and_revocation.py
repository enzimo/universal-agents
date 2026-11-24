"""Unit tests for NKeys, NATS JWT v2 encoding/sizing, S3 per-role permissions,
revocation adapters, and ``transport.nats.credential_lifetime_seconds`` settings.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from assistant_gateway.config import GatewayInstanceConfig, _INSTANCE_CONFIG_COMMENTS
from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT, MAX_PARTITION_COUNT
from custom_messages.wire.partitions import account_mappings
from custom_messages.wire.permissions import HostPlacement, roles_permissions
from custom_transport.protocol1 import nkeys
from custom_transport.protocol1.contract_files import max_credential_bytes, role_permissions, transport
from custom_transport.protocol1.nats_jwt import (
    NatsSigningKey,
    decode_jwt_payload,
    encode_account_jwt,
    encode_operator_jwt,
    encode_user_jwt,
    fits_credential_limit,
    user_claims,
    user_credential_bytes,
)
from default_values import (
    MAX_NATS_CREDENTIAL_LIFETIME_SECONDS,
    MIN_NATS_CREDENTIAL_LIFETIME_SECONDS,
    TRANSPORT_DEFAULTS,
)
from installation_authority.revocation import (
    ACCOUNTZ_SUBJECT,
    CLAIMS_UPDATE_SUBJECT,
    AccountBaseClaims,
    LifetimeWaitRevoker,
    OperatorModeRevoker,
)
from runtime_settings.transport import (
    CREDENTIAL_LIFETIME_SETTING,
    NatsAccountSettings,
    TransportSettings,
)
from team_transfer.sections import ExportSource, RecordOutcome, SectionExport, SectionFiles
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingV1, SettingsSection
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from tests.pinned_memory_support import new_instance

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.yaml"
PASSPHRASE = "correct horse battery staple"


# -- NKeys (operator, account, user) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "pub_fn", "seed_fn", "pub_dec", "seed_dec", "pub_prefix", "seed_prefix"),
    [
        ("operator", nkeys.operator_public_key, nkeys.operator_seed, nkeys.operator_public_key_bytes, nkeys.operator_seed_bytes, "O", "SO"),
        ("account", nkeys.account_public_key, nkeys.account_seed, nkeys.account_public_key_bytes, nkeys.account_seed_bytes, "A", "SA"),
        ("user", nkeys.user_public_key, nkeys.user_seed, nkeys.public_key_bytes, nkeys.seed_bytes, "U", "SU"),
    ],
)
def test_nkeys_round_trip_for_operator_account_and_user(
    role: str, pub_fn: Any, seed_fn: Any, pub_dec: Any, seed_dec: Any, pub_prefix: str, seed_prefix: str
) -> None:
    raw = bytes(range(32))
    pub = pub_fn(raw)
    seed = seed_fn(raw)
    assert pub.startswith(pub_prefix)
    assert seed.startswith(seed_prefix)
    assert pub_dec(pub) == raw
    assert seed_dec(seed) == raw
    assert nkeys.public_key_role_and_bytes(pub) == (role, raw)
    assert nkeys.seed_role_and_bytes(seed) == (role, raw)


def test_nkeys_refuse_wrong_role_and_damaged_checksum() -> None:
    op = NatsSigningKey.generate("operator")
    acc = NatsSigningKey.generate("account")
    usr = NatsSigningKey.generate("user")

    with pytest.raises(ValueError, match="not a NATS user public key"):
        nkeys.public_key_bytes(acc.public_key)
    with pytest.raises(ValueError, match="not a NATS account public key"):
        nkeys.account_public_key_bytes(op.public_key)
    with pytest.raises(ValueError, match="not a NATS operator seed"):
        nkeys.operator_seed_bytes(usr.seed())
    damaged = usr.public_key[:-1] + ("A" if usr.public_key[-1] != "A" else "B")
    with pytest.raises(ValueError, match="checksum"):
        nkeys.public_key_bytes(damaged)


# -- NATS JWT v2 encoding and credential sizing ---------------------------------------------------


def test_nats_signing_key_round_trip_and_jwt_encoding() -> None:
    op = NatsSigningKey.generate("operator")
    op_signing = NatsSigningKey.generate("operator")
    sys_acc = NatsSigningKey.generate("account")
    inst_acc = NatsSigningKey.generate("account")
    inst_signing = NatsSigningKey.generate("account")
    user = NatsSigningKey.generate("user")

    restored = NatsSigningKey.from_seed(op_signing.seed(), expected_role="operator")
    assert restored.public_key == op_signing.public_key
    with pytest.raises(ValueError, match="expected a NATS account seed"):
        NatsSigningKey.from_seed(op_signing.seed(), expected_role="account")
    with pytest.raises(ValueError, match="only a user NKey"):
        inst_signing.login_options("a.b.c")

    op_jwt = encode_operator_jwt(op, "operator", system_account=sys_acc.public_key, signing_keys=[op_signing.public_key])
    op_payload = decode_jwt_payload(op_jwt)
    assert op_payload["iss"] == op.public_key
    assert op_payload["nats"]["system_account"] == sys_acc.public_key
    assert op_payload["nats"]["signing_keys"] == [op_signing.public_key]

    mappings = account_mappings(96)
    acc_jwt = encode_account_jwt(
        op_signing,
        inst_acc.public_key,
        "installation",
        signing_keys=[inst_signing.public_key],
        mappings=mappings,
        revocations={user.public_key: 1_800_000_000},
    )
    acc_payload = decode_jwt_payload(acc_jwt)
    assert acc_payload["iss"] == op_signing.public_key
    assert acc_payload["sub"] == inst_acc.public_key
    assert acc_payload["nats"]["revocations"] == {user.public_key: 1_800_000_000}
    assert set(acc_payload["nats"]["mappings"]) == set(mappings)

    perms = roles_permissions(
        [role_permissions("worker_host")],
        HostPlacement("worker-1", ("team_a",), partitions={"team_a": (0, 1)}, installation="install_1"),
    )
    u_jwt = encode_user_jwt(
        inst_signing,
        inst_acc.public_key,
        user.public_key,
        "worker-1",
        perms,
        issued_at=1_800_000_000,
        expires_at=1_800_000_900,
    )
    u_payload = decode_jwt_payload(u_jwt)
    assert u_payload["iss"] == inst_signing.public_key
    assert u_payload["sub"] == user.public_key
    assert u_payload["exp"] == 1_800_000_900
    assert u_payload["nats"]["issuer_account"] == inst_acc.public_key
    assert u_payload["nats"]["resp"] == {"max": 1, "ttl": 38_000_000_000}


def test_user_credential_bytes_matches_real_jwt_size_and_enforces_limit() -> None:
    inst_acc = NatsSigningKey.generate("account")
    inst_signing = NatsSigningKey.generate("account")
    user = NatsSigningKey.generate("user")
    team = "team_" + "a" * 19  # 24 chars
    pools = tuple(f"pool_{i:02d}_" + "p" * 16 for i in range(10))  # 10 x 24-char pools
    placement = HostPlacement(
        "worker-host-1",
        (team,),
        partitions={team: tuple(range(DEFAULT_PARTITION_COUNT))},
        pools={team: pools},
        installation="install_example",
    )
    perms = roles_permissions([role_permissions("worker_host")], placement, partition_count=DEFAULT_PARTITION_COUNT)
    real_jwt = encode_user_jwt(
        inst_signing,
        inst_acc.public_key,
        user.public_key,
        placement.runtime,
        perms,
        issued_at=1_900_000_000,
        expires_at=1_900_000_900,
    )
    estimated = user_credential_bytes(["worker_host"], placement, partition_count=DEFAULT_PARTITION_COUNT)
    assert estimated == len(real_jwt.encode("ascii"))
    assert fits_credential_limit(["worker_host"], placement, partition_count=DEFAULT_PARTITION_COUNT)

    # At MAX_PARTITION_COUNT + 10 pools still fits; 60 pools pushes over max_credential_bytes.
    at_max = HostPlacement(
        "worker-host-1",
        (team,),
        partitions={team: tuple(range(MAX_PARTITION_COUNT))},
        pools={team: pools},
        installation="install_example",
    )
    assert fits_credential_limit(["worker_host"], at_max, partition_count=MAX_PARTITION_COUNT)

    too_many_pools = HostPlacement(
        "worker-host-1",
        (team,),
        partitions={team: tuple(range(MAX_PARTITION_COUNT))},
        pools={team: tuple(f"pool_{i:02d}_" + "p" * 16 for i in range(60))},
        installation="install_example",
    )
    assert user_credential_bytes(["worker_host"], too_many_pools, partition_count=MAX_PARTITION_COUNT) > max_credential_bytes()
    assert not fits_credential_limit(["worker_host"], too_many_pools, partition_count=MAX_PARTITION_COUNT)


# -- S3 per-role partition filling (C05) ----------------------------------------------------------


def test_roles_permissions_fills_team_services_and_lead_host_partitions_independently() -> None:
    """PR #151 S3 / C05: a mixed [lead_host, team_services] certificate gets every partition for
    withdrawal only, and only owned partitions for consumer pull, live control, and agent records."""

    placement = HostPlacement(
        "peer-lead-host-1",
        ("team_a",),
        partitions={"team_a": (3,)},
        installation="install_example",
    )
    merged = roles_permissions(
        [role_permissions("lead_host"), role_permissions("team_services")],
        placement,
        partition_count=8,
    )
    publish = set(merged["publish"])
    subscribe = set(merged["subscribe"])

    # team_services withdrawal covers all 8 partitions (0..7)
    for p in range(8):
        assert f"$JS.API.STREAM.MSG.GET.UA1_PART_team_a~{p}" in publish
        assert f"$JS.API.STREAM.MSG.DELETE.UA1_PART_team_a~{p}" in publish

    # lead_host consumer pull/ack/info, agent records, and live control cover ONLY owned partition 3
    assert "$JS.API.CONSUMER.MSG.NEXT.UA1_PART_team_a~3.*" in publish
    assert "$JS.ACK.UA1_PART_team_a~3.*.>" in publish
    assert "$KV.UA1_AGENTS.team_a.3.*" in publish
    assert "ua.v1.team_a.*.ctl.agent.*.3" in subscribe
    for unowned in (0, 1, 2, 4, 5, 6, 7):
        assert f"$JS.API.CONSUMER.MSG.NEXT.UA1_PART_team_a~{unowned}.*" not in publish
        assert f"$JS.ACK.UA1_PART_team_a~{unowned}.*.>" not in publish
        assert f"$KV.UA1_AGENTS.team_a.{unowned}.*" not in publish
        assert f"ua.v1.team_a.*.ctl.agent.*.{unowned}" not in subscribe

    assert merged["allow_responses"] == {"expires": "38s", "max": 1}


# -- Revocation adapters --------------------------------------------------------------------------


class _FakeSystemConnection:
    def __init__(self, responder: Any) -> None:
        self.requests: list[tuple[str, bytes, float]] = []
        self._responder = responder

    async def request(self, subject: str, payload: bytes, timeout: float = 5.0) -> Any:
        self.requests.append((subject, payload, timeout))
        return await self._responder(subject, payload)


@pytest.mark.asyncio
async def test_lifetime_wait_revoker_reports_unconfirmed_revocation() -> None:
    user = NatsSigningKey.generate("user")
    revoker = LifetimeWaitRevoker()
    assert revoker.supports_instant_revocation is False
    outcome = await revoker.revoke(user.public_key, revoke_until=1_800_000_000)
    assert outcome.revoked is False
    assert outcome.revoked_at == 1_800_000_000
    with pytest.raises(ValueError, match="not an NKey"):
        await revoker.revoke("NOT_A_KEY", revoke_until=1_800_000_000)
    with pytest.raises(ValueError, match="not a NATS user public key"):
        await revoker.revoke(NatsSigningKey.generate("account").public_key, revoke_until=1_800_000_000)


def test_user_claims_denies_all_instead_of_emitting_empty_allow_lists() -> None:
    """C10: NATS treats empty allow lists as unrestricted; user_claims must emit deny:['>']."""

    empty = user_claims({"publish": [], "subscribe": []})
    assert empty["pub"] == {"deny": [">"]}
    assert empty["sub"] == {"deny": [">"]}

    pub_only = user_claims({"publish": ["ua.v1.team_a.h.svc"], "subscribe": []})
    assert pub_only["pub"] == {"allow": ["ua.v1.team_a.h.svc"]}
    assert pub_only["sub"] == {"deny": [">"]}

    sub_only = user_claims({"publish": [], "subscribe": ["_INBOX.h.>"]})
    assert sub_only["pub"] == {"deny": [">"]}
    assert sub_only["sub"] == {"allow": ["_INBOX.h.>"]}


@pytest.mark.asyncio
async def test_operator_mode_revoker_preserves_base_claims_and_cumulative_revocations() -> None:
    op_signing = NatsSigningKey.generate("operator")
    inst_acc = NatsSigningKey.generate("account")
    inst_signing = NatsSigningKey.generate("account")
    u1 = NatsSigningKey.generate("user")
    u2 = NatsSigningKey.generate("user")
    u3 = NatsSigningKey.generate("user")
    mappings = account_mappings(16)
    active_jwt = ""
    active_revocations: dict[str, int] = {}

    async def ok_reply(subject: str, payload: bytes) -> Any:
        nonlocal active_jwt, active_revocations
        if subject == CLAIMS_UPDATE_SUBJECT:
            active_jwt = payload.decode("ascii")
            decoded = decode_jwt_payload(active_jwt)
            active_revocations = dict(decoded["nats"].get("revocations", {}))
            return SimpleNamespace(
                data=json.dumps({"data": {"account": inst_acc.public_key, "code": 200, "message": "jwt updated"}}).encode()
            )
        assert subject == ACCOUNTZ_SUBJECT
        req = json.loads(payload.decode("utf-8"))
        assert req == {"account": inst_acc.public_key}
        return SimpleNamespace(
            data=json.dumps(
                {
                    "data": {
                        "account_detail": {
                            "account_name": inst_acc.public_key,
                            "jwt": active_jwt,
                            "revoked_user": active_revocations,
                        }
                    }
                }
            ).encode()
        )

    fake_nc = _FakeSystemConnection(ok_reply)
    base = AccountBaseClaims(
        account_public_key=inst_acc.public_key,
        signing_keys=(inst_signing.public_key,),
        mappings=mappings,
    )
    revoker = OperatorModeRevoker(op_signing, base, fake_nc)
    assert revoker.supports_instant_revocation is True

    # First revocation includes a pre-existing ledger revocation (u1) plus u2.
    out1 = await revoker.revoke(u2.public_key, revoke_until=2000, all_revocations={u1.public_key: 1500})
    assert out1.revoked is True
    assert revoker.applied_revocations == {u1.public_key: 1500, u2.public_key: 2000}

    # Second revocation for u3 preserves u1 and u2, plus mappings and signing_keys.
    out2 = await revoker.revoke(u3.public_key, revoke_until=3000)
    assert out2.revoked is True
    claims_updates = [req for req in fake_nc.requests if req[0] == CLAIMS_UPDATE_SUBJECT]
    payload = decode_jwt_payload(claims_updates[-1][1].decode("ascii"))
    assert payload["iss"] == op_signing.public_key
    assert payload["sub"] == inst_acc.public_key
    assert payload["nats"]["signing_keys"] == [inst_signing.public_key]
    assert set(payload["nats"]["mappings"]) == set(mappings)
    assert payload["nats"]["revocations"] == {
        u1.public_key: 1500,
        u2.public_key: 2000,
        u3.public_key: 3000,
    }

    # C09: prune_before drops expired revocations from both the pushed JWT and applied_revocations.
    u4 = NatsSigningKey.generate("user")
    out3 = await revoker.revoke(u4.public_key, revoke_until=4000, prune_before=2500)
    assert out3.revoked is True
    assert revoker.applied_revocations == {u3.public_key: 3000, u4.public_key: 4000}
    claims_updates = [req for req in fake_nc.requests if req[0] == CLAIMS_UPDATE_SUBJECT]
    pruned_payload = decode_jwt_payload(claims_updates[-1][1].decode("ascii"))
    assert pruned_payload["nats"]["revocations"] == {u3.public_key: 3000, u4.public_key: 4000}


@pytest.mark.asyncio
async def test_operator_mode_revoker_fails_closed_when_accountz_does_not_confirm_pushed_jwt() -> None:
    """C08: DirResolver returns code=200 even when nats-server rejects the JWT; ACCOUNTZ must confirm."""

    op_signing = NatsSigningKey.generate("operator")
    inst_acc = NatsSigningKey.generate("account")
    u1 = NatsSigningKey.generate("user")
    base = AccountBaseClaims(account_public_key=inst_acc.public_key)

    async def stale_accountz(subject: str, _payload: bytes) -> Any:
        if subject == CLAIMS_UPDATE_SUBJECT:
            return SimpleNamespace(
                data=json.dumps({"data": {"account": inst_acc.public_key, "code": 200, "message": "jwt updated"}}).encode()
            )
        return SimpleNamespace(
            data=json.dumps(
                {
                    "data": {
                        "account_detail": {
                            "account_name": inst_acc.public_key,
                            "jwt": "stale.old.jwt",
                            "revoked_user": None,
                        }
                    }
                }
            ).encode()
        )

    revoker = OperatorModeRevoker(op_signing, base, _FakeSystemConnection(stale_accountz))
    outcome = await revoker.revoke(u1.public_key, revoke_until=2000)
    assert outcome.revoked is False
    assert "does not match" in outcome.detail
    assert revoker.applied_revocations == {}


@pytest.mark.asyncio
async def test_operator_mode_revoker_handles_rejection_and_timeout_without_mutating_state() -> None:
    op_signing = NatsSigningKey.generate("operator")
    inst_acc = NatsSigningKey.generate("account")
    u1 = NatsSigningKey.generate("user")
    base = AccountBaseClaims(account_public_key=inst_acc.public_key)

    with pytest.raises(ValueError, match="operator key"):
        OperatorModeRevoker(inst_acc, base, _FakeSystemConnection(None))

    async def err_reply(_subject: str, _payload: bytes) -> Any:
        return SimpleNamespace(
            data=json.dumps({"error": {"code": 400, "description": "account validation failed"}}).encode()
        )

    revoker = OperatorModeRevoker(op_signing, base, _FakeSystemConnection(err_reply))
    outcome = await revoker.revoke(u1.public_key, revoke_until=2000)
    assert outcome.revoked is False
    assert "account validation failed" in outcome.detail
    assert revoker.applied_revocations == {}

    async def timeout_reply(_subject: str, _payload: bytes) -> Any:
        raise asyncio.TimeoutError("no responders")

    revoker2 = OperatorModeRevoker(op_signing, base, _FakeSystemConnection(timeout_reply))
    outcome2 = await revoker2.revoke(u1.public_key, revoke_until=2000)
    assert outcome2.revoked is False
    assert "TimeoutError" in outcome2.detail
    assert revoker2.applied_revocations == {}


# -- transport.nats.credential_lifetime_seconds schema & team transfer exclusion ------------------


def test_credential_lifetime_setting_matches_contract_bounds_and_comments() -> None:
    contract = transport()
    assert ".".join(CREDENTIAL_LIFETIME_SETTING) == contract["nats_account"]["credential_lifetime_setting"]
    assert TransportSettings().nats == NatsAccountSettings()
    assert NatsAccountSettings().credential_lifetime_seconds == TRANSPORT_DEFAULTS.nats_credential_lifetime_seconds.value == 900

    assert NatsAccountSettings.model_validate({"credential_lifetime_seconds": MIN_NATS_CREDENTIAL_LIFETIME_SECONDS}).credential_lifetime_seconds == 60
    assert NatsAccountSettings.model_validate({"credential_lifetime_seconds": MAX_NATS_CREDENTIAL_LIFETIME_SECONDS}).credential_lifetime_seconds == 86_400
    with pytest.raises(ValidationError, match="transport.nats.credential_lifetime_seconds must be from 60 to 86400"):
        NatsAccountSettings.model_validate({"credential_lifetime_seconds": 59})
    with pytest.raises(ValidationError, match="transport.nats.credential_lifetime_seconds must be from 60 to 86400"):
        NatsAccountSettings.model_validate({"credential_lifetime_seconds": 86_401})
    for bad in (True, 120.5, "ten"):
        with pytest.raises(ValidationError, match="transport.nats.credential_lifetime_seconds must be a whole number"):
            NatsAccountSettings.model_validate({"credential_lifetime_seconds": bad})
    with pytest.raises(ValidationError, match="extra_key"):
        NatsAccountSettings.model_validate({"extra_key": 1})

    # Comments in both instance_config.yaml comments and config.example.yaml
    assert _INSTANCE_CONFIG_COMMENTS.get("transport.nats")
    assert _INSTANCE_CONFIG_COMMENTS.get("transport.nats.credential_lifetime_seconds")
    example_text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    loaded_example = yaml.safe_load(example_text)
    assert loaded_example["transport"]["nats"]["credential_lifetime_seconds"] == 900
    lifetime_line = next(line for line in example_text.splitlines() if "credential_lifetime_seconds:" in line)
    assert " # " in lifetime_line


def test_credential_lifetime_validates_in_config_yaml_and_instance_config(tmp_path: Path) -> None:
    from config_loader import Config, resolve_agent_type_settings
    from runtime_settings.resolution import normalize_runtime_sections

    chosen = {"nats": {"credential_lifetime_seconds": 300}}
    normalized = normalize_runtime_sections({"transport": chosen})
    assert normalized["transport"]["nats"] == chosen["nats"]
    resolve_agent_type_settings(Config({"transport": chosen}))

    with pytest.raises(ValidationError, match="transport.nats.credential_lifetime_seconds"):
        resolve_agent_type_settings(Config({"transport": {"nats": {"credential_lifetime_seconds": 10}}}))

    path, _config = new_instance(tmp_path, "alpha", "team_a")
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    saved["transport"].pop("nats", None)
    path.write_text(yaml.safe_dump(saved), encoding="utf-8")
    loaded = GatewayInstanceConfig.load(path)
    assert loaded.transport.nats.credential_lifetime_seconds == 900


def test_credential_lifetime_is_excluded_from_team_transfer_export_and_refused_on_import(tmp_path: Path) -> None:
    path, config = new_instance(tmp_path / "src", "alpha", "team_a")
    config.transport.nats.credential_lifetime_seconds = 300
    config.save(path)

    kdf = new_key_derivation(opslimit=1, memlimit=8192)
    bundle = tmp_path / "export.uat"
    export_team(export_source(path), bundle, PASSPHRASE, owner_person_id="usr_owner", sections=(SettingsSection(),), key_derivation=kdf)
    with TransferReader.open(bundle, PASSPHRASE) as reader:
        document = reader.section_document("settings")
    assert document is not None
    records = [SettingV1.model_validate(r) for r in document.records]
    assert CREDENTIAL_LIFETIME_SETTING not in {r.path for r in records}

    # A crafted bundle carrying the setting is refused with a clear reason, leaving the destination default intact.
    class Crafted(SettingsSection):
        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            return SectionExport(
                records=[SettingV1(path=CREDENTIAL_LIFETIME_SETTING, value=300).model_dump(mode="json")]
            )

    crafted_bundle = tmp_path / "crafted.uat"
    export_team(export_source(path), crafted_bundle, PASSPHRASE, owner_person_id="usr_owner", sections=(Crafted(),), key_derivation=kdf)
    dest = tmp_path / "dst"
    with TransferReader.open(crafted_bundle, PASSPHRASE) as reader:
        plan = import_team(reader, dest, dry_run=False, sections=(SettingsSection(),))
    outcomes = {r.key: r for r in plan.records}
    assert outcomes["transport.nats.credential_lifetime_seconds"].outcome is RecordOutcome.REFUSED
    assert "installation-wide" in outcomes["transport.nats.credential_lifetime_seconds"].reason
    imported = GatewayInstanceConfig.load(dest / "instance_config.yaml")
    assert imported.transport.nats.credential_lifetime_seconds == 900


def test_version_14_settings_export_imports_with_credential_lifetime_default(tmp_path: Path) -> None:
    path, _config = new_instance(tmp_path / "src", "alpha", "team_a")

    class Version14(SettingsSection):
        schema_version = 14

        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            return SectionExport(
                records=[SettingV1(path=("transport", "queues", "warn_at_percent"), value=75).model_dump(mode="json")]
            )

    bundle = tmp_path / "v14.uat"
    export_team(
        export_source(path),
        bundle,
        PASSPHRASE,
        owner_person_id="usr_owner",
        sections=(Version14(),),
        key_derivation=new_key_derivation(opslimit=1, memlimit=8192),
    )
    dest = tmp_path / "dst"
    with TransferReader.open(bundle, PASSPHRASE) as reader:
        plan = import_team(reader, dest, dry_run=False, sections=(SettingsSection(),))
    assert not plan.by_outcome(RecordOutcome.REFUSED)
    imported = GatewayInstanceConfig.load(dest / "instance_config.yaml")
    assert imported.transport.queues.warn_at_percent == 75
    assert imported.transport.nats.credential_lifetime_seconds == 900
