"""Read bounded batches before applying service-owned visibility checks.

Repositories cannot decide current role membership. Services can iterate these
batches and select a page of visible records without materializing the team list.
"""

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Generic
from typing import TypeVar

T = TypeVar("T")
READ_BATCH_SIZE = 20


@dataclass(frozen=True)
class CountedPage(Generic[T]):
    items: tuple[T, ...]
    total: int
    has_more: bool


def counted_page(records: Iterable[T], *, offset: int, limit: int) -> CountedPage[T]:
    """Count authorized records while retaining only the requested page.

    Visibility must be applied by the caller before this selection. Reading in
    bounded batches avoids loading the full collection just to show a total.
    """
    items: list[T] = []
    total = 0
    for record in records:
        if offset <= total < offset + limit:
            items.append(record)
        total += 1
    return CountedPage(tuple(items), total, offset + limit < total)


def iter_record_batches(fetch: Callable[[int, int], Sequence[T]]) -> Iterator[T]:
    offset = 0
    while True:
        rows = fetch(READ_BATCH_SIZE, offset)
        yield from rows
        if len(rows) < READ_BATCH_SIZE:
            return
        offset += len(rows)
