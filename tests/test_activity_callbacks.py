from __future__ import annotations

import io

from custom_agents.activity_callbacks import (
    TerminalReasoningCallbackHandler,
    TerminalReasoningDiagnosticsCallbackHandler,
)


class _FakeLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    def info(self, message: str, *args: object) -> None:
        self.info_messages.append(message % args)

    def warning(self, message: str, *args: object) -> None:
        self.warning_messages.append(message % args)


def test_terminal_reasoning_callback_terminates_open_stream_on_result() -> None:
    stream = io.StringIO()
    callback = TerminalReasoningCallbackHandler(stream=stream)

    callback(reasoningText="partial trace")
    callback(result=object())

    assert stream.getvalue() == "[provider reasoning] partial trace\n"


def test_terminal_reasoning_diagnostics_counts_raw_and_normalized_reasoning() -> None:
    logger = _FakeLogger()
    callback = TerminalReasoningDiagnosticsCallbackHandler(
        logger=logger,
        agent_id="agent_1",
        task_id="task_1",
    )

    callback(event={"contentBlockDelta": {"delta": {"reasoningContent": {"text": "think"}}}})
    callback(reasoningText="think", reasoning=True, delta={"reasoningContent": {"text": "think"}})
    callback(event={"contentBlockDelta": {"delta": {"text": "OK"}}})
    callback(data="OK")
    callback(result=object())

    assert not logger.warning_messages
    assert len(logger.info_messages) == 1
    summary = logger.info_messages[0]
    assert "task_id=task_1" in summary
    assert "raw_reasoning_chunks=1" in summary
    assert "raw_reasoning_text_chars=5" in summary
    assert "normalized_reasoning_events=1" in summary
    assert "normalized_reasoning_chars=5" in summary
    assert "raw_text_chunks=1" in summary
    assert "normalized_data_events=1" in summary
