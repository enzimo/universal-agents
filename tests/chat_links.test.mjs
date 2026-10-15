import assert from "node:assert/strict";
import test from "node:test";
import { readChatPageLink } from "../assistant_gateway/onboarding_web/assets/chat_links.mjs";

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
