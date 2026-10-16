"""Progress events emitted while the orchestrator and its workers run."""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

EventSource = Literal["orchestrator", "worker"]
EventKind = Literal[
    "start",  # worker/orchestrator began (data: task, model)
    "text",  # assistant text delta (data: delta)
    "tool_start",  # tool call began (data: tool, args)
    "tool_end",  # tool call finished (data: tool, is_error)
    "retry",  # pi auto-retry after a transient error (data: attempt, max_attempts, error)
    "end",  # finished (data: ok, error, duration_s, usage)
]


@dataclass(frozen=True)
class Event:
    source: EventSource
    kind: EventKind
    agent: str
    worker_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


EventCallback = Callable[[Event], Awaitable[None] | None]


async def emit(callback: EventCallback | None, event: Event) -> None:
    """Deliver an event; callback errors are logged, never propagated."""
    if callback is None:
        return
    try:
        result = callback(event)
        if inspect.isawaitable(result):
            await result
    except Exception:
        log.exception("event callback failed")


def summarize_args(args: Any, limit: int = 120) -> str:
    """Short, single-line rendering of tool arguments for progress output."""
    if isinstance(args, dict) and isinstance(args.get("tasks") or args.get("steps"), list):
        items = args.get("tasks") or args.get("steps")
        agents = [str(item.get("agent", "?")) for item in items if isinstance(item, dict)]
        noun = "tasks" if "tasks" in args else "steps"
        text = f"{len(items)} {noun}: {', '.join(agents)}"
    elif isinstance(args, dict):
        for key in ("path", "command", "pattern", "agent"):
            if key in args and isinstance(args[key], str):
                text = args[key]
                break
        else:
            text = ", ".join(f"{k}={v!r}" for k, v in args.items())
    else:
        text = str(args)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
