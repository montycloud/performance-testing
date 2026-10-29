"""Parallel-within, sequential-between request batches."""

from __future__ import annotations

from collections.abc import Callable, Iterable

import gevent.pool


def run_batch(callables: Iterable[Callable[[], object]]) -> None:
    """Spawn every callable as a greenlet and wait for all of them.

    Calls inside one batch fire in parallel; the caller awaits run_batch()
    before starting the next batch, which keeps batches sequential.
    """
    group = gevent.pool.Group()
    for fn in callables:
        group.spawn(fn)
    group.join()
