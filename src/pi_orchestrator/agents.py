"""Agent definitions: Markdown files with YAML frontmatter.

```markdown
---
name: scout
description: Fast codebase recon
tools: read, grep, find, ls, bash
model: anthropic/claude-haiku-4-5   # optional, provider/id[:thinking]
thinking: low                       # optional
system_prompt_mode: append          # append (default, keeps pi's coding prompt) | replace
context_files: true                 # let pi load AGENTS.md / CLAUDE.md (default true)
---
You are a scout. ...
```

Lookup order (later wins by ``name``): built-in → user (``~/.pi/agent/orchestrator/agents``)
→ project (``<cwd>/.pi/orchestrator/agents``) → explicit ``agents_dirs``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml

WRITE_TOOLS = frozenset({"edit", "write"})
"""Tools that modify files directly. Agents with these count as write-capable."""
READ_ONLY_STRIPPED_TOOLS = frozenset({"bash", "edit", "write"})
"""Tools removed in read-only mode (bash can modify files too)."""
DEFAULT_TOOLS = ("read", "grep", "find", "ls")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_-]+$")

AgentSource = Literal["builtin", "user", "project", "custom"]


class AgentDefinitionError(ValueError):
    """An agent file is malformed."""


class UnknownAgentError(KeyError):
    def __init__(self, name: str, available: Iterable[str]) -> None:
        self.name = name
        self.available = sorted(available)
        super().__init__(name)

    def __str__(self) -> str:
        return f"unknown agent {self.name!r}; available agents: {', '.join(self.available) or '(none)'}"


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] = DEFAULT_TOOLS
    model: str | None = None
    thinking: str | None = None
    system_prompt_mode: Literal["append", "replace"] = "append"
    context_files: bool = True
    source: AgentSource = "builtin"
    path: Path | None = None

    @property
    def is_write_capable(self) -> bool:
        return any(tool in WRITE_TOOLS for tool in self.tools)

    def restricted(self, read_only: bool) -> AgentDefinition:
        """The definition with bash/edit/write removed when ``read_only`` is set."""
        if not read_only or not any(t in READ_ONLY_STRIPPED_TOOLS for t in self.tools):
            return self
        return replace(self, tools=tuple(t for t in self.tools if t not in READ_ONLY_STRIPPED_TOOLS))

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tools": list(self.tools),
            "model": self.model,
            "write_capable": self.is_write_capable,
            "source": self.source,
            "path": str(self.path) if self.path else None,
        }


@dataclass
class AgentCatalog:
    agents: dict[str, AgentDefinition] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)

    def get(self, name: str) -> AgentDefinition:
        try:
            return self.agents[name]
        except KeyError:
            raise UnknownAgentError(name, self.agents) from None

    def names(self) -> list[str]:
        return sorted(self.agents)

    def __iter__(self):
        return iter(self.agents[name] for name in self.names())

    def __len__(self) -> int:
        return len(self.agents)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        raise AgentDefinitionError("missing YAML frontmatter (file must start with ---)")
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        raise AgentDefinitionError("frontmatter must start with a line containing only ---")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            raw = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            break
    else:
        raise AgentDefinitionError("unterminated frontmatter (missing closing ---)")
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise AgentDefinitionError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise AgentDefinitionError("frontmatter must be a mapping")
    return meta, body.strip()


def _parse_tools(value: Any) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_TOOLS
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        raise AgentDefinitionError("`tools` must be a comma-separated string or a list")
    tools = tuple(dict.fromkeys(item for item in items if item))
    for tool in tools:
        if not _TOOL_RE.match(tool):
            raise AgentDefinitionError(f"invalid tool name {tool!r}")
    return tools


def parse_agent_text(text: str, *, source: AgentSource = "custom", path: Path | None = None) -> AgentDefinition:
    meta, body = _split_frontmatter(text)
    name = str(meta.get("name") or (path.stem if path else "")).strip()
    if not _NAME_RE.match(name):
        raise AgentDefinitionError(f"invalid agent name {name!r} (use lowercase letters, digits, - and _)")
    description = str(meta.get("description") or "").strip()
    if not description:
        raise AgentDefinitionError("`description` is required")
    if not body:
        raise AgentDefinitionError("the agent prompt (Markdown body) is empty")
    mode = str(meta.get("system_prompt_mode") or "append").strip()
    if mode not in ("append", "replace"):
        raise AgentDefinitionError("`system_prompt_mode` must be append or replace")
    context_files = meta.get("context_files", True)
    if not isinstance(context_files, bool):
        raise AgentDefinitionError("`context_files` must be true or false")
    model = meta.get("model")
    thinking = meta.get("thinking")
    return AgentDefinition(
        name=name,
        description=description,
        prompt=body,
        tools=_parse_tools(meta.get("tools")),
        model=str(model).strip() if model else None,
        thinking=str(thinking).strip() if thinking else None,
        system_prompt_mode=mode,  # type: ignore[arg-type]
        context_files=context_files,
        source=source,
        path=path,
    )


def parse_agent_file(path: Path, *, source: AgentSource = "custom") -> AgentDefinition:
    return parse_agent_text(path.read_text(encoding="utf-8"), source=source, path=path)


def pi_agent_dir() -> Path:
    """pi's global config dir (``PI_CODING_AGENT_DIR`` or ``~/.pi/agent``)."""
    override = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(override).expanduser() if override else Path.home() / ".pi" / "agent"


def user_agents_dir() -> Path:
    return pi_agent_dir() / "orchestrator" / "agents"


def project_agents_dir(cwd: str | os.PathLike[str]) -> Path:
    return Path(cwd) / ".pi" / "orchestrator" / "agents"


def _load_dir(catalog: AgentCatalog, directory: Path, source: AgentSource) -> None:
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.md")):
        try:
            agent = parse_agent_file(path, source=source)
        except (AgentDefinitionError, OSError, UnicodeDecodeError) as exc:
            catalog.diagnostics.append(f"{path}: {exc}")
            continue
        catalog.agents[agent.name] = agent


def _load_builtin(catalog: AgentCatalog) -> None:
    root = resources.files("pi_orchestrator") / "agents"
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".md"):
            continue
        agent = parse_agent_text(entry.read_text(encoding="utf-8"), source="builtin", path=Path(str(entry)))
        catalog.agents[agent.name] = agent


def load_agents(
    cwd: str | os.PathLike[str],
    extra_dirs: Iterable[str | os.PathLike[str]] = (),
    *,
    include_builtin: bool = True,
    include_user: bool = True,
    include_project: bool = True,
) -> AgentCatalog:
    catalog = AgentCatalog()
    if include_builtin:
        _load_builtin(catalog)
    if include_user:
        _load_dir(catalog, user_agents_dir(), "user")
    if include_project:
        _load_dir(catalog, project_agents_dir(cwd), "project")
    for directory in extra_dirs:
        path = Path(directory).expanduser()
        if not path.is_dir():
            catalog.diagnostics.append(f"{path}: agents directory not found")
            continue
        _load_dir(catalog, path, "custom")
    return catalog
