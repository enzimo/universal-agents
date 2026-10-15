"""Small HTTP service for user-facing documentation pages."""

from __future__ import annotations

import asyncio
import gzip
import html
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

from aiohttp import web

from custom_agents.skill_installation import verify_managed_skill_attestation_paths
from runtime_settings.appearance import dark_appearance_css_declarations
from user_docs.published_sites import (
    SITE_CHAT_SIGN_IN_PATH,
    SITE_ID_PATTERN,
    PublishedSiteRecordV1,
    PublishedSiteStore,
    SiteFileUnavailable,
)
from user_docs.skill_docs import (
    BUNDLED_SKILL_DOCS_ROOT,
    SkillDoc,
    list_skill_docs,
    markdown_to_html,
    resolve_user_docs_skills_dir,
    slugify_skill_name,
)


LOGGER = logging.getLogger(__name__)
USER_DOCS_PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_USER_DOCS_ROOT = USER_DOCS_PACKAGE_ROOT / "site"
DOCS_ASSETS_ROOT = USER_DOCS_PACKAGE_ROOT
DOCS_SOURCE_ASSETS_ROOT = USER_DOCS_PACKAGE_ROOT.parent / "user_docs_source" / "assets"
WEB_CHAT_ASSETS_ROOT = USER_DOCS_PACKAGE_ROOT.parent / "assistant_gateway" / "onboarding_web" / "assets"
DOCS_ROUTE_PREFIX = "/docs"
SKILL_CARD_NAME_MAX_CHARS = 48
SKILL_CARD_DESCRIPTION_MAX_CHARS = 160
SKILL_CARD_DEFAULT_DESCRIPTION = "Skill instructions and user-facing notes."
GeneratedSiteChatHandler = Callable[[web.Request], Awaitable[web.Response]]
# The person signed in to Web Chat for a request, or None (the sites index
# lists only that person's pages).
GeneratedSiteViewer = Callable[[web.Request], "str | None"]
ExtraRoute = tuple[str, str, Callable[[web.Request], Awaitable[web.StreamResponse]]]


class UserDocsService:
    def __init__(
        self,
        *,
        docs_root: Path | str | None = None,
        skills_dir: Path | str | None = None,
        managed_skills_dir: Path | str | None = None,
        managed_skill_attestations_dir: Path | str | None = None,
        published_sites: PublishedSiteStore | None = None,
        generated_site_chat_handler: GeneratedSiteChatHandler | None = None,
        generated_site_viewer: GeneratedSiteViewer | None = None,
        service_base_url: str | None = None,
        appearance: Mapping[str, object] | None = None,
        extra_routes: list[ExtraRoute] | None = None,
        client_max_size: int | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.docs_root = Path(docs_root or DEFAULT_USER_DOCS_ROOT).expanduser().resolve()
        self.skills_dir = resolve_user_docs_skills_dir(skills_dir)
        self.managed_skills_dir = (
            Path(managed_skills_dir).expanduser().resolve()
            if managed_skills_dir
            else None
        )
        self.managed_skill_attestations_dir = (
            Path(managed_skill_attestations_dir).expanduser().resolve()
            if managed_skill_attestations_dir
            else None
        )
        if (self.managed_skills_dir is None) != (
            self.managed_skill_attestations_dir is None
        ):
            raise ValueError(
                "managed_skills_dir and managed_skill_attestations_dir must be configured together"
            )
        self.published_sites = published_sites
        self.generated_site_chat_handler = generated_site_chat_handler
        self.generated_site_viewer = generated_site_viewer
        self.service_base_url = str(service_base_url or "").strip().rstrip("/") or None
        self.appearance = dict(appearance or {})
        self.extra_routes = list(extra_routes or [])
        self.client_max_size = client_max_size
        self._logger = logger or LOGGER

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=self.client_max_size) if self.client_max_size else web.Application()
        app.router.add_get("/health", self._handle_health)
        for method, path, handler in self.extra_routes:
            app.router.add_route(method, path, handler)
        app.router.add_get("/", self._handle_docs_root_redirect)
        app.router.add_get(DOCS_ROUTE_PREFIX, self._handle_docs_root_redirect)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/", self._handle_static_doc)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/sitemap.xml", self._handle_sitemap)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/sitemap.xml.gz", self._handle_sitemap)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/assets/styles.css", self._handle_docs_stylesheet)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/assets/static-mesh.js", self._handle_static_mesh_script)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/assets/appearance.js", self._handle_appearance_script)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/assets/metallic_mesh.js", self._handle_metallic_mesh_script)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/assets/chat_markdown.js", self._handle_chat_markdown_script)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/assets/operational_ids.js", self._handle_operational_ids_script)
        app.router.add_get(
            DOCS_ROUTE_PREFIX + "/assets/ua-runtime-theme.css",
            self._handle_runtime_theme_stylesheet,
        )
        app.router.add_get(DOCS_ROUTE_PREFIX + "/sites", self._handle_generated_sites_index)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/sites/", self._handle_generated_sites_index)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/sites/{site_id}", self._handle_generated_site_redirect)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/sites/{site_id}/", self._handle_generated_site)
        app.router.add_post("/api/sites/{site_id}/chat", self._handle_generated_site_chat)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/skills", self._handle_skills_index)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/skills/", self._handle_skills_index)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/skills/{skill_slug}", self._handle_skill_detail)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/skills/{skill_slug}/", self._handle_skill_detail)
        app.router.add_get(DOCS_ROUTE_PREFIX + "/{path:.*}", self._handle_static_doc)
        return app

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "docs_root": str(self.docs_root),
                "skills_dir": str(self.skills_dir),
                "managed_skills_dir": (
                    str(self.managed_skills_dir) if self.managed_skills_dir else None
                ),
                "base_url": self.service_base_url,
                "generated_site_records_dir": (
                    str(self.published_sites.records_dir) if self.published_sites else None
                ),
                "generated_site_chat_enabled": self.generated_site_chat_handler is not None,
                "sections": ["onboarding", "docker", "configuration", "channels", "auth", "tools", "skills", "sites"],
            }
        )

    async def _handle_docs_stylesheet(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse((DOCS_ASSETS_ROOT / "assets" / "styles.css").resolve())

    async def _handle_static_mesh_script(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse((DOCS_SOURCE_ASSETS_ROOT / "static-mesh.js").resolve())

    async def _handle_appearance_script(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse((DOCS_SOURCE_ASSETS_ROOT / "appearance.js").resolve())

    async def _handle_metallic_mesh_script(
        self, _request: web.Request
    ) -> web.FileResponse:
        # One renderer source, shared with Web Chat and onboarding.
        return web.FileResponse((WEB_CHAT_ASSETS_ROOT / "metallic_mesh.js").resolve())

    async def _handle_chat_markdown_script(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse((WEB_CHAT_ASSETS_ROOT / "chat_markdown.js").resolve())

    async def _handle_operational_ids_script(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse((WEB_CHAT_ASSETS_ROOT / "operational_ids.js").resolve())

    async def _handle_runtime_theme_stylesheet(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=self._runtime_theme_css(),
            content_type="text/css",
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_docs_root_redirect(self, _request: web.Request) -> web.Response:
        raise web.HTTPMovedPermanently(location=DOCS_ROUTE_PREFIX + "/")

    async def _handle_static_doc(self, request: web.Request) -> web.FileResponse:
        request_path = str(request.match_info.get("path") or "")
        redirect_location = self._static_doc_canonical_redirect(request_path, request)
        if redirect_location is not None:
            raise web.HTTPMovedPermanently(location=redirect_location)
        file_path = self._resolve_static_doc_path(request_path)
        if file_path is None:
            raise web.HTTPNotFound(text="Documentation page not found.")
        return web.FileResponse(file_path)

    async def _handle_sitemap(self, request: web.Request) -> web.Response:
        # Material uses exact sitemap URLs to decide which links can use instant
        # navigation. Its host rewrite retains the build-time port, so the served
        # map must use the browser's origin, including local/proxy port mappings.
        compressed = request.path.endswith(".gz")
        try:
            body = await asyncio.to_thread(
                self._sitemap_for_origin, str(request.url.origin()), compressed
            )
        except FileNotFoundError:
            raise web.HTTPNotFound(text="Documentation sitemap not found.") from None
        return web.Response(
            body=body,
            content_type="application/gzip" if compressed else "application/xml",
            headers={"Cache-Control": "no-store"},
        )

    def _sitemap_for_origin(self, origin: str, compressed: bool) -> bytes:
        root = ElementTree.fromstring((self.docs_root / "sitemap.xml").read_bytes())
        for location in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
            value = urlsplit(location.text or "")
            if value.path.startswith(DOCS_ROUTE_PREFIX + "/"):
                location.text = origin + urlunsplit(("", "", value.path, value.query, value.fragment))
        body = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        return gzip.compress(body, mtime=0) if compressed else body

    def _static_doc_canonical_redirect(self, request_path: str, request: web.Request) -> str | None:
        cleaned = request_path.strip("/")
        if not cleaned or request_path.endswith("/"):
            return None
        directory_index = (self.docs_root / cleaned / "index.html").resolve()
        try:
            directory_index.relative_to(self.docs_root)
        except ValueError:
            return None
        if not directory_index.is_file():
            return None
        query = f"?{request.rel_url.query_string}" if request.rel_url.query_string else ""
        return f"{DOCS_ROUTE_PREFIX}/{cleaned}/{query}"

    def _resolve_static_doc_path(self, request_path: str) -> Path | None:
        cleaned = request_path.strip("/")
        if not cleaned:
            candidates = [self.docs_root / "index.html"]
        else:
            base = self.docs_root / cleaned
            candidates = [base, base / "index.html", self.docs_root / f"{cleaned}.html"]
        for candidate in candidates:
            file_path = candidate.resolve()
            try:
                file_path.relative_to(self.docs_root)
            except ValueError:
                continue
            if file_path.is_file():
                return file_path
        return None

    def _runtime_theme_css(self) -> str:
        appearance = self.appearance if isinstance(self.appearance, Mapping) else {}
        color_mode = (
            "light"
            if str(appearance.get("color_mode") or "").strip().lower() == "light"
            else "dark"
        )
        light_colors = appearance.get("light_colors")
        colors = list(light_colors) if isinstance(light_colors, list) else []
        defaults = ["#082f73", "#ffd60a", "#20f05a", "#ff3030"]
        normalized_colors = [
            self._css_hex(colors[index] if index < len(colors) else "", fallback)
            for index, fallback in enumerate(defaults)
        ]
        diffusion = self._css_int(appearance.get("light_diffusion"), 58, 0, 100)
        opacity_low = self._css_int(appearance.get("polygon_opacity_low"), 20, 0, 100)
        opacity_high = self._css_int(appearance.get("polygon_opacity_high"), 70, 0, 100)
        if opacity_low > opacity_high:
            opacity_low = opacity_high
        line_color = self._css_hex(appearance.get("polygon_line_color"), "#202426")
        line_opacity = self._css_int(appearance.get("polygon_line_opacity"), 8, 0, 100)
        return "\n".join(
            [
                ":root {",
                f"  --ua-runtime-color-mode: {color_mode};",
                f"  --ua-doc-light-blue: {normalized_colors[0]};",
                f"  --ua-doc-light-yellow: {normalized_colors[1]};",
                f"  --ua-doc-light-green: {normalized_colors[2]};",
                f"  --ua-doc-light-red: {normalized_colors[3]};",
                f"  --ua-doc-light-diffusion: {diffusion};",
                f"  --ua-doc-polygon-opacity-low: {opacity_low / 100:.3f};",
                f"  --ua-doc-polygon-opacity-high: {opacity_high / 100:.3f};",
                f"  --ua-doc-polygon-line-color: {line_color};",
                f"  --ua-doc-polygon-line-opacity: {line_opacity / 100:.3f};",
                f"  --ua-light-mesh-seed: {self._css_int(appearance.get('polygon_seed'), 4147, 1, 4_294_967_295)};",
                "  --ua-light-dynamic: "
                + (
                    "true"
                    if bool(appearance.get("dynamic_background", True))
                    else "false"
                )
                + ";",
                *dark_appearance_css_declarations(appearance.get("dark")),
                "}",
                "",
            ]
        )

    @staticmethod
    def _css_hex(value: object, fallback: str) -> str:
        candidate = str(value or "").strip()
        return candidate if re.fullmatch(r"#[0-9A-Fa-f]{6}", candidate) else fallback

    @staticmethod
    def _css_int(value: object, fallback: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            parsed = fallback
        return max(minimum, min(maximum, parsed))

    async def _handle_skills_index(self, _request: web.Request) -> web.Response:
        workspace_skills, managed_skills, bundled_skills = self._skill_docs_by_source()
        return web.Response(
            text=self._render_page(
                title="Skills",
                current="skills",
                body=self._render_skills_index(workspace_skills, managed_skills, bundled_skills),
                toc=[
                    ("#_top", "Skills"),
                    ("#workspace-skills", "Workspace Skills"),
                    ("#managed-installed-skills", "Managed Installed Skills"),
                    ("#built-in-runtime-skills", "Built-in runtime skills"),
                ],
            ),
            content_type="text/html",
        )

    async def _handle_skill_detail(self, request: web.Request) -> web.Response:
        requested = slugify_skill_name(request.match_info["skill_slug"]).casefold()
        workspace_skills, managed_skills, bundled_skills = self._skill_docs_by_source()
        requested_source = str(request.query.get("source") or "").strip().casefold()
        if requested_source not in {"", "workspace", "managed", "bundled"}:
            raise web.HTTPNotFound(text="Skill documentation page not found.")
        source_groups: tuple[tuple[str, list[SkillDoc]], ...]
        if requested_source == "managed":
            source_groups = (("Managed installed skill", managed_skills),)
        elif requested_source == "workspace":
            source_groups = (("Workspace skill", workspace_skills),)
        elif requested_source == "bundled":
            source_groups = (("Built-in runtime skill", bundled_skills),)
        else:
            source_groups = (
                ("Workspace skill", workspace_skills),
                ("Managed installed skill", managed_skills),
                ("Built-in runtime skill", bundled_skills),
            )
        match = next(
            (
                (skill, source_label)
                for source_label, skills in source_groups
                for skill in skills
                if skill.slug.casefold() == requested
            ),
            None,
        )
        if match is None:
            raise web.HTTPNotFound(text="Skill documentation page not found.")
        skill, source_label = match
        return web.Response(
            text=self._render_page(
                title=f"{skill.name} Skill",
                current="skills",
                body=self._render_skill_detail(skill, source_label=source_label),
                toc=[
                    ("#_top", skill.name),
                    ("#instructions", "Instructions"),
                ],
            ),
            content_type="text/html",
        )

    def _skill_docs_by_source(self) -> tuple[list[SkillDoc], list[SkillDoc], list[SkillDoc]]:
        """Return runtime-ordered skill docs without presenting unverified managed trees."""

        workspace_skills = list_skill_docs(self.skills_dir)
        bundled_skills = list_skill_docs(BUNDLED_SKILL_DOCS_ROOT)
        managed_skills: list[SkillDoc] = []
        if self.managed_skills_dir is None or self.managed_skill_attestations_dir is None:
            return workspace_skills, managed_skills, bundled_skills

        for skill in list_skill_docs(self.managed_skills_dir):
            try:
                verify_managed_skill_attestation_paths(
                    skill_path=skill.skill_root,
                    installed_root=self.managed_skills_dir,
                    attestations_root=self.managed_skill_attestations_dir,
                )
            except Exception as exc:
                self._logger.warning(
                    "Omitting managed skill documentation after integrity verification failed. "
                    "event=managed_skill_docs_integrity_failed skill_name=%s path=%s error=%s",
                    skill.name,
                    skill.skill_root,
                    exc,
                )
                continue
            managed_skills.append(skill)
        return workspace_skills, managed_skills, bundled_skills

    async def _handle_generated_sites_index(self, request: web.Request) -> web.Response:
        viewer = self.generated_site_viewer(request) if self.generated_site_viewer else None
        return web.Response(
            text=self._render_page(
                title="Generated Sites",
                current="sites",
                body=self._render_generated_sites_index(viewer),
                toc=[
                    ("#_top", "Generated Sites"),
                    ("#available-sites", "Your Sites"),
                ],
            ),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_generated_site_redirect(self, request: web.Request) -> web.Response:
        site_id = self._validated_site_id(request)
        raise web.HTTPMovedPermanently(location=f"{DOCS_ROUTE_PREFIX}/sites/{site_id}/")

    async def _handle_generated_site(self, request: web.Request) -> web.Response:
        """Serve a page exactly as published: through its record, only while its digest matches.

        Anyone with the link may open it (the site id has a random part); a
        page whose file changed, went missing or became a link is not found.
        """

        store, record = self._generated_site_or_404(self._validated_site_id(request))
        try:
            page = store.read_file(record)
        except SiteFileUnavailable as exc:
            raise web.HTTPNotFound(text="Generated site not found.") from exc
        return web.Response(body=page, content_type="text/html", charset="utf-8")

    async def _handle_generated_site_chat(self, request: web.Request) -> web.Response:
        self._generated_site_or_404(self._validated_site_id(request))
        if self.generated_site_chat_handler is None:
            raise web.HTTPServiceUnavailable(text="Generated-site chat is not connected to a gateway.")
        return await self.generated_site_chat_handler(request)

    def _validated_site_id(self, request: web.Request) -> str:
        site_id = str(request.match_info.get("site_id") or "").strip()
        if not SITE_ID_PATTERN.match(site_id):
            raise web.HTTPNotFound(text="Generated site not found.")
        return site_id

    def _generated_site_or_404(self, site_id: str) -> tuple[PublishedSiteStore, PublishedSiteRecordV1]:
        store = self.published_sites
        record = store.record(site_id) if store is not None else None
        if store is None or record is None:
            raise web.HTTPNotFound(text="Generated site not found.")
        return store, record

    def _render_page(
        self,
        *,
        title: str,
        current: str,
        body: str,
        toc: list[tuple[str, str]],
    ) -> str:
        auth_current = ' aria-current="page"' if current == "auth" else ""
        skills_current = ' aria-current="page"' if current == "skills" else ""
        sites_current = ' aria-current="page"' if current == "sites" else ""
        overview_current = ' aria-current="page"' if current == "overview" else ""
        toc_items = "\n".join(
            f'<li><a href="{html.escape(href)}">{html.escape(label)}</a></li>'
            for href, label in toc
        )
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - Universal Agents</title>
  <link rel="icon" href="{DOCS_ROUTE_PREFIX}/assets/favicon.ico" sizes="any">
  <link rel="stylesheet" href="{DOCS_ROUTE_PREFIX}/assets/styles.css">
  <link rel="stylesheet" href="{DOCS_ROUTE_PREFIX}/assets/ua-runtime-theme.css">
  <script src="{DOCS_ROUTE_PREFIX}/assets/appearance.js"></script>
  <script src="{DOCS_ROUTE_PREFIX}/assets/static-mesh.js" defer></script>
  <script src="{DOCS_ROUTE_PREFIX}/assets/metallic_mesh.js" defer></script>
</head>
<body>
  <a class="skip-link" href="#_top">Skip to content</a>
  <div class="page">
    <header class="site-header">
      <div class="header-row">
        <a class="brand" href="/webchat">
          <img class="brand-icon" src="{DOCS_ROUTE_PREFIX}/assets/agent-mark.png" alt="">
          <span>Universal Agents</span>
        </a>
        <div class="header-spacer"></div>
        <a class="search-chip" href="{DOCS_ROUTE_PREFIX}/search/">Search</a>
      </div>
      <details class="mobile-section-nav">
        <summary>Docs</summary>
        <ul>
          <li><a href="{DOCS_ROUTE_PREFIX}/"{overview_current}>Overview</a></li>
          <li><a href="{DOCS_ROUTE_PREFIX}/features/">Agent Features</a></li>
          <li><a href="{DOCS_ROUTE_PREFIX}/generated-sites/">Generated Sites</a></li>
          <li><a href="{DOCS_ROUTE_PREFIX}/onboarding/">Onboarding</a></li>
          <li><a href="{DOCS_ROUTE_PREFIX}/docker/">Docker Runtime</a></li>
          <li><a href="{DOCS_ROUTE_PREFIX}/configuration/">Configuration</a></li>
          <li><a href="{DOCS_ROUTE_PREFIX}/auth/"{auth_current}>Authentication</a></li>
          <li><a href="{DOCS_ROUTE_PREFIX}/skills/"{skills_current}>Skills</a></li>
          <li><a href="{DOCS_ROUTE_PREFIX}/sites/"{sites_current}>Generated Sites</a></li>
          <li><a href="{DOCS_ROUTE_PREFIX}/auth/gh">GitHub CLI</a></li>
          <li><a href="{DOCS_ROUTE_PREFIX}/auth/bw">Bitwarden CLI</a></li>
          <li><a href="{DOCS_ROUTE_PREFIX}/auth/gog">gog</a></li>
        </ul>
      </details>
    </header>

    <div class="layout">
      <aside class="sidebar" aria-label="Documentation navigation">
        <nav>
          <section>
            <span class="nav-section-title">Get Started</span>
            <ul class="doc-list">
              <li><a href="{DOCS_ROUTE_PREFIX}/"{overview_current}>Overview</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/features/">Agent Features</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/generated-sites/">Generated Sites</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/onboarding/">Onboarding</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/docker/">Docker Runtime</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/configuration/">Configuration</a></li>
            </ul>
          </section>
          <section>
            <span class="nav-section-title">Channels</span>
            <ul class="doc-list">
              <li><a href="{DOCS_ROUTE_PREFIX}/channels/">Overview</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/channels/telegram/">Telegram</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/channels/whatsapp/">WhatsApp</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/channels/discord/">Discord</a></li>
            </ul>
          </section>
          <section>
            <span class="nav-section-title">Tools and Authentication</span>
            <ul class="doc-list">
              <li><a href="{DOCS_ROUTE_PREFIX}/tools/">Overview</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/auth/"{auth_current}>Authentication</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/auth/gh">GitHub CLI</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/auth/gog">Google gog</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/auth/bw">Bitwarden CLI</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/auth/onepassword/">1Password CLI</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/auth/keepass/">KeePassXC CLI</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/auth/vault/">HashiCorp Vault</a></li>
            </ul>
          </section>
          <section>
            <span class="nav-section-title">Runtime Pages</span>
            <ul class="doc-list">
              <li><a href="{DOCS_ROUTE_PREFIX}/skills/"{skills_current}>Skills</a></li>
              <li><a href="{DOCS_ROUTE_PREFIX}/sites/"{sites_current}>Generated Sites</a></li>
            </ul>
          </section>
        </nav>
      </aside>

      <main id="_top" class="main-pane">{body}</main>

      <aside class="toc" aria-label="On this page">
        <nav>
          <h2>Table of contents</h2>
          <ul class="toc-list">{toc_items}</ul>
        </nav>
      </aside>
    </div>
  </div>
</body>
</html>
"""

    def _render_generated_sites_index(self, viewer: str | None) -> str:
        """Cards for the signed-in person's pages only; a sign-in hint without a session."""

        if self.published_sites is None:
            site_cards = '<div class="note-panel">Generated-site hosting is not configured for this service.</div>'
        elif viewer is None:
            site_cards = (
                f'<div class="note-panel">Sign in to <a href="{SITE_CHAT_SIGN_IN_PATH}">Web Chat</a> in this browser to see '
                "the pages made for you. Page links shared in chat open without signing in.</div>"
            )
        else:
            site_cards = self._render_generated_site_cards(self.published_sites.records_for(viewer))
        return f"""
        <section class="content-panel hero">
          <div class="content">
            <p class="eyebrow">Generated sites</p>
            <h1>Single-page websites generated by the agent.</h1>
            <p class="lede">
              These pages are hosted by the same user-facing web service as the runtime docs.
              Articles, comparisons, how-to pages, and interactive apps can include an embedded chat panel when this service is attached to the gateway.
              This list shows only the pages made for you; anyone who has a page's link can open that page.
            </p>
          </div>
        </section>
        <section id="available-sites" class="content-panel">
          <div class="content">
            <h2>Your Sites</h2>
            <div class="card-grid" aria-label="Generated sites">{site_cards}</div>
          </div>
        </section>
"""

    @staticmethod
    def _render_generated_site_cards(records: list[PublishedSiteRecordV1]) -> str:
        if not records:
            return '<div class="note-panel">No pages have been made for you yet.</div>'
        return "\n".join(
            f"""<a class="link-card" href="{DOCS_ROUTE_PREFIX}/sites/{html.escape(record.site_id)}/">
                <span>
                  <h3>{html.escape(record.title)}</h3>
                  <p>Generated single-page website.</p>
                </span>
                <span class="arrow" aria-hidden="true">-&gt;</span>
              </a>"""
            for record in records
        )

    def _render_skill_cards(
        self,
        skills: list[SkillDoc],
        *,
        empty_message: str,
        source: str,
    ) -> str:
        if skills:
            source_query = f"?source={source}" if source in {"managed", "bundled"} else ""
            return "\n".join(
                f"""<a class="link-card" href="{skill.docs_path}{source_query}" aria-label="{html.escape(f"Open {skill.name} skill documentation")}">
                <span>
                  <h3>{html.escape(self._truncate_card_text(skill.name, SKILL_CARD_NAME_MAX_CHARS))}</h3>
                  <p>{html.escape(self._truncate_card_text(skill.description or SKILL_CARD_DEFAULT_DESCRIPTION, SKILL_CARD_DESCRIPTION_MAX_CHARS))}</p>
                </span>
                <span class="arrow" aria-hidden="true">-&gt;</span>
              </a>"""
                for skill in skills
            )
        return f'<div class="note-panel">{html.escape(empty_message)}</div>'

    def _render_skills_index(
        self,
        workspace_skills: list[SkillDoc],
        managed_skills: list[SkillDoc],
        bundled_skills: list[SkillDoc],
    ) -> str:
        bundled_cards = self._render_skill_cards(
            bundled_skills,
            empty_message="No built-in runtime guides are available in this installation.",
            source="bundled",
        )
        workspace_cards = self._render_skill_cards(
            workspace_skills,
            empty_message="No workspace skills were found in the configured skills directory.",
            source="workspace",
        )
        managed_cards = self._render_skill_cards(
            managed_skills,
            empty_message="No attested managed skills are currently installed.",
            source="managed",
        )
        return f"""
        <section class="content-panel hero">
          <div class="content">
            <p class="eyebrow">Dynamic skill docs</p>
            <h1>Installed skills, regenerated from disk.</h1>
            <p class="lede">
              These pages include workspace skills, reviewed installations, and built-in runtime guides.
              Use them when a workflow, tool, provider, or credentialed service may need user-facing setup context.
            </p>
          </div>
        </section>
        <section id="workspace-skills" class="content-panel">
          <div class="content">
            <h2>Workspace Skills</h2>
            <p>Operator-managed and startup-refreshed skills from the configured workspace skills directory.</p>
            <div class="card-grid" aria-label="Workspace skills">{workspace_cards}</div>
          </div>
        </section>
        <section id="managed-installed-skills" class="content-panel">
          <div class="content">
            <h2>Managed Installed Skills</h2>
            <p>Reviewed skills installed into protected runtime storage and verified against their installation attestations.</p>
            <div class="card-grid" aria-label="Managed installed skills">{managed_cards}</div>
          </div>
        </section>
        <section id="built-in-runtime-skills" class="content-panel">
          <div class="content">
            <h2>Built-in runtime skills</h2>
            <p>Instructions shipped with Universal Agents. Reading a guide does not grant tool access or mean the skill is enabled for every agent. A configured skill with the same name takes precedence at runtime.</p>
            <div class="card-grid" aria-label="Built-in runtime skills">{bundled_cards}</div>
          </div>
        </section>
"""

    @staticmethod
    def _truncate_card_text(value: str, max_chars: int) -> str:
        normalized = " ".join(value.split())
        if len(normalized) <= max_chars:
            return normalized

        prefix = normalized[: max_chars - 1].rstrip()
        final_space = prefix.rfind(" ")
        if final_space >= (max_chars - 1) // 2:
            prefix = prefix[:final_space].rstrip()
        return f"{prefix}…"

    def _render_skill_detail(self, skill: SkillDoc, *, source_label: str) -> str:
        return f"""
        <section class="content-panel hero">
          <div class="content">
            <p class="eyebrow">Skill documentation</p>
            <h1>{html.escape(skill.name)}</h1>
            <p class="lede">{html.escape(skill.description or "Skill instructions generated from SKILL.md.")}</p>
            <div class="meta-grid">
              <div><span>Source category</span><code>{html.escape(source_label)}</code></div>
              <div><span>Skill source</span><code>{html.escape(skill.skill_root.name + "/SKILL.md")}</code></div>
              <div><span>Docs route</span><code>{html.escape(skill.docs_path)}</code></div>
            </div>
          </div>
        </section>
        <section id="instructions" class="content-panel">
          <div class="content markdown-body">
            <h2>Instructions</h2>
            {markdown_to_html(skill.instructions_markdown)}
          </div>
        </section>
"""
