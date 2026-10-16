"""Delegation engine: run agents singly, in parallel or as a chain.

Used by the standalone orchestrator (via the pi shim) and by the MCP server (Claude Code).
"""

from __future__ import annotations

import asyncio
import itertools
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .agents import AgentCatalog, AgentDefinition, UnknownAgentError
from .config import Settings
from .events import EventCallback
from .worker import Usage, WorkerResult, run_worker

PREVIOUS_PLACEHOLDER = "{previous}"


class DelegationError(ValueError):
    """The delegation request is invalid (unknown agent, too many tasks, …)."""


@dataclass(frozen=True)
class TaskSpec:
    agent: str
    task: str
    model: str | None = None

    @classmethod
    def coerce(cls, value: TaskSpec | Mapping[str, Any]) -> TaskSpec:
        if isinstance(value, TaskSpec):
            return value
        if not isinstance(value, Mapping):
            raise DelegationError("each task must be an object with `agent` and `task`")
        agent, task = value.get("agent"), value.get("task")
        if not isinstance(agent, str) or not agent.strip():
            raise DelegationError("each task needs a non-empty `agent`")
        if not isinstance(task, str) or not task.strip():
            raise DelegationError(f"task for agent {agent!r} needs non-empty `task` text")
        model = value.get("model")
        return cls(agent=agent.strip(), task=task, model=model if isinstance(model, str) and model else None)


@dataclass
class DelegationOutcome:
    mode: str
    results: list[WorkerResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if not self.results:
            return False
        if self.mode == "parallel":
            return any(r.ok for r in self.results)
        return all(r.ok for r in self.results)

    @property
    def usage(self) -> Usage:
        total = Usage()
        for result in self.results:
            total = total + result.usage
        return total


def _truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n\n[… {len(text) - limit:,} characters omitted by pi-orchestrator …]\n\n"
    keep = max(limit - len(marker), 0)
    head = keep * 2 // 3
    return text[:head] + marker + text[len(text) - (keep - head) :]


def format_outcome(outcome: DelegationOutcome, *, max_chars: int, planned_steps: int | None = None) -> str:
    """Markdown report for the orchestrating model, at most ~``max_chars`` long."""
    results = outcome.results
    succeeded = sum(r.ok for r in results)
    if outcome.mode == "single":
        header = ""
    elif outcome.mode == "parallel":
        header = f"# Parallel delegation: {succeeded}/{len(results)} succeeded\n"
    else:
        total = planned_steps or len(results)
        status = "completed" if outcome.ok and len(results) == total else "stopped"
        header = f"# Chain {status}: {succeeded}/{total} steps succeeded\n"

    sections: list[tuple[str, str]] = []
    for index, result in enumerate(results, 1):
        label = f"{index}. " if outcome.mode != "single" else ""
        status = "ok" if result.ok else f"FAILED: {result.error or result.stop_reason or 'unknown error'}"
        meta = f"{result.duration_s:.1f}s · {result.usage.short()}"
        if result.model:
            meta += f" · {result.model}"
        title = f"## {label}{result.agent} — {status}\n_{meta}_\n"
        body = result.output.strip() or "(no output)"
        sections.append((title, body))

    budget = max(max_chars - len(header) - sum(len(t) for t, _ in sections), 1000)
    per_section = max(budget // max(len(sections), 1), 500)
    parts = [header] + [f"{title}\n{_truncate_middle(body, per_section)}\n" for title, body in sections]
    return "\n".join(part for part in parts if part).strip() + "\n"


class Delegator:
    """Runs delegation requests against an :class:`AgentCatalog`."""

    def __init__(
        self,
        settings: Settings,
        catalog: AgentCatalog,
        *,
        cwd: str | os.PathLike[str],
        on_event: EventCallback | None = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.cwd = os.fspath(cwd)
        self.on_event = on_event
        self.history: list[WorkerResult] = []
        self._ids = itertools.count(1)

    def _resolve(self, spec: TaskSpec) -> AgentDefinition:
        try:
            return self.catalog.get(spec.agent).restricted(self.settings.read_only)
        except UnknownAgentError as exc:
            raise DelegationError(str(exc)) from None

    async def _run(self, spec: TaskSpec) -> WorkerResult:
        agent = self._resolve(spec)
        result = await run_worker(
            agent,
            spec.task,
            self.settings,
            cwd=self.cwd,
            model=spec.model,
            on_event=self.on_event,
            worker_id=f"{agent.name}#{next(self._ids)}",
        )
        return result

    async def single(self, agent: str, task: str, *, model: str | None = None) -> DelegationOutcome:
        spec = TaskSpec.coerce({"agent": agent, "task": task, "model": model})
        result = await self._run(spec)
        self.history.append(result)
        return DelegationOutcome("single", [result])

    async def parallel(self, tasks: Iterable[TaskSpec | Mapping[str, Any]]) -> DelegationOutcome:
        specs = [TaskSpec.coerce(t) for t in tasks]
        if not specs:
            raise DelegationError("`tasks` must contain at least one task")
        if len(specs) > self.settings.max_parallel_tasks:
            raise DelegationError(
                f"too many parallel tasks ({len(specs)}); the maximum is {self.settings.max_parallel_tasks}"
            )
        agents = [self._resolve(spec) for spec in specs]
        writers = [a.name for a in agents if a.is_write_capable]
        if len(writers) > 1 and not self.settings.allow_parallel_writers:
            raise DelegationError(
                "at most one write-capable agent may run in a parallel batch "
                f"(got {', '.join(writers)}); run writers sequentially with delegate_task or delegate_chain"
            )
        semaphore = asyncio.Semaphore(max(1, self.settings.max_concurrency))

        async def limited(spec: TaskSpec) -> WorkerResult:
            async with semaphore:
                return await self._run(spec)

        async with asyncio.TaskGroup() as group:
            futures = [group.create_task(limited(spec)) for spec in specs]
        results = [f.result() for f in futures]
        self.history.extend(results)  # submission order, matching the report
        return DelegationOutcome("parallel", results)

    async def chain(self, steps: Iterable[TaskSpec | Mapping[str, Any]]) -> DelegationOutcome:
        specs = [TaskSpec.coerce(s) for s in steps]
        if not specs:
            raise DelegationError("`steps` must contain at least one step")
        for spec in specs:
            self._resolve(spec)
        outcome = DelegationOutcome("chain")
        previous = ""
        for spec in specs:
            task = spec.task.replace(PREVIOUS_PLACEHOLDER, previous)
            result = await self._run(TaskSpec(spec.agent, task, spec.model))
            self.history.append(result)
            outcome.results.append(result)
            if not result.ok:
                break
            previous = result.output
        return outcome

    async def handle(self, op: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
        """Serve one shim request: ``{"ok", "text", "details"}`` or ``{"ok": False, "error"}``."""
        params = params or {}
        try:
            if op == "ping":
                return {"ok": True, "text": "pi-orchestrator", "details": {}}
            if op == "delegate_task":
                outcome = await self.single(params.get("agent", ""), params.get("task", ""))
                planned = 1
            elif op == "delegate_parallel":
                outcome = await self.parallel(_as_list(params.get("tasks"), "tasks"))
                planned = len(outcome.results)
            elif op == "delegate_chain":
                steps = _as_list(params.get("steps"), "steps")
                outcome = await self.chain(steps)
                planned = len(steps)
            else:
                return {"ok": False, "error": f"unknown operation {op!r}"}
        except DelegationError as exc:
            return {"ok": False, "error": str(exc)}
        text = format_outcome(outcome, max_chars=self.settings.max_output_chars, planned_steps=planned)
        details = {
            "mode": outcome.mode,
            "results": [r.to_dict(include_output=False) for r in outcome.results],
        }
        if outcome.ok:
            return {"ok": True, "text": text, "details": details}
        return {"ok": False, "error": text, "details": details}


def _as_list(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise DelegationError(f"`{name}` must be a list")
    return value
