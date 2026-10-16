from __future__ import annotations

import json

import pytest
from mcp import Client

from pi_orchestrator.mcp_server import create_server

from conftest import ORCH


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content if getattr(block, "type", None) == "text")


@pytest.fixture
def server(settings, project):
    return create_server(settings, default_cwd=str(project))


async def test_tools_are_listed_with_schemas(server):
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == {"list_agents", "delegate_task", "delegate_parallel", "delegate_chain", "orchestrate"}
    task_schema = tools["delegate_task"].input_schema
    assert set(task_schema["required"]) == {"agent", "task"}
    assert "ctx" not in task_schema["properties"]
    parallel = tools["delegate_parallel"].input_schema
    assert parallel["properties"]["tasks"]["type"] == "array"
    assert tools["list_agents"].annotations.read_only_hint is True


async def test_list_agents(server, project):
    async with Client(server) as client:
        result = await client.call_tool("list_agents", {})
    payload = json.loads(text_of(result))
    assert payload["cwd"] == str(project.resolve())
    assert [a["name"] for a in payload["agents"]] == ["planner", "reviewer", "scout", "worker"]


async def test_delegate_task_with_progress(server, script, project, pi_log):
    script([{"system_contains": "You are a scout", "step": {"text": "scout report", "delay": 0.2}}])
    progress: list[str | None] = []

    async def on_progress(value, total, message):
        progress.append(message)

    async with Client(server) as client:
        result = await client.call_tool(
            "delegate_task", {"agent": "scout", "task": "map it"}, progress_callback=on_progress
        )
    assert not result.is_error, text_of(result)
    assert "scout report" in text_of(result)
    assert any("started" in (m or "") for m in progress)
    assert any("finished" in (m or "") for m in progress)
    assert pi_log.starts()[0]["cwd"] == str(project.resolve())


async def test_delegate_task_explicit_cwd(server, script, tmp_path, pi_log):
    other = tmp_path / "other"
    other.mkdir()
    script([{"step": {"text": "ok"}}])
    async with Client(server) as client:
        result = await client.call_tool("delegate_task", {"agent": "scout", "task": "x", "cwd": str(other)})
    assert not result.is_error
    assert pi_log.starts()[0]["cwd"] == str(other.resolve())


async def test_failures_are_tool_errors(server, script, tmp_path):
    script([{"step": {"error": "rate limited"}}])
    async with Client(server) as client:
        failed = await client.call_tool("delegate_task", {"agent": "scout", "task": "x"})
        unknown = await client.call_tool("delegate_task", {"agent": "ghost", "task": "x"})
        bad_cwd = await client.call_tool("delegate_task", {"agent": "scout", "task": "x", "cwd": str(tmp_path / "no")})
        writers = await client.call_tool(
            "delegate_parallel",
            {"tasks": [{"agent": "worker", "task": "a"}, {"agent": "worker", "task": "b"}]},
        )
    assert failed.is_error and "rate limited" in text_of(failed)
    assert unknown.is_error and "unknown agent" in text_of(unknown)
    assert bad_cwd.is_error and "not a directory" in text_of(bad_cwd)
    assert writers.is_error and "write-capable" in text_of(writers)


async def test_parallel_and_chain(server, script):
    script(
        [
            {"system_contains": "You are a scout", "step": {"text": "S"}},
            {"system_contains": "planning specialist", "step": {"text": "P"}},
        ]
    )
    async with Client(server) as client:
        parallel = await client.call_tool(
            "delegate_parallel",
            {"tasks": [{"agent": "scout", "task": "a"}, {"agent": "planner", "task": "b"}]},
        )
        chain = await client.call_tool(
            "delegate_chain",
            {"steps": [{"agent": "scout", "task": "a"}, {"agent": "planner", "task": "use {previous}"}]},
        )
    assert "Parallel delegation: 2/2 succeeded" in text_of(parallel)
    assert "Chain completed: 2/2 steps succeeded" in text_of(chain)


async def test_orchestrate(server, script):
    script(
        [
            {
                "system_contains": ORCH,
                "turn": 0,
                "step": {"tool": "delegate_task", "args": {"agent": "scout", "task": "x"}},
            },
            {"system_contains": ORCH, "turn": 1, "step": {"text": "Summary: all good"}},
            {"system_contains": "You are a scout", "step": {"text": "S"}},
        ]
    )
    async with Client(server) as client:
        result = await client.call_tool("orchestrate", {"task": "check the repo", "read_only": True})
    text = text_of(result)
    assert not result.is_error, text
    assert text.startswith("Summary: all good")
    assert "1 delegation(s)" in text and "- scout: ok" in text
