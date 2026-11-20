// The thread list's hover preview: about 400 ms after a mouse rests on a row,
// a small card shows the thread's title in full, when anything was last
// posted, its last four turns (two lines each, with who wrote them and when)
// and a footer: the thread's agent trail, the agent types that worked in it
// and how many turns it has ("Team lead, Web Research Agent · 12 turns").
// Escape closes it.
//
// It reads the existing history route with a small page and asks for the turn
// count (`turn_count=1`), and it caches each thread's card until the thread's
// last message sequence changes. Like the menu, it is keyed by session key and
// listens once on the list, because the list is drawn again on many events.
// Touch screens never get it: a long press there opens the menu instead.

import { ATTACHMENT_SNIPPET, formatThreadAge, isOrdinaryMessage, plainSnippet } from "./chat_thread_list.mjs";
import { renderTrail, trailAgentNames, trailView } from "./agent_trail.mjs";

export const PREVIEW_DELAY_MS = 400;
export const PREVIEW_TURNS = 4;
// One small history page; a second one only when the first held fewer than
// four turns (cards and commands are not turns).
export const PREVIEW_PAGE_SIZE = 12;
const CARD_GAP_PX = 8;
const VIEWPORT_MARGIN_PX = 8;

// The last `limit` ordinary messages of a history page, oldest first.
export function previewTurns(messages, limit = PREVIEW_TURNS) {
  return messages.filter(isOrdinaryMessage).slice(-limit).map(message => ({
    role: String(message.role),
    text: plainSnippet(message.content) || attachmentName(message),
    createdAt: Date.parse(message.created_at_utc || "") || 0,
  }));
}

function attachmentName(message) {
  const first = Array.isArray(message.attachments) && message.attachments[0];
  return (first && String(first.filename || "")) || ATTACHMENT_SNIPPET;
}

// `canPreview(sessionKey)` says whether a card may show now: never for the
// chat that is open beside it, and not while the thread menu is open.
// `trailFor(thread)` is the row's trail view (chat.js knows whether the row is
// busy), so the footer's triangles match the row's.
export function createThreadPreview({ document, window, lifecycle, requestClient, list, threadFor, assistantName, canPreview = () => true, now = () => Date.now(), trailFor = thread => trailView(thread) }) {
  const cache = new Map(); // sessionKey -> { sequence, turns, turnCount }
  let hovered = "";
  // A row just pressed is not previewed again until the pointer leaves it:
  // the press opens that chat, and the card would cover it (plan review C30).
  let pressed = "";
  let timer = 0;
  let card = null;
  let generation = 0;
  // Moves on at sign-out: an answer for an earlier sign-in is not cached (review F15).
  let signIn = 0;

  function hide() {
    if (timer) lifecycle.clearTimeout(timer);
    timer = 0;
    hovered = "";
    generation += 1;
    if (card) card.remove();
    card = null;
  }

  function rowElement(sessionKey) {
    return [...list.querySelectorAll(".thread-row")].find(row => row.dataset.sessionKey === sessionKey) || null;
  }

  function hover(sessionKey) {
    if (sessionKey === hovered) return;
    hide();
    if (sessionKey === pressed || !canPreview(sessionKey)) return;
    hovered = sessionKey;
    timer = lifecycle.timeout(() => {
      timer = 0;
      show(sessionKey);
    }, PREVIEW_DELAY_MS);
  }

  function show(sessionKey) {
    const thread = threadFor(sessionKey);
    if (!thread || !canPreview(sessionKey)) return hide();
    const cached = cache.get(sessionKey);
    if (cached && cached.sequence === Number(thread.lastSequence || 0)) {
      draw(thread, cached);
      return;
    }
    draw(thread, null);
    const current = ++generation;
    const loadedFor = signIn;
    const sequence = Number(thread.lastSequence || 0);
    load(sessionKey).then(content => {
      if (loadedFor !== signIn) return;
      cache.set(sessionKey, { sequence, ...content });
      // Drop an answer for a row the pointer has left, or an older request.
      if (current === generation && hovered === sessionKey) draw(threadFor(sessionKey) || thread, cache.get(sessionKey));
    }).catch(() => {
      if (current === generation && hovered === sessionKey) draw(thread, { failed: true });
    });
  }

  async function page(sessionKey, before) {
    const query = "?limit=" + PREVIEW_PAGE_SIZE + "&turn_count=1" + (before ? "&before=" + encodeURIComponent(before) : "");
    const response = await requestClient.fetch(
      "/api/web-chat/sessions/" + encodeURIComponent(sessionKey) + "/messages" + query,
      { cache: "no-store", credentials: "same-origin", signal: AbortSignal.timeout(10000) },
    );
    if (!response.ok) throw new Error("Preview not available.");
    return response.json();
  }

  async function load(sessionKey) {
    const first = await page(sessionKey, "");
    let messages = Array.isArray(first.messages) ? first.messages : [];
    if (previewTurns(messages).length < PREVIEW_TURNS && first.has_more && first.before_cursor) {
      const earlier = await page(sessionKey, first.before_cursor);
      messages = (Array.isArray(earlier.messages) ? earlier.messages : []).concat(messages);
    }
    return { turns: previewTurns(messages), turnCount: Number(first.turn_count || 0) };
  }

  function element(tagName, className, text) {
    const node = document.createElement(tagName);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function draw(thread, content) {
    if (card) card.remove();
    card = element("div", "thread-preview");
    card.setAttribute("aria-hidden", "true");
    const header = element("div", "thread-preview-header");
    header.appendChild(element("strong", "thread-preview-title", thread.titleFull || thread.title || ""));
    const at = Number(thread.lastActivityAt || 0);
    if (at) {
      const age = formatThreadAge(at, now());
      // When anything was last posted, as the row's age (review F12).
      header.appendChild(element("span", "thread-preview-age", age === "now" ? "active just now" : "active " + age + " ago"));
    }
    card.appendChild(header);
    if (!content) {
      card.appendChild(element("p", "thread-preview-status", "Loading…"));
    } else if (content.failed) {
      card.appendChild(element("p", "thread-preview-status", "Preview not available."));
    } else if (!content.turns.length) {
      card.appendChild(element("p", "thread-preview-status", "No messages yet."));
    } else {
      content.turns.forEach(turn => {
        const item = element("div", "thread-preview-turn");
        const who = turn.role === "user" ? "You" : String(assistantName() || "Assistant");
        const when = turn.createdAt ? " · " + formatThreadAge(turn.createdAt, now()) : "";
        item.appendChild(element("span", "thread-preview-who" + (turn.role === "user" ? " is-user" : ""), who + when));
        item.appendChild(element("span", "thread-preview-text", turn.text));
        card.appendChild(item);
      });
      card.appendChild(previewFooter(thread, content.turnCount));
    }
    document.body.appendChild(card);
    place();
  }

  // The footer: the thread's trail, who worked in it and how many turns it has.
  function previewFooter(thread, turnCount) {
    const footer = element("div", "thread-preview-footer");
    const view = trailFor(thread);
    if (view.triangles.length) footer.appendChild(renderTrail(document, view));
    const names = trailAgentNames(thread);
    const turns = turnCount + (turnCount === 1 ? " turn" : " turns");
    footer.appendChild(element("p", "thread-preview-count", names ? names + " · " + turns : turns));
    return footer;
  }

  function place() {
    const row = rowElement(hovered);
    if (!card || !row) return hide();
    const anchor = row.getBoundingClientRect();
    const rect = card.getBoundingClientRect();
    let left = anchor.right + CARD_GAP_PX;
    if (left + rect.width > window.innerWidth - VIEWPORT_MARGIN_PX) left = Math.max(VIEWPORT_MARGIN_PX, anchor.left - rect.width - CARD_GAP_PX);
    const top = Math.max(VIEWPORT_MARGIN_PX, Math.min(anchor.top, window.innerHeight - rect.height - VIEWPORT_MARGIN_PX));
    card.style.left = left + "px";
    card.style.top = top + "px";
  }

  const rowKey = event => {
    const row = event.target && typeof event.target.closest === "function" ? event.target.closest(".thread-row") : null;
    return row ? String(row.dataset.sessionKey || "") : "";
  };

  // A mouse over a row (found again by session key after each redraw); any
  // other pointer, a press, or leaving the list ends the preview.
  lifecycle.listen(list, "pointerover", event => {
    if (event.pointerType !== "mouse") return;
    const sessionKey = rowKey(event);
    if (sessionKey !== pressed) pressed = "";
    if (sessionKey) hover(sessionKey);
    else hide();
  });
  lifecycle.listen(list, "pointerleave", () => {
    pressed = "";
    hide();
  });
  lifecycle.listen(list, "pointerdown", event => {
    pressed = rowKey(event);
    hide();
  });
  lifecycle.listen(list, "scroll", hide);
  lifecycle.listen(list, "contextmenu", hide);
  lifecycle.listen(window, "blur", hide);
  // Escape closes the card (or one about to show) wherever focus is, and it
  // stays closed until the pointer leaves that row (review F7, WCAG 1.4.13).
  lifecycle.listen(document, "keydown", event => {
    if (event.key !== "Escape" || !(card || timer)) return;
    pressed = hovered;
    hide();
  });

  return {
    hide,
    // Sign-out: no card stays on the sign-in page and no message text is
    // kept for whoever signs in next (plan review C29).
    clear() {
      hide();
      cache.clear();
      pressed = "";
      signIn += 1;
    },
    // After the list is drawn again: keep the card beside the same thread's
    // row, or close it when that row is gone (plan review C03).
    refresh() {
      if (card) place();
    },
    isShowing: () => Boolean(card),
  };
}
