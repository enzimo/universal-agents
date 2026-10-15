"use strict";
// Prints the Share settings text that appearance.js produces for its defaults
// with one tuned dark value, so Python tests can load it like a config file.
const fs = require("node:fs");
const vm = require("node:vm");

const window = { location: { pathname: "/" }, getComputedStyle: () => ({ getPropertyValue: () => "" }) };
const document = { documentElement: { setAttribute() {}, style: { setProperty() {} } }, dispatchEvent() {} };
window.document = document;
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), {
  window, document, CustomEvent: class {}, JSON, Math, Number, String, Array, Object,
});
const appearance = window.UAAppearance.normalize({ mode: "dark", dark: { lightX: -40, accents: { bronze: { share: 39 } } } });
process.stdout.write(window.UAAppearance.settingsText(appearance));
