import assert from "node:assert/strict";
import test from "node:test";
import { createAttentionCount } from "../assistant_gateway/onboarding_web/assets/settings_support.mjs";
import { fakeBrowser, settle } from "./settings_fake_dom.mjs";

function fixture() {
  const env = fakeBrowser();
  const reads = [], warnings = [];
  env.browser.console.warn = (...args) => warnings.push(args);
  const count = createAttentionCount({
    read: () => new Promise((resolve, reject) => reads.push({ resolve, reject })),
    browser: env.browser,
  });
  return { ...env, count, reads, warnings };
}

test("a newer list total supersedes an older poll, including an unchanged total", async t => {
  for (const total of [0, 2]) {
    await t.test(`list total ${total}`, async () => {
      const f = fixture();
      f.count.start();
      await settle();
      const poll = f.count.refresh();
      f.count.set(total);
      f.reads[0].resolve(1);
      await poll;
      assert.equal(f.count.current(), total);
      f.count.stop();
    });
  }
});

test("a new sign-in reads immediately and ignores the earlier session's reply or failure", async t => {
  for (const fails of [false, true]) {
    await t.test(fails ? "old failure" : "old reply", async () => {
      const f = fixture();
      f.count.start();
      await settle();
      const old = f.count.refresh();
      f.count.stop();
      f.count.start();
      await settle();
      assert.equal(f.reads.length, 2, "The new session must not wait for the old request");
      const current = f.count.refresh();
      if (fails) f.reads[0].reject(new Error("The old request failed"));
      else f.reads[0].resolve(9);
      await old;
      assert.equal(f.count.current(), 0);
      assert.equal(f.warnings.length, 0, "Old failures do not affect the new session's warning state");
      assert.equal(f.count.refresh(), current, "An old reply cannot clear the new request");
      f.reads[1].resolve(2);
      await current;
      assert.equal(f.count.current(), 2);
      f.count.stop();
    });
  }
});

test("a refresh after a change replaces a pending poll and still coalesces ordinary reads", async () => {
  const f = fixture();
  f.count.start();
  await settle();
  const old = f.count.refresh();
  const current = f.count.refresh({ force: true });
  await settle();
  assert.equal(f.reads.length, 2);
  f.reads[0].resolve(5);
  await old;
  assert.equal(f.count.current(), 0);
  assert.equal(f.count.refresh(), current);
  f.reads[1].resolve(1);
  await current;
  assert.equal(f.count.current(), 1);
  f.count.stop();
});

test("a read queued before sign-out never starts in the next session", async () => {
  const f = fixture();
  f.count.start();
  f.count.stop();
  f.count.start();
  await settle();
  assert.equal(f.reads.length, 1);
  const current = f.count.refresh();
  f.reads[0].resolve(2);
  await current;
  assert.equal(f.count.current(), 2);
  f.count.stop();
});
