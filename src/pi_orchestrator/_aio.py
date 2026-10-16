"""asyncio helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

_background: set[asyncio.Future[Any]] = set()


async def finish_even_if_cancelled(coro: Coroutine[Any, Any, Any]) -> None:
    """Await ``coro`` to completion, even if the caller is cancelled meanwhile.

    Used for cleanup (aborting and reaping pi processes). If the awaiting task is cancelled,
    which anyio cancel scopes do repeatedly, the cleanup keeps running in the background
    (strongly referenced here) and the caller's cancellation still propagates.
    """
    task = asyncio.ensure_future(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    await asyncio.shield(task)
