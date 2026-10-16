"""Async client for pi's RPC mode (``pi --mode rpc``).

Protocol summary (see pi's docs/rpc.md):

* commands are JSON objects written to stdin, one per line, optionally carrying an ``id``;
* every command gets exactly one ``{"type": "response", "id": ..., "success": ...}``;
* agent events (``agent_start``, ``message_update``, ``tool_execution_*``, ``agent_end`` …)
  stream on stdout as JSON lines without an ``id``;
* extensions talk to the client through ``extension_ui_request`` records. Dialog methods
  (select/confirm/input/editor) block until the client writes an ``extension_ui_response``.

Framing is strict JSONL: records are split on ``\\n`` only. ``str.splitlines()`` and
``StreamReader.readline()`` are deliberately avoided: the former also splits on U+2028/U+2029
(legal inside JSON strings), the latter caps lines at 64 KiB (``agent_end`` can be much larger).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import signal
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})
READ_CHUNK = 1 << 16

UiHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
"""Answers a dialog ``extension_ui_request``. Return the response fields
(``{"value": ...}`` / ``{"confirmed": ...}``) or None to cancel the dialog."""

RecordCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class PiRpcError(RuntimeError):
    """A pi RPC command failed (``success: false``)."""


class PiProcessError(PiRpcError):
    """The pi process could not be started or exited unexpectedly."""


_EOF: dict[str, Any] = {"type": "__eof__"}


@dataclass
class RunOutcome:
    """Result of one prompt, after pi has settled (retries and compaction included)."""

    text: str
    stop_reason: str | None
    error: str | None
    stats: dict[str, Any] = field(default_factory=dict)
    model: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.stop_reason not in ("error", "aborted")


class PiRpcClient:
    """Drives one ``pi --mode rpc`` subprocess.

    Use as an async context manager::

        async with PiRpcClient(["pi", "--mode", "rpc", "--no-session"], cwd=repo) as pi:
            outcome = await pi.prompt_and_wait("List the files here")
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
        on_ui_request: UiHandler | None = None,
        settle_grace_s: float = 0.3,
        error_settle_grace_s: float = 2.0,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = env
        self.on_ui_request = on_ui_request
        self.settle_grace_s = settle_grace_s
        self.error_settle_grace_s = error_settle_grace_s

        self._proc: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._listeners: list[asyncio.Queue[dict[str, Any]]] = []
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._stderr: deque[str] = deque(maxlen=200)
        self._eof = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
                # Own process group: a terminal Ctrl+C reaches us, and we abort pi gracefully.
                start_new_session=sys.platform != "win32",
            )
        except FileNotFoundError as exc:
            raise PiProcessError(
                f"pi executable not found: {self.argv[0]!r}. Install it with "
                "`npm install -g @mariozechner/pi-coding-agent` or set PI_ORCHESTRATOR_PI_COMMAND."
            ) from exc
        self._reader = asyncio.create_task(self._read_stdout(), name="pi-rpc-stdout")
        self._stderr_reader = asyncio.create_task(self._read_stderr(), name="pi-rpc-stderr")

    async def close(self, grace_s: float = 5.0) -> None:
        """Close stdin and wait for pi to exit; escalate to SIGTERM/SIGKILL if needed."""
        proc = self._proc
        if proc is None:
            return
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
            with contextlib.suppress(Exception):
                await proc.stdin.wait_closed()
        try:
            await asyncio.wait_for(proc.wait(), grace_s)
        except TimeoutError:
            self._signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
            except TimeoutError:
                self._signal(signal.SIGKILL)
                await proc.wait()
        for task in list(self._tasks):
            task.cancel()
        for task in (self._reader, self._stderr_reader):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(task, 2.0)

    def _signal(self, sig: signal.Signals) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if sys.platform != "win32":
                os.killpg(proc.pid, sig)  # also reaches bash children spawned by pi tools
            elif sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()

    async def __aenter__(self) -> PiRpcClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    # ------------------------------------------------------------------ io

    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        stdout = self._proc.stdout
        buffer = b""
        try:
            while True:
                chunk = await stdout.read(READ_CHUNK)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    line, buffer = buffer[:newline], buffer[newline + 1 :]
                    self._handle_line(line)
            if buffer.strip():
                self._handle_line(buffer)
        finally:
            self._on_eof()

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self._stderr.append(text)
                log.debug("pi stderr: %s", text)

    def _handle_line(self, line: bytes) -> None:
        if line.endswith(b"\r"):
            line = line[:-1]
        if not line.strip():
            return
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            log.debug("ignoring non-JSON line from pi: %r", line[:200])
            return
        if isinstance(record, dict):
            self._dispatch(record)

    def _dispatch(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "response":
            future = self._pending.pop(str(record.get("id")), None)
            if future is not None and not future.done():
                future.set_result(record)
            elif record.get("success") is False:
                log.warning("pi reported an uncorrelated error: %s", record.get("error"))
            return
        if kind == "extension_ui_request" and record.get("method") in DIALOG_METHODS:
            task = asyncio.create_task(self._answer_ui(record))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        for queue in self._listeners:
            queue.put_nowait(record)

    def _on_eof(self) -> None:
        self._eof = True
        error = PiProcessError(self._exit_message())
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        for queue in self._listeners:
            queue.put_nowait(_EOF)

    def _exit_message(self) -> str:
        tail = self.stderr_tail.strip()
        msg = "pi process exited unexpectedly"
        return f"{msg}:\n{tail[-2000:]}" if tail else msg

    async def _answer_ui(self, request: dict[str, Any]) -> None:
        response: dict[str, Any] | None = None
        try:
            if self.on_ui_request is not None:
                response = await self.on_ui_request(request)
        except asyncio.CancelledError:
            await self._reply_ui(request, None)
            raise
        except Exception:
            log.exception("extension UI handler failed")
        await self._reply_ui(request, response)

    async def _reply_ui(self, request: dict[str, Any], response: dict[str, Any] | None) -> None:
        payload = {"type": "extension_ui_response", "id": request.get("id")}
        payload.update(response if response is not None else {"cancelled": True})
        with contextlib.suppress(Exception):
            await self._write(payload)

    async def _write(self, record: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._eof:
            raise PiProcessError(self._exit_message() if self._eof else "pi process is not running")
        # json.dumps escapes non-ASCII (including U+2028), so the line is plain ASCII.
        data = (json.dumps(record) + "\n").encode()
        async with self._write_lock:
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise PiProcessError(self._exit_message()) from exc

    @contextlib.contextmanager
    def _listen(self) -> Iterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        if self._eof:
            queue.put_nowait(_EOF)
        self._listeners.append(queue)
        try:
            yield queue
        finally:
            self._listeners.remove(queue)

    # ------------------------------------------------------------------ commands

    async def send(self, command: str, **fields: Any) -> dict[str, Any]:
        """Send a command and return its ``data`` (``{}`` if none). Raises PiRpcError on failure."""
        request_id = f"req-{next(self._ids)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"id": request_id, "type": command, **fields})
            response = await future
        finally:
            self._pending.pop(request_id, None)
        if not response.get("success", False):
            raise PiRpcError(f"pi `{command}` failed: {response.get('error') or 'unknown error'}")
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    async def abort(self) -> None:
        """Ask pi to abort the current run (best effort)."""
        with contextlib.suppress(PiRpcError, TimeoutError):
            await asyncio.wait_for(self.send("abort"), 5.0)

    async def send_and_wait_for(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        command: str,
        *,
        timeout_s: float = 30.0,
        **fields: Any,
    ) -> dict[str, Any]:
        """Send a command, then wait for the first event matching ``predicate``."""
        with self._listen() as queue:
            await self.send(command, **fields)
            async with asyncio.timeout(timeout_s):
                while True:
                    record = await queue.get()
                    if record is _EOF:
                        raise PiProcessError(self._exit_message())
                    if predicate(record):
                        return record

    async def prompt_and_wait(self, message: str, on_record: RecordCallback | None = None) -> RunOutcome:
        """Send a prompt and wait until pi has settled.

        pi emits ``agent_end`` *before* deciding whether to auto-retry a transient error or to
        auto-compact, so after ``agent_end`` we keep listening for a short grace period and
        follow ``auto_retry_*`` / ``compaction_*`` / ``agent_start`` if they appear.
        """
        last_assistant: dict[str, Any] | None = None
        with self._listen() as queue:
            await self.send("prompt", message=message)
            settle_timeout: float | None = None  # None = agent still running
            while True:
                try:
                    if settle_timeout is None:
                        record = await queue.get()
                    else:
                        record = await asyncio.wait_for(queue.get(), settle_timeout)
                except TimeoutError:
                    break
                if record is _EOF:
                    raise PiProcessError(self._exit_message())
                if on_record is not None:
                    result = on_record(record)
                    if asyncio.iscoroutine(result):
                        await result
                kind = record.get("type")
                if kind == "message_end":
                    message_obj = record.get("message") or {}
                    if message_obj.get("role") == "assistant":
                        last_assistant = message_obj
                elif kind == "agent_start" or kind == "compaction_start":
                    settle_timeout = None
                elif kind == "agent_end":
                    errored = (last_assistant or {}).get("stopReason") == "error"
                    settle_timeout = self.error_settle_grace_s if errored else self.settle_grace_s
                elif kind == "auto_retry_start":
                    settle_timeout = float(record.get("delayMs") or 0) / 1000 + self.error_settle_grace_s
                elif kind == "auto_retry_end":
                    settle_timeout = self.settle_grace_s
                elif kind == "compaction_end":
                    settle_timeout = self.error_settle_grace_s

        text = ""
        stats: dict[str, Any] = {}
        with contextlib.suppress(PiRpcError):
            text = (await self.send("get_last_assistant_text")).get("text") or ""
        with contextlib.suppress(PiRpcError):
            stats = await self.send("get_session_stats")
        stop_reason = (last_assistant or {}).get("stopReason")
        error: str | None = None
        if stop_reason == "error":
            error = (last_assistant or {}).get("errorMessage") or "model request failed"
        elif stop_reason == "aborted":
            error = "aborted"
        return RunOutcome(
            text=text,
            stop_reason=stop_reason,
            error=error,
            stats=stats,
            model=(last_assistant or {}).get("model"),
        )
