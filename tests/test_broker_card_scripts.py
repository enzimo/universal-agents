"""Run the JavaScript tests of the broker card view and its Web Chat frame.

They also run in CI's browser page lifecycle step (.github/workflows/tests.yml);
this keeps them in the Python suite too.
"""

import subprocess
from pathlib import Path

CARD_SCRIPT_TESTS = ("broker_card_bridge.test.mjs", "broker_card.test.mjs")


def test_broker_card_scripts():
    result = subprocess.run(
        ["rtk", "proxy", "node", "--test", *(str(Path(__file__).with_name(name)) for name in CARD_SCRIPT_TESTS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_chat_frame_uses_the_brokers_sandbox_tokens():
    """One sandbox for the card view's own policy and Web Chat's iframe (secure_tools.broker_pages)."""

    from secure_tools.broker_pages import CARD_COLOR_MODE_PARAM, CARD_FRAME_SANDBOX

    module = (Path(__file__).parents[1] / "assistant_gateway/onboarding_web/assets/broker_card.mjs").read_text()
    assert f'export const CARD_FRAME_SANDBOX = "{CARD_FRAME_SANDBOX}";' in module
    assert f'export const CARD_COLOR_MODE_PARAM = "{CARD_COLOR_MODE_PARAM}";' in module
