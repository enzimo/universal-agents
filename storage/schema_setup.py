"""Let processes that open one PostgreSQL store at the same time set up its tables in turn.

A store creates its tables and indexes and adds the columns an earlier
release's tables lack when it opens (``CREATE ... IF NOT EXISTS``,
``storage.table_columns.ensure_column``). The launcher starts the manager,
the startup agents and the gateway close together, and operator commands
open stores while they run, so several processes often open one store at
once. PostgreSQL does not make those statements safe to run together: two
transactions that both find an index or table missing both create it, and
one fails with a duplicate key in the system catalog
(``pg_class_relname_nsp_index``); one creating an index while another adds a
column to the same table can deadlock. Either error stops that process, and
the launcher then stops the runtime. It happens on a new install and on the
first start after an upgrade that adds a table, index or column.

``begin_schema_setup`` takes a transaction-level advisory lock named after
the module that owns the tables, as the first statement of the setup
transaction. A second process waits until the first commits and then finds
everything in place, which also makes the read in ``ensure_column`` exact,
so ``event=storage_column_added`` names only columns this process added. The
lock is released at commit or rollback, and different stores never wait for
each other. Table sets that several stores create (the change ledger,
schedule occurrences, scheduled-run problems, task custody) take their own
lock inside each store's setup, always after the store's own lock, so two
setups never hold one lock each while waiting for the other's.

SQLite needs no lock: a schema change takes the database's write lock and is
checked against the schema current at that point, and ``ensure_sqlite_column``
reads the columns again after "duplicate column".
"""

from __future__ import annotations

from typing import Any

from .sql_dialect import POSTGRES

__all__ = ["begin_schema_setup"]


def begin_schema_setup(conn: Any, owner: str) -> None:
    """Wait for any other process setting up ``owner``'s tables, on PostgreSQL; do nothing on SQLite.

    Call it before the first schema statement of the transaction, with the
    module name (``__name__``) of the code that owns the tables.
    """

    if getattr(conn, "dialect", None) is not POSTGRES:
        return
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"schema_setup:{owner}",))
