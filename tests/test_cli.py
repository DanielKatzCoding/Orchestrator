from __future__ import annotations

import io
import json
import shlex

import pytest

from pi_orchestrator.cli import build_parser, main, settings_from_args

from conftest import FAKE_PI_COMMAND, ORCH

PI = ["--pi-command", shlex.join(FAKE_PI_COMMAND)]


def test_settings_from_args_and_env(monkeypatch):
    monkeypatch.setenv("PI_ORCHESTRATOR_WORKER_MODEL", "env/worker")
    monkeypatch.setenv("PI_ORCHESTRATOR_MAX_CONCURRENCY", "7")
    args = build_parser().parse_args(["run", "--model", "cli/brain", "--read-only", "--agents-dir", "/a", "task"])
    settings = settings_from_args(args)
    assert settings.model == "cli/brain"
    assert settings.worker_model == "env/worker"
    assert settings.max_concurrency == 7
    assert settings.read_only is True
    assert settings.agents_dirs == ("/a",)


def test_agents_json(capsys, project):
    assert main(["agents", "--json", "--cwd", str(project)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [a["name"] for a in payload["agents"]] == ["planner", "reviewer", "scout", "worker"]


def test_agents_human(capsys, project):
    assert main(["agents", "--cwd", str(project), "--read-only"]) == 0
    out = capsys.readouterr().out
    assert "worker  (builtin)" in out and "[writes]" not in out


def test_delegate(capsys, project, script):
    script([{"system_contains": "You are a scout", "step": {"text": "direct scout answer"}}])
    assert main(["delegate", "--agent", "scout", "--cwd", str(project), *PI, "look", "around"]) == 0
    captured = capsys.readouterr()
    assert "direct scout answer" in captured.out


def test_delegate_unknown_agent(capsys, project):
    assert main(["delegate", "-a", "ghost", "--cwd", str(project), *PI, "x"]) == 2
    assert "unknown agent" in capsys.readouterr().err


def brain_script(script):
    script(
        [
            {
                "system_contains": ORCH,
                "turn": 0,
                "step": {"tool": "delegate_task", "args": {"agent": "scout", "task": "x"}},
            },
            {"system_contains": ORCH, "turn": 1, "step": {"text": "All done."}},
            {"system_contains": "You are a scout", "step": {"text": "S"}},
        ]
    )


def test_run_default_command(capsys, project, script):
    brain_script(script)
    assert main(["summarize", "the", "repo", "--cwd", str(project), *PI]) == 0  # `run` is implied
    captured = capsys.readouterr()
    assert "All done." in captured.out
    assert "[scout#1]" in captured.err and "1 delegation(s)" in captured.err


def test_run_json_reads_stdin(capsys, project, script, monkeypatch):
    brain_script(script)
    monkeypatch.setattr("sys.stdin", io.StringIO("task from stdin\n"))
    assert main(["run", "--json", "--cwd", str(project), *PI]) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    result = records[-1]
    assert result["type"] == "result" and result["ok"] and result["output"] == "All done."
    assert result["delegations"][0]["agent"] == "scout"
    assert {r["kind"] for r in records[:-1]} >= {"start", "tool_start", "end"}


def test_run_failure_exit_code(capsys, project, script):
    script([{"system_contains": ORCH, "step": {"reject": "No API key found for anthropic."}}])
    assert main(["run", "--cwd", str(project), *PI, "x"]) == 1
    assert "No API key" in capsys.readouterr().err


def test_doctor(capsys, project):
    code = main(["doctor", "--json", "--cwd", str(project), *PI])
    checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["pi"]["ok"] and "0.73.1" in checks["pi"]["detail"]
    assert checks["models"]["ok"] and "fake" in checks["models"]["detail"]
    assert checks["agents"]["ok"]
    assert checks["shim channel"]["ok"], checks["shim channel"]
    assert code == (0 if checks["node"]["ok"] else 1)


def test_doctor_without_pi(capsys, project):
    assert main(["doctor", "--cwd", str(project), "--pi-command", "no-such-pi"]) == 1
    assert "✘ pi: not runnable" in capsys.readouterr().out


def test_bad_cwd(project):
    with pytest.raises(SystemExit, match="not a directory"):
        main(["agents", "--cwd", str(project / "missing")])
