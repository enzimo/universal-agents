const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {script, terminal, provider} = JSON.parse(fs.readFileSync(0, "utf8"));

// Count writes even when a setter receives the same value. Replacing unchanged
// status markup was the source of visible flicker on each three-second poll.
function element(attributes = {}) {
  let writes = 0;
  let text = "";
  let disabled = true;
  return {
    attributes, listeners: {},
    get writes() { return writes; },
    get textContent() { return text; },
    set textContent(value) { writes++; text = value; },
    get disabled() { return disabled; },
    set disabled(value) { writes++; disabled = value; },
    getAttribute(name) { return attributes[name] ?? null; },
    setAttribute(name, value) { writes++; attributes[name] = value; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
  };
}
const message = "Waiting for Google authorization.";
const restartCommand = `/link cli:${provider}`;
const status = element({"data-markdown": message, "data-auth-provider": provider, "data-restart-command": restartCommand});
const button = element();
const redirect = element();
// The plain link under Open (secure_tool_broker.sign_in_pages.auth_link_html).
const link = element();
const linkRow = element();
linkRow.hidden = true;
const selectors = {"[data-broker-auth-open]": button, "[data-broker-auth-link]": link, "[data-broker-auth-link-row]": linkRow};
const elements = {"broker-auth-status": status, "google-auth-link": button, "google-redirect-uri": redirect};
const code = provider === "github" ? element() : null;
const copy = provider === "github" ? element() : null;
elements["auth-code"] = code;
elements["auth-code-copy"] = copy;
const timers = new Map();
const opens = [];
const navigations = [];
let nextTimer = 0;
let resolveFetch;
let requests = 0;
const browser = {
  location: {pathname: `/auth/${provider}/test`, href: `https://broker.test/auth/${provider}/test`, replace: url => navigations.push(url)},
  open: (...args) => opens.push(args),
  setTimeout(callback, delay) { assert.equal(delay, 3000); timers.set(++nextTimer, callback); return nextTimer; },
  clearTimeout(id) { timers.delete(id); },
  UABrokerRenderMarkdown(node, text) { node.textContent = text; },
};
const sandbox = {
  window: browser, document: {getElementById: id => elements[id], querySelector: selector => selectors[selector] ?? null}, URL,
  fetch(url, options) {
    assert.equal(url, `/auth/${provider}/test/status`);
    assert.equal(options.cache, "no-store");
    requests++;
    return new Promise(resolve => { resolveFetch = resolve; });
  },
};
const tick = () => new Promise(resolve => setImmediate(resolve));
async function respond(payload, code = 200) {
  resolveFetch({ok: code === 200, status: code, json: async () => payload});
  await tick();
}
function poll() {
  assert.equal(timers.size, 1);
  const [id, callback] = [...timers][0];
  timers.delete(id);
  callback();
}

(async () => {
  vm.runInNewContext(script, sandbox);
  assert.equal(requests, 1);
  assert.equal(timers.size, 0, "do not schedule another request while one is in flight");
  button.listeners.click();
  assert.equal(opens.length, 0, "a pending button cannot open a page");
  await respond({status: "pending", message});
  assert.equal(status.writes, 0);
  assert.equal(button.writes, 0);

  const url = provider === "google"
    ? "https://accounts.google.com/o/oauth2/auth?client_id=test&redirect_uri=http%3A%2F%2Flocalhost%3A1234&state=test"
    : "https://github.com/login/device";
  poll();
  await respond({status: "pending", message, external_url: url, user_code: "ABCD-EFGH"});
  assert.equal(button.textContent, "Open");
  assert.equal(button.disabled, false);
  assert.equal(link.href, url, "the plain link opens the same address");
  assert.equal(link.textContent, url.length <= 100 ? url : "the sign-in link");
  assert.equal(linkRow.hidden, false);
  if (provider === "google") assert.equal(redirect.textContent, "http://localhost:1234");
  if (code) {
    assert.equal(code.textContent, "ABCD-EFGH");
    assert.equal(copy.disabled, false);
  }
  button.listeners.click();
  assert.deepEqual(opens, [[url, "_blank", "noopener,noreferrer"]]);
  const observedWrites = () => [status.writes, button.writes, redirect.writes, code?.writes, copy?.writes];
  const writes = observedWrites();
  for (let i = 0; i < 3; i++) {
    poll();
    await respond({status: "pending", message, external_url: url, user_code: "ABCD-EFGH"});
  }
  assert.deepEqual(observedWrites(), writes, "identical polls must not rewrite DOM nodes");
  assert.equal(navigations.length, 0);

  poll();
  await respond({status: "pending", message: "Finishing Google authorization.", external_url: url});
  assert.equal(status.textContent, "Finishing Google authorization.");
  assert.equal(elements["google-auth-link"], button);
  assert.equal(button.writes, writes[1]);
  poll();
  await respond({}, 503);
  assert.equal(timers.size, 1, "retry transient server failures");
  poll();
  await respond({status: terminal, message: "Complete"}, terminal === "gone" ? 404 : 200);
  assert.equal(timers.size, 0, "terminal sessions must stop polling");
  if (terminal === "gone") {
    assert.equal(button.disabled, true);
    assert.ok(status.textContent.includes(`Start again with ${restartCommand}.`));
    assert.equal(navigations.length, 0);
  } else {
    assert.deepEqual(navigations, [browser.location.href], "perform a single GET navigation, never reload a POST");
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
