"""Real-layout checks of the Web Chat Knowledge page (opt-in: ``RUN_CHROMIUM_TESTS=1``)."""

import pytest

from tests.browser_probe import requires_chromium, run_browser_probe


@pytest.mark.integration
@pytest.mark.live
@requires_chromium
def test_knowledge_page_layout_refusal_placement_and_narrow_screen():
    results = run_browser_probe("/webchat", "knowledge_page_browser_probe.mjs", "verifyKnowledgePage")
    assert len(results) == 15
