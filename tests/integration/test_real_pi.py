"""End-to-end tests against the real `pi` CLI.

They load ``tests/pi_ext/scripted_provider.ts`` into every pi process, so the real pi agent loop,
real tools and the real orchestrator shim run with a deterministic model and no API key.
The last test talks to a real model and only runs when ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest
from mcp import Client

from pi_orchestrator.agents import load_agents
from pi_orchestrator.config import Settings
from pi_orchestrator.mcp_server import create_server
from pi_orchestrator.orchestrator import Orchestrator
from pi_orchestrator.worker import run_worker

from conftest import ORCH, TESTS

pytestmark = [
    pytest.mark.real_pi,
    pytest.mark.skipif(shutil.which("pi") is None, reason="the real `pi` CLI is not installed"),
]

SCRIPTED_PROVIDER = TESTS / "pi_ext" / "scripted_provider.ts"


@pytest.fixture
def scripted(tmp_path, monkeypatch):
    def install(rules):
        path = tmp_path / "scripted-rules.json"
        path.write_text(json.dumps(rules))
        monkeypatch.setenv("SCRIPTED_PI_RULES", str(path))

    return install


@pytest.fixture
def real_settings():
    return Settings(
        model="scripted/scripted-1",
        extensions=(str(SCRIPTED_PROVIDER),),
        load_context_files=False,
        worker_timeout_s=120,
    )


async def test_shim_channel_round_trip(real_settings, project, scripted):
    scripted([])
    async with Orchestrator(real_settings, cwd=project) as orchestrator:
        assert await orchestrator.ping() < 30


async def test_worker_runs_real_pi_tools(real_settings, project, scripted):
    scripted(
        [
            {"system_contains": "You are a scout", "turn": 0, "reply": {"tool": "read", "args": {"path": "README.md"}}},
            {"system_contains": "You are a scout", "turn": 1, "reply": {"text": "README says: {last_tool_result}"}},
        ]
    )
    scout = load_agents(project).get("scout")
    result = await run_worker(scout, "read the readme", real_settings, cwd=project)
    assert result.ok, result.error
    assert "demo project" in result.output
    assert result.usage.turns == 2 and result.usage.tool_calls == 1


async def test_orchestrator_end_to_end(real_settings, project, scripted):
    scripted(
        [
            {
                "system_contains": ORCH,
                "turn": 0,
                "reply": {
                    "tool": "delegate_parallel",
                    "args": {"tasks": [{"agent": "scout", "task": "map"}, {"agent": "reviewer", "task": "review"}]},
                },
            },
            {
                "system_contains": ORCH,
                "turn": 1,
                "reply": {
                    "tool": "delegate_chain",
                    "args": {
                        "steps": [{"agent": "planner", "task": "plan"}, {"agent": "worker", "task": "do: {previous}"}]
                    },
                },
            },
            {"system_contains": ORCH, "turn": 2, "reply": {"text": "FINAL\n{last_tool_result}"}},
            {"system_contains": "You are a scout", "reply": {"text": "scout: one README"}},
            {"system_contains": "senior code reviewer", "reply": {"text": "review: APPROVE"}},
            {"system_contains": "planning specialist", "reply": {"text": "PLAN-42"}},
            {"system_contains": "worker agent", "reply": {"text": "implemented PLAN-42"}},
        ]
    )
    result = await Orchestrator(real_settings, cwd=project).run("improve the readme")
    assert result.ok, result.error
    assert [d.agent for d in result.delegations] == ["scout", "reviewer", "planner", "worker"]
    assert result.delegations[-1].task == "do: PLAN-42"
    assert result.output.startswith("FINAL")
    assert "Chain completed: 2/2 steps succeeded" in result.output
    assert "implemented PLAN-42" in result.output


async def test_mcp_orchestrate_with_real_pi(real_settings, project, scripted):
    scripted(
        [
            {
                "system_contains": ORCH,
                "turn": 0,
                "reply": {"tool": "delegate_task", "args": {"agent": "scout", "task": "map"}},
            },
            {"system_contains": ORCH, "turn": 1, "reply": {"text": "Summary: {last_tool_result}"}},
            {"system_contains": "You are a scout", "reply": {"text": "scout: one README"}},
        ]
    )
    async with Client(create_server(real_settings, default_cwd=str(project))) as client:
        result = await client.call_tool("orchestrate", {"task": "summarize"})
    text = "\n".join(block.text for block in result.content)
    assert not result.is_error, text
    assert "scout: one README" in text and "- scout: ok" in text


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY for a real model")
async def test_real_model_scout(project):
    settings = Settings(worker_model=os.environ.get("PI_ORCHESTRATOR_TEST_MODEL", "anthropic/claude-haiku-4-5"))
    scout = load_agents(project).get("scout")
    result = await run_worker(
        scout, "List the files in the current directory. Reply with the file names only.", settings, cwd=project
    )
    assert result.ok, result.error
    assert "README.md" in result.output
