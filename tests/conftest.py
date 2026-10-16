from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pi_orchestrator.config import Settings
from pi_orchestrator.events import Event

TESTS = Path(__file__).parent
FAKE_PI = TESTS / "fake_pi.py"
FAKE_PI_COMMAND = (sys.executable, str(FAKE_PI))
ORCH = "# Role: pi-orchestrator"  # marker in the orchestrator's system prompt


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the user's real ~/.pi agents and PI_ORCHESTRATOR_* settings out of the tests."""
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent-dir"))
    for key in list(os.environ):
        if key.startswith("PI_ORCHESTRATOR_") or key.startswith("FAKE_PI_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    (path / "README.md").write_text("# demo project\n")
    return path


@pytest.fixture
def script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[list[dict[str, Any]]], Path]:
    """Install fake-pi rules: script([{"system_contains": ..., "turn": 0, "step": {...}}, ...])."""

    def install(rules: list[dict[str, Any]]) -> Path:
        path = tmp_path / "fake-pi-script.json"
        path.write_text(json.dumps({"rules": rules}))
        monkeypatch.setenv("FAKE_PI_SCRIPT", str(path))
        return path

    return install


class PiLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def starts(self) -> list[dict[str, Any]]:
        return [r for r in self.records() if r["event"] == "start"]

    def max_overlap(self) -> int:
        """Max number of fake pi prompts running at the same time."""
        points: list[tuple[float, int]] = []
        for record in self.records():
            points.append((record["t"], 1 if record["event"] == "start" else -1))
        current = best = 0
        for _, delta in sorted(points, key=lambda p: (p[0], p[1])):
            current += delta
            best = max(best, current)
        return best


@pytest.fixture
def pi_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PiLog:
    path = tmp_path / "fake-pi.log"
    monkeypatch.setenv("FAKE_PI_LOG", str(path))
    return PiLog(path)


@pytest.fixture
def settings() -> Settings:
    return Settings(pi_command=FAKE_PI_COMMAND, worker_timeout_s=30)


@pytest.fixture
def events() -> list[Event]:
    return []


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still "exists"; check /proc where available.
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        return "State:\tZ" not in status.read_text()
    return True
