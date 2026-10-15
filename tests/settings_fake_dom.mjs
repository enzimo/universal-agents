// A small DOM and browser stand-in for the Settings module tests
// (team_controls, memory_controls, settings_controls). innerHTML throws, so
// any HTML parsing path fails the tests. Selectors support one simple part:
// "tag", ".class" or "#id".
import { readFileSync } from "node:fs";
import vm from "node:vm";

class FakeElement extends EventTarget {
  constructor(document, tagName) {
    super();
    Object.assign(this, { ownerDocument: document, tagName: tagName.toUpperCase(), children: [],
      parentNode: null, attributes: new Map(), className: "", ownText: "", hidden: false, id: "",
      disabled: false, readOnly: false, value: "", type: "", open: false, tabIndex: 0,
      scrollTop: 0, rows: 0, maxLength: 0, placeholder: "", htmlFor: "", focused: 0 });
  }
  get classList() {
    const names = () => this.className.split(/\s+/).filter(Boolean);
    const set = values => { this.className = [...new Set(values)].join(" "); };
    return {
      add: (...added) => set([...names(), ...added]),
      remove: (...removed) => set(names().filter(name => !removed.includes(name))),
      toggle: (name, force) => {
        const on = force === undefined ? !names().includes(name) : Boolean(force);
        set(on ? [...names(), name] : names().filter(value => value !== name));
      },
      contains: name => names().includes(name),
    };
  }
  set innerHTML(_value) { throw new Error("innerHTML must not be used"); }
  get innerHTML() { throw new Error("innerHTML must not be used"); }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this.replaceChildren(); this.ownText = String(value); }
  get nodeType() { return 1; }
  get childNodes() { return this.children; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children.at(-1) || null; }
  get isConnected() {
    for (let node = this; node; node = node.parentNode) if (node === this.ownerDocument.body) return true;
    return false;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  prepend(...nodes) {
    nodes.reverse().forEach(node => {
      node.remove();
      node.parentNode = this;
      this.children.unshift(node);
    });
  }
  appendChild(node) {
    node.remove();
    node.parentNode = this;
    this.children.push(node);
    return node;
  }
  replaceChildren(...nodes) {
    this.children.forEach(child => { child.parentNode = null; });
    this.children = [];
    this.ownText = "";
    this.append(...nodes);
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter(child => child !== this);
    this.parentNode = null;
  }
  replaceWith(node) {
    const parent = this.parentNode;
    if (!parent) return;
    node.remove();
    parent.children.splice(parent.children.indexOf(this), 1, node);
    node.parentNode = parent;
    this.parentNode = null;
  }
  contains(node) {
    for (let current = node; current; current = current.parentNode) if (current === this) return true;
    return false;
  }
  matches(selector) {
    if (selector.startsWith(".")) return this.classList.contains(selector.slice(1));
    if (selector.startsWith("#")) return this.id === selector.slice(1);
    return this.tagName === selector.toUpperCase();
  }
  querySelectorAll(selector) { return descendants(this).filter(node => node.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.focused += 1; this.ownerDocument.activeElement = this; }
  scrollIntoView() {}
  click() { this.dispatchEvent(new Event("click")); }
}

// A text node: enough for code that walks `childNodes` and replaces text
// (knowledge_render.mjs `linkify`).
class FakeText {
  constructor(document, data) {
    Object.assign(this, { ownerDocument: document, nodeType: 3, data: String(data), parentNode: null, children: [] });
  }
  get textContent() { return this.data; }
  get classList() { return { contains: () => false }; }
  matches() { return false; }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter(child => child !== this);
    this.parentNode = null;
  }
  replaceWith(node) { FakeElement.prototype.replaceWith.call(this, node); }
}

export function descendants(node) {
  return node.children.flatMap(child => [child, ...descendants(child)]);
}

export function fakeDocument() {
  const document = new EventTarget();
  document.createElement = tagName => new FakeElement(document, tagName);
  document.createTextNode = data => new FakeText(document, data);
  document.createElementNS = (namespaceURI, tagName) =>
    Object.assign(new FakeElement(document, tagName), { namespaceURI });
  document.body = new FakeElement(document, "body");
  document.activeElement = document.body;
  document.getElementById = id => descendants(document.body).find(node => node.id === id) || null;
  return document;
}

/** Adds `<tag id=…>` elements under the body; returns them by id. */
export function withElements(document, specs) {
  const nodes = {};
  specs.forEach(([id, tagName = "div", parentId]) => {
    const node = document.createElement(tagName);
    node.id = id;
    (parentId ? nodes[parentId] : document.body).append(node);
    nodes[id] = node;
  });
  return nodes;
}

export const byClass = (node, name) => descendants(node).filter(child => child.classList.contains(name));
export const byTag = (node, tagName) => descendants(node).filter(child => child.tagName === tagName.toUpperCase());
export const buttonLabels = node => byTag(node, "button").map(button => button.textContent);
export const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };

export function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export function errorResponse(status, message, recovery = "") {
  return jsonResponse({ error: { code: "test_error", title: "Not done", message, recovery } }, status);
}

/**
 * The browser globals the Settings modules use. Requests wait until the test
 * answers them: `requests` lists `{ path, method, headers, body, respond }`.
 */
export function fakeBrowser(document = fakeDocument()) {
  const requests = [], intervals = new Map(), timeouts = [];
  let timerId = 0;
  const window = {};
  // chat.html loads the real busy-button helper before these modules.
  vm.runInNewContext(
    readFileSync(new URL("../assistant_gateway/onboarding_web/assets/action_feedback.js", import.meta.url), "utf8"),
    { window },
  );
  const browser = {
    document,
    console: { warn() {} },
    UAActionFeedback: window.UAActionFeedback,
    // The pager is tested elsewhere; this stand-in records its settings.
    UAPagination: {
      create(settings) {
        const nav = document.createElement("nav");
        nav.className = "list-pagination";
        nav.settings = settings;
        return nav;
      },
    },
    fetch(path, init = {}) {
      return new Promise(resolve => {
        requests.push({
          path, method: init.method || "GET", headers: init.headers || {},
          body: init.body === undefined ? undefined : JSON.parse(init.body), init,
          respond: response => resolve(response),
        });
      });
    },
    setInterval(callback, delay) { timerId += 1; intervals.set(timerId, { callback, delay }); return timerId; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(callback, delay) { timerId += 1; timeouts.push({ id: timerId, callback, delay }); return timerId; },
    clearTimeout(id) {
      const index = timeouts.findIndex(timer => timer.id === id);
      if (index >= 0) timeouts.splice(index, 1);
    },
    requestAnimationFrame: callback => callback(),
  };
  return { browser, document, requests, intervals, timeouts };
}

/** Answers the oldest unanswered request whose path starts with `prefix`. */
export async function answer(requests, prefix, response) {
  await settle();
  const request = requests.find(entry => !entry.answered && entry.path.startsWith(prefix));
  if (!request) throw new Error(`No open request for ${prefix}; saw ${requests.map(entry => entry.path).join(", ")}`);
  request.answered = true;
  request.respond(response);
  await settle();
  return request;
}

export function openRequests(requests) {
  return requests.filter(entry => !entry.answered);
}
