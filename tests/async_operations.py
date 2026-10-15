"""Bind simple test doubles to the declared asynchronous operation contracts."""

import inspect
from typing import get_type_hints


def async_operation(operation):
    async def invoke(*args, **kwargs):
        result = operation(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    return invoke


def bind_async_operations(contract, operations):
    return {
        name: async_operation(operation)
        if inspect.iscoroutinefunction(getattr(annotation, "__call__", None))
        else operation
        for name, annotation in get_type_hints(contract).items()
        for operation in (operations[name],)
    }
