"""Command-line interface: ``pi-orchestrator [run|agents|delegate|mcp|doctor] ...``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .agents import load_agents
from .config import Settings
from .delegation import DelegationError, Delegator
from .orchestrator import Orchestrator, OrchestratorResult
from .pi_rpc import PiRpcError
from .render import JsonRenderer, TerminalRenderer

COMMANDS = ("run", "agents", "delegate", "mcp", "doctor")
MIN_PI_VERSION = (0, 73, 0)


def _common_options() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("common options")
    g.add_argument("--cwd", help="project directory the agents work in (default: current directory)")
    g.add_argument("--model", help="orchestrator model, provider/id[:thinking] (default: pi's default model)")
    g.add_argument("--worker-model", help="default model for workers that don't pin one")
    g.add_argument("--thinking", help="orchestrator thinking level: off|minimal|low|medium|high|xhigh")
    g.add_argument("--agents-dir", action="append", default=None, help="extra agent definitions dir (repeatable)")
    g.add_argument("--extension", action="append", default=None, help="extra pi extension for every pi process")
    g.add_argument("--max-concurrency", type=int, help="max workers running at once (default 4)")
    g.add_argument("--timeout", type=float, dest="worker_timeout_s", help="per-worker timeout in seconds")
    g.add_argument("--read-only", action="store_true", default=None, help="remove bash/edit/write from all agents")
    g.add_argument("--no-context-files", action="store_true", help="don't let pi load AGENTS.md/CLAUDE.md")
    g.add_argument("--pi-command", help="command used to launch pi (default: pi)")
    g.add_argument("--json", action="store_true", help="emit JSON lines instead of human-readable output")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="pi-orchestrator",
        description="An orchestrator agent built on pi: plans, delegates to specialist pi agents, combines results.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = sub.add_parser("run", parents=[common], help="run the orchestrator on a task (default command)")
    run.add_argument("task", nargs="*", help="the task; read from stdin if omitted and piped, REPL if a TTY")
    run.add_argument("--show-worker-text", action="store_true", help="also stream workers' text to stderr")

    agents = sub.add_parser("agents", parents=[common], help="list available agents")
    agents.add_argument("--verbose", "-v", action="store_true", help="also print each agent's prompt")

    delegate = sub.add_parser("delegate", parents=[common], help="run a single agent directly")
    delegate.add_argument("--agent", "-a", required=True, help="agent name (see `pi-orchestrator agents`)")
    delegate.add_argument("task", nargs="+", help="the task for the agent")

    sub.add_parser("mcp", parents=[common], help="serve the delegation tools over MCP (stdio), e.g. for Claude Code")
    sub.add_parser("doctor", parents=[common], help="check pi, models, agents and the shim channel")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {
        "model": args.model,
        "worker_model": args.worker_model,
        "thinking": args.thinking,
        "max_concurrency": args.max_concurrency,
        "worker_timeout_s": args.worker_timeout_s,
        "read_only": args.read_only,
    }
    base = Settings.from_env()
    if args.agents_dir:
        overrides["agents_dirs"] = (*base.agents_dirs, *args.agents_dir)
    if args.extension:
        overrides["extensions"] = (*base.extensions, *args.extension)
    if args.pi_command:
        overrides["pi_command"] = tuple(shlex.split(args.pi_command))
    if args.no_context_files:
        overrides["load_context_files"] = False
    return base.with_overrides(**overrides)


def _cwd(args: argparse.Namespace) -> str:
    cwd = Path(args.cwd or os.environ.get("PI_ORCHESTRATOR_CWD") or os.getcwd()).expanduser().resolve()
    if not cwd.is_dir():
        raise SystemExit(f"pi-orchestrator: --cwd {cwd} is not a directory")
    return str(cwd)


# ---------------------------------------------------------------------- commands


def _print_result_footer(result: OrchestratorResult) -> None:
    usage = result.total_usage
    status = "done" if result.ok else f"failed: {result.error}"
    print(
        f"\n— {status} in {result.duration_s:.1f}s · {len(result.delegations)} delegation(s) · {usage.short()}",
        file=sys.stderr,
    )


async def _cmd_run(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    cwd = _cwd(args)
    json_out = JsonRenderer() if args.json else None
    renderer = json_out or TerminalRenderer(show_worker_text=args.show_worker_text)
    orchestrator = Orchestrator(settings, cwd=cwd, on_event=renderer)
    for diagnostic in orchestrator.catalog.diagnostics:
        print(f"warning: {diagnostic}", file=sys.stderr)

    task = " ".join(args.task).strip()
    if not task and not sys.stdin.isatty():
        task = sys.stdin.read().strip()
    if task:
        result = await orchestrator.run(task)
        if json_out:
            json_out.write({"type": "result", **result.to_dict()})
        else:
            if result.error and not result.output:
                print(f"pi-orchestrator: {result.error}", file=sys.stderr)
            _print_result_footer(result)
        return 0 if result.ok else 1

    # Interactive REPL: one brain process, many turns.
    try:
        await orchestrator.start()
    except PiRpcError as exc:
        print(f"pi-orchestrator: {exc}", file=sys.stderr)
        return 1
    print("pi-orchestrator interactive mode. Type a task, /exit to quit.", file=sys.stderr)
    async with orchestrator:
        while True:
            try:
                line = await asyncio.to_thread(input, "orchestrator> ")
            except EOFError:
                print(file=sys.stderr)
                return 0
            line = line.strip()
            if not line:
                continue
            if line in ("/exit", "/quit"):
                return 0
            result = await orchestrator.ask(line)
            if result.error and not result.output:
                print(f"pi-orchestrator: {result.error}", file=sys.stderr)
            _print_result_footer(result)


async def _cmd_agents(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    catalog = load_agents(_cwd(args), settings.agents_dirs)
    if args.json:
        print(json.dumps({"agents": [a.summary() for a in catalog], "diagnostics": catalog.diagnostics}, indent=2))
        return 0
    for agent in catalog:
        effective = agent.restricted(settings.read_only)
        writes = " [writes]" if effective.is_write_capable else ""
        model = f" model={agent.model}" if agent.model else ""
        print(f"{agent.name}{writes}  ({agent.source}){model}")
        print(f"    tools: {', '.join(effective.tools) or '(none)'}")
        print(f"    {agent.description}")
        if args.verbose:
            print("    ---")
            for line in agent.prompt.splitlines():
                print(f"    {line}")
    for diagnostic in catalog.diagnostics:
        print(f"warning: {diagnostic}", file=sys.stderr)
    return 0


async def _cmd_delegate(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    cwd = _cwd(args)
    renderer = JsonRenderer() if args.json else TerminalRenderer(show_worker_text=True)
    delegator = Delegator(settings, load_agents(cwd, settings.agents_dirs), cwd=cwd, on_event=renderer)
    try:
        outcome = await delegator.single(args.agent, " ".join(args.task))
    except DelegationError as exc:
        print(f"pi-orchestrator: {exc}", file=sys.stderr)
        return 2
    result = outcome.results[0]
    if args.json:
        renderer.write({"type": "result", **result.to_dict()})  # type: ignore[union-attr]
    else:
        print(result.output if result.ok else f"pi-orchestrator: {result.error}\n{result.output}".rstrip())
    return 0 if result.ok else 1


def _pi_version(command: Sequence[str]) -> tuple[str | None, str | None]:
    try:
        proc = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    text = (proc.stdout or proc.stderr).strip()
    return (text.splitlines()[-1] if text else None), (None if proc.returncode == 0 else text)


def _version_tuple(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in match.groups()) if match else ()


async def _cmd_doctor(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    cwd = _cwd(args)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})
        if not args.json:
            mark = "✔" if ok else "✘"
            print(f"{mark} {name}: {detail}")

    version, error = _pi_version(settings.pi_command)
    if version is None:
        check("pi", False, f"not runnable ({error}); install with `npm install -g @mariozechner/pi-coding-agent`")
    else:
        new_enough = _version_tuple(version) >= MIN_PI_VERSION
        need = ".".join(map(str, MIN_PI_VERSION))
        check(
            "pi", new_enough, f"{' '.join(settings.pi_command)} {version}" + ("" if new_enough else f" (need ≥ {need})")
        )

    node = shutil.which("node")
    check("node", node is not None, node or "not on PATH (pi needs Node.js ≥ 20.6)")

    if version is not None:
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [*settings.pi_command, "--no-extensions", *settings.extension_args(), "--list-models"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=settings.process_env(),
            )
            rows = [line.split()[0] for line in proc.stdout.splitlines()[1:] if line.strip()]
            providers = sorted(set(rows))
            detail = f"{len(rows)} models from {', '.join(providers)}" if rows else "no models with credentials"
            check(
                "models", bool(rows), detail + ("" if rows else "; run `pi` and /login, or set e.g. ANTHROPIC_API_KEY")
            )
        except subprocess.TimeoutExpired:
            check("models", False, "`pi --list-models` timed out")

    catalog = load_agents(cwd, settings.agents_dirs)
    check("agents", bool(catalog.agents), ", ".join(catalog.names()) or "none found")
    for diagnostic in catalog.diagnostics:
        check("agent file", False, diagnostic)

    if version is not None:
        orchestrator = Orchestrator(settings, cwd=cwd, catalog=catalog)
        try:
            latency = await orchestrator.ping()
            check("shim channel", True, f"orchestrator shim answered in {latency * 1000:.0f} ms")
        except (PiRpcError, TimeoutError) as exc:
            check("shim channel", False, str(exc) or "timed out")
        finally:
            await orchestrator.close()

    uv = shutil.which("uv")
    check("uv", uv is not None, uv or "not on PATH (needed by the Claude Code plugin's MCP server)")

    if args.json:
        print(json.dumps({"checks": checks}, indent=2))
    ok = all(c["ok"] for c in checks if c["check"] != "uv")
    return 0 if ok else 1


def _cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve

    serve(settings_from_args(args), default_cwd=args.cwd)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "run")
    args = build_parser().parse_args(argv)
    if args.command == "mcp":
        return _cmd_mcp(args)
    handler = {"run": _cmd_run, "agents": _cmd_agents, "delegate": _cmd_delegate, "doctor": _cmd_doctor}[args.command]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        print("\npi-orchestrator: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
