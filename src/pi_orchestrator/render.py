"""Terminal and JSONL rendering of orchestrator events."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

from .events import Event


def _one_line(text: str, limit: int = 100) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TerminalRenderer:
    """Streams the orchestrator's text to ``out`` and worker progress to ``err``."""

    def __init__(
        self,
        out: TextIO | None = None,
        err: TextIO | None = None,
        *,
        color: bool | None = None,
        show_worker_text: bool = False,
    ) -> None:
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        if color is None:
            color = self.err.isatty() and "NO_COLOR" not in os.environ
        self.color = color
        self.show_worker_text = show_worker_text
        self._at_line_start = True

    def _style(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def _status(self, text: str) -> None:
        if not self._at_line_start:
            self.out.write("\n")
            self.out.flush()
            self._at_line_start = True
        self.err.write(text + "\n")
        self.err.flush()

    def __call__(self, event: Event) -> None:
        data = event.data
        if event.source == "orchestrator":
            if event.kind == "text":
                delta = data.get("delta", "")
                self.out.write(delta)
                self.out.flush()
                if delta:
                    self._at_line_start = delta.endswith("\n")
            elif event.kind == "tool_start":
                self._status(self._style(f"⏵ {data.get('tool')} {data.get('args', '')}".rstrip(), "36"))
            elif event.kind == "tool_end" and data.get("is_error"):
                self._status(self._style(f"✘ {data.get('tool')} returned an error", "31"))
            elif event.kind == "retry":
                self._status(self._style(f"↻ orchestrator retry {data.get('attempt')}: {data.get('error')}", "33"))
            elif event.kind == "end" and not self._at_line_start:
                self.out.write("\n")
                self.out.flush()
                self._at_line_start = True
            return

        tag = f"[{event.worker_id or event.agent}]"
        if event.kind == "start":
            model = f" ({data['model']})" if data.get("model") else ""
            self._status(self._style(f"▶ {tag} {_one_line(data.get('task', ''))}{model}", "1"))
        elif event.kind == "tool_start":
            self._status(self._style(f"  {tag} {data.get('tool')} {data.get('args', '')}".rstrip(), "2"))
        elif event.kind == "text" and self.show_worker_text:
            self.err.write(self._style(data.get("delta", ""), "2"))
        elif event.kind == "retry":
            self._status(self._style(f"↻ {tag} retry {data.get('attempt')}: {data.get('error')}", "33"))
        elif event.kind == "end":
            usage = data.get("usage") or {}
            cost = f" · ${usage.get('cost', 0):.4f}" if usage.get("cost") else ""
            if data.get("ok"):
                self._status(self._style(f"✔ {tag} done in {data.get('duration_s', 0):.1f}s{cost}", "32"))
            else:
                self._status(
                    self._style(f"✘ {tag} failed: {_one_line(data.get('error') or 'unknown error', 200)}", "31")
                )


class JsonRenderer:
    """Writes every event as one JSON line."""

    def __init__(self, out: TextIO | None = None) -> None:
        self.out = out or sys.stdout

    def __call__(self, event: Event) -> None:
        self.write({"type": "event", **event.to_dict()})

    def write(self, record: dict[str, Any]) -> None:
        self.out.write(json.dumps(record, default=str) + "\n")
        self.out.flush()
