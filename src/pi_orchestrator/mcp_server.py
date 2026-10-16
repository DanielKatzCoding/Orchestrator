"""MCP server exposing pi delegation to MCP clients such as Claude Code.

Inside Claude Code, Claude itself is the orchestrator: it calls these tools to hand isolated,
self-contained tasks to pi agents (any provider/model pi supports) and combines the reports.
``orchestrate`` runs the full standalone pi orchestrator instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .agents import load_agents
from .config import Settings
from .delegation import DelegationError, DelegationOutcome, Delegator, format_outcome
from .events import Event
from .orchestrator import Orchestrator

T = TypeVar("T")

SERVER_INSTRUCTIONS = """\
pi-orchestrator runs specialist pi coding agents (scout, planner, worker, reviewer, plus any custom
agents) in isolated sessions with their own context windows, tools and models.

- Call `list_agents` first to see what is available.
- Each task must be self-contained: the agent has not seen this conversation. Include goals, paths,
  constraints and the findings it needs.
- Always pass `cwd` = the absolute path of the project the agent should work in.
- `delegate_parallel` is for independent tasks (at most one agent that edits files per batch);
  `delegate_chain` pipes each step's output into the next via `{previous}`.
- Long runs report progress; if your client turns the call into a background task, wait for it.
"""


class TaskInput(BaseModel):
    agent: str = Field(description="Agent name, from list_agents")
    task: str = Field(description="Complete, self-contained instructions for the agent")
    model: str | None = Field(default=None, description="Optional model override, provider/id[:thinking]")


class _Progress:
    """Turns orchestrator events into throttled MCP progress notifications plus a heartbeat.

    Claude Code aborts a stdio tool call after ~30 min without any progress notification,
    so long delegations keep the call alive with a heartbeat.
    """

    def __init__(self, ctx: Context | None, *, min_interval_s: float = 2.0, heartbeat_s: float = 20.0) -> None:
        self.ctx = ctx
        self.min_interval_s = min_interval_s
        self.heartbeat_s = heartbeat_s
        self.count = 0
        self.last_sent = 0.0
        self.started = time.monotonic()
        self.active: dict[str, str] = {}

    async def send(self, message: str) -> None:
        if self.ctx is None:
            return
        self.count += 1
        self.last_sent = time.monotonic()
        with contextlib.suppress(Exception):
            await self.ctx.report_progress(self.count, None, message)

    async def on_event(self, event: Event) -> None:
        tag = event.worker_id or event.agent
        if event.kind == "start" and event.source == "worker":
            self.active[tag] = event.agent
            message = f"{tag} started"
        elif event.kind == "end" and event.source == "worker":
            self.active.pop(tag, None)
            message = f"{tag} {'finished' if event.data.get('ok') else 'failed'}"
        elif event.kind == "tool_start":
            message = f"{tag}: {event.data.get('tool')} {event.data.get('args', '')}".rstrip()
        elif event.kind == "retry":
            message = f"{tag}: retrying after error"
        else:
            return
        forced = event.kind in ("start", "end")
        if forced or time.monotonic() - self.last_sent >= self.min_interval_s:
            await self.send(message)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            elapsed = int(time.monotonic() - self.started)
            running = ", ".join(sorted(self.active)) or "orchestrator"
            await self.send(f"still running ({elapsed}s): {running}")

    async def wrap(self, work: Callable[[], Awaitable[T]]) -> T:
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            return await work()
        finally:
            heartbeat.cancel()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80] + f"\n\n[… truncated by pi-orchestrator, {len(text) - limit + 80:,} chars omitted]"


def create_server(settings: Settings | None = None, *, default_cwd: str | None = None) -> MCPServer:
    base_settings = settings or Settings.from_env()

    def resolve_cwd(cwd: str | None) -> str:
        candidate = cwd or default_cwd or os.environ.get("PI_ORCHESTRATOR_CWD") or os.environ.get("CLAUDE_PROJECT_DIR")
        path = Path(candidate or os.getcwd()).expanduser()
        if not path.is_absolute():
            path = Path(os.getcwd()) / path
        if not path.is_dir():
            raise ToolError(f"cwd {str(path)!r} is not a directory")
        return str(path.resolve())

    def delegator_for(cwd: str, progress: _Progress) -> Delegator:
        catalog = load_agents(cwd, base_settings.agents_dirs)
        return Delegator(base_settings, catalog, cwd=cwd, on_event=progress.on_event)

    def report(outcome: DelegationOutcome, planned: int | None = None) -> str:
        text = format_outcome(outcome, max_chars=base_settings.max_output_chars, planned_steps=planned)
        if not outcome.ok:
            raise ToolError(text)
        return text

    server = MCPServer("pi-orchestrator", instructions=SERVER_INSTRUCTIONS, version=__version__)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def list_agents(cwd: str | None = None) -> str:
        """List the pi agents available for delegation (name, tools, whether they can modify files).

        Args:
            cwd: Absolute project path; project agents in <cwd>/.pi/orchestrator/agents are included.
        """
        project = resolve_cwd(cwd)
        catalog = load_agents(project, base_settings.agents_dirs)
        agents = []
        for agent in catalog:
            effective = agent.restricted(base_settings.read_only)
            summary = effective.summary()
            summary.pop("path", None)
            agents.append(summary)
        payload: dict[str, Any] = {"cwd": project, "read_only": base_settings.read_only, "agents": agents}
        if catalog.diagnostics:
            payload["diagnostics"] = catalog.diagnostics
        return json.dumps(payload, indent=2)

    @server.tool()
    async def delegate_task(
        agent: str, task: str, cwd: str | None = None, model: str | None = None, ctx: Context | None = None
    ) -> str:
        """Run one pi agent on one self-contained task in an isolated session and return its report.

        Args:
            agent: Agent name from list_agents (e.g. scout, planner, worker, reviewer).
            task: Complete instructions; the agent has not seen this conversation.
            cwd: Absolute path of the project the agent works in.
            model: Optional model override, provider/id[:thinking] (any model pi can use).
        """
        progress = _Progress(ctx)
        delegator = delegator_for(resolve_cwd(cwd), progress)
        try:
            outcome = await progress.wrap(lambda: delegator.single(agent, task, model=model))
        except DelegationError as exc:
            raise ToolError(str(exc)) from None
        return report(outcome)

    @server.tool()
    async def delegate_parallel(tasks: list[TaskInput], cwd: str | None = None, ctx: Context | None = None) -> str:
        """Run independent pi agent tasks concurrently and return every report.

        At most one agent that edits files (edit/write tools) may be in a batch.

        Args:
            tasks: The tasks, each with agent, task and optional model.
            cwd: Absolute path of the project the agents work in.
        """
        progress = _Progress(ctx)
        delegator = delegator_for(resolve_cwd(cwd), progress)
        try:
            outcome = await progress.wrap(lambda: delegator.parallel([t.model_dump() for t in tasks]))
        except DelegationError as exc:
            raise ToolError(str(exc)) from None
        return report(outcome)

    @server.tool()
    async def delegate_chain(steps: list[TaskInput], cwd: str | None = None, ctx: Context | None = None) -> str:
        """Run pi agent steps in order; `{previous}` in a step's task is replaced by the previous output.

        Stops at the first failing step.

        Args:
            steps: The steps, each with agent, task and optional model.
            cwd: Absolute path of the project the agents work in.
        """
        progress = _Progress(ctx)
        delegator = delegator_for(resolve_cwd(cwd), progress)
        try:
            outcome = await progress.wrap(lambda: delegator.chain([s.model_dump() for s in steps]))
        except DelegationError as exc:
            raise ToolError(str(exc)) from None
        return report(outcome, planned=len(steps))

    @server.tool()
    async def orchestrate(
        task: str,
        cwd: str | None = None,
        model: str | None = None,
        read_only: bool = False,
        ctx: Context | None = None,
    ) -> str:
        """Hand a whole task to the standalone pi orchestrator, which plans and delegates on its own.

        Returns its final summary plus a list of the delegations it made.

        Args:
            task: The task, stated completely.
            cwd: Absolute path of the project to work in.
            model: Optional model for the pi orchestrator itself, provider/id[:thinking].
            read_only: If true, no agent may modify files.
        """
        settings = base_settings.with_overrides(model=model, read_only=read_only or None)
        progress = _Progress(ctx)
        orchestrator = Orchestrator(settings, cwd=resolve_cwd(cwd), on_event=progress.on_event)
        result = await progress.wrap(lambda: orchestrator.run(task))
        lines = [f"- {d.agent}: {'ok' if d.ok else 'FAILED'} ({d.duration_s:.1f}s)" for d in result.delegations]
        footer = (
            f"\n\n---\n{len(result.delegations)} delegation(s), {result.duration_s:.1f}s, "
            f"{result.total_usage.short()}\n" + "\n".join(lines)
        )
        body = _clip(result.output.strip() or "(no output)", base_settings.max_output_chars - len(footer))
        if not result.ok:
            raise ToolError(f"orchestrator failed: {result.error}\n\n{body}{footer}")
        return body + footer

    return server


def serve(settings: Settings | None = None, *, default_cwd: str | None = None) -> None:
    """Run the server over stdio (blocking)."""
    create_server(settings, default_cwd=default_cwd).run("stdio")
