"""Parse editable YAML without silently discarding repeated settings."""

import yaml

from team_transfer.errors import TransferError


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in keys
                keys.add(key)
            except TypeError as exc:
                raise TransferError(
                    "Editable YAML requires scalar setting names."
                ) from exc
            if duplicate:
                raise TransferError(f"Editable YAML repeats setting {key}.")
        return super().construct_mapping(node, deep=deep)


def load_editable_yaml(text):
    try:
        return yaml.load(text, Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        # Parser exception text includes the input. Return only its location
        # and recovery guidance, without exposing the edited value in logs.
        raise TransferError(
            f"Editable YAML is invalid{location}. Check indentation, brackets, and quoted values."
        ) from None
