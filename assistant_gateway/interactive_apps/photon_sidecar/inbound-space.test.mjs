import assert from "node:assert/strict";
import test from "node:test";

import { normalizeSpace } from "./inbound-space.mjs";

test("a space keeps the type the provider reported", () => {
  assert.deepEqual(normalizeSpace({ id: "any;-;+1555", type: "dm", phone: "+1555" }, {}), {
    id: "any;-;+1555",
    type: "dm",
    phone: "+1555",
  });
  assert.equal(normalizeSpace({ id: "chat-1" }, { type: "group" }).type, "group");
});

test("an unknown space type stays null, never a guessed direct chat", () => {
  assert.deepEqual(normalizeSpace({ id: "chat-1" }, undefined), { id: "chat-1", type: null, phone: null });
  assert.equal(normalizeSpace({}, {}).type, null);
});
