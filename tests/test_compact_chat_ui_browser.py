"""Real-browser layout, geometry, and WCAG contrast checks for compact Web Chat prompts."""

import pytest

from tests.browser_probe import requires_chromium, run_browser_probe


@pytest.mark.integration
@pytest.mark.live
@requires_chromium
def test_compact_chat_ui_layout_and_contrast_in_light_and_dark_modes():
    results = run_browser_probe(
        "/webchat",
        "compact_chat_ui_browser_probe.mjs",
        "verifyCompactChatUi",
    )
    assert len(results) == 2
