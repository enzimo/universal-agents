from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_ASSET = REPO_ROOT / "assistant_gateway" / "onboarding_web" / "assets" / "chat_markdown.js"


def _render_markdown(markdown: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the Web Chat markdown renderer")

    script = """
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[1], "utf8");
const input = fs.readFileSync(0, "utf8");
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "chat_markdown.js" });
process.stdout.write(sandbox.UAWebChatMarkdown.render(input));
"""
    result = subprocess.run(
        [node, "-e", script, str(MARKDOWN_ASSET)],
        input=markdown,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def test_review_links_display_labels_and_preserve_destinations() -> None:
    inbox_url = "https://nova.example/webchat?view=inbox#interaction=human_int_test"
    html = _render_markdown(
        f"[Review all requests]({inbox_url})\n\n"
        "[Review schedules and objectives](/webchat?view=objectives)"
    )
    assert f'href="{inbox_url}"' in html
    assert ">Review all requests</a>" in html
    assert f">{inbox_url}</a>" not in html
    assert 'href="/webchat?view=objectives"' in html
    assert ">Review schedules and objectives</a>" in html


@pytest.mark.parametrize("destination", ["//example.com", "/\\example.com", "javascript:alert(1)"])
def test_relative_navigation_does_not_allow_unsafe_link_forms(destination: str) -> None:
    assert '<a href=' not in _render_markdown(f"[Review]({destination})")


def test_web_chat_markdown_renderer_renders_tables_and_escapes_html() -> None:
    html = _render_markdown(
        """Here's the team in table format:

| Agent | What <script>It</script> Does |
|---|---|
| **WebResearchAgent** | Web search, [docs](https://example.com), and `code` |
| CodeWritingAgent | Write/modify code |

Raw <b>html</b>
"""
    )

    assert '<div class="message-table-wrap"><table>' in html
    assert '<th scope="col">Agent</th>' in html
    assert "What &lt;script&gt;It&lt;/script&gt; Does" in html
    assert "<strong>WebResearchAgent</strong>" in html
    assert '<a href="https://example.com" target="_blank" rel="noreferrer">docs</a>' in html
    assert "<code>code</code>" in html
    assert "| Agent |" not in html
    assert "<script>" not in html
    assert "<b>" not in html
    assert "Raw &lt;b&gt;html&lt;/b&gt;" in html


def test_web_chat_markdown_renderer_continues_after_fenced_code_blocks() -> None:
    html = _render_markdown(
        """Before

```js
const ok = 1 < 2;
```

After
"""
    )

    assert "<p>Before</p>" in html
    assert '<pre><code class="language-js">const ok = 1 &lt; 2;</code></pre>' in html
    assert "<p>After</p>" in html


def test_web_chat_markdown_renderer_autolinks_bare_urls() -> None:
    html = _render_markdown(
        """- Automate auth workflows (docs: http://192.168.1.50:18090/docs/auth/)
- Link with punctuation: https://example.com/docs).
- Existing [docs](https://example.com/label) still work.
- Inline code `https://example.com/code` stays code.
"""
    )

    assert (
        '<a href="http://192.168.1.50:18090/docs/auth/" target="_blank" rel="noreferrer">'
        "http://192.168.1.50:18090/docs/auth/</a>)"
    ) in html
    assert (
        '<a href="https://example.com/docs" target="_blank" rel="noreferrer">'
        "https://example.com/docs</a>)."
    ) in html
    assert '<a href="https://example.com/label" target="_blank" rel="noreferrer">docs</a>' in html
    assert "<code>https://example.com/code</code>" in html
    assert 'href="https://example.com/code"' not in html


def test_web_chat_markdown_renderer_keeps_markdown_markers_out_of_url_href() -> None:
    html = _render_markdown(
        "Open it here: **http://127.0.0.1:18090/docs/sites/thread-test_20260617T033818Z-garage-door-opener-install/**"
    )

    expected_url = "http://127.0.0.1:18090/docs/sites/thread-test_20260617T033818Z-garage-door-opener-install/"
    assert f'<strong><a href="{expected_url}" target="_blank" rel="noreferrer">{expected_url}</a></strong>' in html
    assert 'href="http://127.0.0.1:18090/docs/sites/thread-test_20260617T033818Z-garage-door-opener-install/**"' not in html
    assert "**" not in html


def test_web_chat_markdown_renderer_bolds_slash_commands() -> None:
    html = _render_markdown(
        """Interactive commands:

- /help - show this help
- /docs - show the documentation home page
- /auth [status|github] - start broker auth
- Inline code `/stop` stays code.
- URL path https://example.com/docs/auth/ stays a plain link.
"""
    )

    assert "<strong>/help</strong> - show this help" in html
    assert "<strong>/docs</strong> - show the documentation home page" in html
    assert "<strong>/auth</strong> [status|github] - start broker auth" in html
    assert "<code>/stop</code>" in html
    assert "<strong>/stop</strong>" not in html
    assert (
        '<a href="https://example.com/docs/auth/" target="_blank" rel="noreferrer">'
        "https://example.com/docs/auth/</a>"
    ) in html


def test_web_chat_markdown_renderer_formats_welcome_guidance() -> None:
    html = _render_markdown(
        """Start a conversation with your agent team.

Interactive commands:

- /help - show this help
- /new - start a new session
- /stop - stop active work
"""
    )

    assert "<p>Start a conversation with your agent team.</p>" in html
    assert "<p>Interactive commands:</p>" in html
    assert "<ul>" in html
    assert "<strong>/help</strong> - show this help" in html


def test_web_chat_markdown_renderer_underlines_double_underscores() -> None:
    """The /accounts list underlines its labels; dunder names and code stay as typed."""

    html = _render_markdown(
        "- **Exa web search** (`http:exa`): shared. __Team account__: linked. "
        "__Your own__: not linked; link it with `/link http:exa`.\n"
        "- Python's __init__.py and a snake_case_name stay plain; `__code__` stays code."
    )

    assert "<strong>Exa web search</strong> (<code>http:exa</code>)" in html
    assert "<u>Team account</u>: linked. <u>Your own</u>: not linked" in html
    assert "<code>/link http:exa</code>" in html
    assert "__init__.py" in html and "snake_case_name" in html
    assert "<code>__code__</code>" in html
    assert html.count("<u>") == 2


def test_web_chat_underlines_only_outside_unicode_words() -> None:
    html = _render_markdown("__Label__: café__cache__ __label__界 élève__init__.py ²__count__")

    assert "<u>Label</u>:" in html
    assert "café__cache__ __label__界 élève__init__.py ²__count__" in html
    assert html.count("<u>") == 1
