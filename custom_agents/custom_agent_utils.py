"""
Utility functions for agent message parsing and JSON handling.
"""
import ast
import json
from typing import Any, Literal, overload


def parse_json_from_md(json_str: str) -> str:
    """
    Parses markdown-annotated JSON into a pure JSON string.
    Strips out "```json", "```" and similar markdown annotation.

    Args:
        json_str: A string that may contain markdown code block annotations.

    Returns:
        The JSON string with markdown annotations removed.
    """
    # Strip leading/trailing whitespace and newlines
    cleaned_str = json_str.strip()

    # Check for markdown code block annotations
    if cleaned_str.startswith("```json"):
        cleaned_str = cleaned_str[len("```json"):].strip()
        if cleaned_str.endswith("```"):
            cleaned_str = cleaned_str[:-len("```")].strip()
    elif cleaned_str.startswith("```"):  # Handle cases where language is not specified
        cleaned_str = cleaned_str[len("```"):].strip()
        if cleaned_str.endswith("```"):
            cleaned_str = cleaned_str[:-len("```")].strip()

    return cleaned_str


@overload
def parse_json_from_raw(raw: str, return_dict: Literal[True]) -> dict[str, Any]: ...


@overload
def parse_json_from_raw(raw: str, return_dict: Literal[False] = False) -> str: ...


@overload
def parse_json_from_raw(raw: str, return_dict: bool) -> dict[str, Any] | str: ...


def parse_json_from_raw(raw: str, return_dict: bool = False) -> dict[str, Any] | str:
    """
    Convert a string that looks like
        '{\\n  "msg_content": {...}, ... }'
    into a json string or a Python dict.

    It tolerates:
    - An outer single-quoted shell produced by repr/print
    - Embedded newline/indent escape sequences
    - Leading or trailing whitespace

    Args:
        raw: The raw string to parse.
        return_dict: If True, return a Python dict; otherwise return JSON string.

    Returns:
        Either a Python dict or a JSON string, depending on return_dict.

    Raises:
        ValueError: If the input is empty.
    """
    if not raw:
        raise ValueError("Input is empty")

    raw = raw.strip()

    # ----------------------------------------------------------
    # 1) Remove an outer single quote wrapper, if present
    # ----------------------------------------------------------
    if raw[0] == raw[-1] == "'":
        # ast.literal_eval safely turns the repr-string into
        # a real str (it also works for triple-quoted input)
        raw = ast.literal_eval(raw)

    # ----------------------------------------------------------
    # 2) Unescape \n, \t, \", … so we get real JSON text
    # ----------------------------------------------------------
    raw = raw.encode("utf-8").decode("unicode_escape")

    # ----------------------------------------------------------
    # 3) Final parse to Python objects
    # ----------------------------------------------------------
    if return_dict:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Parsed JSON must contain an object when return_dict=True")
        return parsed
    return raw
