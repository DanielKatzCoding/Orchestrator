from __future__ import annotations

import asyncio
import json

import pytest

from pi_orchestrator.pi_rpc import PiProcessError, PiRpcClient, PiRpcError

from conftest import FAKE_PI_COMMAND


def client(**kwargs) -> PiRpcClient:
    return PiRpcClient([*FAKE_PI_COMMAND, "--mode", "rpc"], **kwargs)


async def test_prompt_returns_text_and_stats(script):
    script([{"step": {"text": "hello from pi"}}])
    async with client() as pi:
        records = []
        outcome = await pi.prompt_and_wait("hi", on_record=records.append)
    assert outcome.ok
    assert outcome.text == "hello from pi"
    assert outcome.stop_reason == "stop"
    assert outcome.stats["assistantMessages"] == 1
    kinds = [r["type"] for r in records]
    assert kinds[0] == "agent_start" and "agent_end" in kinds


async def test_unicode_line_separators_inside_json_strings(script):
    # U+2028/U+2029 are legal inside JSON strings; splitting on them would corrupt records.
    text = "before middle after"
    script([{"step": {"text": text}}])
    async with client() as pi:
        outcome = await pi.prompt_and_wait("hi")
    assert outcome.text == text


async def test_records_larger_than_64k(script):
    script([{"step": {"text_size": 300_000}}])
    async with client() as pi:
        outcome = await pi.prompt_and_wait("big")
    assert len(outcome.text) == 300_000


async def test_rejected_prompt_raises(script):
    script([{"step": {"reject": "No API key found for anthropic."}}])
    async with client() as pi:
        with pytest.raises(PiRpcError, match="No API key"):
            await pi.prompt_and_wait("hi")


async def test_error_stop_reason(script):
    script([{"step": {"error": "400 bad request"}}])
    async with client(error_settle_grace_s=0.2) as pi:
        outcome = await pi.prompt_and_wait("hi")
    assert not outcome.ok
    assert outcome.stop_reason == "error"
    assert outcome.error == "400 bad request"


async def test_auto_retry_is_followed_to_the_end(script):
    script([{"turn": 0, "step": {"retry_once": "529 overloaded"}}, {"turn": 1, "step": {"text": "recovered"}}])
    async with client() as pi:
        outcome = await pi.prompt_and_wait("hi")
    assert outcome.ok
    assert outcome.text == "recovered"


async def test_ui_requests_go_to_handler(script):
    script(
        [
            {"turn": 0, "step": {"tool": "delegate_task", "args": {"agent": "scout", "task": "x"}}},
            {"turn": 1, "step": {"text": "got: {last_tool_result}"}},
        ]
    )
    seen = []

    async def handler(request):
        seen.append(json.loads(request["placeholder"]))
        return {"value": json.dumps({"ok": True, "text": "worker output"})}

    async with client(on_ui_request=handler) as pi:
        outcome = await pi.prompt_and_wait("go")
    assert seen == [{"op": "delegate_task", "params": {"agent": "scout", "task": "x"}}]
    assert outcome.text == "got: worker output"


async def test_ui_requests_without_handler_are_cancelled(script):
    script(
        [
            {"turn": 0, "step": {"tool": "delegate_task", "args": {}}},
            {"turn": 1, "step": {"text": "got: {last_tool_result}"}},
        ]
    )
    async with client() as pi:
        outcome = await pi.prompt_and_wait("go")
    assert outcome.text == "got: Delegation aborted"


async def test_crash_raises_process_error(script):
    script([{"step": {"exit": 3}}])
    async with client() as pi:
        with pytest.raises(PiProcessError, match="exited unexpectedly"):
            await pi.prompt_and_wait("hi")
        with pytest.raises(PiProcessError):
            await pi.send("get_state")


async def test_missing_executable():
    pi = PiRpcClient(["definitely-not-a-real-pi-binary", "--mode", "rpc"])
    with pytest.raises(PiProcessError, match="pi executable not found"):
        await pi.start()


async def test_abort_stops_a_running_prompt(script):
    script([{"step": {"sleep": 30}}])
    async with client() as pi:
        running = asyncio.create_task(pi.prompt_and_wait("hang"))
        await asyncio.sleep(0.5)
        await pi.abort()
        outcome = await asyncio.wait_for(running, 10)
    assert outcome.stop_reason == "aborted"
    assert not outcome.ok


async def test_close_is_idempotent_and_reaps(script):
    script([{"step": {"text": "x"}}])
    pi = client()
    await pi.start()
    await pi.close()
    await pi.close()
    assert pi.returncode is not None
