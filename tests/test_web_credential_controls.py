from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL_CONTROLS_ASSET = (
    REPO_ROOT
    / "assistant_gateway"
    / "onboarding_web"
    / "assets"
    / "credential_controls.js"
)


def test_session_countdown_reaches_expired_state_at_deadline() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the Web Chat credential controls")

    harness = r"""
const fs = require("fs");
const vm = require("vm");

const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox, {
  filename: "credential_controls.js",
});
const format = sandbox.window.UACredentialControls.formatSessionCountdown;
const deadline = Date.parse("2026-08-30T12:01:00Z");
process.stdout.write(JSON.stringify({
  aboveMinute: format("2026-08-30T12:01:00Z", deadline - 60001),
  finalSecond: format("2026-08-30T12:01:00Z", deadline - 1),
  atDeadline: format("2026-08-30T12:01:00Z", deadline),
  afterDeadline: format("2026-08-30T12:01:00Z", deadline + 1),
  invalid: format("not-a-date", deadline),
}));
"""
    result = subprocess.run(
        [node, "-e", harness, str(CREDENTIAL_CONTROLS_ASSET)],
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(result.stdout) == {
        "aboveMinute": "1:01 remaining",
        "finalSecond": "0:01 remaining",
        "atDeadline": "expired",
        "afterDeadline": "expired",
        "invalid": "",
    }
