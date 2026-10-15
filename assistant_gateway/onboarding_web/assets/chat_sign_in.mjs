// Web Chat's sign-in greeting (owner's request, 2026-10-08).
//
// Every time the sign-in page shows (the page opens with nobody signed in,
// after signing out, after a session ends), the background fills in and a
// time-of-day greeting forms from polygons as the page heading, like the
// onboarding intro's title (UAMotion.playGreeting, intro_word.js); the small
// sign-in window fades in under it.
//
// The greeting names the person once this browser has seen them sign in:
// "Good Morning, Alice!". The server cannot know who is at the page before
// sign-in, and a lasting device cookie that tells it would show anyone at
// the page whose device it is, so only this browser remembers, and only the
// first word of the person's display name. Signing out keeps it; the next
// person to sign in replaces it; "Not Alice?" forgets it.

export const GREETING_NAME_KEY = "ua.webchat.greeting_name";

// The page's background waits hidden (chat.html sets this on <html>) until
// the page knows whether it greets: Web Chat first asks the server whether
// someone is still signed in, and during that wait the background would draw
// in full, then restart from black when the greeting fills it in. Setup
// starts its fill before the background's first frame, so it needs no hold.
// The greeting releases it once its fill has started (the fill's first frame
// is drawn at once, so the full background never shows); signing in
// releases it with the background shown at once.
export const BACKGROUND_HOLD_ATTRIBUTE = "data-ua-background-hold";

export const MORNING_FROM_HOUR = 5;
export const AFTERNOON_FROM_HOUR = 12;
export const EVENING_FROM_HOUR = 17;

/** "Good Morning" from 5:00, "Good Afternoon" from 12:00, "Good Evening" from 17:00 to 4:59. */
export function timeOfDayGreeting(hour) {
  if (hour >= MORNING_FROM_HOUR && hour < AFTERNOON_FROM_HOUR) return "Good Morning";
  if (hour >= AFTERNOON_FROM_HOUR && hour < EVENING_FROM_HOUR) return "Good Afternoon";
  return "Good Evening";
}

/**
 * The greeting as the polygon title's parts: they share one line when it
 * fits and otherwise the name goes on its own line.
 */
export function greetingLines(date, name) {
  var greeting = timeOfDayGreeting(date.getHours());
  return name ? [greeting + ",", name + "!"] : [greeting + "!"];
}

/** The first word of a display name ("Alice Smith" → "Alice"); "" without one. */
export function firstName(displayName) {
  return String(displayName || "").trim().split(/\s+/)[0] || "";
}

/** The sign-in page's greeting, sign-in window and "Not Alice?" link. */
export function createSignInGreeting({ browser, stage, heading, card, focusTarget, forgetButton, now = () => new Date() }) {
  var greeting = null;

  function storedName() {
    try {
      return String(browser.localStorage.getItem(GREETING_NAME_KEY) || "").trim();
    } catch (error) {
      browser.console.warn("ua.web_chat: could not read the remembered name for the greeting", error);
      return "";
    }
  }

  function storeName(name) {
    try {
      if (name) {
        browser.localStorage.setItem(GREETING_NAME_KEY, name);
      } else {
        browser.localStorage.removeItem(GREETING_NAME_KEY);
      }
    } catch (error) {
      browser.console.warn("ua.web_chat: could not update the remembered name for the greeting", error);
    }
  }

  // The width the greeting may take before the name moves to a second line:
  // the sign-in page's whole width.
  function room() {
    return stage.clientWidth;
  }

  function releaseBackground() {
    browser.document.documentElement.removeAttribute(BACKGROUND_HOLD_ATTRIBUTE);
  }

  function endGreeting() {
    if (greeting) greeting.stop();
    greeting = null;
  }

  // `shellShown`: the sign-in window is already open and stays so.
  function play(shellShown) {
    endGreeting();
    var name = storedName();
    forgetButton.textContent = name ? "Not " + name + "?" : "";
    forgetButton.hidden = !name;
    var lines = greetingLines(now(), name);
    if (!browser.UAMotion) {
      // Without the motion script the greeting is plain text and the window shows at once.
      heading.textContent = lines.join(" ");
      stage.classList.add("show-title");
      card.removeAttribute("inert");
      card.classList.remove("is-intro-hidden");
      focusTarget.focus();
      releaseBackground();
      return;
    }
    // playGreeting starts the background fill before it returns.
    greeting = browser.UAMotion.playGreeting({
      stage, heading, shell: card, focusTarget, lines, maxWidth: room, shellShown,
    });
    releaseBackground();
  }

  return {
    /**
     * Plays the greeting with the sign-in window hidden until it has formed.
     * Already showing (several requests found the session ended at once): it
     * goes on rather than starting over.
     */
    show() {
      if (greeting) return;
      play(false);
    },
    /** Ends the greeting's motion once someone has signed in; the background shows at once. */
    stop() {
      endGreeting();
      releaseBackground();
    },
    /** Remembers who just signed in; a session without a named person forgets. */
    remember(payload) {
      var person = String(payload.canonical_user_id || "").trim();
      storeName(person ? firstName(payload.display_name) : "");
    },
    /** "Not Alice?": forgets the name and greets again without it. */
    forget() {
      storeName("");
      play(true);
      focusTarget.focus();
    },
  };
}
