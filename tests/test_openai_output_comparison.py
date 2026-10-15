import logging
from types import SimpleNamespace

import pytest

from custom_agents.openai_output_comparison import (
    encrypted_content_differences,
    material_output,
)
from custom_agents.openai_reasoning_responses_model import OpenAIReasoningResponsesModel


def call(arguments):
    return {
        "type": "function_call",
        "id": "fc1",
        "call_id": "call1",
        "name": "probe",
        "arguments": arguments,
    }


def test_json_formatting_only_is_debug_and_original_replay_is_preserved(caplog):
    stream = [call('{"b": 2, "a": 1}')]
    terminal = [call('{"a":1,"b":2}')]
    state = {}
    with caplog.at_level(
        logging.DEBUG, logger="custom_agents.openai_reasoning_responses_model"
    ):
        OpenAIReasoningResponsesModel._remember_output_batch(
            state, SimpleNamespace(id="resp1", model="test", output=terminal), stream
        )
    assert "material=False" in caplog.text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    assert (
        state["universal_agents_openai_reasoning_output_batches"][0]["output"] == stream
    )


@pytest.mark.parametrize(
    "left,right",
    [
        ('{"a":1,"a":2}', '{"a":2}'),
        ('{"a":NaN}', '{"a": NaN}'),
    ],
)
def test_real_or_ambiguous_arguments_remain_material(left, right):
    assert material_output([call(left)]) != material_output([call(right)])


def test_warning_identifies_fields_without_contents(caplog):
    stream = [{"type": "reasoning", "encrypted_content": "SECRET_ONE"}, call("{}")]
    terminal = [
        {
            "type": "reasoning",
            "encrypted_content": "SECRET_TWO",
            "PRIVATE_FIELD": "secret",
        },
        call('{"private_argument":42}'),
    ]
    OpenAIReasoningResponsesModel._remember_output_batch(
        {}, SimpleNamespace(id="resp1", model="test", output=terminal), stream
    )
    assert "material=True" in caplog.text
    assert "encrypted_content" in caplog.text
    assert "arguments" in caplog.text
    assert "other_field" in caplog.text
    assert "response_id=resp1" in caplog.text
    assert "encrypted_content_diagnostics=" in caplog.text
    assert "'equal_after_proxy_unwrap': False" in caplog.text
    for private in ("SECRET_ONE", "SECRET_TWO", "PRIVATE_FIELD", "private_argument"):
        assert private not in caplog.text


def test_encrypted_content_diagnostics_identify_one_sided_proxy_wrapper():
    stream = [{"type": "reasoning", "encrypted_content": "litellm_enc:bWV0YQ==;CIPHER"}]
    terminal = [{"type": "reasoning", "encrypted_content": "CIPHER"}]

    assert encrypted_content_differences(stream, terminal) == [
        {
            "index": 0,
            "stream_length": len("litellm_enc:bWV0YQ==;CIPHER"),
            "terminal_length": len("CIPHER"),
            "stream_proxy_wrapped": True,
            "terminal_proxy_wrapped": False,
            "equal_after_proxy_unwrap": True,
            "common_prefix_length": len("CIPHER"),
        }
    ]


def test_encrypted_content_diagnostics_report_missing_copy():
    assert encrypted_content_differences(
        [{"type": "reasoning", "encrypted_content": "CIPHER"}],
        [{"type": "reasoning"}],
    ) == [{"index": 0, "stream_present": True, "terminal_present": False}]


def reasoning(encrypted_content):
    return {"id": "rs1", "type": "reasoning", "encrypted_content": encrypted_content}


def test_provider_re_encryption_is_debug_and_stream_copy_is_replayed(caplog):
    """Same-length unwrapped ciphertext is OpenAI encrypting one item twice."""
    stream = [reasoning("gAAAAABpAAAAxyz-iv"), call("{}")]
    terminal = [reasoning("gAAAAABpAAAAabc-iv"), call("{}")]
    state = {}
    with caplog.at_level(
        logging.DEBUG, logger="custom_agents.openai_reasoning_responses_model"
    ):
        OpenAIReasoningResponsesModel._remember_output_batch(
            state, SimpleNamespace(id="resp1", model="test", output=terminal), stream
        )
    assert "material=False" in caplog.text
    assert "'common_prefix_length': 12" in caplog.text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    assert (
        state["universal_agents_openai_reasoning_output_batches"][0]["output"] == stream
    )
