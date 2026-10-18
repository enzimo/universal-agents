/** Knowledge addresses identify pages or proposals, never a person's identity. */
export const HOME_PAGE = "home";
const SPACES = new Set(["team", "personal"]);
const ADDRESS_FIELDS = ["space", "page", "revision", "proposal"];

export function readKnowledgeAddress(location) {
  const query = new URL(location.href).searchParams;
  const tab = query.get("space");
  const selectedTab = SPACES.has(tab) || tab === "proposals" ? tab : "team";
  const reference = String(query.get("page") || "");
  const separator = reference.indexOf("/");
  const space = reference.slice(0, separator);
  const page = reference.slice(separator + 1);
  const article = SPACES.has(space) && Boolean(page);
  const revision = Number(query.get("revision") || 0);
  return {
    tab: selectedTab,
    article,
    space: article ? space : selectedTab === "personal" ? "personal" : "team",
    page: article ? page : HOME_PAGE,
    revision: Number.isSafeInteger(revision) && revision > 0 ? revision : 0,
    proposal: selectedTab === "proposals" ? String(query.get("proposal") || "") : "",
  };
}

export function knowledgeAddress(location, { tab, space, page, revision = 0, proposal = "" }) {
  const url = new URL(location.href);
  ADDRESS_FIELDS.forEach(field => url.searchParams.delete(field));
  url.searchParams.set("view", "knowledge");
  url.searchParams.set("space", tab);
  if (page) {
    url.searchParams.set("page", `${space}/${page}`);
    if (revision) url.searchParams.set("revision", String(revision));
  } else if (proposal) url.searchParams.set("proposal", proposal);
  url.hash = "";
  return url.href;
}
