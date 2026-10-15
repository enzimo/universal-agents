"""Broker-owned interactive authentication flow registry."""

from __future__ import annotations

import html
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from secure_tool_broker.auth_pages import (
    render_broker_auth_page,
    render_close_page_button,
    render_markdown_text,
    render_session_countdown,
)
from secure_tool_broker.credential_state import credential_owner_environment
from secure_tool_broker.credential_state_store import (
    credential_state_backend_mode,
    read_credential_state,
    write_credential_state,
)
from secure_tool_broker.google_workspace import (
    CLIENT_MISSING_MESSAGE,
    CLIENT_OWNER_PERSON,
    GOOGLE_TOKEN_KEY,
    KEYRING_PASSWORD_MESSAGE,
    TEAM_CLIENT_COMMAND,
    GoogleNotReady,
    OAuthClient,
    check_sign_in,
    desktop_client_problem,
    gog_environment,
    install_client,
    make_gog_home,
    oauth_client_for,
    remove_gog_home,
    run_gog,
    store_person_client,
    store_person_token,
)
from secure_tool_broker.redaction import redact_text
from secure_tool_broker.sign_in_pages import (
    SIGN_IN_PAGE_SECONDS,
    auth_link_html,
    session_status_payload,
    sign_in_wait_script,
)
from secure_tool_broker.model_key_page import ModelKeyFlow, PersonalModelKeyFlow
from secure_tool_broker.x_sign_in import XTeamClientFlow, XWebSignInFlow
from secure_tools.auth_methods import TEAM_CLIENT_METHOD, X_SIGN_IN_PROVIDER, sign_in_command
from secure_tools.providers import ToolResult


LOGGER = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://[^\s<>'\"]+")
GOOGLE_ACCOUNTS_URL_PREFIX = "https://accounts.google.com"
GITHUB_DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")
MAX_OUTPUT_CHARS = 6000
GOG_KEYRING_PASSWORD_REMEDIATION = KEYRING_PASSWORD_MESSAGE
DEFAULT_GITHUB_WEB_AUTH_SCOPES = ("repo", "read:org", "gist", "read:project")
FULL_GITHUB_WEB_AUTH_SCOPES = (
    "repo",
    "repo:status",
    "repo_deployment",
    "public_repo",
    "repo:invite",
    "security_events",
    "admin:repo_hook",
    "write:repo_hook",
    "read:repo_hook",
    "admin:org",
    "write:org",
    "read:org",
    "admin:public_key",
    "write:public_key",
    "read:public_key",
    "admin:org_hook",
    "gist",
    "notifications",
    "user",
    "read:user",
    "user:email",
    "user:follow",
    "project",
    "read:project",
    "delete_repo",
    "write:packages",
    "read:packages",
    "delete:packages",
    "admin:gpg_key",
    "write:gpg_key",
    "read:gpg_key",
    "admin:ssh_signing_key",
    "write:ssh_signing_key",
    "read:ssh_signing_key",
    "codespace",
    "workflow",
    "admin:enterprise",
    "manage_runners:enterprise",
    "manage_billing:enterprise",
    "read:enterprise",
    "read:audit_log",
)


@dataclass
class BrokerAuthSession:
    provider: str
    method: str
    created_at: datetime
    expires_at: datetime
    owner_canonical_user_id: str = ""
    owner_team_id: str | None = None
    # The person's team roles, vouched for by the gateway when the session was
    # created (checked again for administrator-only methods such as team_client).
    owner_roles: tuple[str, ...] = ()
    used: bool = False
    submission_in_progress: bool = False
    state: dict[str, Any] = field(default_factory=dict)
    process: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None


class BrokerAuthFlow(Protocol):
    provider: str
    method: str
    display_name: str
    kind: str

    def start(
        self,
        session: BrokerAuthSession,
        payload: Mapping[str, Any],
        logger: logging.Logger,
    ) -> None:
        ...

    def render(self, session: BrokerAuthSession) -> str:
        ...

    def status_payload(self, session: BrokerAuthSession) -> dict[str, Any]:
        ...

    def cleanup(self, session: BrokerAuthSession) -> None:
        ...


def _append_output(session: BrokerAuthSession, fragment: str) -> None:
    if not fragment:
        return
    current = str(session.state.get("output") or "")
    rendered = redact_text(fragment)
    combined = (current + rendered)[-MAX_OUTPUT_CHARS:]
    session.state["output"] = combined
    _capture_external_url_and_code(session, combined)


def _is_complete_google_auth_url(url: str) -> bool:
    normalized = str(url or "").strip()
    return (
        normalized.startswith(GOOGLE_ACCOUNTS_URL_PREFIX)
        and "client_id=" in normalized
        and ("redirect_uri=" in normalized or "/o/oauth" in normalized)
    )


def _google_auth_url_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    source = str(text or "")
    for match in re.finditer(re.escape(GOOGLE_ACCOUNTS_URL_PREFIX), source):
        tail = source[match.start() :]
        pieces: list[str] = []
        for raw_line in tail.splitlines():
            line = raw_line.strip()
            if not line:
                if pieces:
                    break
                continue
            if not pieces:
                prefix_index = line.find(GOOGLE_ACCOUNTS_URL_PREFIX)
                if prefix_index < 0:
                    break
                line = line[prefix_index:]
            elif re.match(r"^(after authorizing|copy the url|paste redirect|visit this url)", line, re.IGNORECASE):
                break
            token = line.split()[0].rstrip(").,")
            if pieces and not re.match(r"^[/?&#A-Za-z0-9_%=.:;,+~!-]", token):
                break
            pieces.append(token)
        candidate = "".join(pieces).rstrip(").,")
        if candidate:
            candidates.append(candidate)
    return candidates


def _capture_external_url_and_code(session: BrokerAuthSession, text: str) -> None:
    urls = URL_RE.findall(text or "")
    if session.provider == "google":
        for url in _google_auth_url_candidates(text):
            if _is_complete_google_auth_url(url):
                current = str(session.state.get("external_url") or "")
                if not _is_complete_google_auth_url(current) or len(url) > len(current):
                    session.state["external_url"] = url
                break
    # GitHub: only the device page counts, and it replaces anything taken
    # earlier. The output arrives in pieces and is re-read as a whole after
    # each one, so a piece can end inside the address ("https://github.com"
    # before "/login/device" arrives); the first address seen used to stick
    # and the Open button went to github.com's home page. Until the device
    # page appears, the page offers GitHub's standard device address.
    for raw_url in urls:
        url = raw_url.rstrip(").,")
        if "github.com/login/device" in url or "github.com/device" in url:
            session.state["external_url"] = url
            break
    match = GITHUB_DEVICE_CODE_RE.search(text or "")
    if match:
        session.state.setdefault("user_code", match.group(0))


def _safe_command_preview(command: list[str]) -> list[str]:
    return [command[0], *command[1:4], "..."] if len(command) > 4 else list(command)


def _linkify_urls(text: str, *, link_class: str = "") -> str:
    if not text:
        return ""
    parts: list[str] = []
    last_index = 0
    class_attr = f' class="{html.escape(link_class)}"' if link_class else ""
    for match in URL_RE.finditer(text):
        raw_url = match.group(0)
        url = raw_url
        trailing = ""
        while url and url[-1] in ").,":
            trailing = url[-1] + trailing
            url = url[:-1]
        parts.append(html.escape(text[last_index : match.start()]))
        if url:
            escaped_url = html.escape(url)
            parts.append(
                f'<a{class_attr} href="{escaped_url}" target="_blank" rel="noreferrer">{escaped_url}</a>'
            )
            parts.append(html.escape(trailing))
        else:
            parts.append(html.escape(raw_url))
        last_index = match.end()
    parts.append(html.escape(text[last_index:]))
    return "".join(parts)


def _redirect_uri_from_auth_url(url: str) -> str:
    parsed = urlparse(url)
    values = parse_qs(parsed.query).get("redirect_uri") or []
    if not values:
        return ""
    return unquote(values[0])


def _gog_keyring_password_remediation(output: str) -> str | None:
    normalized = output.lower()
    if (
        "gog_keyring_password" in normalized
        or ("no tty available" in normalized and "keyring" in normalized)
        or "file keyring item" in normalized
    ):
        return GOG_KEYRING_PASSWORD_REMEDIATION
    return None


def _should_auto_continue_prompt(session: BrokerAuthSession, output_lower: str) -> bool:
    if session.provider == "google":
        return False
    return "press enter" in output_lower


def _google_success_summary_output(output: str) -> str:
    """Keep completed auth account details without replaying manual-flow prompts."""
    lines: list[str] = []
    for raw_line in redact_text(output).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^Paste redirect URL \(Enter or Ctrl-D\):\s*", "", line)
        if not line:
            continue
        normalized = line.lower()
        if normalized.startswith("visit this url to authorize"):
            continue
        if "accounts.google.com/o/oauth2/auth" in normalized:
            continue
        if normalized.startswith("after authorizing,"):
            continue
        if normalized.startswith("copy the url from your browser"):
            continue
        if normalized.startswith("paste redirect url"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _persist_github_web_auth_token(session: BrokerAuthSession, executable_path: str, logger: logging.Logger) -> bool:
    if credential_state_backend_mode() != "credential_source":
        return True
    hostname = str(session.state.get("hostname") or "github.com").strip() or "github.com"
    command = [executable_path, "auth", "token", "--hostname", hostname]
    try:
        completed = subprocess.run(
            command,
            cwd=os.getcwd(),
            env=credential_owner_environment(session.owner_canonical_user_id),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        session.state["error"] = "GitHub auth completed, but token export timed out after 15s."
        return False
    except Exception as exc:
        session.state["error"] = f"GitHub auth completed, but token export failed to start: {redact_text(str(exc))}"
        return False
    stdout = completed.stdout or ""
    stderr = redact_text(completed.stderr or "").strip()
    if completed.returncode != 0:
        output = "\n".join(part for part in (redact_text(stdout).strip(), stderr) if part)
        session.state["error"] = (
            output
            or f"GitHub auth completed, but gh auth token exited with code {completed.returncode}."
        )
        logger.warning(
            "GitHub broker web auth token export failed after login. exit_code=%s stderr=%s stdout=%s",
            completed.returncode,
            stderr,
            redact_text(stdout).strip(),
        )
        return False
    token = stdout.strip()
    if not token:
        session.state["error"] = "GitHub auth completed, but gh auth token returned an empty token."
        return False
    write_kwargs = (
        {"owner_id": session.owner_canonical_user_id}
        if session.owner_canonical_user_id
        else {}
    )
    store_result = write_credential_state("github.token", token, **write_kwargs)
    if store_result.status != "success":
        session.state["error"] = (
            store_result.error
            or "GitHub auth completed, but credential-source persistence failed."
        )
        logger.warning(
            "GitHub broker web auth credential-state persistence failed. backend=%s error=%s content=%s",
            credential_state_backend_mode(),
            redact_text(str(store_result.error or "")),
            store_result.content,
        )
        return False
    session.state["credential_state_persisted"] = True
    session.state["credential_state_backend"] = "credential_source"
    session.state["credential_state_source"] = (store_result.content or {}).get("credential_state_source")
    return True


def _finalize_github_web_auth(session: BrokerAuthSession, logger: logging.Logger) -> bool:
    executable_path = shutil.which("gh")
    if not executable_path:
        session.state["error"] = "GitHub auth completed, but required CLI is not installed or not on PATH: gh"
        return False
    return _persist_github_web_auth_token(session, executable_path, logger)


def _session_gog_environment(session: BrokerAuthSession) -> dict[str, str]:
    """gog's environment for this sign-in's private home (``google_workspace``)."""

    home = str(session.state.get("gog_home") or "")
    if not home:
        raise GoogleNotReady(
            ToolResult(status="error", error=f"This Google sign-in lost its private folder. Start again with {sign_in_command('google')}.")
        )
    return gog_environment(Path(home))


def _export_session_token(session: BrokerAuthSession, executable_path: str, env: Mapping[str, str], email: str) -> str:
    """The refresh-token JSON gog stored in this sign-in's home."""

    export_path = Path(env["HOME"]) / "token-export.json"
    completed = run_gog(
        [executable_path, "--no-input", "auth", "tokens", "export", email, "--out", str(export_path), "--overwrite"], env
    )
    try:
        if isinstance(completed, ToolResult):
            raise GoogleNotReady(completed)
        if completed.returncode != 0:
            output = redact_text(f"{completed.stdout or ''}\n{completed.stderr or ''}").strip()
            raise GoogleNotReady(
                ToolResult(
                    status="error",
                    error=_gog_keyring_password_remediation(output)
                    or output
                    or f"Google sign-in finished, but gog auth tokens export exited with code {completed.returncode}.",
                )
            )
        return export_path.read_text(encoding="utf-8").strip() if export_path.is_file() else ""
    finally:
        export_path.unlink(missing_ok=True)


def _finalize_google_web_auth(session: BrokerAuthSession, logger: logging.Logger) -> bool:
    """Check the new sign-in with Google, then store it as the person's own."""

    executable_path = shutil.which("gog")
    email = str(session.state.get("email") or "").strip()
    if not executable_path or not email:
        session.state["error"] = "Google sign-in finished, but the broker has no gog program or no account email."
        return False
    try:
        env = _session_gog_environment(session)
        valid, detail = check_sign_in(executable_path, env)
        if not valid:
            session.state["error"] = f"Google sign-in finished, but Google did not accept it: {redact_text(detail)}"
            session.state["output"] = "\n".join(part for part in (str(session.state.get("output") or ""), redact_text(detail)) if part)
            return False
        token_text = _export_session_token(session, executable_path, env, email)
    except GoogleNotReady as exc:
        session.state["error"] = str(exc.result.error or "Google sign-in could not be finished.")
        return False
    if not token_text:
        session.state["error"] = "Google sign-in finished, but gog exported an empty token."
        return False
    stored = store_person_token(session.owner_canonical_user_id, token_text)
    if stored.status != "success":
        session.state["error"] = stored.error or "Google sign-in finished, but the credential store did not save it."
        logger.warning(
            "Google sign-in could not be stored. event=google_sign_in_store_failed person_id=%s backend=%s error=%s",
            session.owner_canonical_user_id,
            credential_state_backend_mode(),
            redact_text(str(stored.error or "")),
        )
        return False
    session.state["web_auth_verified"] = True
    session.state["credential_state_backend"] = credential_state_backend_mode()
    logger.info(
        "A person's Google sign-in was stored. event=google_sign_in_stored person_id=%s method=web",
        session.owner_canonical_user_id,
    )
    return True


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()


def _explain_auth_process_exit(session: BrokerAuthSession) -> tuple[str, str]:
    """Classify explicit CLI evidence, without treating an exit code as a cause."""
    output = str(session.state.get("output") or "").lower()
    retry = f"Start again with {sign_in_command(session.provider)}."
    if session.provider == "google":
        remediation = _gog_keyring_password_remediation(output)
        if remediation:
            return "keyring_password", remediation
        if re.search(r"\bauthorized as [^\s,]+@[^\s,]+, expected [^\s,]+@[^\s,]+", output):
            # An email mismatch also catches a different account selected at consent.
            # Never assume two addresses are aliases or bypass gog's identity check.
            return "account_email_mismatch", (
                "Google returned a different account email than the one entered. "
                "If you renamed this same Google account, Google may still report its previous email to this app. "
                f"Start again with {sign_in_command('google')} and enter the email shown after 'authorized as' in Broker CLI Output. "
                "Otherwise, retry and select the intended Google account."
            )
        for marker, explanation in (
            ("invalid_grant", "Google rejected the authorization code. It may have expired or already been used."),
            ("access_denied", "Google reported that authorization was denied. Check that you granted permission and that your account is allowed to use this OAuth app."),
            ("redirect_uri_mismatch", "Google rejected the redirect address, so the Google sign-in app is probably not a Desktop app client. Ask a team administrator to store a Desktop app client with /auth google team_client, or paste your own on the sign-in page."),
            ("invalid_client", "Google rejected the Google sign-in app (OAuth client). Ask a team administrator to check the team's app (/auth google team_client), or paste your own Desktop app client."),
        ):
            if marker in output:
                return marker, f"{explanation} {retry}"
        if "context deadline exceeded" in output or "timed out" in output:
            return "timeout", f"Google authentication did not finish before its time limit. {retry}"
    stage = (
        "after the redirect URL was submitted"
        if session.state.get("redirect_submitted")
        else "before the broker received a completed browser authorization"
    )
    return "process_exit", (
        f"The authentication program stopped {stage}. "
        "It did not report a recognized cause. Review the Broker CLI Output below. "
        f"{retry} If it fails again, give the operator the diagnostic reference below."
    )


def _record_auth_process_failure(session: BrokerAuthSession, logger: logging.Logger) -> None:
    """Log only controlled metadata; CLI output and OAuth URLs stay out of logs."""
    reference = session.state.setdefault("diagnostic_reference", f"authdiag_{uuid4().hex}")
    session.state.setdefault("failure_reason", "auth_workflow_failed")
    logger.warning(
        "Broker authentication failed. event=broker_auth_process_failed "
        "diagnostic_reference=%s provider=%s method=%s stage=%s reason=%s "
        "exit_code=%s output_chars=%s redirect_submitted=%s elapsed_seconds=%s",
        reference, session.provider, session.method,
        session.state.get("auth_stage", "process_start"),
        session.state["failure_reason"], session.state.get("exit_code", "-"),
        len(str(session.state.get("output") or "")),
        bool(session.state.get("redirect_submitted")),
        session.state.get("elapsed_seconds", 0),
    )


def _auth_failure_details_html(session: BrokerAuthSession) -> str:
    reference = session.state.get("diagnostic_reference")
    if not reference:
        return ""
    details = "\n".join(
        f"{label}: {session.state.get(key, 'unknown')}"
        for label, key in (
            ("Diagnostic reference", "diagnostic_reference"),
            ("Stage", "auth_stage"),
            ("Reason", "failure_reason"),
            ("Exit code", "exit_code"),
            ("Elapsed seconds", "elapsed_seconds"),
        )
    )
    return (
        '<details class="content-panel"><summary>Technical Details</summary>'
        f'<pre>{html.escape(details)}</pre></details>'
    )


def _monitor_web_auth_process(
    *,
    session: BrokerAuthSession,
    timeout_seconds: int,
    logger: logging.Logger,
) -> None:
    process = session.process
    session.state.setdefault("auth_stage", "browser_authorization")
    started_at = time.monotonic()
    if process is None:
        session.state["status"] = "error"
        session.state["error"] = "Auth process was not started."
        session.state["auth_stage"] = "process_start"
        _record_auth_process_failure(session, logger)
        return

    def timeout_process() -> None:
        if process.poll() is None:
            session.state["status"] = "error"
            session.state["error"] = f"Auth process timed out after {timeout_seconds}s."
            session.state["error"] += f" Start again with {sign_in_command(session.provider)} and finish the browser steps before the time limit."
            session.state["failure_reason"] = "timeout"
            _terminate_process(process)

    timeout_timer = threading.Timer(timeout_seconds, timeout_process)
    timeout_timer.daemon = True
    timeout_timer.start()
    deadline = time.monotonic() + timeout_seconds
    sent_continue = False
    try:
        assert process.stdout is not None
        while True:
            if time.monotonic() > deadline and process.poll() is None:
                _terminate_process(process)
                session.state["status"] = "error"
                session.state["error"] = f"Auth process timed out after {timeout_seconds}s."
                session.state["error"] += f" Start again with {sign_in_command(session.provider)} and finish the browser steps before the time limit."
                session.state["failure_reason"] = "timeout"
                break
            fragment = process.stdout.read(1)
            if fragment:
                _append_output(session, fragment)
                output_lower = str(session.state.get("output") or "").lower()
                if (
                    not sent_continue
                    and process.stdin is not None
                    and _should_auto_continue_prompt(session, output_lower)
                    and process.poll() is None
                ):
                    try:
                        process.stdin.write("\n")
                        process.stdin.flush()
                        sent_continue = True
                    except OSError:
                        sent_continue = True
                continue
            if process.poll() is not None:
                break
            time.sleep(0.05)

        exit_code = process.wait(timeout=1)
        session.state["exit_code"] = exit_code
        session.state.setdefault("completed_at", datetime.utcnow().isoformat() + "Z")
        if session.state.get("status") != "error":
            if exit_code == 0:
                session.state["auth_stage"] = "credential_verification_and_storage"
                if session.provider == "github" and not _finalize_github_web_auth(session, logger):
                    session.state["status"] = "error"
                elif session.provider == "google" and not _finalize_google_web_auth(session, logger):
                    session.state["status"] = "error"
                else:
                    session.state["status"] = "success"
                    session.state["message"] = "Authentication completed successfully."
            else:
                session.state["status"] = "error"
                reason, explanation = _explain_auth_process_exit(session)
                session.state["failure_reason"] = reason
                session.state["error"] = explanation
    except Exception as exc:
        session.state["status"] = "error"
        session.state["error"] = redact_text(str(exc))
        logger.exception(
            "Broker auth process monitor failed. provider=%s method=%s",
            session.provider,
            session.method,
        )
    finally:
        timeout_timer.cancel()
        # A Google sign-in is stored (or failed); its private gog home is not needed again.
        remove_gog_home(session.state.pop("gog_home", None))
        if session.state.get("status") == "error":
            session.state["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
            _record_auth_process_failure(session, logger)
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass


class GitHubWebAuthFlow:
    provider = "github"
    method = "web"
    display_name = "GitHub"
    kind = "external_browser"
    timeout_seconds = SIGN_IN_PAGE_SECONDS
    default_scopes = DEFAULT_GITHUB_WEB_AUTH_SCOPES
    scope_profile = "standard"

    def start(
        self,
        session: BrokerAuthSession,
        payload: Mapping[str, Any],
        logger: logging.Logger,
    ) -> None:
        executable_path = shutil.which("gh")
        if not executable_path:
            session.state.update(
                {
                    "status": "error",
                    "error": "Required CLI is not installed or not on PATH: gh",
                }
            )
            return

        hostname = str(payload.get("hostname") or "github.com").strip() or "github.com"
        git_protocol = str(payload.get("git_protocol") or "https").strip().lower() or "https"
        if git_protocol not in {"https", "ssh"}:
            git_protocol = "https"
        command = [
            executable_path,
            "auth",
            "login",
            "--web",
            "--hostname",
            hostname,
            "--git-protocol",
            git_protocol,
            "--insecure-storage",
        ]
        scopes = _github_web_auth_scopes(payload.get("scopes"), default_scopes=self.default_scopes)
        command.extend(["--scopes", scopes])
        if git_protocol == "ssh":
            command.append("--skip-ssh-key")

        env = credential_owner_environment(session.owner_canonical_user_id)
        env.setdefault("BROWSER", "echo")
        session.state.update(
            {
                "status": "pending",
                "hostname": hostname,
                "git_protocol": git_protocol,
                "scope_profile": self.scope_profile,
                "requested_scopes": scopes,
                "command": _safe_command_preview(command),
                "message": "Waiting for GitHub browser authorization to complete.",
            }
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=os.getcwd(),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=0,
            )
        except Exception as exc:
            session.state["status"] = "error"
            session.state["error"] = f"gh command failed to start: {redact_text(str(exc))}"
            return

        session.process = process
        thread = threading.Thread(
            target=_monitor_web_auth_process,
            kwargs={
                "session": session,
                "timeout_seconds": self.timeout_seconds,
                "logger": logger,
            },
            name=f"broker-auth-{self.provider}-{self.method}",
            daemon=True,
        )
        session.thread = thread
        thread.start()

    def render(self, session: BrokerAuthSession) -> str:
        status = str(session.state.get("status") or "pending")
        countdown_html = render_session_countdown(session.expires_at)
        external_url = str(session.state.get("external_url") or "https://github.com/login/device")
        user_code = str(session.state.get("user_code") or "")
        message = (
            str(session.state.get("error") or session.state.get("message") or "")
            if status == "error"
            else str(session.state.get("message") or session.state.get("error") or "")
        )
        output = str(session.state.get("output") or "")
        scope_profile = str(session.state.get("scope_profile") or self.scope_profile)
        requested_scopes = str(session.state.get("requested_scopes") or "")
        scope_note = (
            "<p class=\"status\">This high-privilege flow requests every supported GitHub OAuth scope "
            "known to this broker, including repository deletion, organization administration, "
            "package deletion, workflow updates, codespaces, notifications, profile, project, "
            "SSH signing key, and enterprise access. "
            "GitHub may still grant fewer scopes based on user choice, organization policy, or account permissions.</p>"
            if scope_profile == "all"
            else (
                f"<p class=\"status\">Requested scopes: <code>{html.escape(requested_scopes)}</code></p>"
                if requested_scopes
                else ""
            )
        )
        copy_script = """
<script>
  async function copyCode() {
    const code = document.getElementById("auth-code");
    const status = document.getElementById("copy-status");
    if (!code) return;
    const value = code.textContent.trim();
    let copied = false;
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        copied = true;
      } catch (_error) {
        copied = false;
      }
    }
    if (!copied) {
      const textarea = document.createElement("textarea");
      textarea.value = value;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      textarea.style.top = "0";
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      try {
        copied = document.execCommand("copy");
      } catch (_error) {
        copied = false;
      }
      document.body.removeChild(textarea);
    }
    if (status) status.textContent = copied ? "Copied" : "Copy failed";
  }
</script>"""
        code_html = (
            "<div class=\"code-row\">"
            "<span>One-time code</span>"
            f"<code id=\"auth-code\" class=\"code-value\">{html.escape(user_code) or 'Wait...'}</code>"
            f'<button id="auth-code-copy" class="copy-button" type="button" onclick="copyCode()" '
            f'aria-label="Copy one-time code" {"" if user_code else "disabled"}>'
            "<svg aria-hidden=\"true\" viewBox=\"0 0 24 24\" width=\"18\" height=\"18\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
            "<rect x=\"9\" y=\"9\" width=\"13\" height=\"13\" rx=\"2\" ry=\"2\"></rect>"
            "<path d=\"M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1\"></path>"
            "</svg>"
            "</button>"
            "<span id=\"copy-status\" aria-live=\"polite\"></span>"
            "</div>"
        )
        output_html = (
            "<details class=\"output\">"
            "<summary>Broker CLI Output</summary>"
            f"<pre>{_linkify_urls(output, link_class='output-link')}</pre>"
            "</details>"
            if output
            else ""
        )
        if status == "success":
            body = (
                "<section class=\"content-panel\">"
                "<p class=\"eyebrow\">GitHub</p>"
                "<h1>GitHub Authentication Complete</h1>"
                "<p>The broker-side GitHub CLI session is now available inside the container. "
                "You can close this browser tab and retry the agent task.</p>"
                f"{render_close_page_button()}"
                "</section>"
            )
        elif status == "error":
            body = (
                "<section class=\"content-panel\">"
                "<p class=\"eyebrow\">GitHub</p>"
                "<h1>GitHub Authentication Failed</h1>"
                f"{render_markdown_text(message or 'The broker-side web login failed.')}"
                f"{_auth_failure_details_html(session)}"
                f"{output_html}"
                "</section>"
            )
        else:
            body = (
                "<section class=\"content-panel\">"
                "<p class=\"eyebrow\">GitHub</p>"
                "<h1>GitHub Browser Auth</h1>"
                "<p>The secure broker started <code>gh auth login --web</code> inside the container. "
                "Use your desktop browser to open the device authorization page, enter the one-time code, "
                "and complete MFA with <span class=\"service-name\">GitHub</span>. "
                "The resulting CLI session is stored in broker-owned auth state.</p>"
                f"{scope_note}"
                f"{countdown_html}"
                "<p>Open this authorization page:</p>"
                f'<p><button type="button" id="github-auth-link" class="auth-open-button" '
                f'data-broker-auth-open data-auth-url="{html.escape(external_url, quote=True)}" '
                'aria-label="Open GitHub authorization in a new tab">Open</button></p>'
                f"{auth_link_html(external_url)}"
                f"{code_html}"
                f'<div class="status ua-markdown" id="broker-auth-status" role="status" '
                f'data-auth-provider="github" data-restart-command="{html.escape(sign_in_command('github'), quote=True)}" '
                f'data-markdown="{html.escape(message, quote=True)}"></div>'
                f"{output_html}"
                "</section>"
            )
        return render_broker_auth_page(
            "GitHub Broker Web Auth",
            body,
            script=copy_script + (sign_in_wait_script() if status not in {"success", "error"} else ""),
        )

    def status_payload(self, session: BrokerAuthSession) -> dict[str, Any]:
        return session_status_payload(session)

    def cleanup(self, session: BrokerAuthSession) -> None:
        if session.process is not None:
            _terminate_process(session.process)


class GitHubWebAllAuthFlow(GitHubWebAuthFlow):
    method = "web-all"
    default_scopes = FULL_GITHUB_WEB_AUTH_SCOPES
    scope_profile = "all"


class GoogleWebAuthFlow:
    provider = "google"
    method = "web"
    display_name = "Google"
    kind = "browser_redirect_form"
    timeout_seconds = SIGN_IN_PAGE_SECONDS

    def start(
        self,
        session: BrokerAuthSession,
        payload: Mapping[str, Any],
        logger: logging.Logger,
    ) -> None:
        return

    def submit(
        self,
        session: BrokerAuthSession,
        form: Mapping[str, Any],
        logger: logging.Logger,
    ) -> None:
        status = str(session.state.get("status") or "")
        if status == "success" and str(form.get("export_token_json") or "").strip():
            self._export_token_json(session, logger)
            return
        redirect_url = str(form.get("redirect_url") or "").strip()
        if redirect_url:
            self._submit_redirect_url(session, redirect_url)
            return
        if session.process is not None and status == "pending":
            session.state["message"] = "Waiting for the Google redirect URL."
            return
        email = str(form.get("email") or "").strip()
        if not email:
            session.state["status"] = "created"
            session.state["error"] = "Google account email is required."
            return
        services = str(form.get("services") or "all").strip() or "all"
        drive_scope = str(form.get("drive_scope") or "full").strip().lower() or "full"
        if drive_scope not in {"full", "readonly", "file"}:
            drive_scope = "full"
        credentials_json = str(form.get("credentials_json") or "").strip()
        self._start_gog_manual_auth(
            session=session,
            email=email,
            services=services,
            drive_scope=drive_scope,
            credentials_json=credentials_json,
            logger=logger,
        )

    def _start_gog_manual_auth(
        self,
        *,
        session: BrokerAuthSession,
        email: str,
        services: str,
        drive_scope: str,
        credentials_json: str,
        logger: logging.Logger,
    ) -> None:
        executable_path = shutil.which("gog")
        if not executable_path:
            session.state.update({"status": "error", "error": "Required CLI is not installed or not on PATH: gog"})
            return
        env = self._prepare_sign_in_home(session, executable_path, credentials_json, logger)
        if env is None:
            return
        remaining_seconds = max(
            1,
            min(
                self.timeout_seconds,
                int((session.expires_at - datetime.now(session.expires_at.tzinfo)).total_seconds()),
            ),
        )
        command = [
            executable_path,
            "auth",
            "add",
            email,
            "--manual",
            "--services",
            services,
            "--drive-scope",
            drive_scope,
            "--force-consent",
            "--timeout",
            f"{remaining_seconds}s",
        ]
        session.state.update(
            {
                "status": "pending",
                "email": email,
                "services": services,
                "drive_scope": drive_scope,
                "command": _safe_command_preview(command),
                "message": "Waiting for Google authorization.",
            }
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=env["HOME"],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=0,
            )
        except Exception as exc:
            remove_gog_home(session.state.pop("gog_home", None))
            session.state["status"] = "error"
            session.state["error"] = f"gog command failed to start: {redact_text(str(exc))}"
            return
        session.process = process
        thread = threading.Thread(
            target=_monitor_web_auth_process,
            kwargs={
                "session": session,
                "timeout_seconds": remaining_seconds,
                "logger": logger,
            },
            name=f"broker-auth-{self.provider}-{self.method}",
            daemon=True,
        )
        session.thread = thread
        thread.start()

    @staticmethod
    def _sign_in_client(session: BrokerAuthSession, credentials_json: str) -> OAuthClient | None:
        """The pasted client (the person's own), else the person's stored one, else the team's.

        Sets a form error and returns None when there is none or it is unusable.
        """

        if credentials_json:
            problem = desktop_client_problem(credentials_json)
            if problem:
                session.state.update({"status": "created", "error": problem})
                return None
            return OAuthClient(credentials_json, CLIENT_OWNER_PERSON)
        client = oauth_client_for(session.owner_canonical_user_id, session.owner_team_id)
        if client is None:
            session.state.update(
                {"status": "created", "error": f"{CLIENT_MISSING_MESSAGE} You can also paste your own below."}
            )
        return client

    def _prepare_sign_in_home(
        self, session: BrokerAuthSession, executable_path: str, credentials_json: str, logger: logging.Logger
    ) -> dict[str, str] | None:
        """A private gog home for this sign-in with the OAuth client installed."""

        client = self._sign_in_client(session, credentials_json)
        if client is None:
            return None
        try:
            home = make_gog_home(session.owner_canonical_user_id)
            session.state["gog_home"] = str(home)
            env = gog_environment(home)
            install_client(executable_path, env, client)
        except GoogleNotReady as exc:
            remove_gog_home(session.state.pop("gog_home", None))
            session.state.update({"status": "error", "error": str(exc.result.error or "gog could not be prepared.")})
            logger.warning(
                "Google sign-in could not prepare gog. event=google_sign_in_prepare_failed person_id=%s error_code=%s",
                session.owner_canonical_user_id,
                exc.result.content.get("error_code") or "-",
            )
            return None
        if credentials_json:
            stored = store_person_client(session.owner_canonical_user_id, credentials_json)
            if stored.status != "success":
                remove_gog_home(session.state.pop("gog_home", None))
                session.state.update({"status": "error", "error": stored.error or "Your OAuth client could not be stored."})
                return None
        session.state["oauth_client_owner"] = client.owner
        return env

    @staticmethod
    def _submit_redirect_url(session: BrokerAuthSession, redirect_url: str) -> None:
        process = session.process
        if process is None or process.poll() is not None or process.stdin is None:
            if session.state.get("status") == "success":
                return
            if session.state.get("status") == "error" and session.state.get("error"):
                return
            exit_code = process.poll() if process is not None else None
            session.state["status"] = "error"
            session.state["error"] = (
                "Google auth process exited before it received the redirect URL"
                + (f" with code {exit_code}" if exit_code is not None else "")
                + f". Review the Broker CLI Output below, then start again with {sign_in_command('google')}."
            )
            return
        if not redirect_url.startswith("http://localhost") and not redirect_url.startswith("http://127.0.0.1"):
            session.state["status"] = "pending"
            session.state["error"] = "Paste the localhost redirect URL shown after Google authorization."
            return
        try:
            process.stdin.write(f"{redirect_url}\n")
            process.stdin.flush()
            session.state["redirect_submitted"] = True
            session.state["auth_stage"] = "authorization_code_exchange"
            session.state["message"] = "Submitted redirect URL; waiting for gog to finish."
        except OSError as exc:
            session.state["status"] = "error"
            session.state["error"] = f"Failed to submit redirect URL to gog: {redact_text(str(exc))}"

    @staticmethod
    def _export_token_json(session: BrokerAuthSession, logger: logging.Logger) -> None:
        """Show the person the sign-in the broker stored for them (after success only)."""

        stored = read_credential_state(GOOGLE_TOKEN_KEY, owner_id=session.owner_canonical_user_id)
        if isinstance(stored, ToolResult) or not stored:
            session.state["export_error"] = "The stored Google sign-in could not be read."
            logger.warning(
                "Google sign-in export failed. event=google_sign_in_export_failed person_id=%s",
                session.owner_canonical_user_id,
            )
            return
        session.state["exported_token_json"] = str(stored).strip()
        session.state.pop("export_error", None)

    def render(self, session: BrokerAuthSession) -> str:
        status = str(session.state.get("status") or "created")
        countdown_html = render_session_countdown(session.expires_at)
        message = (
            str(session.state.get("error") or session.state.get("message") or "")
            if status == "error"
            else str(session.state.get("message") or session.state.get("error") or "")
        )
        raw_external_url = str(session.state.get("external_url") or "")
        external_url = raw_external_url if _is_complete_google_auth_url(raw_external_url) else ""
        redirect_uri = _redirect_uri_from_auth_url(external_url) if external_url else ""
        output = str(session.state.get("output") or "")
        waiting_for_redirect = status not in {"success", "error", "created"}
        script = sign_in_wait_script() if waiting_for_redirect else ""
        output_html = (
            "<details class=\"content-panel\" id=\"broker-cli-output\"><summary>Broker CLI Output</summary>"
            f"<pre>{_linkify_urls(output, link_class='output-link')}</pre></details>"
            if output
            else ""
        )
        if status == "success":
            success_summary = _google_success_summary_output(output)
            success_summary_html = (
                "<section class=\"content-panel\"><h2>Authenticated Account</h2>"
                f"<pre>{html.escape(success_summary)}</pre></section>"
                if success_summary
                else ""
            )
            exported_token_json = str(session.state.get("exported_token_json") or "")
            export_error = str(session.state.get("export_error") or "")
            export_result_html = ""
            if exported_token_json:
                export_result_html = (
                    "<p class=\"status\">Token JSON exported. This value contains a Google OAuth refresh token; "
                    "store it as a secret and use it with <code>/link cli:google json_token</code> for later broker imports.</p>"
                    "<p><label for=\"exported_token_json\">gog token JSON</label><br>"
                    f"<textarea id=\"exported_token_json\" rows=\"12\" readonly autocomplete=\"off\">{html.escape(exported_token_json)}</textarea></p>"
                )
            elif export_error:
                export_result_html = f"<p class=\"status\">Token JSON export failed: {html.escape(export_error)}</p>"
            export_html = (
                "<section class=\"content-panel\">"
                "<h2>Token JSON</h2>"
                "<p>The broker can export the resulting gog refresh-token JSON for later <code>/link cli:google json_token</code> imports. "
                "It is shown only in this browser page after you request it, and is not returned to the agent.</p>"
                "<form method=\"post\">"
                "<input type=\"hidden\" name=\"export_token_json\" value=\"1\">"
                "<button type=\"submit\">Show Token JSON</button>"
                "</form>"
                f"{export_result_html}"
                "</section>"
            )
            verification_html = (
                "<section class=\"content-panel\">"
                "<h2>Broker Verification</h2>"
                "<p class=\"status\">Google accepted the sign-in (<code>gog auth list --check</code>). The broker stored it "
                "as yours: agents use it only for requests made for you.</p>"
                "</section>"
                if session.state.get("web_auth_verified")
                else ""
            )
            body = (
                "<section class=\"content-panel\">"
                "<p class=\"eyebrow\">Google</p>"
                "<h1>Google Authentication Complete</h1>"
                "<p>Your Google sign-in is stored for your requests. You can close this browser tab and retry the agent task.</p>"
                "<p class=\"note\">This refresh token avoids Google's fixed 7-day Testing limit only if the OAuth consent "
                "screen was In Production when you authorized. Google may still revoke it after a password change, manual "
                "revocation, prolonged inactivity, or other account security events.</p>"
                f"{render_close_page_button()}"
                f"</section>{success_summary_html}{verification_html}{export_html}"
            )
        elif status == "error":
            body = (
                "<section class=\"content-panel\">"
                "<p class=\"eyebrow\">Google</p>"
                "<h1>Google Authentication Failed</h1>"
                f"{render_markdown_text(message or 'The broker-side Google web login failed.')}"
                f"</section>{_auth_failure_details_html(session)}{output_html}"
            )
        elif status == "created":
            message_html = render_markdown_text(message, class_name="status") if message else ""
            body = (
                "<section class=\"content-panel\">"
                "<p class=\"eyebrow\">Google</p>"
                "<h1>Google Browser Auth</h1>"
                "<p>This starts broker-side <code>gog auth add --manual</code>. "
                "Enter the Google account email to authorize, then use the Google authorization URL shown on the next page.</p>"
                "<div class=\"note\"><strong>Make the token durable before authorizing.</strong> For an External OAuth app, "
                "Google expires refresh tokens after 7 days while the consent screen is in Testing. Open Google Cloud Console "
                "&gt; APIs &amp; Services &gt; OAuth consent screen, click <strong>Publish App</strong>, confirm that the publishing "
                "status is <strong>In Production</strong>, and then start this flow. Personal use can remain unverified. "
                "Production removes the fixed Testing lifetime; it does not make a token permanent or immune to revocation.</div>"
                "<p>You sign in with your own Google account. The sign-in goes through a Google sign-in app "
                "(a Google Cloud \"Desktop app\" OAuth client). The broker uses your own app if you stored one, "
                f"otherwise the team's, which an administrator stores once with <code>{TEAM_CLIENT_COMMAND}</code>. "
                "To use your own app, paste its downloaded JSON below; the broker keeps it for your sign-ins only. "
                "It is not a service account key and should not be pasted into chat.</p>"
                "<p>The broker keeps your sign-in in the credential store and uses it only for requests made for you.</p>"
                f"{countdown_html}"
                "<details open><summary><strong>How to get the Desktop app OAuth client ID JSON</strong></summary>"
                "<ol>"
                "<li>Open <a href=\"https://console.cloud.google.com/apis/credentials\" target=\"_blank\" rel=\"noreferrer\">"
                "Google Cloud Console &gt; APIs &amp; Services &gt; Credentials</a>.</li>"
                "<li>Select or create the Google Cloud project that should own the OAuth client.</li>"
                "<li>Configure the OAuth consent screen, then change its publishing status from Testing to "
                "<strong>In Production</strong> with <strong>Publish App</strong> before authorizing.</li>"
                "<li>Click <strong>Create Credentials</strong>, choose <strong>OAuth client ID</strong>, "
                "then set <strong>Application type</strong> to <strong>Desktop app</strong>.</li>"
                "<li>Name the client, click <strong>Create</strong>, then click <strong>Download JSON</strong>.</li>"
                "<li>Use a Desktop app client. Web application clients are not supported because gog uses a dynamic "
                "localhost or 127.0.0.1 loopback redirect URI for each authorization.</li>"
                "<li>Open the downloaded JSON locally and paste its full contents into the field below, or give it to "
                "a team administrator for the team's app. "
                "Do not paste this JSON into agent chat.</li>"
                "<li>Enable the Google APIs needed by the services you select, such as Gmail API, Google Calendar API, "
                "Google Drive API, Google Docs API, and Google Sheets API.</li>"
                "</ol></details>"
                f"{message_html}"
            )
            body += (
                "<form method=\"post\">"
                "<p><label for=\"email\">Google account email</label><br>"
                "<input id=\"email\" name=\"email\" type=\"email\" autocomplete=\"email\" required></p>"
                "<p><label for=\"services\">Services</label><br>"
                "<select id=\"services\" name=\"services\"><option value=\"all\" selected>all supported gog services</option>"
                "<option value=\"gmail,calendar,drive,docs,sheets,people,tasks,contacts,chat,classroom\">common workspace services</option>"
                "<option value=\"user\">user identity only</option></select></p>"
                "<p><label for=\"drive_scope\">Drive scope</label><br>"
                "<select id=\"drive_scope\" name=\"drive_scope\"><option value=\"full\" selected>full</option>"
                "<option value=\"readonly\">readonly</option><option value=\"file\">file</option></select></p>"
                "<p><label for=\"credentials_json\">Your own OAuth client ID JSON (optional; leave empty to use your stored app or the team's)</label><br>"
                "<textarea id=\"credentials_json\" name=\"credentials_json\" rows=\"8\" autocomplete=\"off\" "
                "placeholder=\"Paste a Google Cloud Desktop app OAuth client ID JSON only to use your own app.\"></textarea></p>"
                "<button type=\"submit\">Start Google Authorization</button>"
                "</form></section>"
            )
        else:
            button_state = (
                f'data-auth-url="{html.escape(external_url, quote=True)}"'
                if external_url else "disabled"
            )
            link_html = (
                "<div id=\"google-auth-link-container\">"
                "<p>Open Google to authorize your account:</p>"
                f'<p><button type="button" id="google-auth-link" class="auth-open-button" data-broker-auth-open '
                f'aria-label="Open Google authorization in a new tab" {button_state}>'
                f'{"Open" if external_url else "Wait..."}</button></p>'
                f"{auth_link_html(external_url)}"
                "</div>"
            )
            redirect_uri_html = (
                '<details id="google-redirect-uri-note"><summary>Redirect Troubleshooting</summary>'
                '<p class="note">Google will validate this exact redirect URI: '
                f'<code id="google-redirect-uri">{html.escape(redirect_uri) or "Waiting for Google authorization URL"}</code>. '
                'If Google shows <code>Error 400: redirect_uri_mismatch</code>, '
                'use a Desktop app OAuth client JSON and restart the flow.</p></details>'
            )
            body = (
                "<section class=\"content-panel\">"
                "<p class=\"eyebrow\">Google</p>"
                "<h1>Google Browser Auth</h1>"
                "<ol class=\"steps\">"
                "<li class=\"step\">Open the Google authorization URL below.</li>"
                "<li class=\"step\">Complete Google login and consent in your browser.</li>"
                "<li class=\"step\">Google redirects to a localhost URL that may not load; copy the full URL from your browser address bar.</li>"
                "<li class=\"step\">Paste that full localhost URL into this broker page.</li>"
                "</ol>"
                "<p class=\"note\">Pasting the localhost URL here completes the same OAuth token exchange as an automatic "
                "desktop callback. Because this request uses <code>--force-consent</code>, Google can issue a fresh refresh token. "
                "For an External app still in Testing, that refresh token expires after 7 days; click <strong>Publish App</strong> "
                "so the consent screen is In Production before completing authorization.</p>"
                f"{countdown_html}"
                f"{link_html}"
                f"{redirect_uri_html}"
                f'<div class="status ua-markdown" id="broker-auth-status" role="status" '
                f'data-auth-provider="google" data-restart-command="{html.escape(sign_in_command('google'), quote=True)}" '
                f'data-markdown="{html.escape(message, quote=True)}"></div>'
                "<form method=\"post\">"
                "<p><label for=\"redirect_url\">Google localhost redirect URL</label><br>"
                "<textarea id=\"redirect_url\" name=\"redirect_url\" required rows=\"4\" autocomplete=\"off\"></textarea></p>"
                "<button type=\"submit\">Submit Redirect URL</button>"
                "</form>"
                f"</section>{output_html}"
            )
        return render_broker_auth_page("Google Broker Web Auth", body, script=script)

    def status_payload(self, session: BrokerAuthSession) -> dict[str, Any]:
        payload = session_status_payload(session)
        external_url = str(payload.get("external_url") or "")
        if external_url and not _is_complete_google_auth_url(external_url):
            payload["external_url"] = None
        return payload

    def cleanup(self, session: BrokerAuthSession) -> None:
        if session.process is not None:
            _terminate_process(session.process)
        remove_gog_home(session.state.pop("gog_home", None))


def _csv(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _github_web_auth_scopes(value: Any, *, default_scopes: tuple[str, ...]) -> str:
    requested = [scope.strip() for scope in _csv(value).split(",") if scope.strip()]
    scopes = list(default_scopes)
    for scope in requested:
        if scope not in scopes:
            scopes.append(scope)
    return ",".join(scopes)


AUTH_FLOW_REGISTRY: dict[str, dict[str, BrokerAuthFlow]] = {
    "github": {
        "web": GitHubWebAuthFlow(),
        "web-all": GitHubWebAllAuthFlow(),
    },
    "google": {
        "web": GoogleWebAuthFlow(),
    },
    X_SIGN_IN_PROVIDER: {
        "web": XWebSignInFlow(),
        TEAM_CLIENT_METHOD: XTeamClientFlow(),
    },
    ModelKeyFlow.provider: {ModelKeyFlow.method: ModelKeyFlow(), PersonalModelKeyFlow.method: PersonalModelKeyFlow()},
}


def get_auth_flow(provider: str, method: str | None) -> BrokerAuthFlow | None:
    provider_flows = AUTH_FLOW_REGISTRY.get(str(provider or "").strip().lower(), {})
    return provider_flows.get(str(method or "").strip().lower())


def available_auth_flow_methods(provider: str) -> tuple[str, ...]:
    return tuple(sorted(AUTH_FLOW_REGISTRY.get(str(provider or "").strip().lower(), {})))
