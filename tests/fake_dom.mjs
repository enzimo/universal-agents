/**
 * A small DOM stand-in for browser module tests (not a test file itself).
 * innerHTML throws, so any HTML parsing path fails the tests that use it.
 */

export class FakeElement extends EventTarget {
  constructor(document, tagName) {
    super();
    Object.assign(this, { ownerDocument: document, tagName: tagName.toUpperCase(), children: [],
      parentNode: null, attributes: new Map(), className: "", ownText: "", hidden: false,
      disabled: false, readOnly: false, value: "", tabIndex: 0, open: false, dataset: {} });
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
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children.at(-1) || null; }
  get isConnected() {
    for (let node = this; node; node = node.parentNode) if (node === this.ownerDocument.body) return true;
    return false;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  removeAttribute(name) { this.attributes.delete(name); }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
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
  focus() { this.ownerDocument.activeElement = this; }
  click() { this.dispatchEvent(new Event("click")); }
}

export function fakeDocument() {
  const document = {};
  document.createElement = tagName => new FakeElement(document, tagName);
  document.body = new FakeElement(document, "body");
  document.activeElement = document.body;
  return document;
}

export function descendants(node) {
  return node.children.flatMap(child => [child, ...descendants(child)]);
}

export const byTag = (node, tagName) => descendants(node).filter(child => child.tagName === tagName.toUpperCase());
export const byClass = (node, name) => descendants(node).filter(child => child.classList.contains(name));
export const buttonLabels = node => byTag(node, "button").map(button => button.textContent);
export const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
