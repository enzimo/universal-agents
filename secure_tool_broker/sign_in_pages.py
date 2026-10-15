"""Page pieces shared by every broker sign-in flow (GitHub, Google, X).

Kept apart from ``secure_tool_broker.auth_flows`` so the X sign-in module can
use them without importing the flow registry, which imports it: the page
script that polls a sign-in's status, and the status payload that script reads.
"""

from __future__ import annotations

import html
from typing import Any

# How long a sign-in page (and the sign-in process behind it) stays open:
# 15 minutes, because setting up a tool can take several steps, such as
# creating a Google Cloud app before signing in (owner's decision 2026-09-29).
SIGN_IN_PAGE_SECONDS = 15 * 60

# Show the whole address as the link text only when it is short enough to read.
AUTH_LINK_TEXT_MAX_CHARS = 100


def auth_link_html(url: str) -> str:
    """A plain link to the sign-in address under the Open button, so a person can open or copy it themselves.

    The Open button opens the address with a script, so a browser offers no
    "copy link" for it. The page script (``sign_in_wait_script``) fills this
    link in when the address arrives; until then it is hidden.
    """

    shown = "" if url else " hidden"
    text = url if len(url) <= AUTH_LINK_TEXT_MAX_CHARS else "the sign-in link"
    return (
        f'<p class="auth-link-fallback" data-broker-auth-link-row{shown}>If Open does not work, '
        f'open or copy <a data-broker-auth-link href="{html.escape(url, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer">{html.escape(text)}</a>.</p>'
    )



def sign_in_wait_script() -> str:
    """Poll a sign-in page's status in place; the restart hint comes from ``data-restart-command``."""

    return """
<script>
(() => {
  const statusEl = document.getElementById("broker-auth-status");
  const authButton = document.querySelector("[data-broker-auth-open]");
  const authLink = document.querySelector("[data-broker-auth-link]");
  const authLinkRow = document.querySelector("[data-broker-auth-link-row]");
  const redirectCode = document.getElementById("google-redirect-uri");
  const deviceCode = document.getElementById("auth-code");
  const copyButton = document.getElementById("auth-code-copy");
  const restartCommand = statusEl ? statusEl.getAttribute("data-restart-command") || "" : "";
  const statusUrl = `${window.location.pathname}/status`;
  let renderedMessage = statusEl ? statusEl.getAttribute("data-markdown") || "" : "";
  let timer = null;
  let stopped = false;

  if (authButton) authButton.addEventListener("click", () => {
    const url = authButton.getAttribute("data-auth-url");
    if (!authButton.disabled && url) window.open(url, "_blank", "noopener,noreferrer");
  });

  function renderMessage(message) {
    if (!statusEl || message === renderedMessage) return;
    renderedMessage = message;
    statusEl.setAttribute("data-markdown", message);
    if (window.UABrokerRenderMarkdown) window.UABrokerRenderMarkdown(statusEl, message);
    else statusEl.textContent = message;
  }

  function stopPolling() {
    stopped = true;
    window.clearTimeout(timer);
  }

  function redirectUriFromAuthUrl(rawUrl) {
    try {
      return new URL(rawUrl).searchParams.get("redirect_uri") || "";
    } catch (_error) {
      return "";
    }
  }

  function renderAuthUrl(rawUrl) {
    if (!rawUrl || !authButton || authButton.getAttribute("data-auth-url") === rawUrl) return;
    authButton.setAttribute("data-auth-url", rawUrl);
    authButton.textContent = "Open";
    authButton.disabled = false;
    if (authLink) {
      authLink.href = rawUrl;
      authLink.textContent = rawUrl.length <= 100 ? rawUrl : "the sign-in link";
      if (authLinkRow) authLinkRow.hidden = false;
    }

    const redirectUri = redirectUriFromAuthUrl(rawUrl);
    if (redirectCode && redirectUri && redirectCode.textContent !== redirectUri) redirectCode.textContent = redirectUri;
  }

  async function pollStatus() {
    if (stopped) return;
    try {
      const response = await fetch(statusUrl, {cache: "no-store"});
      if (stopped) return;
      if (response.status === 404 || response.status === 410) {
        stopPolling();
        if (authButton) {
          authButton.disabled = true;
          authButton.textContent = "Closed";
        }
        renderMessage(`This sign-in page is no longer available. Start again with ${restartCommand}.`);
        return;
      }
      if (!response.ok) return;
      const payload = await response.json();
      if (stopped) return;
      renderMessage(payload.message || "");
      if (payload.external_url) renderAuthUrl(payload.external_url);
      if (deviceCode && payload.user_code && deviceCode.textContent !== payload.user_code) {
        deviceCode.textContent = payload.user_code;
        if (copyButton) copyButton.disabled = false;
      }
      if (payload.status === "success" || payload.status === "error") {
        stopPolling();
        // Navigate with GET once, including when this page was rendered by a form POST.
        window.location.replace(window.location.href);
      }
    } catch (_error) {
      if (!stopped) renderMessage("Unable to check authorization status. Retrying automatically.");
    } finally {
      // Schedule after completion so slow responses cannot overlap or arrive out of order.
      if (!stopped) timer = window.setTimeout(pollStatus, 3000);
    }
  }
  pollStatus();
})();
</script>"""


def session_status_payload(session: Any) -> dict[str, Any]:
    status = str(session.state.get("status") or "pending")
    message = (
        str(session.state.get("error") or session.state.get("message") or "")
        if status == "error"
        else str(session.state.get("message") or session.state.get("error") or "")
    )
    return {
        "provider": session.provider,
        "method": session.method,
        "status": status,
        "message": message,
        "external_url": session.state.get("external_url"),
        "user_code": session.state.get("user_code"),
        "exit_code": session.state.get("exit_code"),
        "diagnostic_reference": session.state.get("diagnostic_reference"),
        "failure_reason": session.state.get("failure_reason"),
        "auth_stage": session.state.get("auth_stage"),
        # The account a finished sign-in used, when the flow knows it (X); no secret.
        "username": session.state.get("username"),
        "expires_at": session.expires_at.isoformat(),
    }


__all__ = ["session_status_payload", "sign_in_wait_script"]
