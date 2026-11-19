"""Run the JavaScript tests of the broker card view and its Web Chat frame.

They also run in CI's browser page lifecycle step (.github/workflows/tests.yml);
this keeps them in the Python suite too.
"""

import subprocess
from pathlib import Path

CARD_SCRIPT_TESTS = ("broker_card_bridge.test.mjs",)


def test_broker_card_scripts():
    result = subprocess.run(
        ["rtk", "proxy", "node", "--test", *(str(Path(__file__).with_name(name)) for name in CARD_SCRIPT_TESTS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
