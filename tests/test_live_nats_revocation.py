"""Live nats-server 2.15.1 operator-mode revocation and credential lifetime tests.

Verifies on a real isolated nats-server 2.15.1 running with a ``FULL`` resolver:
- ``OperatorModeRevoker`` pushes operator-signed account JWT updates over a system-account
  connection (``$SYS.REQ.CLAIMS.UPDATE``), immediately closing an open connection of the
  revoked user key and blocking reconnect while a renewed key connects and partition mappings
  remain intact;
- cumulative revocations persist across revoker restarts (C07);
- an account JWT signed by an account key instead of an operator key is refused;
- step-3 reply-vs-revocation sequencing (C01): flushing a reply before revoking delivers
  the reply before the old connection is closed;
- user JWT ``exp`` closes an open connection on expiry and blocks reconnect afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import nats
import pytest

from custom_messages.wire.partitions import account_mappings, agent_partition
from custom_messages.wire.permissions import HostPlacement, roles_permissions
from custom_transport.protocol1.contract_files import role_permissions
from custom_transport.protocol1.nats_jwt import (
    SYSTEM_ACCOUNT_LIMITS,
    NatsSigningKey,
    encode_account_jwt,
    encode_operator_jwt,
    encode_user_jwt,
)
from docker.nats_service import contract_server_settings
from installation_authority.revocation import (
    CLAIMS_UPDATE_SUBJECT,
    AccountBaseClaims,
    OperatorModeRevoker,
)
from tests.live_nats_support import flush_fully
from tests.own_nats_server import own_nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
PARTITIONS = 16


def _nats_server_binary() -> str:
    configured = os.environ.get("LIVE_TEST_NATS_SERVER")
    if configured:
        if not (Path(configured).is_file() and os.access(configured, os.X_OK)):
            pytest.fail(f"LIVE_TEST_NATS_SERVER={configured} is not an executable file")
        return configured
    found = shutil.which("nats-server")
    if found is None:
        pytest.skip("live operator-mode revocation tests need nats-server on PATH or LIVE_TEST_NATS_SERVER")
    return found


@dataclass
class _OperatorServer:
    url: str
    operator: NatsSigningKey
    operator_signing: NatsSigningKey
    system_account: NatsSigningKey
    system_signing: NatsSigningKey
    installation_account: NatsSigningKey
    installation_signing: NatsSigningKey
    base_claims: AccountBaseClaims

    def issue_system_user(self, name: str = "sys-admin") -> tuple[NatsSigningKey, str]:
        key = NatsSigningKey.generate("user")
        token = encode_user_jwt(self.system_signing, self.system_account.public_key, key.public_key, name, None)
        return key, token

    def issue_host_user(
        self,
        name: str,
        permissions: dict | None = None,
        *,
        issued_at: int | None = None,
        expires_at: int | None = None,
    ) -> tuple[NatsSigningKey, str]:
        key = NatsSigningKey.generate("user")
        token = encode_user_jwt(
            self.installation_signing,
            self.installation_account.public_key,
            key.public_key,
            name,
            permissions,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        return key, token


@contextmanager
def _operator_nats_server(folder: Path) -> Iterator[_OperatorServer]:
    jetstream, control_line, _minimum_version = contract_server_settings()
    op = NatsSigningKey.generate("operator")
    op_signing = NatsSigningKey.generate("operator")
    sys_acc = NatsSigningKey.generate("account")
    sys_signing = NatsSigningKey.generate("account")
    inst_acc = NatsSigningKey.generate("account")
    inst_signing = NatsSigningKey.generate("account")
    mappings = account_mappings(PARTITIONS)

    op_jwt_path = folder / "operator.jwt"
    op_jwt_path.write_text(
        encode_operator_jwt(
            op,
            "operator",
            system_account=sys_acc.public_key,
            signing_keys=[op_signing.public_key],
        ),
        encoding="utf-8",
    )
    sys_jwt = encode_account_jwt(
        op,
        sys_acc.public_key,
        "system",
        signing_keys=[sys_signing.public_key],
        limits=SYSTEM_ACCOUNT_LIMITS,
    )
    inst_jwt = encode_account_jwt(
        op_signing,
        inst_acc.public_key,
        "installation",
        signing_keys=[inst_signing.public_key],
        mappings=mappings,
    )
    jwt_dir = folder / "jwt_store"
    jwt_dir.mkdir()
    conf_path = folder / "operator.conf"
    conf_path.write_text(
        json.dumps(
            {
                "operator": str(op_jwt_path),
                "system_account": sys_acc.public_key,
                "resolver": {
                    "type": "full",
                    "dir": str(jwt_dir),
                    "allow_delete": False,
                    "interval": "2m",
                },
                "resolver_preload": {
                    sys_acc.public_key: sys_jwt,
                    inst_acc.public_key: inst_jwt,
                },
                "max_control_line": control_line,
                "jetstream": jetstream,
            }
        ),
        encoding="utf-8",
    )
    base = AccountBaseClaims(
        account_public_key=inst_acc.public_key,
        signing_keys=(inst_signing.public_key,),
        mappings=mappings,
    )
    with own_nats_server(_nats_server_binary(), folder, "-c", str(conf_path), partitions=None) as url:
        yield _OperatorServer(
            url=url,
            operator=op,
            operator_signing=op_signing,
            system_account=sys_acc,
            system_signing=sys_signing,
            installation_account=inst_acc,
            installation_signing=inst_signing,
            base_claims=base,
        )


@pytest.mark.asyncio
async def test_operator_mode_revoker_closes_connection_blocks_reconnect_and_keeps_mappings(tmp_path: Path) -> None:
    with _operator_nats_server(tmp_path) as server:
        sys_key, sys_jwt = server.issue_system_user()
        sys_nc = await nats.connect(server.url, **sys_key.login_options(sys_jwt), allow_reconnect=False)
        try:
            revoker = OperatorModeRevoker(server.operator_signing, server.base_claims, sys_nc)

            # Host connects with its old NATS key.
            old_key, old_jwt = server.issue_host_user("worker-1")
            disconnected = asyncio.Event()

            async def on_disconnect() -> None:
                disconnected.set()

            old_nc = await nats.connect(
                server.url,
                **old_key.login_options(old_jwt),
                allow_reconnect=False,
                disconnected_cb=on_disconnect,
            )
            await flush_fully(old_nc)

            # Revoke old_key (timestamp covers future certificate end, CR-1).
            revoke_ts = int(time.time()) + 3600
            outcome = await revoker.revoke(old_key.public_key, revoke_until=revoke_ts)
            assert outcome.revoked is True

            # Open connection is closed immediately and cannot reconnect.
            await asyncio.wait_for(disconnected.wait(), timeout=2.0)
            with pytest.raises(Exception, match="Authorization Violation"):
                await nats.connect(server.url, **old_key.login_options(old_jwt), allow_reconnect=False)

            # Renewed key connects cleanly, and partition mappings survived the account JWT update (C07).
            new_key, new_jwt = server.issue_host_user("worker-1")
            new_nc = await nats.connect(server.url, **new_key.login_options(new_jwt), allow_reconnect=False)
            try:
                expected_part = agent_partition("team_a", "agent-1", PARTITIONS)
                arrived = asyncio.Future()

                async def on_mapped(msg) -> None:
                    if not arrived.done():
                        arrived.set_result(msg.subject)

                await new_nc.subscribe(f"ua.v1.team_a.*.ctl.agent.agent-1.{expected_part}", cb=on_mapped)
                await flush_fully(new_nc)
                await new_nc.publish("ua.v1.team_a.worker-1.ctl.agent.agent-1", b"ping")
                await flush_fully(new_nc)
                assert await asyncio.wait_for(arrived, timeout=2.0) == (
                    f"ua.v1.team_a.worker-1.ctl.agent.agent-1.{expected_part}"
                )
            finally:
                await new_nc.close()

            # A freshly constructed revoker (simulating authority restart) passing cumulative
            # ledger revocations revokes a second user while keeping the first user revoked.
            second_key, second_jwt = server.issue_host_user("worker-2")
            second_nc = await nats.connect(server.url, **second_key.login_options(second_jwt), allow_reconnect=False)
            await flush_fully(second_nc)
            restarted_revoker = OperatorModeRevoker(server.operator_signing, server.base_claims, sys_nc)
            out2 = await restarted_revoker.revoke(
                second_key.public_key,
                revoke_until=revoke_ts,
                all_revocations={old_key.public_key: revoke_ts},
            )
            assert out2.revoked is True
            for revoked_key, revoked_jwt in ((old_key, old_jwt), (second_key, second_jwt)):
                with pytest.raises(Exception, match="Authorization Violation"):
                    await nats.connect(server.url, **revoked_key.login_options(revoked_jwt), allow_reconnect=False)

            # An account update signed with the account signing key (not an operator key)
            # is ignored by nats-server ("account validation failed"): an active user named
            # in its revocations stays connected and previously revoked keys stay revoked.
            from custom_transport.protocol1.nats_jwt import encode_jwt

            third_key, third_jwt = server.issue_host_user("worker-3")
            third_nc = await nats.connect(server.url, **third_key.login_options(third_jwt), allow_reconnect=False)
            try:
                await flush_fully(third_nc)
                forged = encode_jwt(
                    server.installation_signing,
                    server.installation_account.public_key,
                    "installation",
                    {
                        "type": "account",
                        "version": 2,
                        "signing_keys": [server.installation_signing.public_key],
                        "revocations": {third_key.public_key: revoke_ts},
                    },
                )
                await sys_nc.request(CLAIMS_UPDATE_SUBJECT, forged.encode("ascii"), timeout=5.0)
                await flush_fully(third_nc)
                assert third_nc.is_connected
                with pytest.raises(Exception, match="Authorization Violation"):
                    await nats.connect(server.url, **old_key.login_options(old_jwt), allow_reconnect=False)
            finally:
                await third_nc.close()
        finally:
            await sys_nc.close()


@pytest.mark.asyncio
async def test_flush_before_revoke_delivers_step3_renewal_reply_before_disconnect(tmp_path: Path) -> None:
    """C01: publishing and flushing host.renew.reply before invoking OperatorModeRevoker.revoke
    delivers the new credential to the host before nats-server closes its old connection."""

    with _operator_nats_server(tmp_path) as server:
        sys_key, sys_jwt = server.issue_system_user()
        auth_key, auth_jwt = server.issue_host_user("authority-1")
        sys_nc = await nats.connect(server.url, **sys_key.login_options(sys_jwt), allow_reconnect=False)
        auth_nc = await nats.connect(server.url, **auth_key.login_options(auth_jwt), allow_reconnect=False)
        try:
            revoker = OperatorModeRevoker(server.operator_signing, server.base_claims, sys_nc)
            for trial in range(10):
                old_key, old_jwt = server.issue_host_user(f"host-{trial}")
                new_key, new_jwt = server.issue_host_user(
                    f"host-{trial}",
                    roles_permissions(
                        [role_permissions("worker_host")],
                        HostPlacement(f"host-{trial}", ("team_a",), partitions={"team_a": ()}, installation="install_1"),
                        partition_count=PARTITIONS,
                    ),
                )
                got_reply: asyncio.Future[bytes] = asyncio.Future()
                disconnected = asyncio.Event()

                async def on_reply(msg, fut=got_reply) -> None:
                    if not fut.done():
                        fut.set_result(msg.data)

                async def on_disc(ev=disconnected) -> None:
                    ev.set()

                host_nc = await nats.connect(
                    server.url,
                    **old_key.login_options(old_jwt),
                    allow_reconnect=False,
                    disconnected_cb=on_disc,
                )
                inbox = f"_INBOX.host-{trial}.renew"
                await host_nc.subscribe(inbox, cb=on_reply)
                await flush_fully(host_nc)

                # Authority sends reply, flushes fully, then revokes old key.
                await auth_nc.publish(inbox, new_jwt.encode("ascii"))
                await flush_fully(auth_nc)
                outcome = await revoker.revoke(old_key.public_key, revoke_until=int(time.time()) + 3600)
                assert outcome.revoked is True

                received_jwt = (await asyncio.wait_for(got_reply, timeout=2.0)).decode("ascii")
                assert received_jwt == new_jwt
                await asyncio.wait_for(disconnected.wait(), timeout=2.0)

                # Host reconnects using the received JWT and new key.
                reconnected = await nats.connect(
                    server.url, **new_key.login_options(received_jwt), allow_reconnect=False
                )
                await flush_fully(reconnected)
                await reconnected.close()
        finally:
            await auth_nc.close()
            await sys_nc.close()


@pytest.mark.asyncio
async def test_user_jwt_expiration_disconnects_open_connection_and_refuses_reconnect(tmp_path: Path) -> None:
    """Decision 100 external-server guarantee: short-lived user JWTs close open connections at exp."""

    with _operator_nats_server(tmp_path) as server:
        now = int(time.time())
        key, token = server.issue_host_user("expiring-host", issued_at=now, expires_at=now + 2)
        disconnected = asyncio.Event()

        async def on_disc() -> None:
            disconnected.set()

        nc = await nats.connect(
            server.url,
            **key.login_options(token),
            allow_reconnect=False,
            disconnected_cb=on_disc,
        )
        await flush_fully(nc)
        await asyncio.wait_for(disconnected.wait(), timeout=5.0)
        # Wait past whole-second exp boundary and verify reconnect is refused.
        await asyncio.sleep(1.1)
        with pytest.raises(Exception, match="Authorization Violation"):
            await nats.connect(server.url, **key.login_options(token), allow_reconnect=False)


@pytest.mark.asyncio
async def test_operator_mode_revoker_rejects_mismatched_operator_signing_key_via_accountz(tmp_path: Path) -> None:
    """C08: when OperatorModeRevoker signs with an operator-role key not trusted by operator.jwt,
    DirResolver still returns code=200 while nats-server rejects the account JWT; ACCOUNTZ must
    catch it so revoke() returns revoked=False and leaves applied_revocations unchanged."""

    with _operator_nats_server(tmp_path) as server:
        sys_key, sys_jwt = server.issue_system_user()
        sys_nc = await nats.connect(server.url, **sys_key.login_options(sys_jwt), allow_reconnect=False)
        user_key, user_jwt = server.issue_host_user("worker-mismatch")
        user_nc = await nats.connect(server.url, **user_key.login_options(user_jwt), allow_reconnect=False)
        try:
            await flush_fully(user_nc)
            untrusted_operator = NatsSigningKey.generate("operator")
            bad_revoker = OperatorModeRevoker(untrusted_operator, server.base_claims, sys_nc)
            outcome = await bad_revoker.revoke(user_key.public_key, revoke_until=int(time.time()) + 3600)
            assert outcome.revoked is False
            assert bad_revoker.applied_revocations == {}
            await flush_fully(user_nc)
            assert user_nc.is_connected is True
        finally:
            await user_nc.close()
            await sys_nc.close()


@pytest.mark.asyncio
async def test_operator_mode_revoker_prunes_expired_entries_and_empty_permissions_deny_all(tmp_path: Path) -> None:
    """C09 & C10 live checks on nats-server 2.15.1:
    - prune_before removes expired revocations from the live account JWT while revoking active keys;
    - empty publish/subscribe permissions emit deny:['>'] and cannot publish or subscribe."""

    with _operator_nats_server(tmp_path) as server:
        sys_key, sys_jwt = server.issue_system_user()
        sys_nc = await nats.connect(server.url, **sys_key.login_options(sys_jwt), allow_reconnect=False)
        # Keep one active installation-account connection so the account stays loaded in ACCOUNTZ.
        keepalive_key, keepalive_jwt = server.issue_host_user("keepalive")
        keepalive_nc = await nats.connect(
            server.url, **keepalive_key.login_options(keepalive_jwt), allow_reconnect=False
        )
        try:
            await flush_fully(keepalive_nc)
            revoker = OperatorModeRevoker(server.operator_signing, server.base_claims, sys_nc)
            now = int(time.time())
            expired_key = NatsSigningKey.generate("user")
            active_key, active_jwt = server.issue_host_user("worker-active")
            active_nc = await nats.connect(server.url, **active_key.login_options(active_jwt), allow_reconnect=False)
            await flush_fully(active_nc)

            out1 = await revoker.revoke(expired_key.public_key, revoke_until=now - 120)
            assert out1.revoked is True
            out2 = await revoker.revoke(active_key.public_key, revoke_until=now + 3600, prune_before=now - 60)
            assert out2.revoked is True
            assert expired_key.public_key not in revoker.applied_revocations
            assert active_key.public_key in revoker.applied_revocations

            # C10: a user JWT with empty publish/subscribe lists must be denied on arbitrary subjects.
            denied_key, denied_jwt = server.issue_host_user("denied-user", {"publish": [], "subscribe": []})
            errors: list[Exception] = []

            async def on_error(exc: Exception) -> None:
                errors.append(exc)

            denied_nc = await nats.connect(
                server.url,
                **denied_key.login_options(denied_jwt),
                allow_reconnect=False,
                error_cb=on_error,
            )
            try:
                await denied_nc.publish("foo.bar", b"nope")
                await denied_nc.subscribe("foo.bar")
                await flush_fully(denied_nc)
                await asyncio.sleep(0.1)
                assert any("permissions violation" in str(err).lower() for err in errors), errors
            finally:
                await denied_nc.close()
        finally:
            await keepalive_nc.close()
            await sys_nc.close()
