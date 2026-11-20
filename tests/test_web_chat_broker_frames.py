"""What lets Web Chat show broker pages inside the conversation (A8, A9).

The chat's Content-Security-Policy allows framing the broker's public origin
and nothing broader, in both deployments (the broker on its own port, and
path-routed on the chat's own address). Only broker pages carry a card view
address; non-web channels still receive the plain page links.
"""

from __future__ import annotations

import pytest

from assistant_gateway.application.transient_presentation import GatewayTransientAction
from assistant_gateway.interactive_apps.website_handlers.conversation import transient_action_fields
from assistant_gateway.interactive_apps.website_support import _WEB_CHAT_CSP, web_chat_csp
from runtime_settings.network import compose_secure_tool_broker_public_base_url
from types import SimpleNamespace


def _directives(policy: str) -> dict[str, list[str]]:
    return {part.split()[0]: part.split()[1:] for part in (item.strip() for item in policy.split(";")) if part}


@pytest.mark.parametrize(
    ("broker_public_port", "expected"),
    [(18600, "https://assistant.example.com:18600"), (None, "https://assistant.example.com")],
    ids=["broker on its own port", "broker path-routed on the chat's address"],
)
def test_the_chat_may_frame_the_brokers_origin_and_nothing_broader(broker_public_port, expected) -> None:
    config = SimpleNamespace(
        network=SimpleNamespace(public_origin="https://assistant.example.com"),
        security_layer=SimpleNamespace(broker_public_port=broker_public_port),
    )
    policy = _directives(web_chat_csp(compose_secure_tool_broker_public_base_url(config)))

    assert policy["frame-src"] == ["'self'", expected]
    assert policy["frame-ancestors"] == ["'none'"]
    base = _directives(_WEB_CHAT_CSP)
    assert {name: value for name, value in policy.items() if name != "frame-src"} == {
        name: value for name, value in base.items() if name != "frame-src"
    }, "only frame-src changes"


@pytest.mark.parametrize(
    "address",
    [
        "https://broker.example; frame-src *",
        "https://broker.example *",
        "https://broker.example:notaport",
        # A scoped IPv6 address and an unparsable one (review finding F1).
        "http://[fe80::1%x *;script-src *;]:18600",
        "http://[fe80::1%25eth0]:18600",
        "http://[notip]/",
        "",
    ],
)
def test_an_unusable_broker_address_adds_nothing_to_the_policy(address, caplog) -> None:
    assert web_chat_csp(address) == _WEB_CHAT_CSP
    assert ("event=web_chat_broker_frame_origin_unusable" in caplog.text) == bool(address)


def _action(url: str, *, broker_page: bool) -> GatewayTransientAction:
    return GatewayTransientAction(
        kind="tool_setup", label="Open", description="Why", url=url, expires_at_utc=None, broker_page=broker_page
    )


def test_only_a_broker_page_carries_its_card_view_address() -> None:
    page = transient_action_fields(_action("https://assistant.example.com/account-link/s1", broker_page=True))
    assert page == {
        "label": "Open",
        "url": "https://assistant.example.com/account-link/s1",
        "expires_at_utc": None,
        "frame_url": "https://assistant.example.com/account-link/s1?view=card",
    }
    vendor = transient_action_fields(_action("https://gitea.example.com/user/settings", broker_page=False))
    assert "frame_url" not in vendor
