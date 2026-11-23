import assert from "node:assert/strict";
import test from "node:test";
import {
  readChatPageLink,
  readWorkspaceFileLink,
  stripWorkspaceDownloadParam,
} from "../assistant_gateway/onboarding_web/assets/chat_links.mjs";

const at = (search, hash = "") => ({ pathname: "/webchat", search, hash });

test("an Inbox link opens the request once and puts plain chat back in the address", () => {
  assert.deepEqual(readChatPageLink(at("?view=inbox", "#access_request=access_req_1")), {
    view: "inbox", accessRequestId: "access_req_1", accessAgentType: "", cleanUrl: "/webchat",
  });
  assert.equal(readChatPageLink(at("?view=inbox")).cleanUrl, "/webchat");
});

test("an Access link opens the agent type once and keeps other query values", () => {
  assert.deepEqual(readChatPageLink(at("?view=access&lang=en", "#agent=TeamLead")), {
    view: "access", accessRequestId: "", accessAgentType: "TeamLead", cleanUrl: "/webchat?lang=en",
  });
});

test("separate pages and plain chat keep their address", () => {
  for (const [search, view] of [["?view=objectives", "objectives"], ["?view=files", "files"], ["", ""]]) {
    assert.deepEqual(readChatPageLink(at(search, "#access_request=x")), {
      view, accessRequestId: "", accessAgentType: "", cleanUrl: "",
    });
  }
});

test("workspace file deep-links read path and download flag and strip download cleanly", () => {
  assert.deepEqual(
    readWorkspaceFileLink(at("?view=files&path=team%2Fguides%2Freport%201.pdf&download=1")),
    { path: "team/guides/report 1.pdf", download: true },
  );
  assert.deepEqual(
    readWorkspaceFileLink(at("?view=files&path=team%2Freport.pdf&download=true")),
    { path: "team/report.pdf", download: true },
  );
  assert.deepEqual(
    readWorkspaceFileLink(at("?view=files&path=team%2Freport.pdf")),
    { path: "team/report.pdf", download: false },
  );
  assert.deepEqual(
    readWorkspaceFileLink(at("?view=files&download=1")),
    { path: "", download: false },
  );
  assert.deepEqual(
    readWorkspaceFileLink(at("?view=objectives&path=team%2Freport.pdf&download=1")),
    { path: "", download: false },
  );

  assert.equal(
    stripWorkspaceDownloadParam(at("?view=files&path=team%2Freport.pdf&download=1")),
    "/webchat?view=files&path=team%2Freport.pdf",
  );
  assert.equal(
    stripWorkspaceDownloadParam(at("?view=files&path=team%2Freport.pdf&download=1&extra=ok", "#top")),
    "/webchat?view=files&path=team%2Freport.pdf&extra=ok#top",
  );
  assert.equal(stripWorkspaceDownloadParam(at("?view=files&path=team%2Freport.pdf")), "");
});
