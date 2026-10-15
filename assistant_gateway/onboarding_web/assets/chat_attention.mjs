// Attention is browser presentation state, never task or approval authority.

import { ACCESS_REVIEW_MESSAGE_TYPE } from "./access_inbox.mjs";

export function createChatAttention({ browser, document, lifecycle, getThreads, isAuthenticated, openThread, isChatView = true }) {
  const baseTitle = document.title;
  // The Chat Alerts switch (a checkbox styled as a slider).
  const toggleInput = document.getElementById("browser-notifications-toggle");
  const status = document.getElementById("browser-notifications-status");
  const seen = new Map(), sequences = new Map(), notices = new Set();
  let principal = "", enabled = false, epoch = 0;
  const supported = () => Boolean(browser.isSecureContext && browser.Notification);
  const away = () => document.hidden || !document.hasFocus();
  const key = () => "ua.webchat.notifications." + encodeURIComponent(principal);
  function refresh() {
    const unread = isAuthenticated() ? getThreads().filter(thread => thread.hasUnreadReply) : [];
    const attention = unread.some(thread => thread.unreadReplyLevel === "attention");
    if (isChatView) document.title = unread.length ? "(" + unread.length + (attention ? " need attention" : " unread") + ") " + baseTitle : baseTitle;
    toggleInput.checked = enabled;
    toggleInput.disabled = !supported() || !principal;
    // Only problems are explained; the switch itself shows on or off.
    status.textContent = !supported() ? "Chat alerts need HTTPS and a supported browser. The tab still shows unread chats."
      : browser.Notification.permission === "denied" ? "Chat alerts are blocked. Allow notifications in your browser's site settings."
      : "";
  }
  function clear() {
    epoch++; principal = ""; enabled = false; seen.clear(); sequences.clear();
    notices.forEach(notice => notice.close()); notices.clear();
    if (isChatView) document.title = baseTitle;
  }
  async function toggle() {
    if (!principal || !supported()) return;
    const version = epoch;
    if (enabled) enabled = false;
    else {
      try {
        const permission = browser.Notification.permission === "granted" ? "granted"
          : await browser.Notification.requestPermission();
        if (version !== epoch) return;
        enabled = permission === "granted";
      } catch (_) {
        if (version !== epoch) return;
        status.textContent = "This browser could not turn on chat alerts. The tab still shows unread chats.";
        toggleInput.checked = enabled;
        return;
      }
    }
    try { browser.localStorage.setItem(key(), enabled ? "on" : "off"); } catch (_) { /* Session-only preference. */ }
    refresh();
  }
  lifecycle.listen(toggleInput, "change", toggle);
  lifecycle.signal.addEventListener("abort", clear, { once: true });
  return {
    away, refresh, clear,
    configure(user) {
      clear(); principal = user;
      try { enabled = browser.localStorage.getItem(key()) === "on"; } catch (_) { /* Session-only preference. */ }
      refresh();
    },
    receive(event, thread) {
      if (!isAuthenticated() || !principal || !event.message_id || event.role === "user") return false;
      const id = event.session_key + ":" + event.message_id;
      const sequence = Number(event.session_sequence || 0);
      if (seen.has(id) || (sequence && sequence <= (sequences.get(event.session_key) || 0))) return false;
      if (sequence) sequences.set(event.session_key, sequence);
      seen.set(id, true);
      if (seen.size > 2000) seen.delete(seen.keys().next().value);
      if (enabled && supported() && browser.Notification.permission === "granted" && away()) {
        const version = epoch;
        try {
          // Generic text avoids copying conversation or approval secrets to the desktop.
          const needsAttention = ["attention", "warning", "error"].includes(event.message_level)
            || event.message_type === ACCESS_REVIEW_MESSAGE_TYPE;
          const notice = new browser.Notification(needsAttention ? "Your attention is needed" : "New agent message", {
            body: "Open Universal Agents to read it.", tag: principal + ":" + id,
          });
          notices.add(notice);
          notice.onclose = () => notices.delete(notice);
          notice.onclick = () => {
            notice.close(); notices.delete(notice);
            if (version !== epoch || !isAuthenticated()) return;
            browser.focus(); openThread(thread);
          };
        } catch (_) {
          status.textContent = "Browser alerts are unavailable. The tab still shows unread chats.";
        }
      }
      return true;
    },
  };
}
