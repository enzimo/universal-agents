"""Shared model-key failure type, independent of lookup and provider classification."""


class ModelKeyError(RuntimeError):
    """A model call could not get or use a key; ``str(error)`` is written for the person."""
