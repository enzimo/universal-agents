import assert from "node:assert/strict";
import test from "node:test";

import {
  FOLLOW_LATEST_DISTANCE_PX, createTranscriptView,
} from "../assistant_gateway/onboarding_web/assets/chat_transcript_view.mjs";

/**
 * A transcript list with real stacking geometry: rows sit one after another,
 * `scrollTop` is clamped like a browser's, and nothing adjusts the position on
 * its own (no native scroll anchoring, as in Safari). The size watcher runs
 * only when a test says content changed size.
 */
class FakeRow {
  constructor(list, id, height) { Object.assign(this, { list, id, height }); }
  getAttribute(name) { return name === "data-message-id" ? this.id : null; }
  getBoundingClientRect() {
    const top = this.list.rows.slice(0, this.list.rows.indexOf(this)).reduce((sum, row) => sum + row.height, 0)
      - this.list.scrollTop;
    return { top, bottom: top + this.height };
  }
}

class FakeList {
  constructor(heights, clientHeight = 500) {
    this.clientHeight = clientHeight;
    this.top = 0;
    this.rows = [];
    heights.forEach((height, index) => this.add(`m${index}`, height));
  }
  add(id, height) { const row = new FakeRow(this, id, height); this.rows.push(row); return row; }
  prepend(id, height) { const row = new FakeRow(this, id, height); this.rows.unshift(row); return row; }
  row(id) { return this.rows.find(row => row.id === id); }
  get children() { return this.rows; }
  get scrollHeight() { return this.rows.reduce((sum, row) => sum + row.height, 0); }
  get scrollTop() { return this.top; }
  set scrollTop(value) { this.top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); }
  get distanceFromBottom() { return this.scrollHeight - this.scrollTop - this.clientHeight; }
  getBoundingClientRect() { return { top: 0, bottom: this.clientHeight }; }
  querySelectorAll(selector) {
    assert.equal(selector, "[data-message-id]");
    return this.rows;
  }
  offsetOf(id) { return this.row(id).getBoundingClientRect().top; }
}

function harness(heights, { clientHeight, withObserver = true } = {}) {
  const list = new FakeList(heights, clientHeight);
  const observers = [];
  class FakeResizeObserver {
    constructor(callback) { Object.assign(this, { callback, watched: new Set() }); observers.push(this); }
    observe(target) { this.watched.add(target); }
    disconnect() { this.watched.clear(); }
  }
  const controller = new AbortController();
  const browser = withObserver ? { ResizeObserver: FakeResizeObserver } : {};
  const view = createTranscriptView({ list, lifecycle: { signal: controller.signal }, browser });
  // One transcript render: capture, change the rows, restore.
  const render = (change = () => {}, options = {}) => {
    const viewport = view.capture();
    change();
    view.restore(viewport, { threadId: "thread_1", ...options });
  };
  // Content changed size after the render; the browser reports it.
  const sizeChanged = () => observers.forEach(observer => { if (observer.watched.size) observer.callback([]); });
  // The person scrolls; the browser's scroll listener runs.
  const scrollBy = delta => { list.scrollTop += delta; view.noteScroll(); };
  return { list, view, render, sizeChanged, scrollBy, observers, controller };
}

// Rows: m0..m9 of 100px, and a large card at m6.
const TRANSCRIPT = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100];

test("a thread opens at the latest message", () => {
  const { list, render } = harness(TRANSCRIPT);
  render();
  assert.equal(list.distanceFromBottom, 0);
});

test("a card that grows after the render keeps a follower at the latest message", () => {
  const { list, render, sizeChanged } = harness(TRANSCRIPT);
  render();
  list.row("m6").height = 1400; // the workflow card filled in
  assert.ok(list.distanceFromBottom > 1000, "the card pushed the latest message out of view");
  sizeChanged();
  assert.equal(list.distanceFromBottom, 0);
});

test("the person's own message and a reply keep a follower at the latest message", () => {
  const { list, render, sizeChanged } = harness(TRANSCRIPT);
  render();
  render(() => list.add("sent", 120));
  assert.equal(list.distanceFromBottom, 0);
  render(() => list.add("reply", 300));
  list.row("m6").height = 1400;
  sizeChanged();
  assert.equal(list.distanceFromBottom, 0);
});

test("growth measured at the next render does not count as scrolling up", () => {
  // The render runs before the size watcher reports the card's growth.
  const { list, render } = harness(TRANSCRIPT);
  render();
  list.row("m6").height = 1400;
  render(() => list.add("reply", 100));
  assert.equal(list.distanceFromBottom, 0);
});

test("a reader keeps their rows still when a card above them grows", () => {
  const { list, view, render, sizeChanged, scrollBy } = harness(TRANSCRIPT);
  render();
  scrollBy(-250);
  assert.equal(view.followsLatest, false);
  const before = list.offsetOf("m8");
  list.row("m1").height = 1400; // a card above the view filled in
  sizeChanged();
  assert.equal(list.offsetOf("m8"), before);
  assert.ok(list.distanceFromBottom > FOLLOW_LATEST_DISTANCE_PX);
});

test("a reply, a status change, and the card's rebuild do not pull a reader down", () => {
  const { list, render, sizeChanged, scrollBy } = harness(TRANSCRIPT);
  render();
  scrollBy(-250);
  const scrollTop = list.scrollTop;
  render(() => list.add("reply", 300));
  assert.equal(list.scrollTop, scrollTop);
  list.row("reply").height = 260; // a status line got shorter below the reader
  sizeChanged();
  assert.equal(list.scrollTop, scrollTop);
});

test("sending a message while reading goes to the latest message, and replies then follow", () => {
  const { list, view, render, sizeChanged, scrollBy } = harness(TRANSCRIPT);
  render();
  scrollBy(-400);
  render(() => list.add("sent", 120), { scrollToBottom: true });
  assert.equal(list.distanceFromBottom, 0);
  assert.equal(view.followsLatest, true);
  render(() => list.add("reply", 300));
  list.row("m6").height = 1400;
  sizeChanged();
  assert.equal(list.distanceFromBottom, 0);
});

test("a scroll the listener has not seen yet still counts at the next render", () => {
  const { list, render } = harness(TRANSCRIPT);
  render();
  list.scrollTop -= 300; // the scroll listener runs on the next frame
  const scrollTop = list.scrollTop;
  render(() => list.add("reply", 100));
  assert.equal(list.scrollTop, scrollTop);
});

test("a small scroll near the bottom keeps following", () => {
  const { view, list, render, sizeChanged, scrollBy } = harness(TRANSCRIPT);
  render();
  scrollBy(-FOLLOW_LATEST_DISTANCE_PX);
  assert.equal(view.followsLatest, true);
  list.row("m6").height = 900;
  sizeChanged();
  assert.equal(list.distanceFromBottom, 0);
});

test("returning to the bottom resumes following", () => {
  const { list, view, render, scrollBy } = harness(TRANSCRIPT);
  render();
  scrollBy(-400);
  scrollBy(400);
  assert.equal(view.followsLatest, true);
  render(() => list.add("reply", 200));
  assert.equal(list.distanceFromBottom, 0);
});

test("earlier history added above keeps the reader's rows still, even at the bottom", () => {
  const { list, render } = harness([100, 100, 100], { clientHeight: 250 });
  render();
  const before = list.offsetOf("m1");
  render(() => { list.prepend("e1", 100); list.prepend("e0", 100); }, { prepend: true });
  assert.equal(list.offsetOf("m1"), before);
});

test("switching to another thread opens it at the latest message", () => {
  const { list, render, scrollBy } = harness(TRANSCRIPT);
  render();
  scrollBy(-400);
  render(() => {}, { threadId: "thread_2" });
  assert.equal(list.distanceFromBottom, 0);
});

test("the size watcher follows the latest render's rows and stops with the page", () => {
  const { list, render, observers, controller } = harness(TRANSCRIPT);
  render();
  assert.equal(observers.length, 1);
  assert.ok(observers[0].watched.has(list) && observers[0].watched.has(list.row("m6")));
  render(() => list.add("reply", 100));
  assert.equal(observers.length, 1, "one watcher for the page");
  assert.ok(observers[0].watched.has(list.row("reply")));
  controller.abort();
  assert.equal(observers[0].watched.size, 0);
});

test("without a size watcher, renders still place the view", () => {
  const { list, render } = harness(TRANSCRIPT, { withObserver: false });
  render();
  render(() => list.add("reply", 100));
  assert.equal(list.distanceFromBottom, 0);
});
