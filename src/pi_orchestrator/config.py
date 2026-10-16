"""Runtime settings shared by the CLI, the orchestrator and the MCP server."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

ENV_PREFIX = "PI_ORCHESTRATOR_"


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_paths(value: str) -> list[str]:
    return [p for p in value.split(os.pathsep) if p]


@dataclass(frozen=True)
class Settings:
    """Knobs for spawning pi agents.

    Every field can be set from the environment as ``PI_ORCHESTRATOR_<FIELD>``
    (upper-cased), e.g. ``PI_ORCHESTRATOR_WORKER_MODEL=anthropic/claude-haiku-4-5``.
    """

    pi_command: tuple[str, ...] = ("pi",)
    """Command used to launch pi. Tests point this at a fake."""
    model: str | None = None
    """Orchestrator model (``provider/id[:thinking]``); also the default worker model. None = pi's default."""
    worker_model: str | None = None
    """Default model for workers whose definition doesn't pin one."""
    thinking: str | None = None
    """Thinking level for the orchestrator (off|minimal|low|medium|high|xhigh)."""
    agents_dirs: tuple[str, ...] = ()
    """Extra directories with agent definitions (highest precedence)."""
    extensions: tuple[str, ...] = ()
    """Extra pi extensions (``-e``) loaded into every pi process, e.g. custom providers."""
    max_concurrency: int = 4
    max_parallel_tasks: int = 8
    allow_parallel_writers: bool = False
    """Allow more than one write-capable agent in a single parallel batch."""
    read_only: bool = False
    """Strip edit/write/bash from every worker."""
    worker_timeout_s: float = 1200.0
    max_output_chars: int = 40_000
    """Cap on the text handed back to the orchestrating model per delegation call."""
    load_context_files: bool = True
    """Let pi load AGENTS.md / CLAUDE.md from the working directory."""
    pi_env: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    """Extra environment variables for every pi process."""

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None, **overrides: Any) -> Settings:
        """Build settings from ``PI_ORCHESTRATOR_*`` variables, then apply non-None overrides."""
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for f in fields(cls):
            raw = env.get(ENV_PREFIX + f.name.upper())
            if raw is None or raw == "":
                continue
            if f.name == "pi_command":
                values[f.name] = tuple(shlex.split(raw))
            elif f.name in ("agents_dirs", "extensions"):
                values[f.name] = tuple(_env_paths(raw))
            elif f.name in ("max_concurrency", "max_parallel_tasks", "max_output_chars"):
                values[f.name] = int(raw)
            elif f.name == "worker_timeout_s":
                values[f.name] = float(raw)
            elif f.name in ("allow_parallel_writers", "read_only", "load_context_files"):
                values[f.name] = _env_bool(raw)
            elif f.name == "pi_env":
                continue
            else:
                values[f.name] = raw
        settings = cls(**values)
        return settings.with_overrides(**overrides)

    def with_overrides(self, **overrides: Any) -> Settings:
        clean: dict[str, Any] = {}
        for key, value in overrides.items():
            if value is None:
                continue
            if key in ("pi_command", "agents_dirs", "extensions") and not isinstance(value, tuple):
                value = tuple(value)
            clean[key] = value
        return replace(self, **clean) if clean else self

    def process_env(self, **extra: str) -> dict[str, str]:
        """Environment for a pi child process."""
        env = dict(os.environ)
        env.setdefault("PI_OFFLINE", "1")
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        env.update(dict(self.pi_env))
        env.update(extra)
        return env

    def extension_args(self) -> list[str]:
        args: list[str] = []
        for ext in self.extensions:
            args += ["-e", str(Path(ext).expanduser())]
        return args
