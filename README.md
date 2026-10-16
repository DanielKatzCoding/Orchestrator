# pi-orchestrator

An **orchestrator agent built on the [pi](https://github.com/badlogic/pi-mono) coding-agent framework**, written in Python and usable from **Claude Code**.

The orchestrator breaks a software task into focused sub-tasks and hands them to specialist agents: `scout`, `planner`, `worker` and `reviewer`, plus any agents you define. It then combines their reports. Every agent is a real `pi` process with its own context window, tools and model, so you get pi's providers (Anthropic, OpenAI, Google, Bedrock, OpenRouter, local models, …), its tools (`read`, `grep`, `find`, `ls`, `bash`, `edit`, `write`), auto-retry and compaction.

You can use it in three ways:

- **CLI:** `pi-orchestrator "add input validation to the signup handler"`.
- **Claude Code plugin:** Claude becomes the orchestrator and sends work to pi agents through MCP tools (`/pi-orchestrator:orchestrate <task>`).
- **Python library:** `Orchestrator`, `Delegator`, `run_worker`.

```
        Claude Code ─ /pi-orchestrator:orchestrate              CLI: pi-orchestrator run "task"
                 │ MCP (stdio)                                   │
      pi_orchestrator.mcp_server                       Orchestrator (Python host)
                 │                                      └─ pi --mode rpc -e orchestrator_shim.ts   ← the "brain"
                 │                                           delegate_* tools ──extension UI channel──┐
                 └──────────────────┬─────────────────────────────────────────────────────────────────┘
                              Delegator (Python): single · parallel · chain ({previous})
                                    │
                      pi --mode rpc --no-session --tools … --append-system-prompt <role>   (one process per task)
```

## Requirements

- **Python ≥ 3.11** and [uv](https://docs.astral.sh/uv/). uv is required for the Claude Code plugin and recommended otherwise.
- **Node.js ≥ 20.6** and **pi ≥ 0.73**: `npm install -g @mariozechner/pi-coding-agent`.
- Credentials for at least one model provider. Either run `pi` once and use `/login`, or export a key such as `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. pi-orchestrator reuses pi's own auth (`~/.pi/agent/auth.json`) and environment variables.

## Install

```bash
uv tool install git+https://github.com/DanielKatzCoding/Orchestrator   # provides the `pi-orchestrator` command
pi-orchestrator doctor                                                  # checks pi, models, agents and the shim channel
```

From a checkout: `uv sync`, then `uv run pi-orchestrator …`.

## CLI usage

```bash
pi-orchestrator "summarize how authentication works in this repo" --read-only
pi-orchestrator run --model anthropic/claude-opus-4-7 --worker-model anthropic/claude-sonnet-4-6 "fix the failing tests"
echo "add a --verbose flag to the CLI" | pi-orchestrator run --json > events.jsonl
pi-orchestrator                                       # interactive REPL (one orchestrator session, many turns)
pi-orchestrator agents                                # list agents (built-in, user, project)
pi-orchestrator delegate --agent scout "map the payment module"   # run one agent directly
pi-orchestrator mcp                                   # MCP server on stdio (used by Claude Code)
```

The orchestrator's text streams to stdout and worker progress goes to stderr (`▶ [scout#1] …`, `✔ [scout#1] done in 12.3s · $0.0123`). Ctrl+C aborts the orchestrator and every running worker.

| Option | Meaning |
|---|---|
| `--model provider/id[:thinking]` | Model for the orchestrator. It is also the default worker model. Without it, pi's default model is used. |
| `--worker-model` | Default model for workers whose definition doesn't pin one. |
| `--thinking` | Orchestrator thinking level: `off`, `minimal`, `low`, `medium`, `high` or `xhigh`. |
| `--cwd` | Project directory the agents work in. |
| `--read-only` | Removes `bash`, `edit` and `write` from every agent. |
| `--agents-dir DIR` | Adds agent definitions; repeatable, highest precedence. |
| `--extension PATH` | Loads an extra pi extension into every pi process, e.g. a custom provider. |
| `--max-concurrency N` | Parallel workers at once (default 4; at most 8 tasks per batch). |
| `--timeout SECONDS` | Per-worker timeout (default 1200). |
| `--no-context-files` | Stops pi loading `AGENTS.md` / `CLAUDE.md`. |
| `--json` | Emits JSON lines (events, then a final `result` record). |

Every setting can also come from the environment as `PI_ORCHESTRATOR_<NAME>`: `PI_ORCHESTRATOR_MODEL`, `PI_ORCHESTRATOR_WORKER_MODEL`, `PI_ORCHESTRATOR_READ_ONLY=1`, `PI_ORCHESTRATOR_PI_COMMAND`, `PI_ORCHESTRATOR_AGENTS_DIRS` (path-separated), `PI_ORCHESTRATOR_MAX_CONCURRENCY`, `PI_ORCHESTRATOR_WORKER_TIMEOUT_S`, `PI_ORCHESTRATOR_MAX_OUTPUT_CHARS`, `PI_ORCHESTRATOR_CWD`, and so on.

## Use from Claude Code

This repository is both a Claude Code **plugin** and a **plugin marketplace**.

```text
/plugin install pi-orchestrator --marketplace DanielKatzCoding/Orchestrator
```

The one-step form needs Claude Code 2.1.275 or later. On older versions:

```text
/plugin marketplace add DanielKatzCoding/Orchestrator
/plugin install pi-orchestrator@pi-orchestrator-marketplace
```

The plugin starts the MCP server with `uv run --project ${CLAUDE_PLUGIN_ROOT} pi-orchestrator mcp`. Its virtualenv lives in the plugin's persistent data directory. `/mcp` should list `plugin:pi-orchestrator:orchestrator` as connected.

- **`/pi-orchestrator:orchestrate <task>`:** Claude runs scout → planner → worker → reviewer through the MCP tools and reports back.
- The **`pi-delegation`** skill teaches Claude when delegating pays off: broad recon, work that would flood its context, cheaper or other models, parallel sub-tasks.

MCP tools (named `mcp__plugin_pi-orchestrator_orchestrator__<tool>` in Claude Code):

| Tool | What it does |
|---|---|
| `list_agents(cwd?)` | Lists the available agents, their tools, and whether they modify files. |
| `delegate_task(agent, task, cwd?, model?)` | Runs one agent on one self-contained task and returns its report. |
| `delegate_parallel(tasks[], cwd?)` | Runs independent tasks concurrently, with at most one file-modifying agent per batch. |
| `delegate_chain(steps[], cwd?)` | Runs steps in order. `{previous}` inserts the prior step's output. |
| `orchestrate(task, cwd?, model?, read_only?)` | Hands the whole task to pi's own orchestrator agent. |

Notes:

- Tools stream MCP progress notifications, plus a heartbeat every 20 s. Claude Code aborts a stdio call after 30 minutes without progress, and the heartbeat prevents that.
- Claude Code moves MCP calls running longer than about 2 minutes to background tasks. The skill tells Claude to wait for the notification.
- Results are capped at about 40 000 characters (`PI_ORCHESTRATOR_MAX_OUTPUT_CHARS`), below Claude Code's 50 000-character inline limit.
- To skip permission prompts, add `"mcp__plugin_pi-orchestrator_orchestrator__*"` to `permissions.allow` in your Claude Code settings.

**Without the plugin**, register the server directly. `--scope project` writes a `.mcp.json` in your project:

```bash
claude mcp add --transport stdio --scope project orchestrator -- \
  uv run --project /absolute/path/to/Orchestrator pi-orchestrator mcp
```

## Agents

| Agent | Tools | Role |
|---|---|---|
| `scout` | read, grep, find, ls, bash (read-only use) | Fast recon. Returns files, key code and architecture for handoff. |
| `planner` | read, grep, find, ls | Turns requirements and findings into a concrete step-by-step plan. |
| `worker` | read, grep, find, ls, bash, edit, write | Implements the plan, runs tests, reports the files it changed. |
| `reviewer` | read, grep, find, ls, bash (read-only use + tests) | Reviews the diff and ends with APPROVE or CHANGES REQUESTED. |

Define your own as Markdown with YAML frontmatter. Later locations override earlier ones by `name`: built-in → `~/.pi/agent/orchestrator/agents/` → `<project>/.pi/orchestrator/agents/` → `--agents-dir`.

```markdown
---
name: test-writer
description: Writes focused unit tests for a given module
tools: read, grep, find, ls, bash, edit, write
model: anthropic/claude-sonnet-4-6     # optional; otherwise --worker-model / --model / pi default
thinking: medium                       # optional
system_prompt_mode: append             # append (default, keeps pi's coding prompt) | replace
context_files: true                    # let pi load AGENTS.md / CLAUDE.md (default)
---
You write small, focused unit tests. Match the project's existing test style...
```

## Library usage

```python
import asyncio
from pi_orchestrator import Delegator, Orchestrator, Settings, load_agents


async def main() -> None:
    settings = Settings(model="anthropic/claude-sonnet-4-6", read_only=True)

    # Full orchestrator (pi brain + workers)
    result = await Orchestrator(settings, cwd=".", on_event=print).run("Summarize the architecture")
    print(result.output, result.total_usage.short())

    # Or drive the agents yourself
    delegator = Delegator(settings, load_agents("."), cwd=".")
    outcome = await delegator.parallel(
        [
            {"agent": "scout", "task": "Map the HTTP layer"},
            {"agent": "scout", "task": "Map the persistence layer"},
        ]
    )
    for r in outcome.results:
        print(r.agent, r.ok, r.output[:200])


asyncio.run(main())
```

## How it works

- **pi RPC mode.** Every agent runs as `pi --mode rpc` and is driven over JSON lines on stdin/stdout (`pi_rpc.PiRpcClient`). Framing follows pi's spec: records are split on `\n` only, so U+2028 inside strings is safe and lines have no size limit. A run counts as finished only after pi settles, which covers auto-retries and compaction that happen after `agent_end`.
- **Workers** start with `--no-session --no-extensions --no-skills --no-prompt-templates --no-themes`, the agent's tool allowlist, and its role prompt appended to pi's coding system prompt (`--append-system-prompt`). They never receive the delegation tools, so delegation can't recurse.
- **The orchestrator brain** is a pi agent with read-only tools plus three delegation tools. They come from a small TypeScript extension, `src/pi_orchestrator/extension/orchestrator_shim.ts`, which pi loads at runtime with no build step. Each tool call goes back to the Python host over pi's RPC *extension UI* channel: `ctx.ui.input("pi-orchestrator:rpc", <json>)` becomes an `extension_ui_request`, and the host answers it. No sockets or ports are involved. The shim is inert unless `PI_ORCHESTRATOR_HOST=1`.
- **Safety.** A parallel batch allows at most one agent with `edit`/`write`. `--read-only` removes `bash`, `edit` and `write` everywhere. Timeouts and Ctrl+C or MCP cancellation abort pi and kill its process group. Results are truncated (head and tail kept) before they go back to the orchestrating model.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest                      # unit tests: a scripted fake pi (tests/fake_pi.py), no Node or API keys
uv run pytest tests/integration    # real `pi` + tests/pi_ext/scripted_provider.ts (a deterministic model, no keys)
ANTHROPIC_API_KEY=... uv run pytest tests/integration -k real_model    # one real-model smoke test
claude plugin validate . --strict  # plugin + marketplace manifests
```

`tests/pi_ext/scripted_provider.ts` is a test-only pi extension. It registers a `scripted/scripted-1` model whose replies come from a rules file (`SCRIPTED_PI_RULES`). That lets the real pi agent loop, real tools and the real shim run deterministically. It also works as an offline demo: write a rules file (see the extension's header and `tests/integration/test_real_pi.py`), then run `SCRIPTED_PI_RULES=rules.json pi-orchestrator --extension tests/pi_ext/scripted_provider.ts --model scripted/scripted-1 "demo"`.

## Credits

Built on [pi](https://github.com/badlogic/pi-mono) by Mario Zechner (MIT). The built-in agent prompts are adapted from pi's `examples/extensions/subagent` agents.

