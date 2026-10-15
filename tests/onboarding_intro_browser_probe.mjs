// Run in the project's onboarding aesthetic preview, in a real browser.
// Loads the onboarding page in a same-origin iframe with different saved
// progress and checks whether the "Universal Agents" intro opens and which
// step shows. The intro once never played because the startup code that
// decides it read setup state from outside mountPage and threw; the load
// event fires after the page's module scripts ran, so a missing intro shows
// such a failure.
export async function verifyOnboardingIntroBrowser() {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const PAGE_KEY = "ua.preview.page";
  const FURTHEST_KEY = "ua.preview.furthest_page";
  const saved = [PAGE_KEY, FURTHEST_KEY].map(key => [key, localStorage.getItem(key)]);
  const results = [];
  const frame = document.createElement("iframe");
  Object.assign(frame.style, { position: "fixed", left: "-5000px", top: "0", width: "1280px", height: "800px", border: "0" });
  document.body.append(frame);

  // Opens `path` with the given saved step and furthest step (null: nothing saved).
  async function open(path, page, furthest) {
    [[PAGE_KEY, page], [FURTHEST_KEY, furthest]].forEach(([key, value]) => {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, String(value));
    });
    const loaded = new Promise(resolve => frame.addEventListener("load", resolve, { once: true }));
    frame.src = path;
    await loaded;
    const doc = frame.contentDocument;
    return {
      intro: Boolean(doc.querySelector(".ua-intro")),
      heading: (doc.querySelector(".preview-shell h1") || {}).textContent || "",
    };
  }

  try {
    let shown = await open("/", null, null);
    check(shown.intro, "a first visit does not open the intro");
    check(shown.heading.includes("Agentic Future"), "a first visit does not start on Welcome");
    results.push("first visit opens the intro on Welcome");

    shown = await open("/", 0, 0);
    check(shown.intro, "a visit that never went past Welcome does not open the intro");
    results.push("return visit still on Welcome opens the intro");

    shown = await open("/", 2, 2);
    check(!shown.intro, "a visit past Welcome opens the intro");
    check(shown.heading.includes("Define Your Agent"), "a visit past Welcome does not reopen the saved step");
    results.push("visit past Welcome reopens the saved step without the intro");

    shown = await open("/", 0, 2);
    check(!shown.intro, "going back to Welcome after progressing opens the intro");
    results.push("back on Welcome after progressing shows no intro");

    shown = await open("/?intro", 2, 2);
    check(shown.intro, "?intro does not open the intro past Welcome");
    results.push("?intro opens the intro on any step");
    return results;
  } finally {
    frame.remove();
    saved.forEach(([key, value]) => {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    });
  }
}
