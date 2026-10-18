// Run in the project's onboarding aesthetic preview, in a real browser
// (scripts/preview_knowledge.py answers the Knowledge routes). Checks what
// only a layout engine shows: the panes fill the page, list rows read from
// the left, a page's text never overlaps the sections after it, a refusal is
// shown at the top of the editor in view, `[[links]]` open their page (a
// merged name shows its note), the page fits a narrow screen, archived pages
// can be found and restored, and the assembled Web Chat page signs out.
export async function verifyKnowledgePage() {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function until(predicate, label) {
    for (let i = 0; i < 160; i++) {
      if (predicate()) return;
      await wait(50);
    }
    throw new Error("Browser condition did not settle: " + label);
  }
  const results = [];
  await fetch("/api/web-chat/auth", {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: "preview" }),
  });
  const frame = document.createElement("iframe");
  Object.assign(frame.style, { position: "fixed", left: "0", top: "0", width: "1440px", height: "900px", border: "0", zIndex: "999" });
  frame.src = "/webchat?view=knowledge";
  document.body.append(frame);
  await new Promise(resolve => frame.addEventListener("load", resolve, { once: true }));
  const win = frame.contentWindow;
  const doc = win.document;
  const rect = selector => doc.querySelector(selector).getBoundingClientRect();
  try {
    await until(() => doc.querySelectorAll(".knowledge-list-button").length === 4 && doc.querySelector(".knowledge-body"), "Home and the team list");
    check(doc.querySelector("#knowledge-directory").hidden, "the directory replaces the default wiki reader");
    check(doc.querySelector("#knowledge-viewer-title").textContent === "Home", "the wiki does not start at Home");
    const releaseLink = [...doc.querySelectorAll(".knowledge-page-link")].find(link => link.textContent === "Release process");
    check(releaseLink, "Home does not link to its topics");
    releaseLink.click();
    await until(() => doc.querySelector("#knowledge-viewer-title").textContent === "Release process", "Home's topic link");
    check(new URL(win.location.href).searchParams.get("page") === "team/release-process", "the article lacks its own address");
    win.history.back();
    await until(() => doc.querySelector("#knowledge-viewer-title").textContent === "Home" && doc.querySelector(".knowledge-body"), "Back to Home");
    win.history.forward();
    await until(() => doc.querySelector("#knowledge-viewer-title").textContent === "Release process", "Forward to the article");
    results.push("Home links and browser Back and Forward open articles");
    const bookmarked = document.createElement("iframe");
    bookmarked.src = win.location.href;
    document.body.append(bookmarked);
    try {
      await until(() => bookmarked.contentDocument?.querySelector("#knowledge-viewer-title")?.textContent === "Release process",
        "the bookmarked article");
      const reloaded = new Promise(resolve => bookmarked.addEventListener("load", resolve, { once: true }));
      bookmarked.contentWindow.location.reload();
      await reloaded;
      await until(() => bookmarked.contentDocument.querySelector("#knowledge-viewer-title")?.textContent === "Release process",
        "the article after reload");
      results.push("an article bookmark survives a full page reload");
    } finally { bookmarked.remove(); }
    doc.querySelector("#knowledge-browse").click();
    const app = rect("#knowledge-app");
    const layout = rect(".knowledge-layout");
    check(app.bottom - layout.bottom < 60, "the panes do not fill the page");
    results.push("panes fill the page");

    const row = [...doc.querySelectorAll(".knowledge-list-button")].find(button => button.textContent.startsWith("Release process"));
    const title = row.querySelector(".knowledge-list-title").getBoundingClientRect();
    check(title.left - row.getBoundingClientRect().left < 20, "list rows are not read from the left");
    results.push("list rows start at the left");

    // The My proposals badge (ui/DESIGN.md, "Attention counts") sits inside its tab on one line.
    await until(() => !doc.querySelector("#knowledge-proposals-count").hidden, "the proposals badge");
    const tab = rect("#knowledge-proposals-tab");
    const badge = rect("#knowledge-proposals-count");
    check(doc.querySelector("#knowledge-proposals-count").textContent === "1", "the badge does not show the count");
    check(badge.left >= tab.left && badge.right <= tab.right && badge.top >= tab.top && badge.bottom <= tab.bottom,
      "the badge sits outside its tab");
    check(Math.abs(tab.height - rect("#knowledge-team-tab").height) < 1, "the badge changes the tab's height");
    results.push("proposals badge inside its tab");

    row.click();
    await until(() => doc.querySelector(".knowledge-body"), "the page view");
    const body = rect(".knowledge-body");
    const next = rect(".knowledge-section");
    check(body.bottom <= next.top + 1, "the page text overlaps the section after it");
    results.push("page text does not overlap");

    const oldName = [...doc.querySelectorAll(".knowledge-page-link")].find(link => link.textContent === "the old checklist");
    check(oldName && doc.querySelector(".knowledge-missing-link"), "page links are not clickable or marked missing");
    oldName.click();
    await until(() => doc.querySelector("#knowledge-viewer").textContent.includes("old-release-checklist was merged into this page."),
      "the merged name's note");
    results.push("a link to a merged name opens its page with a note");

    doc.querySelector("#knowledge-viewer-tools button").click();
    await until(() => doc.querySelector(".knowledge-editor"), "the editor");
    const text = doc.querySelector(".knowledge-editor-body");
    text.value += "\nAsk ops@example.com";
    doc.querySelector(".knowledge-editor").requestSubmit();
    await until(() => doc.querySelector(".knowledge-editor .warning-callout"), "the review refusal");
    const message = rect(".knowledge-editor .warning-callout");
    const viewer = rect("#knowledge-viewer");
    check(message.top >= viewer.top - 1 && message.top < viewer.bottom, "the refusal is out of view");
    check(text.value.includes("Ask ops@example.com"), "the typed text was lost");
    results.push("refusal shown in view and text kept");

    frame.style.width = "390px";
    frame.style.height = "844px";
    await until(() => win.innerWidth === 390, "the narrow screen");
    check(doc.documentElement.scrollWidth <= win.innerWidth + 1, "the narrow page scrolls sideways");
    results.push("fits a narrow screen");

    // Exercise the real chat.js assembly and listeners with controlled data
    // for the pagination and cross-space cases. Sign-out reaches the preview
    // server normally, so a missing logout listener cannot pass this probe.
    frame.style.width = "1440px";
    frame.style.height = "900px";
    const originalFetch = win.fetch.bind(win);
    const sample = await (await originalFetch("/api/web-chat/knowledge/pages/team/release-process?session_key=preview")).json();
    const pageRequests = [];
    const current = Array.from({ length: 20 }, (_, index) => ({
      ...sample.page, page: "topic-" + index, title: "Topic " + index,
      summary: "A topic summary with enough text to wrap across several lines in the directory.",
    }));
    const archived = Array.from({ length: 21 }, (_, index) => ({
      ...sample.page, page: "retired-" + index, title: "Retired page " + index,
      status: "archived", body: "Retired instructions " + index + ".", summary: "Retired instructions.", links: [],
    }));
    let logoutRequests = 0;
    let finishLatePage;
    win.fetch = async (url, options = {}) => {
      const path = new URL(String(url), win.location.href);
      const method = options.method || "GET";
      if (path.pathname === "/api/web-chat/logout") logoutRequests += 1;
      if (!path.pathname.startsWith("/api/web-chat/knowledge/")) return originalFetch(url, options);
      pageRequests.push({ path, method });
      const response = payload => Promise.resolve(new win.Response(JSON.stringify(payload), {
        status: 200, headers: { "Content-Type": "application/json" },
      }));
      if (path.pathname === "/api/web-chat/knowledge/pages" && path.searchParams.get("status") === "archived") {
        const query = path.searchParams.get("query") || "";
        const offset = Number(path.searchParams.get("offset") || 0);
        const found = archived.filter(page => page.title.toLowerCase().includes(query.toLowerCase()));
        return response({ status: "ok", pages: found.slice(offset, offset + 20), total: found.length });
      }
      if (path.pathname === "/api/web-chat/knowledge/pages" && path.searchParams.get("space") === "team") {
        return response({ status: "ok", pages: current, total: 3000 });
      }
      if (path.pathname === "/api/web-chat/knowledge/pages/personal/travel-preferences") {
        return response({ status: "ok", page: {
          ...sample.page, space: "personal", page: "travel-preferences", title: "Travel preferences",
          body: "See [[team/release-process]].", links: [
            { written: "team/release-process", space: "team", page: "release-process", exists: true },
          ],
        } });
      }
      const retired = path.pathname.match(/\/pages\/team\/(retired-\d+)(\/save)?$/);
      if (retired) {
        const page = archived.find(item => item.page === retired[1]);
        if (retired[2]) {
          const values = JSON.parse(options.body);
          archived.splice(archived.indexOf(page), 1);
          return response({ status: "ok", page: { ...page, ...values, status: "active", revision: page.revision + 1 } });
        }
        if (finishLatePage === null) return new Promise(resolve => { finishLatePage = () => resolve(response({ status: "ok", page })); });
        return response({ status: "ok", page });
      }
      return originalFetch(url, options);
    };

    doc.querySelector("#knowledge-personal-tab").click();
    doc.querySelector("#knowledge-browse").click();
    await until(() => doc.querySelector(".knowledge-list-button"), "the personal list");
    doc.querySelector(".knowledge-list-button").click();
    await until(() => doc.querySelector(".knowledge-page-link"), "the personal page's team link");
    doc.querySelector(".knowledge-page-link").click();
    await until(() => doc.querySelector("#knowledge-viewer-meta").textContent.startsWith("Team page"), "the linked team page");
    const beforeRefresh = pageRequests.length;
    doc.querySelector("#knowledge-refresh").click();
    await until(() => pageRequests.slice(beforeRefresh).some(request => request.path.pathname.endsWith("/pages/team/release-process")),
      "refresh of the linked team's page");
    check(doc.querySelector("#knowledge-personal-tab").getAttribute("aria-selected") === "true", "refresh changed the selected list space");
    check(!pageRequests.slice(beforeRefresh).some(request => request.path.pathname.endsWith("/pages/personal/release-process")),
      "refresh requested the displayed page in the wrong space");
    results.push("refresh keeps a linked page's space");

    doc.querySelector("#knowledge-team-tab").click();
    doc.querySelector("#knowledge-browse").click();
    await until(() => doc.querySelectorAll(".knowledge-list-button").length === 20, "the current team list");
    const list = doc.querySelector("#knowledge-list");
    const pager = rect("#knowledge-pagination");
    check(list.scrollHeight > list.clientHeight, "twenty rows are clipped instead of making the list scroll");
    list.scrollTop = list.scrollHeight;
    const lastRow = rect(".knowledge-list-item:last-child");
    check(list.scrollTop > 0 && lastRow.bottom <= rect("#knowledge-list").bottom + 1,
      "scrolling cannot reach the twentieth row");
    check(pager.top >= rect("#knowledge-list").bottom - 1 && pager.bottom <= rect(".knowledge-list-pane").bottom,
      "pagination is outside the visible directory footer");
    check(doc.querySelectorAll(".knowledge-list-button").length === 20 && doc.querySelector("#knowledge-pagination").textContent.includes("3,000"),
      "the directory does not keep a 3,000-page collection paginated");
    results.push("twenty full-height rows scroll above visible pagination for 3,000 pages");
    frame.style.width = "390px";
    frame.style.height = "844px";
    await until(() => win.innerWidth === 390, "the narrow directory");
    list.scrollTop = list.scrollHeight;
    check(doc.documentElement.scrollWidth <= win.innerWidth + 1, "the long directory scrolls sideways on mobile");
    check(rect(".knowledge-list-item:last-child").bottom <= rect("#knowledge-list").bottom + 1,
      "the narrow directory cannot reach its last row");
    check(rect("#knowledge-pagination").bottom <= rect(".knowledge-list-pane").bottom + 1,
      "the narrow directory clips its pagination");
    check(rect("#knowledge-pagination").bottom <= rect(".knowledge-layout").bottom + 1,
      "the narrow directory places pagination below the visible area");
    results.push("the narrow directory keeps its rows and pagination reachable");
    frame.style.width = "1440px";
    frame.style.height = "900px";
    await until(() => win.innerWidth === 1440, "the wide directory");
    const status = doc.querySelector("#knowledge-page-status");
    check(status.labels.length === 1 && status.labels[0].textContent === "Page status", "the page status filter lacks its label");
    status.value = "archived";
    status.dispatchEvent(new win.Event("change", { bubbles: true }));
    await until(() => doc.querySelectorAll(".knowledge-list-button").length === 20
      && doc.querySelector(".knowledge-list-button").textContent.includes("Archived"), "the archived first page");
    check(doc.querySelector(".knowledge-list-button").textContent.includes("Archived"), "archived rows lack a status label");
    const nextPage = [...doc.querySelectorAll(".list-pagination button")].find(button => /next/i.test(button.getAttribute("aria-label") || button.textContent));
    check(nextPage, "archived pages lack pagination");
    nextPage.click();
    await until(() => doc.querySelectorAll(".knowledge-list-button").length === 1, "the archived second page");
    const lastList = pageRequests.filter(request => request.path.pathname === "/api/web-chat/knowledge/pages").at(-1).path;
    check(lastList.searchParams.get("status") === "archived" && lastList.searchParams.get("offset") === "20", "pagination lost the archive filter");
    doc.querySelector("#knowledge-query").value = "Retired page 20";
    doc.querySelector("#knowledge-search").requestSubmit();
    await until(() => pageRequests.filter(request => request.path.pathname === "/api/web-chat/knowledge/pages").at(-1).path.searchParams.get("offset") === "0",
      "the archived search");
    doc.querySelector(".knowledge-list-button").click();
    await until(() => [...doc.querySelectorAll("#knowledge-viewer-tools button")].some(button => button.textContent === "Restore"), "the archived page");
    [...doc.querySelectorAll("#knowledge-viewer-tools button")].find(button => button.textContent === "Restore").click();
    await until(() => doc.querySelector(".knowledge-editor"), "the restore editor");
    doc.querySelector(".knowledge-editor").requestSubmit();
    await until(() => doc.querySelector("#knowledge-list").textContent.includes("No pages match this search."), "the restored page removed from archives");
    check([...doc.querySelectorAll("#knowledge-viewer-tools button")].some(button => button.textContent === "Edit"), "restoring did not return the page to current");
    results.push("archived pages can be searched, paginated and restored");

    // A late page response must not refill the viewer after logout clears it.
    doc.querySelector("#knowledge-query").value = "";
    doc.querySelector("#knowledge-search").requestSubmit();
    await until(() => doc.querySelectorAll(".knowledge-list-button").length === 20, "the remaining archived list");
    finishLatePage = null;
    doc.querySelector(".knowledge-list-button").click();
    await until(() => typeof finishLatePage === "function", "the pending page request");
    doc.querySelector("#knowledge-logout").click();
    await until(() => !doc.querySelector("#chat-auth").hidden && doc.querySelector("#knowledge-app").hidden, "Knowledge logout");
    finishLatePage();
    await wait(100);
    check(logoutRequests === 1, "Knowledge logout did not make exactly one logout request");
    check(doc.querySelector("#knowledge-viewer").children.length === 0 && doc.querySelector("#knowledge-list").children.length === 0,
      "a late response refilled Knowledge after logout");
    check(status.value === "active", "logout did not reset the page status filter");
    results.push("Knowledge logout clears data and rejects a late page response");
  } finally {
    frame.remove();
  }
  return results;
}
