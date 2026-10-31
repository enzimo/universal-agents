"""Writers for memory store tests: who a test's fact changes are for, in the change ledger.

Store tests that are not about the change ledger write as system upkeep, which
the ledger records and never refuses (``storage.pinned_memory_changes``).
"""

from __future__ import annotations

from storage.change_ledger import ChangeWriter, ChangeWriterKind
from storage.pinned_memory_changes import MemoryWriter

SYSTEM_CHANGE_WRITER = ChangeWriter(ChangeWriterKind.SYSTEM)
SYSTEM_MEMORY_WRITER = MemoryWriter(team_id="team_test", writer=SYSTEM_CHANGE_WRITER)
