from pathlib import Path

from internal_credentials import (
    InternalCredentialRequest,
    generate_internal_credentials,
    generated_username,
    merge_env_file,
    parse_env_file,
)


def test_generated_username_is_human_readable() -> None:
    assert generated_username("My Assistant!") == "team_my_assistant"


def test_generate_preserves_existing_values_by_default() -> None:
    values = generate_internal_credentials(
        InternalCredentialRequest(instance_name="bot"),
        existing={
            "ACTIVITY_COLLECTOR_USER_TOKEN": "acu_existing",
            "ACTIVITY_COLLECTOR_ADMIN_TOKEN": "aca_existing",
            "ACTIVITY_COLLECTOR_INGEST_TOKEN": "aci_existing",
            "WEB_CHAT_AUTH_TOKEN": "uaweb_existing",
        },
    )

    assert values["ACTIVITY_COLLECTOR_USER_TOKEN"] == "acu_existing"
    assert values["ACTIVITY_COLLECTOR_ADMIN_TOKEN"] == "aca_existing"
    assert values["ACTIVITY_COLLECTOR_INGEST_TOKEN"] == "aci_existing"
    assert values["WEB_CHAT_AUTH_TOKEN"] == "uaweb_existing"


def test_generate_rotates_existing_internal_tokens() -> None:
    values = generate_internal_credentials(
        InternalCredentialRequest(instance_name="bot", rotate=True),
        existing={
            "ACTIVITY_COLLECTOR_USER_TOKEN": "acu_existing",
            "ACTIVITY_COLLECTOR_ADMIN_TOKEN": "aca_existing",
            "ACTIVITY_COLLECTOR_INGEST_TOKEN": "aci_existing",
            "WEB_CHAT_AUTH_TOKEN": "uaweb_existing",
        },
    )

    assert values["ACTIVITY_COLLECTOR_USER_TOKEN"] != "acu_existing"
    assert values["ACTIVITY_COLLECTOR_ADMIN_TOKEN"] != "aca_existing"
    assert values["ACTIVITY_COLLECTOR_INGEST_TOKEN"] != "aci_existing"
    assert values["WEB_CHAT_AUTH_TOKEN"] != "uaweb_existing"


def test_postgres_dsn_generated_only_for_postgres() -> None:
    sqlite_values = generate_internal_credentials(InternalCredentialRequest(instance_name="bot"))
    postgres_values = generate_internal_credentials(
        InternalCredentialRequest(instance_name="bot", storage_backend="postgres")
    )

    assert "POSTGRES_DSN" not in sqlite_values
    assert postgres_values["POSTGRES_DSN"].startswith("postgresql://team_bot:")


def test_merge_env_file_preserves_existing_and_adds_missing(tmp_path: Path) -> None:
    env_file = tmp_path / "team.env"
    env_file.write_text("EXA_API_KEY=external\nACTIVITY_COLLECTOR_USER_TOKEN=acu_existing\n", encoding="utf-8")

    values = generate_internal_credentials(
        InternalCredentialRequest(instance_name="bot"),
        existing=parse_env_file(env_file),
    )
    merged = merge_env_file(env_file, values)

    assert merged["EXA_API_KEY"] == "external"
    assert merged["ACTIVITY_COLLECTOR_USER_TOKEN"] == "acu_existing"
    assert merged["ACTIVITY_COLLECTOR_ADMIN_TOKEN"]
    assert merged["ACTIVITY_COLLECTOR_INGEST_TOKEN"]
    text = env_file.read_text(encoding="utf-8")
    assert "EXA_API_KEY=external" in text
    assert "ACTIVITY_COLLECTOR_ADMIN_TOKEN=" in text
    assert "ACTIVITY_COLLECTOR_INGEST_TOKEN=" in text


def test_merge_env_file_rotates_internal_values(tmp_path: Path) -> None:
    env_file = tmp_path / "team.env"
    env_file.write_text(
        "NATS_URL=nats://nats.internal:4222\nACTIVITY_COLLECTOR_USER_TOKEN=old\nACTIVITY_COLLECTOR_ADMIN_TOKEN=old\n",
        encoding="utf-8",
    )

    values = generate_internal_credentials(
        InternalCredentialRequest(instance_name="bot", rotate=True),
        existing=parse_env_file(env_file),
    )
    merged = merge_env_file(env_file, values, rotate=True)

    assert merged["NATS_URL"] == "nats://nats.internal:4222"
    assert merged["ACTIVITY_COLLECTOR_USER_TOKEN"] != "old"
    assert merged["ACTIVITY_COLLECTOR_ADMIN_TOKEN"] != "old"
    assert merged["ACTIVITY_COLLECTOR_INGEST_TOKEN"].startswith("aci_")


def test_merge_env_file_removes_stale_internal_values(tmp_path: Path) -> None:
    env_file = tmp_path / "team.env"
    env_file.write_text(
        "NATS_URL=nats://nats.internal:4222\nPOSTGRES_DSN=postgresql://old\nSECURE_TOOL_BROKER_TOKEN=old\n",
        encoding="utf-8",
    )

    merged = merge_env_file(
        env_file,
        {"NATS_URL": "nats://nats.internal:4222"},
        remove_keys={"POSTGRES_DSN", "SECURE_TOOL_BROKER_TOKEN"},
    )

    assert merged["NATS_URL"] == "nats://nats.internal:4222"
    assert "POSTGRES_DSN" not in merged
    assert "SECURE_TOOL_BROKER_TOKEN" not in merged
    text = env_file.read_text(encoding="utf-8")
    assert "POSTGRES_DSN=" not in text
    assert "SECURE_TOOL_BROKER_TOKEN=" not in text


def test_broker_token_and_keyring_password_are_always_generated() -> None:
    # The secure tool broker always runs, so there is no switch for its secrets.
    generated = generate_internal_credentials(InternalCredentialRequest(instance_name="My Bot"))

    assert generated["SECURE_TOOL_BROKER_TOKEN"].startswith("stb_")
    assert generated["GOG_KEYRING_PASSWORD"].startswith("gog_")
    assert not hasattr(InternalCredentialRequest(instance_name="x"), "include_broker_token")


def test_generate_internal_credentials_includes_owned_service_secrets() -> None:
    generated = generate_internal_credentials(
        InternalCredentialRequest(
            instance_name="My Bot",
            storage_backend="postgres",
            include_nats_credentials=True,
        )
    )

    assert generated["NATS_USER"] == "team_my_bot"
    assert generated["NATS_PASSWORD"].startswith("nats_")
    assert generated["POSTGRES_DSN"].startswith("postgresql://team_my_bot:")
    assert generated["ACTIVITY_COLLECTOR_USER_TOKEN"].startswith("acu_")
    assert generated["ACTIVITY_COLLECTOR_ADMIN_TOKEN"].startswith("aca_")
    assert generated["ACTIVITY_COLLECTOR_INGEST_TOKEN"].startswith("aci_")
    assert generated["WEB_CHAT_AUTH_TOKEN"].startswith("uaweb_")
    assert generated["SECURE_TOOL_BROKER_TOKEN"].startswith("stb_")
    assert generated["GOG_KEYRING_PASSWORD"].startswith("gog_")


def test_generate_internal_credentials_preserves_file_secret_surfaces() -> None:
    generated = generate_internal_credentials(
        InternalCredentialRequest(
            instance_name="My Bot",
            storage_backend="postgres",
            include_nats_credentials=True,
        ),
        existing={
            "NATS_PASSWORD_FILE": "/run/secrets/nats_password",
            "POSTGRES_DSN_FILE": "/run/secrets/postgres_dsn",
            "ACTIVITY_COLLECTOR_USER_TOKEN_FILE": "/run/secrets/activity_user",
            "ACTIVITY_COLLECTOR_INGEST_TOKEN_FILE": "/run/secrets/activity_ingest",
            "WEB_CHAT_AUTH_TOKEN_FILE": "/run/secrets/web_chat",
            "SECURE_TOOL_BROKER_TOKEN_FILE": "/run/secrets/broker_token",
            "GOG_KEYRING_PASSWORD_FILE": "/run/secrets/gog_keyring",
        },
    )

    assert generated["NATS_USER"] == "team_my_bot"
    assert "NATS_PASSWORD" not in generated
    assert "POSTGRES_DSN" not in generated
    assert "ACTIVITY_COLLECTOR_USER_TOKEN" not in generated
    assert generated["ACTIVITY_COLLECTOR_ADMIN_TOKEN"].startswith("aca_")
    assert "ACTIVITY_COLLECTOR_INGEST_TOKEN" not in generated
    assert "WEB_CHAT_AUTH_TOKEN" not in generated
    assert "SECURE_TOOL_BROKER_TOKEN" not in generated
    assert "GOG_KEYRING_PASSWORD" not in generated


def test_generate_internal_credentials_replaces_guest_nats_fallback() -> None:
    generated = generate_internal_credentials(
        InternalCredentialRequest(
            instance_name="My Bot",
            include_nats_credentials=True,
        ),
        existing={
            "NATS_USER": "guest",
            "NATS_PASSWORD": "guest",
        },
    )

    assert generated["NATS_USER"] == "team_my_bot"
    assert generated["NATS_PASSWORD"] != "guest"
