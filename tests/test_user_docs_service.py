from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from custom_agents.skill_repository import build_skill_tree_manifest
from user_docs.published_sites import (
    SITE_INDEX_FILENAME,
    PublishedSiteRecordV1,
    PublishedSiteStore,
    site_folder_parts,
)
from user_docs.skill_docs import find_skill_doc, list_skill_docs
from user_docs.service import (
    SKILL_CARD_DESCRIPTION_MAX_CHARS,
    SKILL_CARD_NAME_MAX_CHARS,
    UserDocsService,
)


def _build_docs_site(tmp_path: Path) -> Path:
    site_dir = tmp_path / "site"
    subprocess.run(
        [
            sys.executable,
            "scripts/build_user_docs.py",
            "--site-dir",
            str(site_dir),
        ],
        check=True,
    )
    return site_dir


def _write_attested_managed_skill(
    *,
    managed_root: Path,
    attestations_root: Path,
    directory_name: str,
    skill_name: str,
    description: str,
) -> Path:
    managed_skill = managed_root / directory_name
    managed_skill.mkdir(parents=True)
    attestations_root.mkdir(parents=True, exist_ok=True)
    (managed_skill / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: {description}\n---\n\n# Managed\n",
        encoding="utf-8",
    )
    manifest = build_skill_tree_manifest(managed_skill)
    (attestations_root / f"{directory_name}.json").write_text(
        json.dumps(
            {
                "skill_name": directory_name,
                "installed_path": str(managed_skill.resolve()),
                "tree_sha256": manifest.tree_sha256,
            }
        ),
        encoding="utf-8",
    )
    return managed_skill


@pytest.mark.asyncio
async def test_user_docs_service_serves_static_mkdocs_pages(tmp_path: Path) -> None:
    site_dir = _build_docs_site(tmp_path)
    service = UserDocsService(docs_root=site_dir)
    async with TestClient(TestServer(service.build_app())) as client:
        root_response = await client.get("/", allow_redirects=False)
        docs_redirect_response = await client.get("/docs", allow_redirects=False)
        gh_no_slash_response = await client.get("/docs/auth/gh", allow_redirects=False)
        bw_no_slash_response = await client.get("/docs/auth/bw", allow_redirects=False)
        gog_no_slash_response = await client.get("/docs/auth/gog", allow_redirects=False)
        newuser_telegram_no_slash_response = await client.get("/docs/newuser/telegram", allow_redirects=False)
        newuser_discord_no_slash_response = await client.get("/docs/newuser/discord", allow_redirects=False)
        index_response = await client.get("/docs/")
        quickstart_response = await client.get("/docs/quickstart")
        model_providers_response = await client.get("/docs/model-providers")
        onboarding_response = await client.get("/docs/onboarding")
        docker_response = await client.get("/docs/docker")
        config_response = await client.get("/docs/configuration")
        telegram_response = await client.get("/docs/channels/telegram")
        whatsapp_response = await client.get("/docs/channels/whatsapp")
        discord_response = await client.get("/docs/channels/discord")
        signal_channel_response = await client.get("/docs/channels/signal")
        photon_channel_response = await client.get("/docs/channels/photon")
        newuser_response = await client.get("/docs/newuser/")
        newuser_telegram_response = await client.get("/docs/newuser/telegram")
        newuser_discord_response = await client.get("/docs/newuser/discord")
        newuser_photon_response = await client.get("/docs/newuser/photon")
        tools_response = await client.get("/docs/tools")
        auth_response = await client.get("/docs/auth/")
        gh_response = await client.get("/docs/auth/gh")
        github_alias_response = await client.get("/docs/auth/github")
        bw_response = await client.get("/docs/auth/bw")
        bitwarden_alias_response = await client.get("/docs/auth/bitwarden")
        gog_response = await client.get("/docs/auth/gog")
        google_alias_response = await client.get("/docs/auth/google")
        onepassword_response = await client.get("/docs/auth/onepassword")
        keepass_response = await client.get("/docs/auth/keepass")
        vault_response = await client.get("/docs/auth/vault")
        css_response = await client.get("/docs/assets/extra.css")
        dynamic_css_response = await client.get("/docs/assets/styles.css")
        runtime_css_response = await client.get("/docs/assets/ua-runtime-theme.css")
        mesh_script_response = await client.get("/docs/assets/static-mesh.js")
        appearance_script_response = await client.get("/docs/assets/appearance.js")
        metallic_mesh_response = await client.get("/docs/assets/metallic_mesh.js")
        markdown_script_response = await client.get("/docs/assets/chat_markdown.js")
        operational_ids_response = await client.get("/docs/assets/operational_ids.js")
        appearance_script_text = await appearance_script_response.text()
        metallic_mesh_text = await metallic_mesh_response.text()

        index_text = await index_response.text()
        quickstart_text = await quickstart_response.text()
        model_providers_text = await model_providers_response.text()
        onboarding_text = await onboarding_response.text()
        docker_text = await docker_response.text()
        config_text = await config_response.text()
        telegram_text = await telegram_response.text()
        whatsapp_text = await whatsapp_response.text()
        discord_text = await discord_response.text()
        signal_channel_text = await signal_channel_response.text()
        photon_channel_text = await photon_channel_response.text()
        newuser_text = await newuser_response.text()
        newuser_telegram_text = await newuser_telegram_response.text()
        newuser_discord_text = await newuser_discord_response.text()
        newuser_photon_text = await newuser_photon_response.text()
        tools_text = await tools_response.text()
        auth_text = await auth_response.text()
        gh_text = await gh_response.text()
        bw_text = await bw_response.text()
        gog_text = await gog_response.text()
        onepassword_text = await onepassword_response.text()
        keepass_text = await keepass_response.text()
        vault_text = await vault_response.text()
        css_text = await css_response.text()
        dynamic_css_text = await dynamic_css_response.text()
        runtime_css_text = await runtime_css_response.text()
        mesh_script_text = await mesh_script_response.text()
        markdown_script_text = await markdown_script_response.text()
        operational_ids_text = await operational_ids_response.text()

    assert root_response.status == 301
    assert index_text.index('src="assets/appearance.js"') < index_text.index("<body")
    assert root_response.headers["Location"] == "/docs/"
    assert docs_redirect_response.status == 301
    assert docs_redirect_response.headers["Location"] == "/docs/"
    assert gh_no_slash_response.status == 301
    assert gh_no_slash_response.headers["Location"] == "/docs/auth/gh/"
    assert bw_no_slash_response.status == 301
    assert bw_no_slash_response.headers["Location"] == "/docs/auth/bw/"
    assert gog_no_slash_response.status == 301
    assert gog_no_slash_response.headers["Location"] == "/docs/auth/gog/"
    assert newuser_telegram_no_slash_response.status == 301
    assert newuser_telegram_no_slash_response.headers["Location"] == "/docs/newuser/telegram/"
    assert newuser_discord_no_slash_response.status == 301
    assert newuser_discord_no_slash_response.headers["Location"] == "/docs/newuser/discord/"
    assert index_response.status == 200
    assert "Universal Agents" in index_text
    assert 'href="/webchat"' in index_text
    assert "Model Providers" in index_text
    assert quickstart_response.status == 200
    assert "install.sh" in quickstart_text
    assert "127.0.0.1:18090" in quickstart_text
    assert "/tour" in quickstart_text
    assert model_providers_response.status == 200
    assert "xAI Grok OAuth" in model_providers_text
    assert "grok-4.5" in model_providers_text
    assert "xai-oauth" in model_providers_text
    assert "Model Keys and" in model_providers_text
    assert onboarding_response.status == 200
    assert "Interactive Onboarding" in onboarding_text
    assert 'href="../quickstart/"' in onboarding_text
    assert 'href="/docker-compose-setup.html"' in onboarding_text
    assert "Colima" in onboarding_text
    assert docker_response.status == 200
    assert "Static Docs Build" in docker_text
    assert "host port" in docker_text
    assert config_response.status == 200
    assert "instance_config.yaml" in config_text
    assert "Message Broker" in config_text
    assert "virtual host" not in config_text
    assert telegram_response.status == 200
    assert "BotFather" in telegram_text
    assert whatsapp_response.status == 200
    assert "Twilio" in whatsapp_text
    assert discord_response.status == 200
    assert "Discord Developer Portal" in discord_text
    assert signal_channel_response.status == 200
    assert "signal-cli-rest-api" in signal_channel_text
    assert photon_channel_response.status == 200
    assert "PHOTON_PROJECT_ID" in photon_channel_text
    assert newuser_response.status == 200
    assert "external_user_id" in newuser_text
    assert "gateway_users" in newuser_text
    assert "/user invite --new" in newuser_text
    assert "/user token" in newuser_text
    assert "always creates a token for the identity that runs it" in newuser_text
    assert "one-time proof" in newuser_text
    assert "It is not a Web Chat token" in newuser_text
    assert "/user link AbCdEf1234 --replace" in newuser_text
    assert "Finish Setting Up Web Chat" in newuser_text
    assert "pending" in newuser_text
    assert newuser_telegram_response.status == 200
    assert "numeric Telegram user id" in newuser_telegram_text
    assert "newuser_target" in newuser_telegram_text
    assert newuser_discord_response.status == 200
    assert "numeric Discord user id" in newuser_discord_text
    assert "newuser_target" in newuser_discord_text
    assert newuser_photon_response.status == 200
    assert "Photon/iMessage onboarding uses Photon" in newuser_photon_text
    assert "sender.id" in newuser_photon_text
    assert tools_response.status == 200
    assert "secure tool broker" in tools_text
    assert auth_response.status == 200
    assert "What Is Sent to the Agent" in auth_text
    assert gh_response.status == 200
    assert github_alias_response.status == 404
    assert "/link cli:github web" in gh_text
    assert "/auth github" not in gh_text
    assert "github.auth.logout" in gh_text
    assert bw_response.status == 200
    assert bitwarden_alias_response.status == 404
    assert "broker-side API key login and unlock" in bw_text
    assert "bw unlock --passwordenv BW_PASSWORD --raw" in bw_text
    assert gog_response.status == 200
    assert google_alias_response.status == 404
    assert "/link cli:google" in gog_text
    assert "/auth google team_client" in gog_text
    assert "gog auth list --check" in gog_text
    assert "GOG_KEYRING_PASSWORD" in gog_text
    # A person's own Google sign-in app is pasted on the sign-in page; the
    # separate /auth google credentials command was removed.
    assert "/auth google credentials" not in gog_text
    assert "Create Credentials" in gog_text
    assert "Download JSON" in gog_text
    assert "agent-credential-store :: github :: token" in bw_text
    assert onepassword_response.status == 200
    assert "OP_SERVICE_ACCOUNT_TOKEN_FILE" in onepassword_text
    assert "OP_CONFIG_DIR" in onepassword_text
    assert "will not run" in onepassword_text
    assert "add an account manually" in onepassword_text
    assert keepass_response.status == 200
    assert "KEEPASSXC_DATABASE" in keepass_text
    assert vault_response.status == 200
    assert "VAULT_ADDR" in vault_text
    assert "agent-credential-store" in vault_text
    assert "credential_state_backend" in config_text
    assert css_response.status == 200
    assert "frosted" in css_text or "--ua-doc-panel" in css_text
    assert "background-attachment: fixed" in css_text
    assert "--ua-doc-layout-width: 112rem" in css_text
    assert "max-width: min(var(--ua-doc-layout-width), calc(100vw - 2rem))" in css_text
    assert dynamic_css_response.status == 200
    assert "--content-width: 78rem" in dynamic_css_text
    assert "--layout-width: 112rem" in dynamic_css_text
    assert ".link-card > span:first-child" in dynamic_css_text
    assert "overflow-wrap: anywhere" in dynamic_css_text
    assert "-webkit-line-clamp: 2" in dynamic_css_text
    assert "-webkit-line-clamp: 6" in dynamic_css_text
    assert runtime_css_response.status == 200
    assert "--ua-doc-light-blue" in runtime_css_text
    assert "--ua-light-mesh-seed: 4147" in runtime_css_text
    assert "--ua-runtime-color-mode: dark" in runtime_css_text
    assert mesh_script_response.status == 200
    assert "createTriangles" in mesh_script_text
    assert "UA_DOC_STATIC_MESH" in mesh_script_text
    assert "renderedKey" in mesh_script_text
    assert appearance_script_response.status == 200
    assert "window.UAAppearance" in appearance_script_text
    assert metallic_mesh_response.status == 200
    assert "window.UAMetallicMesh" in metallic_mesh_text
    assert markdown_script_response.status == 200
    assert "UAWebChatMarkdown" in markdown_script_text
    assert operational_ids_response.status == 200
    assert "UAOperationalIds" in operational_ids_text


@pytest.mark.asyncio
async def test_user_docs_service_reports_health() -> None:
    service = UserDocsService()
    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.get("/health")
        payload = await response.json()

    assert response.status == 200
    assert payload["status"] == "ok"
    assert payload["sections"] == ["onboarding", "docker", "configuration", "channels", "auth", "tools", "skills", "sites"]
    assert "skills_dir" in payload
    assert payload["managed_skills_dir"] is None
    assert "generated_site_records_dir" in payload


@pytest.mark.asyncio
async def test_user_docs_service_generates_skill_docs_from_current_files(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: demo-skill
description: Demo skill for user-facing generated documentation.
---

# Demo Skill

Use this skill when a user needs a demo.

## Setup

- Run `demo auth`
- Confirm the browser prompt
""",
        encoding="utf-8",
    )

    service = UserDocsService(skills_dir=skill_root)
    async with TestClient(TestServer(service.build_app())) as client:
        index_response = await client.get("/docs/skills/")
        detail_response = await client.get("/docs/skills/demo-skill")
        missing_response = await client.get("/docs/skills/missing-skill")

        index_text = await index_response.text()
        detail_text = await detail_response.text()

    assert index_response.status == 200
    assert "Installed skills, regenerated from disk" in index_text
    assert "demo-skill" in index_text
    assert "Demo skill for user-facing generated documentation" in index_text
    assert "Table of contents" in index_text
    assert "top-tabs" not in index_text
    assert 'href="/webchat"' in index_text
    assert "Agent Features" in index_text
    assert detail_response.status == 200
    assert "/docs/assets/static-mesh.js" in detail_text
    assert "Demo Skill" in detail_text
    assert "demo auth" in detail_text
    assert "Confirm the browser prompt" in detail_text
    assert missing_response.status == 404


@pytest.mark.asyncio
async def test_user_docs_service_separates_attested_managed_skills(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace-skills"
    workspace_skill = workspace_root / "workspace-demo"
    workspace_skill.mkdir(parents=True)
    (workspace_skill / "SKILL.md").write_text(
        "---\nname: workspace-demo\ndescription: Workspace source.\n---\n\n# Workspace\n",
        encoding="utf-8",
    )

    managed_root = tmp_path / "runtime" / "skill-installation" / "installed"
    attestations_root = tmp_path / "runtime" / "skill-installation" / "attestations"
    _write_attested_managed_skill(
        managed_root=managed_root,
        attestations_root=attestations_root,
        directory_name="managed-demo",
        skill_name="managed-demo",
        description="Reviewed source.",
    )

    service = UserDocsService(
        skills_dir=workspace_root,
        managed_skills_dir=managed_root,
        managed_skill_attestations_dir=attestations_root,
    )
    async with TestClient(TestServer(service.build_app())) as client:
        index_response = await client.get("/docs/skills/")
        managed_response = await client.get("/docs/skills/managed-demo?source=managed")
        index_text = await index_response.text()
        managed_text = await managed_response.text()

    assert index_response.status == 200
    assert '<section id="workspace-skills"' in index_text
    assert '<section id="managed-installed-skills"' in index_text
    assert "workspace-demo" in index_text
    assert "managed-demo" in index_text
    assert 'href="/docs/skills/managed-demo?source=managed"' in index_text
    assert "Managed installed skill" in managed_text
    assert "Reviewed source." in managed_text


@pytest.mark.asyncio
async def test_user_docs_service_keeps_same_name_sources_independently_visible(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace-skills"
    workspace_skill = workspace_root / "shared-demo"
    workspace_skill.mkdir(parents=True)
    (workspace_skill / "SKILL.md").write_text(
        "---\nname: shared-demo\ndescription: Workspace version.\n---\n\n# Workspace\n",
        encoding="utf-8",
    )
    managed_root = tmp_path / "runtime" / "skill-installation" / "installed"
    attestations_root = tmp_path / "runtime" / "skill-installation" / "attestations"
    _write_attested_managed_skill(
        managed_root=managed_root,
        attestations_root=attestations_root,
        directory_name="shared-demo",
        skill_name="shared-demo",
        description="Managed version.",
    )

    service = UserDocsService(
        skills_dir=workspace_root,
        managed_skills_dir=managed_root,
        managed_skill_attestations_dir=attestations_root,
    )
    async with TestClient(TestServer(service.build_app())) as client:
        index_response = await client.get("/docs/skills/")
        workspace_response = await client.get("/docs/skills/shared-demo")
        managed_response = await client.get(
            "/docs/skills/shared-demo?source=managed"
        )
        index_text = await index_response.text()
        workspace_text = await workspace_response.text()
        managed_text = await managed_response.text()

    assert index_text.count("shared-demo") >= 2
    assert 'href="/docs/skills/shared-demo?source=managed"' in index_text
    assert "Workspace version." in workspace_text
    assert "Workspace skill" in workspace_text
    assert "Managed version." in managed_text
    assert "Managed installed skill" in managed_text


@pytest.mark.asyncio
async def test_user_docs_service_omits_managed_skill_when_attestation_drifts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    managed_root = tmp_path / "runtime" / "skill-installation" / "installed"
    attestations_root = tmp_path / "runtime" / "skill-installation" / "attestations"
    managed_skill = _write_attested_managed_skill(
        managed_root=managed_root,
        attestations_root=attestations_root,
        directory_name="drifted-demo",
        skill_name="drifted-demo",
        description="Original.",
    )
    skill_file = managed_skill / "SKILL.md"
    skill_file.write_text(
        "---\nname: drifted-demo\ndescription: Changed.\n---\n\n# Changed\n",
        encoding="utf-8",
    )

    service = UserDocsService(
        skills_dir=tmp_path / "workspace-skills",
        managed_skills_dir=managed_root,
        managed_skill_attestations_dir=attestations_root,
    )
    with caplog.at_level("WARNING", logger="user_docs.service"):
        async with TestClient(TestServer(service.build_app())) as client:
            index_response = await client.get("/docs/skills/")
            detail_response = await client.get("/docs/skills/drifted-demo")
            index_text = await index_response.text()

    assert index_response.status == 200
    assert detail_response.status == 404
    assert "drifted-demo" not in index_text
    assert "managed_skill_docs_integrity_failed" in caplog.text


@pytest.mark.asyncio
async def test_user_docs_service_caps_skill_card_text_without_changing_detail_content(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "long-skill"
    skill_dir.mkdir(parents=True)
    skill_name = "skill-" + ("n" * 80)
    skill_description = "A detailed workflow " + ("description " * 30)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {skill_name}
description: {skill_description}
---

# Complete Instructions
""",
        encoding="utf-8",
    )

    service = UserDocsService(skills_dir=skill_root)
    async with TestClient(TestServer(service.build_app())) as client:
        index_response = await client.get("/docs/skills/")
        detail_response = await client.get(f"/docs/skills/{skill_name}")
        index_text = await index_response.text()
        detail_text = await detail_response.text()

    displayed_name = service._truncate_card_text(skill_name, SKILL_CARD_NAME_MAX_CHARS)
    displayed_description = service._truncate_card_text(
        skill_description,
        SKILL_CARD_DESCRIPTION_MAX_CHARS,
    )

    assert index_response.status == 200
    assert f"<h3>{displayed_name}</h3>" in index_text
    assert f"<p>{displayed_description}</p>" in index_text
    assert len(displayed_name) <= SKILL_CARD_NAME_MAX_CHARS
    assert len(displayed_description) <= SKILL_CARD_DESCRIPTION_MAX_CHARS
    assert displayed_name.endswith("…")
    assert displayed_description.endswith("…")
    assert f'aria-label="Open {skill_name} skill documentation"' in index_text
    assert f'href="/docs/skills/{skill_name}"' in index_text
    assert detail_response.status == 200
    assert f"<h1>{skill_name}</h1>" in detail_text
    assert f'<p class="lede">{skill_description.strip()}</p>' in detail_text


def test_skill_docs_deduplicate_physical_aliases_and_semantic_slugs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    skill_root = tmp_path / "skills"
    canonical_dir = skill_root / "canonical"
    canonical_dir.mkdir(parents=True)
    (canonical_dir / "SKILL.md").write_text(
        "---\nname: Duplicate Skill\ndescription: Canonical.\n---\n\n# Canonical\n",
        encoding="utf-8",
    )
    (skill_root / "physical-alias").symlink_to(canonical_dir, target_is_directory=True)
    semantic_dir = skill_root / "semantic-duplicate"
    semantic_dir.mkdir()
    (semantic_dir / "SKILL.md").write_text(
        "---\nname: duplicate-skill\ndescription: Duplicate.\n---\n\n# Duplicate\n",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="user_docs.skill_docs"):
        docs = list_skill_docs(skill_root)

    assert [(doc.slug, doc.description) for doc in docs] == [
        ("duplicate-skill", "Canonical."),
    ]
    assert "duplicate physical skill file alias" in caplog.text
    assert "duplicate semantic skill slug" in caplog.text


def _publish(store: PublishedSiteStore, site_id: str, owner: str | None, title: str) -> Path:
    """Write a page and its record the way publish_single_page_site does."""

    parts = site_folder_parts(owner, site_id)
    digests = store.write_files(parts, {SITE_INDEX_FILENAME: f"<!doctype html><title>{title}</title>".encode()})
    store.save(
        PublishedSiteRecordV1(
            site_id=site_id,
            folder="/".join(parts),
            owner_person_id=owner,
            title=title,
            site_kind="article",
            created_at="2026-09-25T00:00:00+00:00",
            updated_at="2026-09-25T00:00:00+00:00",
            files=digests,
        )
    )
    return store.workspaces_dir.joinpath(*parts)


@pytest.mark.asyncio
async def test_user_docs_service_serves_generated_sites_and_optional_chat(tmp_path: Path) -> None:
    store = PublishedSiteStore(records_dir=tmp_path / "state" / "published_sites", workspaces_dir=tmp_path / "workspaces")
    _publish(store, "demo-site-1a2b3c4d", "alice", "Generated Demo")
    _publish(store, "bob-site-5e6f7a8b", "bob", "Bob Private Plan")
    # A folder without a record (as an agent could create) is never served.
    planted = tmp_path / "workspaces" / "users" / "alice" / "sites" / "planted"
    planted.mkdir(parents=True)
    (planted / SITE_INDEX_FILENAME).write_text("<p>planted</p>", encoding="utf-8")

    async def fake_chat(_request):
        return web.json_response({"reply": "ok"})

    def viewer(request: web.Request) -> str | None:
        return request.headers.get("X-Test-Person")

    service = UserDocsService(
        published_sites=store,
        generated_site_chat_handler=fake_chat,
        generated_site_viewer=viewer,
        service_base_url="http://127.0.0.1:18500",
    )
    async with TestClient(TestServer(service.build_app())) as client:
        anonymous_index = await (await client.get("/docs/sites/")).text()
        alice_index_response = await client.get("/docs/sites/", headers={"X-Test-Person": "alice"})
        alice_index = await alice_index_response.text()
        site_response = await client.get("/docs/sites/demo-site-1a2b3c4d/")
        site_text = await site_response.text()
        planted_response = await client.get("/docs/sites/planted/")
        chat_response = await client.post("/api/sites/demo-site-1a2b3c4d/chat", json={"message": "hello"})
        chat_payload = await chat_response.json()
        missing_chat = await client.post("/api/sites/planted/chat", json={"message": "hello"})
        missing_response = await client.get("/docs/sites/bad!id/")

    assert "Sign in to" in anonymous_index
    assert "Generated Demo" not in anonymous_index
    assert alice_index_response.status == 200
    assert "<h3>Generated Demo</h3>" in alice_index
    assert 'href="/docs/sites/demo-site-1a2b3c4d/"' in alice_index
    assert "Bob Private Plan" not in alice_index
    assert "Table of contents" in alice_index
    assert "top-tabs" not in alice_index
    assert site_response.status == 200
    assert "Generated Demo" in site_text
    assert planted_response.status == 404
    assert chat_response.status == 200
    assert chat_payload["reply"] == "ok"
    assert missing_chat.status == 404
    assert missing_response.status == 404

    service_without_chat = UserDocsService(published_sites=store)
    async with TestClient(TestServer(service_without_chat.build_app())) as client:
        no_chat_response = await client.post("/api/sites/demo-site-1a2b3c4d/chat", json={"message": "hello"})

    assert no_chat_response.status == 503


@pytest.mark.asyncio
async def test_generated_site_is_not_served_after_its_file_changes_or_becomes_a_link(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = PublishedSiteStore(records_dir=tmp_path / "state" / "published_sites", workspaces_dir=tmp_path / "workspaces")
    site_dir = _publish(store, "guide-0a1b2c3d", "alice", "Guide")
    secret = tmp_path / "secret.txt"
    secret.write_text("control file", encoding="utf-8")
    service = UserDocsService(published_sites=store)
    page = site_dir / SITE_INDEX_FILENAME

    async with TestClient(TestServer(service.build_app())) as client:
        page.write_text("<script>changed()</script>", encoding="utf-8")
        with caplog.at_level("WARNING"):
            changed = await client.get("/docs/sites/guide-0a1b2c3d/")
        page.unlink()
        page.symlink_to(secret)
        linked = await client.get("/docs/sites/guide-0a1b2c3d/")
        linked_text = await linked.text()

    assert changed.status == 404
    assert "event=generated_site_changed site_id=guide-0a1b2c3d" in caplog.text
    assert linked.status == 404
    assert "control file" not in linked_text


def test_a_site_record_cannot_point_outside_its_owners_sites_folder() -> None:
    with pytest.raises(ValueError):
        PublishedSiteRecordV1(
            site_id="guide",
            folder="users/bob/sites/guide",
            owner_person_id="alice",
            title="Guide",
            site_kind="article",
            created_at="2026-09-25T00:00:00+00:00",
            updated_at="2026-09-25T00:00:00+00:00",
            files={SITE_INDEX_FILENAME: "0" * 64},
        )


def test_gog_auth_doc_describes_primary_durable_web_flow() -> None:
    instructions = Path("user_docs_source/auth/gog.md").read_text(encoding="utf-8")

    assert "/link cli:google" in instructions
    assert "main way to sign in to Google" in instructions
    assert "In Production" in instructions
    assert "7 days" in instructions
    assert "/link cli:google json_token" in instructions
    assert "/link cli:google token_json" not in instructions
    # Only the administrators' team_client command stays under /auth google.
    assert "/auth google team_client" in instructions
    for removed in ("/auth google credentials", "/auth google json_token", "/auth google web"):
        assert removed not in instructions


def test_current_bw_skill_doc_is_discoverable() -> None:
    doc = find_skill_doc("bw", skills_dir=Path(".skills"))
    if doc is None:
        pytest.skip("Optional local .skills/bw checkout is not present.")

    assert doc.name == "bw"
    assert "secure_cli" in doc.instructions_markdown
    assert "credential.search" in doc.instructions_markdown
    assert "/auth bitwarden" in doc.instructions_markdown
    assert "/auth bitwarden server" in doc.instructions_markdown
    assert "Do not ask the user to exec into the broker container" in doc.instructions_markdown


@pytest.mark.asyncio
async def test_bundled_skill_docs_keep_workspace_override_and_explicit_source(tmp_path):
    root = tmp_path / "skills"
    skill = root / "runtime-operations-workflows"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: runtime-operations-workflows\ndescription: Workspace override\n---\nWorkspace-only instructions."
    )
    service = UserDocsService(skills_dir=root)
    async with TestClient(TestServer(service.build_app())) as client:
        default = await client.get("/docs/skills/runtime-operations-workflows")
        assert "Workspace-only instructions" in await default.text()
        bundled = await client.get(
            "/docs/skills/runtime-operations-workflows?source=bundled"
        )
        text = await bundled.text()
        assert bundled.status == 200
        assert "Built-in runtime skill" in text
        assert "submit_access_workflow" in text
        assert "Workspace-only instructions" not in text
        absent = await client.get(
            "/docs/skills/runtime-operations-workflows?source=managed"
        )
        assert absent.status == 404
