"""The standalone orchestrator: a pi agent ("brain") whose delegation tools call back into Python.

The brain runs as ``pi --mode rpc -e orchestrator_shim.ts``. The shim's tools forward each call
over pi's extension-UI channel (an ``extension_ui_request`` with method ``input`` and title
:data:`SHIM_CHANNEL`); :class:`Orchestrator` answers it by running workers through the
:class:`~pi_orchestrator.delegation.Delegator`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from ._aio import finish_even_if_cancelled
from .agents import AgentCatalog, load_agents
from .config import Settings
from .delegation import Delegator
from .events import Event, EventCallback, emit
from .pi_rpc import PiRpcClient, PiRpcError
from .prompts import build_orchestrator_prompt
from .worker import PI_DISCOVERY_FLAGS, Usage, WorkerResult, translate_record

SHIM_CHANNEL = "pi-orchestrator:rpc"
BRAIN_TOOLS = ("read", "grep", "find", "ls", "delegate_task", "delegate_parallel", "delegate_chain")
ORCHESTRATOR_AGENT = "orchestrator"


def shim_path() -> Path:
    return Path(str(resources.files("pi_orchestrator") / "extension" / "orchestrator_shim.ts"))


@dataclass
class OrchestratorResult:
    ok: bool
    output: str
    error: str | None = None
    stop_reason: str | None = None
    model: str | None = None
    usage: Usage = field(default_factory=Usage)
    """Usage of the orchestrator's own model for this run."""
    worker_usage: Usage = field(default_factory=Usage)
    delegations: list[WorkerResult] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def total_usage(self) -> Usage:
        return self.usage + self.worker_usage

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["delegations"] = [d.to_dict(include_output=False) for d in self.delegations]
        data["total_usage"] = asdict(self.total_usage)
        return data


def _usage_delta(after: Usage, before: Usage) -> Usage:
    return Usage(**{k: getattr(after, k) - getattr(before, k) for k in asdict(after)})


class Orchestrator:
    """Runs the orchestrator brain. One instance = one pi brain process (reusable across turns)."""

    def __init__(
        self,
        settings: Settings,
        *,
        cwd: str | os.PathLike[str],
        catalog: AgentCatalog | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        self.settings = settings
        self.cwd = os.fspath(cwd)
        self.catalog = catalog if catalog is not None else load_agents(self.cwd, settings.agents_dirs)
        self.on_event = on_event
        self.delegator = Delegator(settings, self.catalog, cwd=self.cwd, on_event=on_event)
        self._client: PiRpcClient | None = None
        self._inflight: set[asyncio.Task[Any]] = set()
        self._brain_usage = Usage()

    # ------------------------------------------------------------------ process

    def build_argv(self) -> list[str]:
        s = self.settings
        argv = [*s.pi_command, "--mode", "rpc", "--no-session", *PI_DISCOVERY_FLAGS]
        argv += ["-e", str(shim_path()), *s.extension_args()]
        argv += ["--tools", ",".join(BRAIN_TOOLS)]
        if s.model:
            argv += ["--model", s.model]
        if s.thinking:
            argv += ["--thinking", s.thinking]
        if not s.load_context_files:
            argv.append("--no-context-files")
        argv += ["--append-system-prompt", build_orchestrator_prompt(self.catalog, s)]
        return argv

    async def start(self) -> None:
        if self._client is not None:
            return
        if not self.catalog.agents:
            raise PiRpcError("no agents available; check your agent directories")
        client = PiRpcClient(
            self.build_argv(),
            cwd=self.cwd,
            env=self.settings.process_env(PI_ORCHESTRATOR_HOST="1"),
            on_ui_request=self._on_ui_request,
        )
        await client.start()
        self._client = client

    async def close(self) -> None:
        for task in list(self._inflight):
            task.cancel()
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def abort(self) -> None:
        """Abort the current turn: cancel running delegations and stop the brain's model call."""
        for task in list(self._inflight):
            task.cancel()
        if self._client is not None:
            await self._client.abort()

    async def __aenter__(self) -> Orchestrator:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ shim channel

    async def _on_ui_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if request.get("method") != "input" or request.get("title") != SHIM_CHANNEL:
            return None  # not ours: cancel the dialog
        try:
            payload = json.loads(request.get("placeholder") or "{}")
        except json.JSONDecodeError:
            return {"value": json.dumps({"ok": False, "error": "malformed request from shim"})}
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)
        try:
            reply = await self.delegator.handle(str(payload.get("op", "")), payload.get("params"))
        finally:
            if task is not None:
                self._inflight.discard(task)
        return {"value": json.dumps(reply)}

    async def ping(self, timeout_s: float = 30.0) -> float:
        """Round-trip the shim channel without calling a model; returns latency in seconds."""
        await self.start()
        assert self._client is not None
        started = time.monotonic()
        await self._client.send_and_wait_for(
            lambda r: (
                r.get("type") == "extension_ui_request"
                and r.get("method") == "notify"
                and "pi-orchestrator:pong" in str(r.get("message", ""))
            ),
            "prompt",
            message="/pi-orchestrator-ping",
            timeout_s=timeout_s,
        )
        return time.monotonic() - started

    # ------------------------------------------------------------------ running

    async def ask(self, task: str) -> OrchestratorResult:
        """Send one task (or follow-up) to the running brain and wait for its final answer."""
        await self.start()
        assert self._client is not None
        started = time.monotonic()
        first_delegation = len(self.delegator.history)
        await emit(
            self.on_event,
            Event("orchestrator", "start", ORCHESTRATOR_AGENT, None, {"task": task, "model": self.settings.model}),
        )

        async def forward(record: dict[str, Any]) -> None:
            event = translate_record(record, source="orchestrator", agent=ORCHESTRATOR_AGENT, worker_id=None)
            if event is not None:
                await emit(self.on_event, event)

        result = OrchestratorResult(ok=False, output="", model=self.settings.model)
        try:
            outcome = await self._client.prompt_and_wait(task, on_record=forward)
        except PiRpcError as exc:
            result.error = str(exc)
        else:
            total = Usage.from_stats(outcome.stats)
            result.ok = outcome.ok
            result.output = outcome.text
            result.error = outcome.error
            result.stop_reason = outcome.stop_reason
            result.model = outcome.model or self.settings.model
            result.usage = _usage_delta(total, self._brain_usage)
            self._brain_usage = total
        finally:
            result.delegations = self.delegator.history[first_delegation:]
            for delegation in result.delegations:
                result.worker_usage = result.worker_usage + delegation.usage
            result.duration_s = round(time.monotonic() - started, 2)
            await emit(
                self.on_event,
                Event(
                    "orchestrator",
                    "end",
                    ORCHESTRATOR_AGENT,
                    None,
                    {
                        "ok": result.ok,
                        "error": result.error,
                        "duration_s": result.duration_s,
                        "usage": asdict(result.total_usage),
                        "delegations": len(result.delegations),
                    },
                ),
            )
        return result

    async def run(self, task: str) -> OrchestratorResult:
        """One-shot: start a brain, run ``task``, shut everything down (also on Ctrl+C)."""
        try:
            await self.start()
            return await self.ask(task)
        except PiRpcError as exc:
            return OrchestratorResult(ok=False, output="", error=str(exc), model=self.settings.model)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await finish_even_if_cancelled(self.abort())
            raise
        finally:
            await finish_even_if_cancelled(self.close())
