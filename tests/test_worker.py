from __future__ import annotations

import asyncio

import pytest

from pi_orchestrator.agents import load_agents, parse_agent_text
from pi_orchestrator.config import Settings
from pi_orchestrator.worker import build_worker_argv, resolve_worker_model, run_worker

from conftest import pid_alive


@pytest.fixture
def scout(project):
    return load_agents(project).get("scout")


def test_argv_shape(scout):
    settings = Settings(pi_command=("pi",), extensions=("/x/ext.ts",))
    argv = build_worker_argv(scout, settings, model="anthropic/claude-haiku-4-5")
    assert argv[:4] == ["pi", "--mode", "rpc", "--no-session"]
    for flag in ("--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes"):
        assert flag in argv
    assert argv[argv.index("-e") + 1] == "/x/ext.ts"
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls,bash"
    assert argv[argv.index("--model") + 1] == "anthropic/claude-haiku-4-5"
    assert argv[argv.index("--append-system-prompt") + 1] == scout.prompt
    assert "--no-context-files" not in argv


def test_argv_replace_mode_thinking_and_context_files():
    agent = parse_agent_text(
        "---\nname: a\ndescription: b\ntools: []\nthinking: high\nsystem_prompt_mode: replace\n"
        "context_files: false\n---\nOnly this."
    )
    argv = build_worker_argv(agent, Settings(pi_command=("pi",)), model=None)
    assert "--no-tools" in argv and "--tools" not in argv
    assert argv[argv.index("--thinking") + 1] == "high"
    assert argv[argv.index("--system-prompt") + 1] == "Only this."
    assert "--no-context-files" in argv
    assert "--model" not in argv


def test_model_resolution_order(scout):
    pinned = parse_agent_text("---\nname: a\ndescription: b\nmodel: p/pinned\n---\nx")
    s = Settings(model="p/orchestrator", worker_model="p/worker")
    assert resolve_worker_model(pinned, s, "p/override") == "p/override"
    assert resolve_worker_model(pinned, s) == "p/pinned"
    assert resolve_worker_model(scout, s) == "p/worker"
    assert resolve_worker_model(scout, Settings(model="p/orchestrator")) == "p/orchestrator"
    assert resolve_worker_model(scout, Settings()) is None


async def test_success(scout, settings, script, project, pi_log, events):
    script([{"system_contains": "You are a scout", "step": {"text": "## Files Retrieved\n1. README.md"}}])
    result = await run_worker(scout, "map the repo", settings, cwd=project, on_event=events.append)
    assert result.ok, result.error
    assert "README.md" in result.output
    assert result.usage.turns == 1 and result.usage.cost == pytest.approx(0.001)
    assert result.duration_s > 0
    assert pi_log.starts()[0]["cwd"] == str(project)
    kinds = [e.kind for e in events]
    assert kinds[0] == "start" and kinds[-1] == "end" and "text" in kinds
    assert {e.worker_id for e in events} == {result.worker_id}


async def test_model_error_is_a_failed_result(scout, settings, script, project):
    script([{"step": {"error": "401 invalid x-api-key"}}])
    result = await run_worker(scout, "t", settings.with_overrides(), cwd=project)
    assert not result.ok
    assert result.error == "401 invalid x-api-key"


async def test_rejected_prompt_is_a_failed_result(scout, settings, script, project):
    script([{"step": {"reject": "No API key found for anthropic."}}])
    result = await run_worker(scout, "t", settings, cwd=project)
    assert not result.ok
    assert "No API key" in result.error


async def test_missing_pi_is_a_failed_result(scout, project):
    result = await run_worker(scout, "t", Settings(pi_command=("no-such-pi-binary",)), cwd=project)
    assert not result.ok
    assert "pi executable not found" in result.error


async def test_timeout_aborts_and_reaps(scout, settings, script, project, pi_log):
    script([{"step": {"sleep": 60}}])
    result = await run_worker(scout, "t", settings.with_overrides(worker_timeout_s=1.0), cwd=project)
    assert not result.ok
    assert "timed out" in result.error
    assert not pid_alive(pi_log.starts()[0]["pid"])


async def test_cancellation_propagates_and_reaps(scout, settings, script, project, pi_log):
    script([{"step": {"sleep": 60}}])
    task = asyncio.create_task(run_worker(scout, "t", settings, cwd=project))
    for _ in range(100):
        if pi_log.starts():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pid_alive(pi_log.starts()[0]["pid"])


async def test_read_only_strips_tools(project, settings, script, pi_log):
    worker = load_agents(project).get("worker")
    script([{"step": {"text": "ok"}}])
    await run_worker(worker, "t", settings.with_overrides(read_only=True), cwd=project)
    argv = pi_log.starts()[0]["argv"]
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"
