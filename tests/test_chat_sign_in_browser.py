"""Real-layout regression checks; also runnable through the collaborative browser."""

import pytest

from tests.browser_probe import requires_chromium, run_browser_probe


@pytest.mark.integration
@pytest.mark.live
@requires_chromium
def test_sign_in_resize_motion_timeout_and_fast_logout():
    results = run_browser_probe("/webchat", "chat_sign_in_browser_probe.mjs", "verifySignInBrowser")
    assert len(results) == 5
