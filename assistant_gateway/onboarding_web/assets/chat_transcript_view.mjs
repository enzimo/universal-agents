/**
 * Keeps the Web Chat transcript where the person expects it.
 *
 * - Someone at or near the bottom follows the latest message: their own
 *   message and each reply stay in view.
 * - Someone who scrolled up to read keeps the rows they are reading where they
 *   are, while replies arrive and cards change.
 * - Sending a message shows it at the bottom even after scrolling up (the
 *   caller renders with `scrollToBottom`), and the person follows from there.
 *
 * Both must hold after a render and also when content changes height later.
 * The transcript is rebuilt on every change, and some content finishes its
 * layout after that: a workflow card or access review card fills in when its
 * read returns, an image loads, a status line changes, the composer grows. A
 * ResizeObserver on the list and its rows re-applies the person's position
 * after each such change. Browsers without native scroll anchoring (Safari)
 * rely on it for the reading case too; where the browser has already kept the
 * rows still, the correction is zero.
 *
 * Whether the person follows the latest message is decided by their own
 * scrolling (and a thread opens at the bottom), not by measuring at render
 * time: after a card above grows, a measurement would say they scrolled up
 * when they did not. Nothing here scrolls to a card; only the person's own
 * actions, such as following a link, may do that.
 */

/** Within this distance of the bottom, the person follows the latest message. */
export const FOLLOW_LATEST_DISTANCE_PX = 48;
const ROW_SELECTOR = "[data-message-id]";
const ROW_ID_ATTRIBUTE = "data-message-id";

/**
 * `list` is the scrolling transcript element. `lifecycle.signal` stops the
 * size watcher when the page goes away.
 */
export function createTranscriptView({ list, lifecycle, browser = globalThis }) {
  // `scrollTop` is the last position this view saw or set; a different value
  // means the person scrolled and the scroll listener has not run yet.
  const state = { followsLatest: true, anchor: null, scrollTop: list.scrollTop, threadId: undefined };
  let observer = null;

  function distanceFromBottom() {
    return list.scrollHeight - list.scrollTop - list.clientHeight;
  }

  function rows() {
    return Array.prototype.slice.call(list.querySelectorAll(ROW_SELECTOR));
  }

  /** The first row the person sees, and how far its top sits below the list's top. */
  function readAnchor() {
    const top = list.getBoundingClientRect().top;
    const row = rows().find(item => item.getBoundingClientRect().bottom > top);
    return row ? { id: row.getAttribute(ROW_ID_ATTRIBUTE), offset: row.getBoundingClientRect().top - top } : null;
  }

  function remember() {
    state.followsLatest = distanceFromBottom() <= FOLLOW_LATEST_DISTANCE_PX;
    state.anchor = state.followsLatest ? null : readAnchor();
    state.scrollTop = list.scrollTop;
  }

  function scrollTo(top) {
    list.scrollTop = top;
    state.scrollTop = list.scrollTop;
  }

  function toLatest() {
    scrollTo(list.scrollHeight);
  }

  /** Puts the anchor row back at its offset; false when the row is gone. */
  function toAnchor(anchor) {
    const row = anchor && rows().find(item => item.getAttribute(ROW_ID_ATTRIBUTE) === anchor.id);
    if (!row) return false;
    const drift = row.getBoundingClientRect().top - list.getBoundingClientRect().top - anchor.offset;
    if (Math.abs(drift) >= 0.5) scrollTo(list.scrollTop + drift);
    return true;
  }

  /** Content changed height after a render: keep following, or keep the reader's rows still. */
  function keepPosition() {
    if (state.followsLatest) {
      toLatest();
    } else {
      toAnchor(state.anchor);
    }
  }

  /** Watches the list and every top-level row the last render added. */
  function watchLayout() {
    const Observer = browser.ResizeObserver;
    if (typeof Observer !== "function") return;
    if (!observer) {
      observer = new Observer(keepPosition);
      lifecycle.signal.addEventListener("abort", () => observer.disconnect(), { once: true });
    }
    observer.disconnect();
    observer.observe(list);
    Array.prototype.forEach.call(list.children, row => observer.observe(row));
  }

  return {
    get followsLatest() {
      return state.followsLatest;
    },
    /** From the list's scroll listener: records the person's position; true when they moved up. */
    noteScroll() {
      const movingUp = list.scrollTop < state.scrollTop;
      remember();
      return movingUp;
    },
    /** Before a render: what the person is looking at now. */
    capture() {
      // Scroll listeners run on the next frame; a scroll they have not seen still counts.
      if (list.scrollTop !== state.scrollTop) remember();
      return {
        threadId: state.threadId,
        scrollTop: list.scrollTop,
        scrollHeight: list.scrollHeight,
        followsLatest: state.followsLatest,
        anchor: readAnchor(),
      };
    },
    /**
     * After a render: a newly shown thread, or `scrollToBottom` (the person
     * sent a message), goes to the latest message and follows it; a follower
     * stays there, and a reader keeps their rows still. `prepend` (earlier history added above) keeps the reader's rows
     * still even at the bottom.
     */
    restore(viewport, { threadId, scrollToBottom = false, prepend = false } = {}) {
      if (scrollToBottom || viewport.threadId !== threadId || (viewport.followsLatest && !prepend)) {
        toLatest();
      } else if (!toAnchor(viewport.anchor)) {
        scrollTo(viewport.scrollTop + (prepend ? list.scrollHeight - viewport.scrollHeight : 0));
      }
      state.threadId = threadId;
      remember();
      watchLayout();
    },
  };
}
