"""Initial secure tool provider declarations.

These provider classes define the stable action surface. The credential-owning
broker process is responsible for executing them; agent processes call the
broker through ``secure_cli`` rather than executing these locally.
"""

from __future__ import annotations

from typing import Any, Mapping

from secure_tools.providers import ActionSpec, ToolContext, ToolResult, ValidatedAction
from secure_tools.registry import register_provider
from secure_tools.hub_providers import hub_cli_providers


class DeclaredProvider:
    name: str
    actions: Mapping[str, ActionSpec]
    action_aliases: Mapping[str, str] = {}

    def _valid_action_names(self) -> str:
        return ", ".join(sorted(self.actions))

    def _normalize_action_name(self, action: str) -> str:
        action_name = str(action or "").strip()
        if action_name in self.actions:
            return action_name
        aliased = self.action_aliases.get(action_name)
        if aliased:
            return aliased
        dotted = action_name.replace(" ", ".").replace("/", ".")
        return self.action_aliases.get(dotted, dotted)

    def _unsupported_action_guidance(self, supplied_action: str) -> str:
        if self.name == "github" and str(supplied_action or "").strip().lower().replace("_", ".").startswith("project."):
            return (
                "Use action='project.item.list' for GitHub Projects v2 item listing. "
                "For other Projects queries and mutations, use action='api.post' with "
                "params={'path': 'graphql', 'body': {...}}; do not retry raw `gh project ...` "
                "through shell or argv-style secure_cli params. "
            )
        return ""

    def validate(self, action: str, params: dict[str, Any]) -> ValidatedAction:
        supplied_action = str(action or "").strip()
        action_name = self._normalize_action_name(supplied_action)
        if action_name not in self.actions:
            if self.name == "github" and supplied_action in {"auth_login", "auth.login", "login"}:
                raise ValueError(
                    "Interactive GitHub login is not supported through secure_cli. "
                    "Credentials must be configured in the broker process. "
                    f"Use provider='github' action='auth.status' to check broker-side gh authentication. "
                    f"Valid {self.name} actions: {self._valid_action_names()}"
                )
            raise ValueError(
                f"Unsupported {self.name} action: {supplied_action or '<empty>'}. "
                f"{self._unsupported_action_guidance(supplied_action)}"
                f"Valid {self.name} actions: {self._valid_action_names()}"
            )
        if self.actions[action_name].returns_secret:
            raise ValueError(f"{self.name}.{action_name} is not allowed to return raw secrets")
        return ValidatedAction(provider=self.name, action=action_name, params=dict(params or {}))

    def execute(self, action: ValidatedAction, context: ToolContext) -> ToolResult:
        return ToolResult(
            status="error",
            error="Provider execution is broker-owned and is not available in the agent process.",
        )


class GitHubProvider(DeclaredProvider):
    name = "github"
    actions = {
        "gh.exec": ActionSpec(
            "gh.exec",
            "Broker-policy-checked gh argv passthrough for non-auth, non-config command paths.",
        ),
        "auth.status": ActionSpec("auth.status", "Check broker-side gh authentication status; optional hostname."),
        "auth.logout": ActionSpec(
            "auth.logout",
            "Log the broker-owned gh CLI out locally; optional hostname and user.",
        ),
        "auth.login.credential": ActionSpec(
            "auth.login.credential",
            "Broker-only GitHub login using a secret fetched from a credential provider.",
        ),
        "repo.view": ActionSpec("repo.view"),
        "issue.list": ActionSpec("issue.list"),
        "issue.view": ActionSpec("issue.view"),
        "issue.create": ActionSpec("issue.create"),
        "issue.comment": ActionSpec("issue.comment"),
        "pr.list": ActionSpec("pr.list"),
        "pr.view": ActionSpec("pr.view"),
        "pr.create": ActionSpec("pr.create"),
        "pr.comment": ActionSpec("pr.comment"),
        "workflow.run.list": ActionSpec("workflow.run.list"),
        "project.item.list": ActionSpec(
            "project.item.list",
            "List GitHub Projects v2 items through broker-owned `gh project item-list`; params: owner, number/project_number, optional limit.",
        ),
        "api.get": ActionSpec("api.get"),
        "api.post": ActionSpec("api.post"),
    }
    action_aliases = {
        "auth_status": "auth.status",
        "auth_logout": "auth.logout",
        "auth logout": "auth.logout",
        "auth_login_credential": "auth.login.credential",
        "auth login credential": "auth.login.credential",
        "auth.login.bitwarden": "auth.login.credential",
        "auth_login_bitwarden": "auth.login.credential",
        "auth login bitwarden": "auth.login.credential",
        "gh_exec": "gh.exec",
        "gh exec": "gh.exec",
        "repo_view": "repo.view",
        "issue_list": "issue.list",
        "issue_view": "issue.view",
        "issue_create": "issue.create",
        "issue_comment": "issue.comment",
        "pr_list": "pr.list",
        "pr_view": "pr.view",
        "pr_create": "pr.create",
        "pr_comment": "pr.comment",
        "workflow_run_list": "workflow.run.list",
        "project.item.list": "project.item.list",
        "project.item-list": "project.item.list",
        "project_item_list": "project.item.list",
        "project item list": "project.item.list",
        "project item-list": "project.item.list",
        "project.item_list": "project.item.list",
        "api_get": "api.get",
        "api_post": "api.post",
    }


class GoogleProvider(DeclaredProvider):
    name = "google"
    actions = {
        "gog.exec": ActionSpec(
            "gog.exec",
            "Run a broker-policy-checked gog argv passthrough without exposing broker credentials.",
        ),
        "gmail_search": ActionSpec("gmail_search"),
        "gmail_read": ActionSpec("gmail_read"),
        "gmail_send": ActionSpec("gmail_send"),
        "calendar_list": ActionSpec("calendar_list"),
        "calendar_create": ActionSpec("calendar_create"),
        "drive_list": ActionSpec("drive_list"),
        "drive_export": ActionSpec("drive_export"),
        "docs_export": ActionSpec("docs_export"),
        "sheets_read": ActionSpec("sheets_read"),
    }
    action_aliases = {
        "gog_exec": "gog.exec",
        "gog exec": "gog.exec",
    }


class BitwardenProvider(DeclaredProvider):
    name = "bitwarden"
    actions = {
        "credential.search": ActionSpec(
            "credential.search",
            "Search live Bitwarden credential metadata without returning secret values.",
        ),
        "credential.get_metadata": ActionSpec(
            "credential.get_metadata",
            "Fetch metadata for one Bitwarden credential without returning secret values.",
        ),
        "credential.use_for_action": ActionSpec(
            "credential.use_for_action",
            "Use a named Bitwarden item for an explicit broker-owned service login automation.",
        ),
    }
    action_aliases = {
        "credential.list": "credential.search",
        "credential_list": "credential.search",
        "credentials.list": "credential.search",
        "item_search_metadata": "credential.search",
        "item_get_metadata": "credential.get_metadata",
        "use_secret_for_action": "credential.use_for_action",
        "use secret for action": "credential.use_for_action",
    }


class CredentialManagerProvider(DeclaredProvider):
    action_aliases = {
        "credential.list": "credential.search",
        "credential_list": "credential.search",
        "credentials.list": "credential.search",
        "item_search_metadata": "credential.search",
        "item_get_metadata": "credential.get_metadata",
        "use_secret_for_action": "credential.use_for_action",
        "use secret for action": "credential.use_for_action",
    }

    @staticmethod
    def _credential_actions(display_name: str) -> dict[str, ActionSpec]:
        return {
            "credential.search": ActionSpec(
                "credential.search",
                f"Search live {display_name} credential metadata without returning secret values.",
            ),
            "credential.get_metadata": ActionSpec(
                "credential.get_metadata",
                f"Fetch metadata for one {display_name} credential without returning secret values.",
            ),
            "credential.use_for_action": ActionSpec(
                "credential.use_for_action",
                f"Use a {display_name} credential for an explicit broker-owned service login automation.",
            ),
        }


class OnePasswordProvider(CredentialManagerProvider):
    name = "onepassword"
    actions = CredentialManagerProvider._credential_actions("1Password")


class KeepassProvider(CredentialManagerProvider):
    name = "keepass"
    actions = CredentialManagerProvider._credential_actions("KeePass/KeePassXC")


class VaultProvider(CredentialManagerProvider):
    name = "vault"
    actions = CredentialManagerProvider._credential_actions("HashiCorp Vault")


class SealboxProvider(CredentialManagerProvider):
    name = "sealbox"
    actions = {
        "credential.search": ActionSpec(
            "credential.search",
            "Search the current registered user's Sealbox tenant metadata with sealbox-cli credential list. Params: query searches keys/names and usernames; key or name filters by credential key/name substring; username filters plaintext username metadata; limit caps returned metadata. Owner selection comes from trusted runtime context, never params. Returns refs, keys, usernames, and password-field metadata only; never returns secret values.",
        ),
        "credential.get_metadata": ActionSpec(
            "credential.get_metadata",
            "Fetch metadata for one credential in the current user's Sealbox tenant by item_id or item_name/key without returning the password.",
        ),
        "credential.use_for_action": ActionSpec(
            "credential.use_for_action",
            "Use a Sealbox credential for an explicit broker-owned service login automation; plaintext is resolved only inside the broker.",
        ),
    }


class CredentialCatalogProvider(DeclaredProvider):
    name = "credential_catalog"
    actions = {
        "credential.collect": ActionSpec(
            "credential.collect",
            "Broker-owned create/update in the current user's catalog after values arrive from a secure broker form or store generation; does not create a user-facing form. Owner selection comes from trusted context. Defaults to write_source=sealbox, stores username/password pairs with sealbox-cli credential set, stores custom secret fields with sealbox-cli secret set, and records only catalog metadata/source refs.",
        ),
        "credential.get_metadata": ActionSpec(
            "credential.get_metadata",
            "Fetch the current user's catalog metadata for a named credential without returning secret values.",
        ),
        "credential.register_existing_source": ActionSpec(
            "credential.register_existing_source",
            "Register an existing broker-local env or *_FILE credential source without returning the secret value.",
        ),
        "credential.bind_for_openshell": ActionSpec(
            "credential.bind_for_openshell",
            "Give code in the OpenShell sandbox the environment variables of the account linked to a tool, for one "
            "use. Params: tool_id of a web API tool with a linked account (for example http:github), optional "
            "sandbox_name and provider_type (default generic). Returns a lease_id and the variable names, never "
            "the values; release the lease with credential.release_openshell_binding.",
        ),
        "credential.release_openshell_binding": ActionSpec(
            "credential.release_openshell_binding",
            "Release a previously created OpenShell credential lease.",
        ),
        "credential.api_request": ActionSpec(
            "credential.api_request",
            "Make a broker-owned HTTP API request to the host of a web API tool; the broker adds the credential of "
            "the account linked to that tool. Requests carry no credentials or placeholders.",
        ),
        "template.list": ActionSpec(
            "template.list",
            "List built-in and approved credential templates without exposing secret values.",
        ),
        "template.get": ActionSpec(
            "template.get",
            "Fetch one credential template definition without exposing secret values.",
        ),
        "template.render_request": ActionSpec(
            "template.render_request",
            "Render a credential template into a SecureCredentialCollectionRequest for gateway form creation.",
        ),
        "template.save_approved_variant": ActionSpec(
            "template.save_approved_variant",
            "Persist a user-approved non-secret credential template variant for reuse.",
        ),
        "signup.prepare": ActionSpec(
            "signup.prepare",
            "Validate a signup proposal without generating secrets or creating a user-facing form.",
        ),
        "signup.execute_approved": ActionSpec(
            "signup.execute_approved",
            "Run or classify a user-approved signup attempt; manual boundaries return manual_signup_required.",
        ),
        "signup.manual_fallback_session": ActionSpec(
            "signup.manual_fallback_session",
            "Create a secure broker web form session for user-completed signup credential submission.",
        ),
    }
    action_aliases = {
        "credential_collect": "credential.collect",
        "collect": "credential.collect",
        "credential_metadata": "credential.get_metadata",
        "credential_get_metadata": "credential.get_metadata",
        "register_existing_source": "credential.register_existing_source",
        "credential_register_existing_source": "credential.register_existing_source",
        "bind_for_openshell": "credential.bind_for_openshell",
        "openshell_bind": "credential.bind_for_openshell",
        "release_openshell_binding": "credential.release_openshell_binding",
        "openshell_release": "credential.release_openshell_binding",
        "api_request": "credential.api_request",
        "credential_api_request": "credential.api_request",
        "credential.api.request": "credential.api_request",
        "template_list": "template.list",
        "template_get": "template.get",
        "template_render_request": "template.render_request",
        "render_template_request": "template.render_request",
        "template_save_approved_variant": "template.save_approved_variant",
        "save_approved_template_variant": "template.save_approved_variant",
        "signup_prepare": "signup.prepare",
        "signup_execute_approved": "signup.execute_approved",
        "manual_fallback_session": "signup.manual_fallback_session",
        "signup_manual_fallback_session": "signup.manual_fallback_session",
    }


class SecureRequestsProvider(DeclaredProvider):
    name = "secure_requests"
    actions = {
        "request": ActionSpec(
            "request",
            "Broker-proxied requests-style HTTP call to the host of a web API tool; the broker adds the credential "
            "of the account linked to that tool. Requests carry no credentials or placeholders.",
        ),
        "get": ActionSpec("get", "Broker-proxied HTTP GET."),
        "post": ActionSpec("post", "Broker-proxied HTTP POST."),
        "put": ActionSpec("put", "Broker-proxied HTTP PUT."),
        "patch": ActionSpec("patch", "Broker-proxied HTTP PATCH."),
        "delete": ActionSpec("delete", "Broker-proxied HTTP DELETE."),
        "head": ActionSpec("head", "Broker-proxied HTTP HEAD."),
    }
    action_aliases = {
        "api_request": "request",
        "http_request": "request",
        "requests.request": "request",
        "requests.get": "get",
        "requests.post": "post",
        "requests.put": "put",
        "requests.patch": "patch",
        "requests.delete": "delete",
        "requests.head": "head",
    }


class ToolSetupProvider(DeclaredProvider):
    """Whether tools are set up for the person a task works for (``secure_tool_broker.tool_setup_status``).

    Agents reach it through ``set_up_tools`` (``fn:tool_setup``), never
    through ``secure_cli``. It returns states and labels only.
    """

    name = "tool_setup"
    actions = {
        "status": ActionSpec(
            "status",
            "For each tool id in params.tool_ids: whether the person the task works for signed in "
            "(with the account name when known), has their own or the team's account linked, and "
            "whether the team's sign-in app is set up. Never returns credentials.",
        ),
    }


def register_builtin_providers() -> None:
    for provider in hub_cli_providers():
        register_provider(provider)
    register_provider(GitHubProvider())
    register_provider(GoogleProvider())
    register_provider(BitwardenProvider())
    register_provider(OnePasswordProvider())
    register_provider(KeepassProvider())
    register_provider(VaultProvider())
    register_provider(SealboxProvider())
    register_provider(CredentialCatalogProvider())
    register_provider(SecureRequestsProvider())
    register_provider(ToolSetupProvider())


register_builtin_providers()
