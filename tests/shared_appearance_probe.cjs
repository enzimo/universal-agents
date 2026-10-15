const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8");
let writes = 0;
const events = [];
const document = {
  documentElement: {setAttribute() {writes++;}, style: {setProperty() {writes++;}}},
  dispatchEvent(event) { events.push(event); },
};
const window = {
  location: {pathname: "/docs/"},
  getComputedStyle: () => ({getPropertyValue: () => ""}),
};
const sandbox = {window, document, CustomEvent: function (type, options) {return {type, ...options};}};
vm.runInNewContext(source, sandbox);
const appearance = window.UAAppearance;
const initialWrites = writes;
const baseline = appearance.current();
assert.equal(events.length, 1);
appearance.setBaseline(baseline);
appearance.setBaseline(baseline);
assert.equal(events.length, 1, "unchanged baseline must not trigger a renderer restart");
assert.equal(writes, initialWrites);

appearance.setUserPreference(baseline);
assert.equal(events.length, 2, "reset controls still need to learn that a preference now exists");
assert.equal(events.at(-1).detail.hasUserPreference, true);
assert.equal(writes, initialWrites, "preference metadata alone must not rewrite theme CSS");
appearance.setUserPreference(baseline);
assert.equal(events.length, 2);

const changed = appearance.current();
changed.mode = "light";
appearance.setUserPreference(changed);
assert.equal(appearance.current().mode, "light");
assert.equal(events.length, 3);
assert.ok(writes > initialWrites);
appearance.clearUserPreference();
assert.equal(appearance.current().mode, "dark");
assert.equal(events.length, 4);
assert.equal(events.at(-1).detail.hasUserPreference, false);
const finalWrites = writes;
vm.runInNewContext(source, sandbox);
assert.equal(window.UAAppearance, appearance, "loading the asset twice must preserve its owner and state");
assert.equal(events.length, 4);
assert.equal(writes, finalWrites);
