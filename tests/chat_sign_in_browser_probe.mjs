// Run in the project's onboarding aesthetic preview, in a real browser.
// The iframe owns a full Web Chat document; changing its width generates a
// real viewport resize and re-evaluates media queries without a desktop host.
export async function verifySignInBrowser() {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function until(predicate) {
    for (let i = 0; i < 160; i++) {
      if (predicate()) return;
      await wait(50);
    }
    throw new Error("Browser condition did not settle");
  }
  const results = [];
  const html = await (await fetch("/webchat")).text();
  const frame = document.createElement("iframe");
  Object.assign(frame.style, { position: "fixed", left: "0", top: "0", width: "1536px", height: "960px", border: "0", zIndex: "999" });
  document.body.append(frame);
  async function load(stalled = false) {
    const script = `<script>
      const RealDate = Date;
      window.Date = class extends RealDate { getHours() { return 12; } };
      const originalFetch = window.fetch;
      let firstSession = true;
      window.fetch = (url, options) => {
        if (!firstSession || !String(url).endsWith('/api/web-chat/session')) return originalFetch(url, options);
        firstSession = false;
        // Each frame starts signed out even after the preview's fake auth
        // cookie was set by an earlier case. Other requests use real cookies.
        return ${stalled}
          ? new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(options.signal.reason), {once:true}))
          : originalFetch(url, {...options, credentials:'omit'});
      };
      window.restoreWarnings = [];
      const warn = console.warn;
      console.warn = (...args) => { window.restoreWarnings.push(String(args[0])); warn(...args); };
    <\/script>`;
    frame.srcdoc = html.replace("<head>", "<head>" + script);
    await new Promise(resolve => frame.addEventListener("load", resolve, {once: true}));
    return frame.contentWindow;
  }
  const key = win => win.document.dispatchEvent(new win.KeyboardEvent("keydown", {key:"Escape", bubbles:true, cancelable:true}));
  try {
    localStorage.setItem("ua.webchat.greeting_name", "Alexandra");
    let win = await load();
    let doc = win.document;
    await until(() => !doc.documentElement.hasAttribute("data-ua-background-hold"));
    key(win);
    await until(() => doc.querySelector("#chat-title").style.width);
    frame.style.width = "390px";
    frame.style.height = "844px";
    await until(() => doc.querySelector("#chat-title").getBoundingClientRect().width <= win.innerWidth
      && doc.documentElement.scrollWidth <= win.innerWidth);
    const title = doc.querySelector("#chat-title").getBoundingClientRect();
    const card = doc.querySelector("#chat-auth-card").getBoundingClientRect();
    check(title.width <= win.innerWidth, "resized greeting exceeds viewport");
    check(card.left >= 0 && card.right <= win.innerWidth, "resized form is offscreen");
    check(doc.documentElement.scrollWidth <= win.innerWidth, "resize creates horizontal overflow");
    check(!doc.querySelector("#chat-auth-card").inert, "resize hides an open form");
    frame.style.width = "1536px";
    await until(() => doc.querySelector("#chat-title").getBoundingClientRect().width > title.width);
    check(doc.querySelector("#chat-title").getBoundingClientRect().width > title.width, "greeting did not expand back");
    results.push("wide → narrow → wide greeting and form");

    for (const forming of [true, false]) {
      win = await load(); doc = win.document;
      await until(() => !doc.documentElement.hasAttribute("data-ua-background-hold"));
      if (!forming) key(win);
      let draws = 0;
      const originalDraw = win.CanvasRenderingContext2D.prototype.drawImage;
      win.CanvasRenderingContext2D.prototype.drawImage = function (...args) {
        if (doc.querySelector("#chat-title").contains(this.canvas)) draws++;
        return originalDraw.apply(this, args);
      };
      doc.documentElement.setAttribute("data-ui-motion", "off");
      doc.dispatchEvent(new win.Event("ua:appearance-change"));
      await until(() => doc.querySelector("#chat-title").style.width);
      await wait(100);
      const before = draws;
      await wait(300);
      check(draws === before, "word keeps drawing after motion is disabled");
      check(!doc.querySelector("#chat-auth-card").inert, "motion change leaves form inert");
      check(!doc.querySelector("#chat-auth-card").classList.contains("ua-intro-enter"), "motion change retains entrance animation");
      results.push(forming ? "motion disabled during forming" : "motion disabled after reveal");
    }

    win = await load(); doc = win.document;
    await until(() => !doc.documentElement.hasAttribute("data-ua-background-hold"));
    key(win);
    doc.querySelector("#chat-auth-token").value = "review-only-preview-token";
    doc.querySelector("#chat-auth-form").requestSubmit();
    await until(() => !doc.querySelector("#chat-app").hidden);
    check(!doc.querySelector("#chat-auth-card").classList.contains("ua-intro-enter"), "sign-in retains entrance class");
    doc.querySelector("#settings-logout-button").click();
    await until(() => !doc.querySelector("#chat-auth").hidden);
    await wait(700);
    check(doc.querySelector("#chat-auth-card").inert, "sign-out reveals form prematurely");
    check(win.getComputedStyle(doc.querySelector("#chat-auth-card")).opacity === "0", "inert form is visible after sign-out");
    key(win);
    check(!doc.querySelector("#chat-auth-card").inert, "sign-out greeting cannot be skipped");
    results.push("early sign-in → sign-out → skip");

    win = await load(true); doc = win.document;
    await until(() => !doc.documentElement.hasAttribute("data-ua-background-hold"));
    check(win.restoreWarnings.some(message => message.includes("session could not be restored")), "timeout is silent");
    key(win);
    check(!doc.querySelector("#chat-auth-card").inert, "session deadline leaves form inert");
    results.push("stalled session restore warns and exposes form");
    return results;
  } finally {
    frame.remove();
    localStorage.removeItem("ua.webchat.greeting_name");
  }
}
