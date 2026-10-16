from __future__ import annotations

from pathlib import Path

import pytest

from pi_orchestrator.agents import (
    AgentDefinitionError,
    UnknownAgentError,
    load_agents,
    parse_agent_text,
    project_agents_dir,
    user_agents_dir,
)


def write_agent(directory: Path, name: str, description: str = "d", tools: str = "read", body: str = "Body") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\ntools: {tools}\n---\n{body}\n")
    return path


def test_builtin_agents(project):
    catalog = load_agents(project)
    assert catalog.names() == ["planner", "reviewer", "scout", "worker"]
    assert not catalog.diagnostics
    assert catalog.get("worker").is_write_capable
    for name in ("scout", "planner", "reviewer"):
        assert not catalog.get(name).is_write_capable, name
    assert all(a.source == "builtin" and a.model is None for a in catalog)


def test_parse_full_frontmatter():
    agent = parse_agent_text(
        "---\n"
        "name: tester\n"
        "description: Runs tests\n"
        "tools: [read, bash, read]\n"
        "model: anthropic/claude-haiku-4-5\n"
        "thinking: low\n"
        "system_prompt_mode: replace\n"
        "context_files: false\n"
        "---\n\nYou run tests.\n"
    )
    assert agent.name == "tester"
    assert agent.tools == ("read", "bash")
    assert agent.model == "anthropic/claude-haiku-4-5"
    assert agent.thinking == "low"
    assert agent.system_prompt_mode == "replace"
    assert agent.context_files is False
    assert agent.prompt == "You run tests."


def test_defaults():
    agent = parse_agent_text("---\nname: a\ndescription: b\n---\nprompt")
    assert agent.tools == ("read", "grep", "find", "ls")
    assert agent.system_prompt_mode == "append"
    assert agent.context_files is True


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no frontmatter", "missing YAML frontmatter"),
        ("---\nname: a\ndescription: b\nprompt", "unterminated"),
        ("---\nname: Bad Name\ndescription: b\n---\nx", "invalid agent name"),
        ("---\nname: a\n---\nx", "description"),
        ("---\nname: a\ndescription: b\n---\n", "empty"),
        ("---\nname: a\ndescription: b\ntools: 5\n---\nx", "tools"),
        ("---\nname: a\ndescription: b\nsystem_prompt_mode: merge\n---\nx", "system_prompt_mode"),
        ("---\n[unclosed\n---\nx", "invalid YAML"),
    ],
)
def test_invalid_definitions(text, message):
    with pytest.raises(AgentDefinitionError, match=message):
        parse_agent_text(text)


def test_precedence_user_project_custom(project, tmp_path):
    write_agent(user_agents_dir(), "scout", description="user scout")
    write_agent(user_agents_dir(), "helper", description="user helper")
    write_agent(project_agents_dir(project), "helper", description="project helper")
    custom = tmp_path / "custom"
    write_agent(custom, "helper", description="custom helper")

    catalog = load_agents(project)
    assert catalog.get("scout").description == "user scout"
    assert catalog.get("helper").description == "project helper"
    assert catalog.get("helper").source == "project"

    catalog = load_agents(project, [custom])
    assert catalog.get("helper").description == "custom helper"
    assert catalog.get("helper").source == "custom"


def test_bad_files_become_diagnostics(project, tmp_path):
    directory = project_agents_dir(project)
    directory.mkdir(parents=True)
    (directory / "broken.md").write_text("nope")
    catalog = load_agents(project, [tmp_path / "missing"])
    assert "scout" in catalog.agents
    assert any("broken.md" in d for d in catalog.diagnostics)
    assert any("not found" in d for d in catalog.diagnostics)


def test_unknown_agent_lists_available(project):
    catalog = load_agents(project)
    with pytest.raises(UnknownAgentError) as info:
        catalog.get("nope")
    assert "planner, reviewer, scout, worker" in str(info.value)


def test_read_only_restriction(project):
    catalog = load_agents(project)
    worker = catalog.get("worker")
    restricted = worker.restricted(True)
    assert restricted.tools == ("read", "grep", "find", "ls")
    assert not restricted.is_write_capable
    assert catalog.get("scout").restricted(True).tools == ("read", "grep", "find", "ls")
    assert worker.restricted(False) is worker
