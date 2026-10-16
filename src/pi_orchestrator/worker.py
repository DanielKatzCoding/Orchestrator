"""Run one agent on one task in its own, isolated ``pi --mode rpc`` process."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from ._aio import finish_even_if_cancelled
from .agents import AgentDefinition
from .config import Settings
from .events import Event, EventCallback, EventSource, emit, summarize_args
from .pi_rpc import PiRpcClient, PiRpcError

PI_DISCOVERY_FLAGS = ("--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes")


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    turns: int = 0
    tool_calls: int = 0

    @classmethod
    def from_stats(cls, stats: dict[str, Any]) -> Usage:
        tokens = stats.get("tokens") or {}
        return cls(
            input_tokens=int(tokens.get("input") or 0),
            output_tokens=int(tokens.get("output") or 0),
            cache_read_tokens=int(tokens.get("cacheRead") or 0),
            cache_write_tokens=int(tokens.get("cacheWrite") or 0),
            total_tokens=int(tokens.get("total") or 0),
            cost=float(stats.get("cost") or 0.0),
            turns=int(stats.get("assistantMessages") or 0),
            tool_calls=int(stats.get("toolCalls") or 0),
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(**{k: getattr(self, k) + getattr(other, k) for k in asdict(self)})

    def short(self) -> str:
        parts = [f"{self.turns} turn{'s' if self.turns != 1 else ''}"]
        if self.total_tokens:
            parts.append(f"{self.total_tokens:,} tokens")
        if self.cost:
            parts.append(f"${self.cost:.4f}")
        return " · ".join(parts)


@dataclass
class WorkerResult:
    agent: str
    task: str
    ok: bool
    output: str
    error: str | None = None
    stop_reason: str | None = None
    model: str | None = None
    usage: Usage = field(default_factory=Usage)
    duration_s: float = 0.0
    worker_id: str = ""

    def to_dict(self, *, include_output: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not include_output:
            data.pop("output")
        return data


def resolve_worker_model(agent: AgentDefinition, settings: Settings, override: str | None = None) -> str | None:
    """Per-call override → agent frontmatter → settings.worker_model → settings.model → pi default."""
    return override or agent.model or settings.worker_model or settings.model


def build_worker_argv(agent: AgentDefinition, settings: Settings, *, model: str | None) -> list[str]:
    argv = [*settings.pi_command, "--mode", "rpc", "--no-session", *PI_DISCOVERY_FLAGS]
    argv += settings.extension_args()
    argv += ["--tools", ",".join(agent.tools)] if agent.tools else ["--no-tools"]
    if model:
        argv += ["--model", model]
    if agent.thinking:
        argv += ["--thinking", agent.thinking]
    if not (agent.context_files and settings.load_context_files):
        argv.append("--no-context-files")
    flag = "--append-system-prompt" if agent.system_prompt_mode == "append" else "--system-prompt"
    argv += [flag, agent.prompt]
    return argv


def translate_record(record: dict[str, Any], *, source: EventSource, agent: str, worker_id: str | None) -> Event | None:
    """Map a raw pi RPC event to an orchestrator :class:`Event` (or None to drop it)."""
    kind = record.get("type")
    if kind == "message_update":
        delta = record.get("assistantMessageEvent") or {}
        if delta.get("type") == "text_delta" and delta.get("delta"):
            return Event(source, "text", agent, worker_id, {"delta": delta["delta"]})
        return None
    if kind == "tool_execution_start":
        tool = record.get("toolName") or "?"
        return Event(source, "tool_start", agent, worker_id, {"tool": tool, "args": summarize_args(record.get("args"))})
    if kind == "tool_execution_end":
        return Event(
            source,
            "tool_end",
            agent,
            worker_id,
            {"tool": record.get("toolName"), "is_error": bool(record.get("isError"))},
        )
    if kind == "auto_retry_start":
        return Event(
            source,
            "retry",
            agent,
            worker_id,
            {
                "attempt": record.get("attempt"),
                "max_attempts": record.get("maxAttempts"),
                "error": record.get("errorMessage"),
            },
        )
    return None


async def run_worker(
    agent: AgentDefinition,
    task: str,
    settings: Settings,
    *,
    cwd: str | os.PathLike[str],
    model: str | None = None,
    on_event: EventCallback | None = None,
    worker_id: str | None = None,
) -> WorkerResult:
    """Run ``agent`` on ``task``. Model/tool failures come back as ``ok=False``; cancellation propagates."""
    agent = agent.restricted(settings.read_only)
    resolved_model = resolve_worker_model(agent, settings, model)
    worker_id = worker_id or f"{agent.name}-{uuid.uuid4().hex[:6]}"
    started = time.monotonic()
    await emit(
        on_event,
        Event(
            "worker",
            "start",
            agent.name,
            worker_id,
            {"task": task, "model": resolved_model, "tools": list(agent.tools)},
        ),
    )

    async def forward(record: dict[str, Any]) -> None:
        event = translate_record(record, source="worker", agent=agent.name, worker_id=worker_id)
        if event is not None:
            await emit(on_event, event)

    client = PiRpcClient(build_worker_argv(agent, settings, model=resolved_model), cwd=cwd, env=settings.process_env())
    result = WorkerResult(agent=agent.name, task=task, ok=False, output="", model=resolved_model, worker_id=worker_id)
    needs_abort = False

    async def shutdown() -> None:
        if needs_abort:
            await client.abort()
        await client.close(grace_s=2.0 if needs_abort else 5.0)

    try:
        await client.start()
        try:
            outcome = await asyncio.wait_for(client.prompt_and_wait(task, on_record=forward), settings.worker_timeout_s)
        except TimeoutError:
            needs_abort = True
            result.error = f"timed out after {settings.worker_timeout_s:.0f}s"
        else:
            result.ok = outcome.ok
            result.output = outcome.text
            result.error = outcome.error
            result.stop_reason = outcome.stop_reason
            result.model = outcome.model or resolved_model
            result.usage = Usage.from_stats(outcome.stats)
    except asyncio.CancelledError:
        needs_abort = True
        result.error = "cancelled"
        raise
    except PiRpcError as exc:
        result.error = str(exc)
    finally:
        await finish_even_if_cancelled(shutdown())
        result.duration_s = round(time.monotonic() - started, 2)
        await emit(
            on_event,
            Event(
                "worker",
                "end",
                agent.name,
                worker_id,
                {
                    "ok": result.ok,
                    "error": result.error,
                    "duration_s": result.duration_s,
                    "usage": asdict(result.usage),
                },
            ),
        )
    return result
