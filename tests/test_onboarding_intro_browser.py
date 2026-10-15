"""The onboarding intro opens until the person goes past Welcome (real browser)."""

import pytest

from tests.browser_probe import requires_chromium, run_browser_probe


@pytest.mark.integration
@pytest.mark.live
@requires_chromium
def test_onboarding_intro_opens_until_past_welcome():
    results = run_browser_probe("/", "onboarding_intro_browser_probe.mjs", "verifyOnboardingIntroBrowser")
    assert len(results) == 5
