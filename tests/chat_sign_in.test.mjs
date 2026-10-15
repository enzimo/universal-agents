// Web Chat's sign-in greeting (chat_sign_in.mjs): the time-of-day text, the
// name this browser remembers, and "Not Alice?".
import assert from "node:assert/strict";
import test from "node:test";
import {
  BACKGROUND_HOLD_ATTRIBUTE, GREETING_NAME_KEY, createSignInGreeting, firstName, greetingLines, timeOfDayGreeting,
} from "../assistant_gateway/onboarding_web/assets/chat_sign_in.mjs";
import { fakeDocument, withElements } from "./settings_fake_dom.mjs";

test("the greeting follows the browser's clock", () => {
  const expected = {
    0: "Good Evening", 4: "Good Evening", 5: "Good Morning", 11: "Good Morning",
    12: "Good Afternoon", 16: "Good Afternoon", 17: "Good Evening", 23: "Good Evening",
  };
  Object.entries(expected).forEach(([hour, greeting]) => {
    assert.equal(timeOfDayGreeting(Number(hour)), greeting, `${hour}:00`);
  });
  const morning = new Date(2026, 9, 8, 9, 30);
  assert.deepEqual(greetingLines(morning, ""), ["Good Morning!"]);
  assert.deepEqual(greetingLines(morning, "Alice"), ["Good Morning,", "Alice!"], "the name can wrap to its own line");
});

test("only the first word of a display name is used", () => {
  assert.equal(firstName("Alice Smith"), "Alice");
  assert.equal(firstName("  Bob \t Jones "), "Bob");
  assert.equal(firstName(""), "");
  assert.equal(firstName(null), "");
});

function signInPage({ storage = new Map(), motion = true, brokenStorage = false } = {}) {
  const document = fakeDocument();
  const nodes = withElements(document, [
    ["stage"], ["heading", "h1", "stage"], ["card", "div", "stage"], ["field", "input", "card"], ["forget", "button", "card"],
  ]);
  Object.assign(nodes.stage, { clientWidth: 1000 });
  const greetings = [];
  const warnings = [];
  // chat.html starts with the background held hidden.
  const root = document.createElement("html");
  root.setAttribute(BACKGROUND_HOLD_ATTRIBUTE, "");
  const localStorage = {
    getItem(key) { if (brokenStorage) throw new Error("blocked"); return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { if (brokenStorage) throw new Error("blocked"); storage.set(key, String(value)); },
    removeItem(key) { if (brokenStorage) throw new Error("blocked"); storage.delete(key); },
  };
  const browser = {
    document: { documentElement: root },
    localStorage,
    console: { warn: (...args) => warnings.push(args) },
    UAMotion: motion ? {
      playGreeting(options) {
        const greeting = {
          options, stopped: false, stop() { greeting.stopped = true; },
          heldWhenStarted: root.hasAttribute(BACKGROUND_HOLD_ATTRIBUTE),
        };
        greetings.push(greeting);
        return greeting;
      },
    } : undefined,
  };
  const greeting = createSignInGreeting({
    browser, stage: nodes.stage, heading: nodes.heading, card: nodes.card,
    focusTarget: nodes.field, forgetButton: nodes.forget, now: () => new Date(2026, 9, 8, 14, 0),
  });
  return { greeting, greetings, warnings, storage, root, ...nodes };
}

test("someone this browser has seen sign in is greeted by name until they say it is not them", () => {
  const page = signInPage();
  page.greeting.show();
  assert.deepEqual(page.greetings[0].options.lines, ["Good Afternoon!"], "nobody remembered yet");
  assert.equal(page.greetings[0].options.shellShown, false, "the window waits for the greeting");
  assert.equal(page.forget.hidden, true);
  assert.equal(page.greetings[0].options.maxWidth(), 1000, "the page's whole width");

  page.greeting.show();
  assert.equal(page.greetings.length, 1, "a greeting already showing goes on rather than starting over");

  page.greeting.remember({ canonical_user_id: "user-1", display_name: "Alice Smith" });
  assert.equal(page.storage.get(GREETING_NAME_KEY), "Alice", "only the first name is kept");
  page.greeting.stop();
  assert.equal(page.greetings[0].stopped, true, "signing in stops the greeting");

  // Signed out: the greeting plays again, with the name.
  page.greeting.show();
  assert.deepEqual(page.greetings[1].options.lines, ["Good Afternoon,", "Alice!"]);
  assert.equal(page.forget.textContent, "Not Alice?");
  assert.equal(page.forget.hidden, false);

  page.greeting.forget();
  assert.equal(page.storage.has(GREETING_NAME_KEY), false);
  assert.equal(page.greetings[1].stopped, true);
  assert.deepEqual(page.greetings[2].options.lines, ["Good Afternoon!"]);
  assert.equal(page.greetings[2].options.shellShown, true, "the open window stays open");
  assert.equal(page.forget.hidden, true);
  assert.ok(page.field.focused > 0, "focus stays in the token field");
});

test("a session without a named person forgets the last name", () => {
  const page = signInPage({ storage: new Map([[GREETING_NAME_KEY, "Alice"]]) });
  page.greeting.remember({ canonical_user_id: "user-2", display_name: "Bob" });
  assert.equal(page.storage.get(GREETING_NAME_KEY), "Bob", "the next person replaces the name");
  page.greeting.remember({ canonical_user_id: "", display_name: "Setup" });
  assert.equal(page.storage.has(GREETING_NAME_KEY), false);
  page.greeting.remember({ canonical_user_id: "user-3", display_name: "" });
  assert.equal(page.storage.has(GREETING_NAME_KEY), false);
});

test("blocked storage greets without a name and says why", () => {
  const page = signInPage({ brokenStorage: true });
  page.greeting.remember({ canonical_user_id: "user-1", display_name: "Alice" });
  page.greeting.show();
  assert.deepEqual(page.greetings[0].options.lines, ["Good Afternoon!"]);
  assert.equal(page.warnings.length, 2);
  assert.ok(page.warnings.every(([message]) => message.startsWith("ua.web_chat: could not")));
});

test("without the motion script the greeting is text and the window shows at once", () => {
  const page = signInPage({ motion: false, storage: new Map([[GREETING_NAME_KEY, "Alice"]]) });
  page.card.classList.add("is-intro-hidden");
  page.card.setAttribute("inert", "");
  page.greeting.show();
  assert.equal(page.heading.textContent, "Good Afternoon, Alice!");
  assert.ok(page.stage.classList.contains("show-title"));
  assert.equal(page.card.classList.contains("is-intro-hidden"), false);
  assert.equal(page.card.hasAttribute("inert"), false);
  assert.equal(page.field.focused, 1);
});

test("the background stays hidden until the greeting fills it in or someone is signed in", () => {
  const page = signInPage();
  page.greeting.show();
  assert.equal(page.greetings[0].heldWhenStarted, true, "the fill starts before the background shows");
  assert.equal(page.root.hasAttribute(BACKGROUND_HOLD_ATTRIBUTE), false);

  const signedIn = signInPage();
  signedIn.greeting.stop();
  assert.equal(signedIn.root.hasAttribute(BACKGROUND_HOLD_ATTRIBUTE), false, "a resumed session shows it at once");

  const plain = signInPage({ motion: false });
  plain.greeting.show();
  assert.equal(plain.root.hasAttribute(BACKGROUND_HOLD_ATTRIBUTE), false, "without the motion script it shows at once");
});
