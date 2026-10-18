"""Inject lost write acknowledgements and readback outages around real repositories."""

import sqlite3
from contextlib import contextmanager


@contextmanager
def uncertain_store(monkeypatch, store, operation, *, committed=False, fail_on=1):
    original = getattr(store, operation)
    read = store.get_request
    outage = False
    calls = 0

    def uncertain_write(*args, **kwargs):
        nonlocal outage, calls
        calls += 1
        if calls != fail_on:
            return original(*args, **kwargs)
        if committed:
            original(*args, **kwargs)
        outage = True
        raise sqlite3.OperationalError("connection lost while saving the outcome")

    def unavailable_readback(key):
        if outage:
            raise sqlite3.OperationalError("the store is still unreachable")
        return read(key)

    with monkeypatch.context() as failure:
        failure.setattr(store, operation, uncertain_write)
        failure.setattr(store, "get_request", unavailable_readback)
        yield
