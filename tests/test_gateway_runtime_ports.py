from __future__ import annotations

import re
from datetime import timezone


from assistant_gateway.application import (
    GatewayClock,
    GatewayIdentityAuthorizer,
    GatewayIdentitySecrets,
    GatewayIdGenerator,
)
from assistant_gateway.runtime_ports import (
    MembershipRoleIdentityAuthorizer,
    SystemGatewayClock,
    SystemGatewayIdentitySecrets,
    UuidGatewayIdGenerator,
)


def test_default_clock_and_id_generator_satisfy_application_ports() -> None:
    clock = SystemGatewayClock()
    ids = UuidGatewayIdGenerator()

    assert isinstance(clock, GatewayClock)
    assert isinstance(ids, GatewayIdGenerator)
    assert clock.now_utc().tzinfo == timezone.utc
    assert re.fullmatch(r"corr_id_[0-9a-f]{12}", ids.new_correlation_id())
    assert re.fullmatch(r"local_corr_[0-9a-f]{12}", ids.new_local_correlation_id())
    assert re.fullmatch(r"cron_[0-9a-f]{12}", ids.new_scheduled_job_id())
    assert re.fullmatch(r"session_[0-9a-f]{12}", ids.new_session_key())
    assert re.fullmatch(
        r"conversation_id_[0-9a-f]{12}",
        ids.new_conversation_id(),
    )


def test_default_identity_adapters_satisfy_application_ports() -> None:
    authorizer = MembershipRoleIdentityAuthorizer()
    secrets = SystemGatewayIdentitySecrets()

    assert isinstance(authorizer, GatewayIdentityAuthorizer)
    assert isinstance(secrets, GatewayIdentitySecrets)
    assert secrets.new_web_user_token().startswith("uauser_")
    assert secrets.new_web_user_invite().startswith("uainvite_")
    link_codes = [secrets.new_link_code() for _ in range(1_000)]
    assert all(re.fullmatch(r"[A-Za-z0-9]{10}", code) for code in link_codes)
