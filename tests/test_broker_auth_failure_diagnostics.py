"""Exercise the real process monitor without contacting an OAuth provider."""

import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from secure_tool_broker.auth_flows import (
    BrokerAuthSession,
    GoogleWebAuthFlow,
    _monitor_web_auth_process,
)


@pytest.mark.parametrize(
    ("output", "reason", "guidance"),
    [
        ("oauth2: invalid_grant", "invalid_grant", "Start again with /link cli:google"),
        ("oauth2: access_denied", "access_denied", "permission"),
        ("redirect_uri_mismatch", "redirect_uri_mismatch", "Desktop app"),
        ("invalid_client", "invalid_client", "Google sign-in app"),
        ("context deadline exceeded", "timeout", "time limit"),
        (
            "Paste redirect URL (Enter or Ctrl-D): authorized as old@example.com, expected new@example.com",
            "account_email_mismatch",
            "renamed this same Google account",
        ),
        (
            "no TTY available for file keyring item",
            "keyring_password",
            "GOG_KEYRING_PASSWORD",
        ),
        (
            "Visit this URL to authorize: https://accounts.google.com/o/oauth2/auth?state=private-state",
            "process_exit",
            "did not report a recognized cause",
        ),
    ],
)
def test_google_auth_failure_explains_exit_and_logs_safe_reference(
    output,
    reason,
    guidance,
    caplog,
):
    now = datetime.now(timezone.utc)
    session = BrokerAuthSession(
        provider="google",
        method="web",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        state={"status": "pending"},
    )
    session.process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import sys; sys.stderr.write({output!r}); sys.exit(1)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    with caplog.at_level(logging.INFO):
        _monitor_web_auth_process(
            session=session,
            timeout_seconds=5,
            logger=logging.getLogger(__name__),
        )
    assert session.state["status"] == "error"
    assert guidance in session.state["error"]
    assert session.state["failure_reason"] == reason
    reference = session.state["diagnostic_reference"]
    assert reference in caplog.text
    assert "event=broker_auth_process_failed" in caplog.text
    assert "exit_code=1" in caplog.text
    assert "private-state" not in caplog.text
    assert "accounts.google.com" not in caplog.text
    page = GoogleWebAuthFlow().render(session)
    assert reference in page
    assert "Technical Details" in page
    assert "Exit code: 1" in page
    assert (
        GoogleWebAuthFlow().status_payload(session)["diagnostic_reference"] == reference
    )


def test_google_auth_failure_after_redirect_keeps_stage_without_logging_code(caplog):
    now = datetime.now(timezone.utc)
    session = BrokerAuthSession(
        provider="google",
        method="web",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        state={"status": "pending"},
    )
    session.process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.readline(); sys.exit(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    GoogleWebAuthFlow._submit_redirect_url(
        session,
        "http://localhost:1234/?code=private-auth-code&state=private-state",
    )
    _monitor_web_auth_process(
        session=session, timeout_seconds=5, logger=logging.getLogger(__name__)
    )
    assert "after the redirect URL was submitted" in session.state["error"]
    assert session.state["auth_stage"] == "authorization_code_exchange"
    assert "private-auth-code" not in caplog.text
    assert "private-state" not in caplog.text
    assert "redirect_submitted=True" in caplog.text


def test_google_auth_email_mismatch_after_redirect_never_finalizes(monkeypatch, caplog):
    now = datetime.now(timezone.utc)
    session = BrokerAuthSession(
        provider="google",
        method="web",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        state={"status": "pending", "email": "new@example.com"},
    )
    output = (
        "Paste redirect URL (Enter or Ctrl-D): "
        "authorized as old@example.com, expected new@example.com"
    )
    session.process = subprocess.Popen(
        [sys.executable, "-c", f"import sys; sys.stdin.readline(); sys.stderr.write({output!r}); sys.exit(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def unexpected_finalize(*args):
        pytest.fail("A mismatched account must never verify or persist credentials")

    monkeypatch.setattr("secure_tool_broker.auth_flows._finalize_google_web_auth", unexpected_finalize)
    GoogleWebAuthFlow._submit_redirect_url(
        session, "http://localhost:1234/?code=private-auth-code&state=private-state"
    )
    _monitor_web_auth_process(session=session, timeout_seconds=5, logger=logging.getLogger(__name__))

    assert session.state["status"] == "error"
    assert session.state["email"] == "new@example.com"
    assert session.state["auth_stage"] == "authorization_code_exchange"
    assert session.state["failure_reason"] == "account_email_mismatch"
    assert "email shown after 'authorized as'" in session.state["error"]
    for private_value in ("old@example.com", "new@example.com", "private-auth-code", "private-state"):
        assert private_value not in caplog.text


def test_google_auth_timeout_preserves_timeout_reason(caplog):
    now = datetime.now(timezone.utc)
    session = BrokerAuthSession(
        provider="google",
        method="web",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        state={"status": "pending"},
    )
    session.process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    _monitor_web_auth_process(
        session=session, timeout_seconds=1, logger=logging.getLogger(__name__)
    )
    assert session.process.poll() is not None
    assert session.state["failure_reason"] == "timeout"
    assert "Start again with /link cli:google" in session.state["error"]
    assert "reason=timeout" in caplog.text
