from __future__ import annotations

import time

import pytest

from pi_orchestrator.agents import load_agents
from pi_orchestrator.delegation import DelegationError, DelegationOutcome, Delegator, format_outcome
from pi_orchestrator.worker import WorkerResult


@pytest.fixture
def delegator(settings, project, events):
    return Delegator(settings, load_agents(project), cwd=project, on_event=events.append)


async def test_single(delegator, script):
    script([{"system_contains": "You are a scout", "step": {"text": "scouted"}}])
    outcome = await delegator.single("scout", "look")
    assert outcome.ok
    assert outcome.results[0].output == "scouted"
    assert delegator.history == outcome.results


async def test_single_unknown_agent(delegator):
    with pytest.raises(DelegationError, match="unknown agent 'ghost'"):
        await delegator.single("ghost", "x")


async def test_parallel_respects_concurrency_and_order(settings, project, script, pi_log):
    script([{"step": {"text": "done", "delay": 0.6}}])
    delegator = Delegator(settings.with_overrides(max_concurrency=2), load_agents(project), cwd=project)
    tasks = [{"agent": "scout", "task": f"area {i}"} for i in range(4)]
    started = time.monotonic()
    outcome = await delegator.parallel(tasks)
    elapsed = time.monotonic() - started
    assert outcome.ok
    assert [r.task for r in outcome.results] == [f"area {i}" for i in range(4)]
    assert pi_log.max_overlap() == 2
    assert elapsed >= 1.2  # two waves of 0.6s


async def test_parallel_partial_failure_is_ok(delegator, script):
    script([{"task_contains": "bad", "step": {"error": "boom"}}, {"step": {"text": "fine"}}])
    outcome = await delegator.parallel([{"agent": "scout", "task": "good"}, {"agent": "scout", "task": "bad"}])
    assert outcome.ok
    assert [r.ok for r in outcome.results] == [True, False]


async def test_parallel_writer_guard(delegator, settings, project, script):
    script([{"step": {"text": "ok"}}])
    two_writers = [{"agent": "worker", "task": "a"}, {"agent": "worker", "task": "b"}]
    with pytest.raises(DelegationError, match="at most one write-capable"):
        await delegator.parallel(two_writers)
    # bash-only agents (scout, reviewer) don't count as writers; one writer is fine
    mixed = [{"agent": "scout", "task": "a"}, {"agent": "reviewer", "task": "b"}, {"agent": "worker", "task": "c"}]
    assert (await delegator.parallel(mixed)).ok
    lenient = Delegator(settings.with_overrides(allow_parallel_writers=True), load_agents(project), cwd=project)
    assert (await lenient.parallel(two_writers)).ok


async def test_parallel_limits(delegator):
    with pytest.raises(DelegationError, match="too many parallel tasks"):
        await delegator.parallel([{"agent": "scout", "task": str(i)} for i in range(9)])
    with pytest.raises(DelegationError, match="at least one"):
        await delegator.parallel([])
    with pytest.raises(DelegationError, match="non-empty `task`"):
        await delegator.parallel([{"agent": "scout", "task": ""}])


async def test_chain_pipes_previous_output(delegator, script, pi_log):
    script(
        [
            {"system_contains": "You are a scout", "step": {"text": "FINDINGS-123"}},
            {"system_contains": "planning specialist", "step": {"text": "PLAN"}},
        ]
    )
    outcome = await delegator.chain(
        [{"agent": "scout", "task": "find"}, {"agent": "planner", "task": "Plan using:\n{previous}"}]
    )
    assert outcome.ok
    assert [r.output for r in outcome.results] == ["FINDINGS-123", "PLAN"]
    assert pi_log.starts()[1]["task"] == "Plan using:\nFINDINGS-123"


async def test_chain_stops_at_first_failure(delegator, script):
    script([{"system_contains": "You are a scout", "step": {"error": "nope"}}, {"step": {"text": "never"}}])
    outcome = await delegator.chain([{"agent": "scout", "task": "a"}, {"agent": "planner", "task": "b"}])
    assert not outcome.ok
    assert len(outcome.results) == 1
    text = format_outcome(outcome, max_chars=10_000, planned_steps=2)
    assert "Chain stopped: 0/2 steps succeeded" in text


async def test_chain_validates_all_agents_first(delegator, pi_log):
    with pytest.raises(DelegationError, match="ghost"):
        await delegator.chain([{"agent": "scout", "task": "a"}, {"agent": "ghost", "task": "b"}])
    assert pi_log.starts() == []


async def test_handle_protocol(delegator, script):
    assert await delegator.handle("ping", None) == {"ok": True, "text": "pi-orchestrator", "details": {}}
    assert (await delegator.handle("explode", {}))["ok"] is False
    assert "unknown agent" in (await delegator.handle("delegate_task", {"agent": "ghost", "task": "x"}))["error"]
    assert "must be a list" in (await delegator.handle("delegate_parallel", {"tasks": "nope"}))["error"]

    script([{"step": {"text": "report"}}])
    reply = await delegator.handle("delegate_task", {"agent": "scout", "task": "x"})
    assert reply["ok"] and "report" in reply["text"]
    assert reply["details"]["results"][0]["agent"] == "scout"
    assert "output" not in reply["details"]["results"][0]


async def test_handle_failure_carries_report(delegator, script):
    script([{"step": {"error": "quota exceeded"}}])
    reply = await delegator.handle("delegate_task", {"agent": "scout", "task": "x"})
    assert reply["ok"] is False
    assert "FAILED: quota exceeded" in reply["error"]


def test_format_outcome_truncates_to_budget():
    results = [WorkerResult(agent="scout", task=str(i), ok=True, output="y" * 50_000) for i in range(3)]
    text = format_outcome(DelegationOutcome("parallel", results), max_chars=12_000)
    assert len(text) < 13_000
    assert text.count("characters omitted") == 3
    assert text.startswith("# Parallel delegation: 3/3 succeeded")
