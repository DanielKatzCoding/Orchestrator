from __future__ import annotations

import asyncio

import pytest

from pi_orchestrator.orchestrator import BRAIN_TOOLS, Orchestrator, shim_path
from pi_orchestrator.prompts import build_orchestrator_prompt

from conftest import ORCH, pid_alive


def brain_rules(*steps):
    return [{"system_contains": ORCH, "turn": i, "step": step} for i, step in enumerate(steps)]


async def test_run_delegates_and_combines(settings, project, script, pi_log, events):
    script(
        [
            *brain_rules(
                {"tool": "delegate_task", "args": {"agent": "scout", "task": "map the project"}},
                {"text": "FINAL: {last_tool_result}"},
            ),
            {"system_contains": "You are a scout", "step": {"text": "scout found README.md"}},
        ]
    )
    orchestrator = Orchestrator(settings, cwd=project, on_event=events.append)
    result = await orchestrator.run("summarize the project")

    assert result.ok, result.error
    assert result.output.startswith("FINAL: ")
    assert "scout found README.md" in result.output
    assert [d.agent for d in result.delegations] == ["scout"]
    assert result.usage.turns == 2
    assert result.worker_usage.turns == 1
    assert result.total_usage.turns == 3

    brain, worker = pi_log.starts()
    assert brain["host"] == "1" and worker["host"] is None  # only the brain gets the shim channel
    assert brain["argv"][brain["argv"].index("-e") + 1] == str(shim_path())
    assert brain["argv"][brain["argv"].index("--tools") + 1] == ",".join(BRAIN_TOOLS)
    assert "-e" not in worker["argv"]  # workers never get the delegation tools

    sources = {(e.source, e.kind) for e in events}
    assert {("orchestrator", "start"), ("worker", "start"), ("worker", "end"), ("orchestrator", "end")} <= sources


async def test_parallel_and_chain_through_shim(settings, project, script):
    script(
        [
            *brain_rules(
                {
                    "tool": "delegate_parallel",
                    "args": {"tasks": [{"agent": "scout", "task": "a"}, {"agent": "reviewer", "task": "b"}]},
                },
                {
                    "tool": "delegate_chain",
                    "args": {
                        "steps": [{"agent": "planner", "task": "p"}, {"agent": "worker", "task": "do {previous}"}]
                    },
                },
                {"text": "CHAIN: {last_tool_result}"},
            ),
            {"system_contains": "You are a scout", "step": {"text": "S"}},
            {"system_contains": "senior code reviewer", "step": {"text": "R"}},
            {"system_contains": "planning specialist", "step": {"text": "PLAN-7"}},
            {"system_contains": "worker agent", "step": {"text": "implemented"}},
        ]
    )
    result = await Orchestrator(settings, cwd=project).run("go")
    assert result.ok
    assert [d.agent for d in result.delegations] == ["scout", "reviewer", "planner", "worker"]
    assert result.delegations[-1].task == "do PLAN-7"
    assert "Chain completed: 2/2" in result.output


async def test_delegation_errors_reach_the_brain(settings, project, script):
    script(
        brain_rules(
            {"tool": "delegate_task", "args": {"agent": "ghost", "task": "x"}},
            {"text": "ERR: {last_tool_result}"},
        )
    )
    result = await Orchestrator(settings, cwd=project).run("go")
    assert "unknown agent 'ghost'" in result.output


async def test_prompt_mentions_agents_and_read_only(settings, project):
    orchestrator = Orchestrator(settings.with_overrides(read_only=True), cwd=project)
    prompt = build_orchestrator_prompt(orchestrator.catalog, orchestrator.settings)
    assert prompt.startswith(ORCH)
    for name in ("scout", "planner", "worker", "reviewer"):
        assert f"`{name}`" in prompt
    assert "Read-only mode is on" in prompt
    assert "| `worker` | no |" in prompt


async def test_brain_model_error(settings, project, script):
    script(brain_rules({"reject": "No API key found for anthropic."}))
    result = await Orchestrator(settings, cwd=project).run("go")
    assert not result.ok
    assert "No API key" in result.error


async def test_missing_pi(project):
    from pi_orchestrator.config import Settings

    result = await Orchestrator(Settings(pi_command=("no-such-pi",)), cwd=project).run("go")
    assert not result.ok
    assert "pi executable not found" in result.error


async def test_ping(settings, project):
    async with Orchestrator(settings, cwd=project) as orchestrator:
        latency = await orchestrator.ping()
    assert latency < 10


async def test_multi_turn_reuses_one_brain(settings, project, script, pi_log):
    script(brain_rules({"text": "first"}, {"text": "second"}))
    async with Orchestrator(settings, cwd=project) as orchestrator:
        one = await orchestrator.ask("1")
        two = await orchestrator.ask("2")
    assert (one.output, two.output) == ("first", "second")
    assert one.usage.turns == 1 and two.usage.turns == 1  # per-turn usage, not cumulative
    assert len({r["pid"] for r in pi_log.starts()}) == 1


async def test_cancelling_run_reaps_brain_and_workers(settings, project, script, pi_log):
    script(
        [
            *brain_rules({"tool": "delegate_task", "args": {"agent": "scout", "task": "slow"}}),
            {"system_contains": "You are a scout", "step": {"sleep": 60}},
        ]
    )
    task = asyncio.create_task(Orchestrator(settings, cwd=project).run("go"))
    for _ in range(200):
        if len(pi_log.starts()) == 2:
            break
        await asyncio.sleep(0.05)
    assert len(pi_log.starts()) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    for record in pi_log.starts():
        assert not pid_alive(record["pid"]), record
