"""Opt-in browser regression for the shared offline installation guide."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RUN_CHROMIUM_TESTS") != "1",
                    reason="set RUN_CHROMIUM_TESTS=1 with Node Playwright and Chromium installed")
def test_setup_guide_themes_and_disclosures():
    probe = Path(__file__).with_name("setup_guide_browser_probe.cjs")
    result = subprocess.run(["rtk", "proxy", "node", str(probe)],
                            capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
