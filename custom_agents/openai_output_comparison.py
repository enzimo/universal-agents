"""Compare response copies without changing the provider items used for replay."""

import json
from typing import Any


# LiteLLM's encrypted-content affinity wraps provider ciphertext as
# ``litellm_enc:{base64 metadata};{original}`` so follow-ups can be routed to the
# deployment that produced it. Detecting the wrapper separates a proxy rewrite
# from a provider-side difference without logging any ciphertext.
LITELLM_ENCRYPTED_CONTENT_PREFIX = "litellm_enc:"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("Non-JSON numeric constant")


def material_output(output: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ignore item lifecycle state, equivalent JSON arguments and re-encryption.

    OpenAI encrypts reasoning separately for the streamed and final copies of
    an item: both are Fernet tokens of the same plaintext that share only the
    version and timestamp, then differ from the random IV onward. Unwrapped
    ``encrypted_content`` is therefore compared by length only; a missing copy,
    a length change (truncation) or a LiteLLM wrapper on one side stays
    significant. Nested status fields and reasoning text remain significant.
    Ambiguous/invalid JSON stays byte-for-byte significant. Never use these
    comparison values for replay or invocation.
    """
    result = []
    for item in output:
        comparison = {key: value for key, value in item.items() if key != "status"}
        encrypted_content = item.get("encrypted_content")
        if isinstance(encrypted_content, str) and not encrypted_content.startswith(
            LITELLM_ENCRYPTED_CONTENT_PREFIX
        ):
            comparison["encrypted_content"] = ("encrypted_length", len(encrypted_content))
        if item.get("type") == "function_call" and isinstance(
            item.get("arguments"), str
        ):
            try:
                arguments = json.loads(
                    item["arguments"],
                    object_pairs_hook=_unique_object,
                    parse_constant=_invalid_constant,
                )
                comparison["arguments"] = json.dumps(
                    arguments, sort_keys=True, ensure_ascii=True, allow_nan=False
                )
            except (ValueError, OverflowError, RecursionError):
                pass  # Retain the original string when equality cannot be established.
        result.append(comparison)
    return result


def difference_fields(
    stream: list[dict[str, Any]], terminal: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Describe conflicting fields without logging content or arbitrary key names."""
    known = {
        "id",
        "type",
        "call_id",
        "name",
        "arguments",
        "status",
        "content",
        "summary",
        "encrypted_content",
        "role",
    }
    differences = []
    for index in range(max(len(stream), len(terminal))):
        if index >= len(stream) or index >= len(terminal):
            fields = ["missing_item"]
        else:
            left, right = stream[index], terminal[index]
            changed = {
                key
                for key in left.keys() | right.keys()
                if (key in left) != (key in right) or left.get(key) != right.get(key)
            }
            fields = sorted(changed & known)
            if changed - known:
                fields.append("other_field")
        if fields:
            differences.append({"index": index, "fields": fields})
        if len(differences) == 16:
            break
    return differences


def _without_litellm_wrapper(value: str) -> str:
    if not value.startswith(LITELLM_ENCRYPTED_CONTENT_PREFIX):
        return value
    _metadata, separator, original = value.partition(";")
    return original if separator else value


def _common_prefix_length(left: str, right: str) -> int:
    length = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        length += 1
    return length


def encrypted_content_differences(
    stream: list[dict[str, Any]], terminal: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Describe how differing ``encrypted_content`` copies differ, without content.

    A copy wrapped by LiteLLM on only one side that matches after unwrapping
    identifies a proxy rewrite. Unwrapped copies that still differ with only a
    short shared prefix point to the provider issuing different ciphertext; a
    long shared prefix with different lengths points to truncation.
    """
    differences = []
    for index, (left, right) in enumerate(zip(stream, terminal)):
        left_value = left.get("encrypted_content")
        right_value = right.get("encrypted_content")
        if left_value == right_value:
            continue
        if not isinstance(left_value, str) or not isinstance(right_value, str):
            differences.append(
                {
                    "index": index,
                    "stream_present": isinstance(left_value, str),
                    "terminal_present": isinstance(right_value, str),
                }
            )
            continue
        left_original = _without_litellm_wrapper(left_value)
        right_original = _without_litellm_wrapper(right_value)
        differences.append(
            {
                "index": index,
                "stream_length": len(left_value),
                "terminal_length": len(right_value),
                "stream_proxy_wrapped": left_value.startswith(
                    LITELLM_ENCRYPTED_CONTENT_PREFIX
                ),
                "terminal_proxy_wrapped": right_value.startswith(
                    LITELLM_ENCRYPTED_CONTENT_PREFIX
                ),
                "equal_after_proxy_unwrap": left_original == right_original,
                "common_prefix_length": _common_prefix_length(
                    left_original, right_original
                ),
            }
        )
        if len(differences) == 16:
            break
    return differences
