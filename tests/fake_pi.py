"""A scripted stand-in for ``pi --mode rpc`` used by the unit tests.

It implements the subset of pi's RPC protocol that pi-orchestrator uses. Behaviour comes from the
JSON file named by ``FAKE_PI_SCRIPT``:

    {"rules": [
        {"system_contains": "You are a scout", "task_contains": "...", "turn": 0, "step": {...}},
        ...
    ]}

``turn`` counts assistant messages already produced in this process. The first rule whose
conditions all match supplies the step. Steps:

    {"text": "hello", "delay": 0.1}          plain answer; ``{last_tool_result}`` expands to the latest
                                             tool result text (``text_size: N`` for a large answer)
    {"tool": "delegate_task", "args": {...}} tool call; ``delegate_*`` tools go through the shim
                                             channel (extension_ui_request → response); then the
                                             loop continues with the next turn
    {"error": "boom"}                        assistant message with stopReason=error
    {"retry_once": "529 overloaded"}         error + auto-retry, then continue with the next turn
    {"reject": "No API key found"}           the prompt command itself fails
    {"sleep": 30}                            hang until aborted (stopReason=aborted)
    {"exit": 3}                              crash with the given exit code

If ``FAKE_PI_LOG`` is set, start/end records (with argv, cwd, timestamps) are appended to it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from typing import Any

CHANNEL = "pi-orchestrator:rpc"
DELEGATE_TOOLS = {"delegate_task", "delegate_parallel", "delegate_chain"}


def _arg_after(flag: str) -> str | None:
    argv = sys.argv[1:]
    if flag in argv:
        index = argv.index(flag)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


SYSTEM = _arg_after("--append-system-prompt") or _arg_after("--system-prompt") or ""
MODEL = _arg_after("--model") or "fake/default"


def _load_rules() -> list[dict[str, Any]]:
    path = os.environ.get("FAKE_PI_SCRIPT")
    if not path:
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("rules", [])


RULES = _load_rules()


def _log(record: dict[str, Any]) -> None:
    path = os.environ.get("FAKE_PI_LOG")
    if not path:
        return
    line = json.dumps({"pid": os.getpid(), "t": time.time(), **record}) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


class FakePi:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.write_lock = threading.Lock()
        self.ui_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.ui_ids = 0
        self.assistant_messages = 0
        self.tool_calls = 0
        self.last_text: str | None = None
        self.last_tool_result = ""
        self.abort_event = asyncio.Event()
        self.running: asyncio.Task[None] | None = None
        self.background: set[asyncio.Future[None]] = set()

    # -------------------------------------------------------------- output
    def out(self, record: dict[str, Any]) -> None:
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def respond(self, request: dict[str, Any], data: Any = None, error: str | None = None) -> None:
        record: dict[str, Any] = {"type": "response", "command": request.get("type"), "success": error is None}
        if "id" in request:
            record["id"] = request["id"]
        if error is not None:
            record["error"] = error
        elif data is not None:
            record["data"] = data
        self.out(record)

    def assistant(self, content: list[dict[str, Any]], stop: str, error: str | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": content,
            "provider": MODEL.split("/")[0],
            "model": MODEL.split("/")[-1],
            "stopReason": stop,
            "timestamp": int(time.time() * 1000),
        }
        if error:
            message["errorMessage"] = error
        self.assistant_messages += 1
        self.out({"type": "message_start", "message": {**message, "content": []}})
        for block in content:
            if block.get("type") == "text":
                text = block["text"]
                for i in range(0, len(text), 4096):
                    self.out(
                        {
                            "type": "message_update",
                            "message": message,
                            "assistantMessageEvent": {"type": "text_delta", "delta": text[i : i + 4096]},
                        }
                    )
        self.out({"type": "message_end", "message": message})
        texts = [b["text"] for b in content if b.get("type") == "text"]
        if texts:
            self.last_text = "\n".join(texts)
        return message

    # -------------------------------------------------------------- script
    def pick(self, task: str) -> dict[str, Any] | None:
        for rule in RULES:
            if "system_contains" in rule and rule["system_contains"] not in SYSTEM:
                continue
            if "task_contains" in rule and rule["task_contains"] not in task:
                continue
            if "turn" in rule and rule["turn"] != self.assistant_messages:
                continue
            return rule["step"]
        return None

    async def ui_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ui_ids += 1
        ui_id = f"ui-{self.ui_ids}"
        future: asyncio.Future[dict[str, Any]] = self.loop.create_future()
        self.ui_waiters[ui_id] = future
        self.out(
            {
                "type": "extension_ui_request",
                "id": ui_id,
                "method": "input",
                "title": CHANNEL,
                "placeholder": json.dumps(payload),
            }
        )
        return await future

    async def run_prompt(self, request: dict[str, Any]) -> None:
        task = request.get("message", "")
        self.abort_event.clear()
        _log(
            {
                "event": "start",
                "argv": sys.argv[1:],
                "cwd": os.getcwd(),
                "task": task,
                "host": os.environ.get("PI_ORCHESTRATOR_HOST"),
            }
        )
        self.out({"type": "agent_start"})
        self.out({"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": task}]}})
        while True:
            step = self.pick(task) or {"text": f"[fake-pi] no rule for turn {self.assistant_messages}"}
            if "delay" in step:
                await asyncio.sleep(step["delay"])
            if "exit" in step:
                sys.stdout.flush()
                os._exit(int(step["exit"]))
            if "sleep" in step:
                try:
                    await asyncio.wait_for(self.abort_event.wait(), float(step["sleep"]))
                    self.assistant([], "aborted")
                except TimeoutError:
                    self.assistant([{"type": "text", "text": "woke up"}], "stop")
                break
            if "error" in step:
                self.assistant([], "error", step["error"])
                break
            if "retry_once" in step:
                message = self.assistant([], "error", step["retry_once"])
                self.out({"type": "agent_end", "messages": [message]})
                self.out(
                    {
                        "type": "auto_retry_start",
                        "attempt": 1,
                        "maxAttempts": 3,
                        "delayMs": 50,
                        "errorMessage": step["retry_once"],
                    }
                )
                await asyncio.sleep(0.05)
                self.out({"type": "agent_start"})
                continue
            if "tool" in step:
                tool, args = step["tool"], step.get("args", {})
                call_id = f"call-{self.tool_calls}"
                self.tool_calls += 1
                self.assistant([{"type": "toolCall", "id": call_id, "name": tool, "arguments": args}], "toolUse")
                self.out({"type": "tool_execution_start", "toolCallId": call_id, "toolName": tool, "args": args})
                is_error = False
                if tool in DELEGATE_TOOLS:
                    response = await self.ui_input({"op": tool, "params": args})
                    if response.get("cancelled"):
                        text, is_error = "Delegation aborted", True
                    else:
                        reply = json.loads(response.get("value") or "{}")
                        is_error = not reply.get("ok")
                        text = reply.get("text") if reply.get("ok") else reply.get("error", "")
                else:
                    text = f"[fake tool {tool}]"
                self.last_tool_result = text
                result = {"content": [{"type": "text", "text": text}], "details": {}}
                self.out(
                    {
                        "type": "tool_execution_end",
                        "toolCallId": call_id,
                        "toolName": tool,
                        "result": result,
                        "isError": is_error,
                    }
                )
                self.out(
                    {
                        "type": "message_end",
                        "message": {"role": "toolResult", "toolCallId": call_id, "content": result["content"]},
                    }
                )
                continue
            text = (step.get("text") or "").replace("{last_tool_result}", self.last_tool_result)
            if "text_size" in step:
                text = ("x" * 99 + "\n") * (int(step["text_size"]) // 100)
            self.assistant([{"type": "text", "text": text or ""}], "stop")
            break
        self.out({"type": "agent_end", "messages": []})
        _log({"event": "end", "task": task})

    async def ping(self, request: dict[str, Any]) -> None:
        response = await self.ui_input({"op": "ping"})
        reply = json.loads(response.get("value") or "{}")
        self.out(
            {
                "type": "extension_ui_request",
                "id": "notify-1",
                "method": "notify",
                "message": f"pi-orchestrator:pong {reply.get('text', '')}",
            }
        )

    # -------------------------------------------------------------- input
    def handle(self, request: dict[str, Any]) -> None:
        kind = request.get("type")
        if kind == "extension_ui_response":
            future = self.ui_waiters.pop(str(request.get("id")), None)
            if future is not None and not future.done():
                future.set_result(request)
            return
        if kind == "prompt":
            message = request.get("message", "")
            if message.startswith("/pi-orchestrator-ping"):
                self.respond(request)
                task = asyncio.ensure_future(self.ping(request))
                self.background.add(task)
                task.add_done_callback(self.background.discard)
                return
            step = self.pick(message)
            if step and "reject" in step:
                self.respond(request, error=step["reject"])
                return
            if self.running and not self.running.done():
                self.respond(request, error="Agent is already streaming")
                return
            self.respond(request)
            self.running = asyncio.ensure_future(self.run_prompt(request))
        elif kind == "abort":
            self.abort_event.set()
            for future in self.ui_waiters.values():
                if not future.done():
                    future.set_result({"cancelled": True})
            self.respond(request)
        elif kind == "get_state":
            self.respond(
                request, {"model": {"id": MODEL}, "isStreaming": bool(self.running and not self.running.done())}
            )
        elif kind == "get_last_assistant_text":
            self.respond(request, {"text": self.last_text})
        elif kind == "get_session_stats":
            self.respond(
                request,
                {
                    "assistantMessages": self.assistant_messages,
                    "toolCalls": self.tool_calls,
                    "tokens": {
                        "input": 100 * self.assistant_messages,
                        "output": 10 * self.assistant_messages,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "total": 110 * self.assistant_messages,
                    },
                    "cost": 0.001 * self.assistant_messages,
                },
            )
        else:
            self.respond(request, error=f"unknown command {kind}")


async def main() -> None:
    loop = asyncio.get_running_loop()
    pi = FakePi(loop)
    done = asyncio.Event()

    def reader() -> None:
        for raw in sys.stdin.buffer:
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            request = json.loads(line)
            loop.call_soon_threadsafe(pi.handle, request)
        loop.call_soon_threadsafe(done.set)

    threading.Thread(target=reader, daemon=True).start()
    await done.wait()
    if pi.running and not pi.running.done():
        pi.running.cancel()


if __name__ == "__main__":
    if "--version" in sys.argv:
        print("0.73.1")
        sys.exit(0)
    if "--list-models" in sys.argv:
        print("provider  model  context  max-out  thinking  images")
        print("fake      fake-1  128K     16K      no        no")
        sys.exit(0)
    asyncio.run(main())
