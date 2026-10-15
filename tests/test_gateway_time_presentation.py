from __future__ import annotations


from time_presentation import format_user_timestamp


def test_format_user_timestamp_logs_invalid_input_without_exposing_raw_value(
    caplog,
) -> None:
    result = format_user_timestamp(
        "not-a-timestamp",
        timezone_name="Not/A-Timezone",
    )

    assert result == "Unavailable"
    assert "event=user_timestamp_parse_failed" in caplog.text


def test_format_user_timestamp_logs_invalid_timezone_and_uses_utc(caplog) -> None:
    result = format_user_timestamp(
        "2026-08-03T21:21:01Z",
        timezone_name="Not/A-Timezone",
    )

    assert result == "Aug 3, 2026 at 9:21:01 PM UTC"
    assert "event=user_timezone_invalid" in caplog.text
