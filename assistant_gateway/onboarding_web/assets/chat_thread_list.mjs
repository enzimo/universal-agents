// Web Chat's thread list: sections, two-line rows and what each row says.
//
// The gateway decides where a thread sits (web_chat_threads.py): what it waits
// on from the person (`needs_you`), whether it is pinned, and whether it is
// snoozed, woke from a snooze, or dismissed (`list_state`). This module turns
// those into the sidebar's sections and each row's second line. It also owns
// the one rule for an "ordinary" message (not a system card, not a slash
// command) and its plain-text snippet, used for the gateway's latest message
// and for messages that arrive on the live page, so both give the same line.

export const THREAD_SECTION = Object.freeze({
  NEEDS_YOU: "needs_you",
  PINNED: "pinned",
  RECENT: "recent",
  SNOOZED: "snoozed",
  DISMISSED: "dismissed",
});

export const SECTION_ORDER = Object.freeze([
  THREAD_SECTION.NEEDS_YOU,
  THREAD_SECTION.PINNED,
  THREAD_SECTION.RECENT,
  THREAD_SECTION.SNOOZED,
  THREAD_SECTION.DISMISSED,
]);

export const SECTION_LABELS = Object.freeze({
  [THREAD_SECTION.NEEDS_YOU]: "Needs you",
  [THREAD_SECTION.PINNED]: "Pinned",
  [THREAD_SECTION.RECENT]: "Recent",
  [THREAD_SECTION.SNOOZED]: "Snoozed",
  [THREAD_SECTION.DISMISSED]: "Dismissed",
});

// The gateway's `list_state` values (web_chat_threads.ThreadListState).
export const LIST_STATE = Object.freeze({ SNOOZED: "snoozed", WOKE: "woke", DISMISSED: "dismissed" });

// The placement the page sends on its own (``WebChatThreadAction.CLEAR_WOKE``):
// when the person opens a woke thread, the gateway clears the mark only if
// the thread is still woke by its own rule, so an old copy of the list here
// cannot undo a newer snooze or dismiss from another device.
export const THREAD_PLACEMENT_ACTION = Object.freeze({ CLEAR_WOKE: "clear_woke" });

// The gateway's `needs_you` values (web_chat_threads.NeedsYouReason) and the
// row's second line for each, short enough to fit the line beside part 2's
// agent trail; the full sentence is the line's title (plan review C24).
export const NEEDS_YOU_LABELS = Object.freeze({
  question: "Waiting for your answer",
  reply: "Waiting for your reply",
  setup: "Needs you to finish setup",
  credential: "Needs a sign-in or key",
});

export const NEEDS_YOU_DESCRIPTIONS = Object.freeze({
  question: "The team asked you a question and is waiting for your answer.",
  reply: "The team paused this request and is waiting for your reply.",
  setup: "A setup page in this chat is waiting for you to finish it.",
  credential: "A form in this chat is waiting for a sign-in or key you enter.",
});

// Sections that start collapsed until the person opens them.
const COLLAPSED_BY_DEFAULT = new Set([THREAD_SECTION.SNOOZED, THREAD_SECTION.DISMISSED]);
// Sections shown even when empty: Recent is where new chats appear.
const ALWAYS_SHOWN = new Set([THREAD_SECTION.RECENT]);

// How much of a message the snippet reads: the gateway sends this many
// characters of the latest message (LAST_MESSAGE_SQL_CHARACTERS), so live
// messages are cut the same way before their snippet is made.
export const SNIPPET_SOURCE_CHARACTERS = 400;
export const SNIPPET_MAX_CHARACTERS = 160;
const ATTACHMENT_SNIPPET = "Attachment";

export function threadSection(thread) {
  if (thread.needsYou) return THREAD_SECTION.NEEDS_YOU;
  if (thread.listState === LIST_STATE.SNOOZED) return THREAD_SECTION.SNOOZED;
  if (thread.listState === LIST_STATE.DISMISSED) return THREAD_SECTION.DISMISSED;
  if (thread.pinned) return THREAD_SECTION.PINNED;
  return THREAD_SECTION.RECENT;
}

// When anything was last posted in the thread, in milliseconds; threads with
// no message yet use their own update time.
export function lastActivityTime(thread) {
  return Number(thread.lastActivityAt || thread.updatedAt || 0);
}

// Sections in display order with their threads, newest activity first.
// Needs you and Pinned appear only when they hold a thread; Snoozed and
// Dismissed only when they hold one, so their headers show a real count.
export function groupThreadSections(threads) {
  const bySection = new Map(SECTION_ORDER.map(id => [id, []]));
  threads.forEach(thread => bySection.get(threadSection(thread)).push(thread));
  return SECTION_ORDER
    .map(id => ({
      id,
      label: SECTION_LABELS[id],
      threads: bySection.get(id).slice().sort((left, right) => lastActivityTime(right) - lastActivityTime(left)),
    }))
    .filter(group => group.threads.length || ALWAYS_SHOWN.has(group.id));
}

// The thread a page opens when nothing is selected: the first row in section
// order among Needs you, Pinned and Recent; a hidden thread only when nothing
// else exists (plan review C02).
export function defaultThread(threads) {
  const groups = groupThreadSections(threads);
  const visible = groups.find(group => !COLLAPSED_BY_DEFAULT.has(group.id) && group.threads.length);
  if (visible) return visible.threads[0];
  const hidden = groups.find(group => group.threads.length);
  return hidden ? hidden.threads[0] : null;
}

// "now", "5m", "3h", "2d", "3w", "1y": the time since a thread's last message.
export function formatThreadAge(timestamp, now) {
  const seconds = Math.max(0, Math.floor((Number(now) - Number(timestamp)) / 1000));
  if (!Number.isFinite(seconds) || !timestamp) return "";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "now";
  if (minutes < 60) return minutes + "m";
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + "h";
  const days = Math.floor(hours / 24);
  if (days < 7) return days + "d";
  if (days < 365) return Math.floor(days / 7) + "w";
  return Math.floor(days / 365) + "y";
}

// The one rule for an ordinary message (gateway: _ordinary_message_sql): not
// a system card or command reply, not a slash command, with text or a file.
export function isOrdinaryMessage(message) {
  const role = String(message.role || "");
  // Trimmed of the same characters the store trims (_MESSAGE_WHITESPACE).
  const content = String(message.content || "").replace(LEADING_MESSAGE_WHITESPACE, "");
  if (role === "system") return false;
  if (role === "user" && content.startsWith("/")) return false;
  return Boolean(content || attachmentsOf(message).length);
}

const LEADING_MESSAGE_WHITESPACE = /^[ \t\n\r]+/;

function attachmentsOf(message) {
  return Array.isArray(message.attachments) ? message.attachments : [];
}

// Markdown reduced to the words a reader sees: no emphasis or code markers,
// heading, list or quote prefixes, or fences; a link shows its text.
export function plainSnippet(text) {
  let plain = String(text || "").slice(0, SNIPPET_SOURCE_CHARACTERS);
  // Fences go first; inline code is set aside so the emphasis rules never
  // change what Markdown shows as written (`__init__.py`).
  const code = [];
  plain = plain
    .replace(/```[^\n`]*\n?/g, " ")
    .replace(/`([^`\n]+)`/g, (_match, inner) => CODE_MARK + (code.push(inner) - 1) + CODE_MARK)
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^[ \t]*#{1,6}[ \t]+/gm, "")
    .replace(/^[ \t]*>[ \t]?/gm, "")
    .replace(/^[ \t]*(?:[-*+]|\d+[.)])[ \t]+/gm, "")
    .replace(/(\*\*|__|~~)(?=\S)([\s\S]*?\S)\1/g, "$2")
    .replace(/(^|[^\w*])\*(?=\S)([^*\n]*?\S)\*(?!\w)/g, "$1$2")
    .replace(/(^|[^\w])_(?=\S)([^_\n]*?\S)_(?!\w)/g, "$1$2")
    .replace(/`+/g, "")
    .replace(CODE_PLACEHOLDER, (_match, index) => code[Number(index)]);
  plain = plain.split(/\s+/).filter(Boolean).join(" ");
  // Cut by characters as people see them, so an emoji is never split.
  const characters = Array.from(plain);
  if (characters.length > SNIPPET_MAX_CHARACTERS) {
    plain = characters.slice(0, SNIPPET_MAX_CHARACTERS - 1).join("").trimEnd() + "…";
  }
  return plain;
}

// Marks an inline code span set aside by plainSnippet (a private-use character).
const CODE_MARK = "\uE000";
const CODE_PLACEHOLDER = /\uE000(\d+)\uE000/g;

// The row's snippet for one message, or null when it is not ordinary.
export function messageSnippet(message) {
  if (!isOrdinaryMessage(message)) return null;
  const text = plainSnippet(message.content);
  if (text) return { role: String(message.role), text };
  const first = attachmentsOf(message)[0] || {};
  return { role: String(message.role), text: String(first.filename || "").trim() || ATTACHMENT_SNIPPET };
}

// The row's second line: what the thread waits on, else its latest message.
export function threadDetail(thread, assistantName) {
  const waiting = thread.needsYou ? NEEDS_YOU_LABELS[thread.needsYou] : "";
  if (waiting) return { kind: "waiting", text: waiting, description: NEEDS_YOU_DESCRIPTIONS[thread.needsYou] };
  const message = thread.lastMessage;
  if (!message || !message.text) return null;
  const who = message.role === "user" ? "You" : String(assistantName || "Assistant");
  return { kind: "message", text: who + ": " + message.text };
}

// The snooze choices, worked out in the person's own time. "This evening" is
// offered only before 5 PM, when 6 PM is still more than an hour away.
export function snoozeChoices(now, formatHint) {
  const at = (days, hour) => {
    const date = new Date(now.getFullYear(), now.getMonth(), now.getDate() + days, hour, 0, 0, 0);
    return date;
  };
  const choices = [{ id: "hour", label: "For 1 hour", until: new Date(now.getTime() + 60 * 60 * 1000) }];
  if (now.getHours() < 17) choices.push({ id: "evening", label: "Until this evening", until: at(0, 18) });
  choices.push({ id: "tomorrow", label: "Until tomorrow", until: at(1, 9) });
  // Next Monday, never today; on a Sunday that is tomorrow, offered once.
  const daysToMonday = ((8 - now.getDay()) % 7) || 7;
  if (daysToMonday > 1) choices.push({ id: "next_week", label: "Until next week", until: at(daysToMonday, 9) });
  return choices.map(choice => ({ ...choice, hint: choice.id === "hour" ? "" : formatHint(choice.until, now) }));
}

// Which sections the person collapsed, kept in this browser per person.
export function createSectionCollapse(storage, key) {
  let saved = {};
  try {
    saved = JSON.parse(storage.getItem(key) || "{}") || {};
  } catch {
    saved = {};
  }
  return {
    isCollapsed(id) {
      return typeof saved[id] === "boolean" ? saved[id] : COLLAPSED_BY_DEFAULT.has(id);
    },
    toggle(id) {
      saved = { ...saved, [id]: !this.isCollapsed(id) };
      try {
        storage.setItem(key, JSON.stringify(saved));
      } catch {
        // A full or blocked storage only forgets the choice at the next load.
      }
      return saved[id];
    },
  };
}

// Draw the sections into `container`: a header button with the count that
// collapses its rows in place, then each row from `renderRow(thread)`.
// `onToggle(id, collapsed)` is optional and only tells the caller.
export function renderThreadSections({ document, container, groups, collapse, renderRow, onToggle }) {
  container.replaceChildren();
  groups.forEach(group => {
    const section = document.createElement("section");
    section.className = "thread-section is-" + group.id.replace(/_/g, "-");
    section.dataset.section = group.id;
    const collapsed = collapse.isCollapsed(group.id);
    const header = document.createElement("button");
    header.type = "button";
    header.className = "thread-section-header";
    header.setAttribute("aria-expanded", collapsed ? "false" : "true");
    const label = document.createElement("span");
    label.className = "thread-section-label";
    label.textContent = group.label;
    const count = document.createElement("span");
    count.className = "thread-section-count";
    count.textContent = String(group.threads.length);
    header.setAttribute("aria-label", group.label + ", " + group.threads.length + (group.threads.length === 1 ? " chat" : " chats"));
    header.append(label, count);
    const rows = document.createElement("div");
    rows.className = "thread-section-rows";
    rows.hidden = collapsed;
    group.threads.forEach(thread => rows.appendChild(renderRow(thread)));
    // Opening or closing a section changes nothing else, so it happens in
    // place: the header keeps keyboard focus (plan review C18).
    header.addEventListener("click", () => {
      const nowCollapsed = collapse.toggle(group.id);
      rows.hidden = nowCollapsed;
      header.setAttribute("aria-expanded", nowCollapsed ? "false" : "true");
      if (onToggle) onToggle(group.id, nowCollapsed);
    });
    section.append(header, rows);
    container.appendChild(section);
  });
}
