---
name: pi-delegation
description: When and how to delegate work to pi agents through the pi-orchestrator MCP tools (delegate_task, delegate_parallel, delegate_chain, orchestrate). Use for broad codebase recon across several areas, work that would flood your context window, tasks that should run on a different or cheaper model, or independent sub-tasks that can run in parallel.
---

# Delegating to pi agents

The pi-orchestrator MCP server runs specialist pi coding agents in isolated sessions. Each session
has its own context window, tool set and model. Only the agent's final report comes back to you.

## When it pays off

- **Broad recon:** several independent `scout` tasks in one `delegate_parallel` call. Each returns a
  compressed summary of files and key code instead of you reading everything.
- **Context hygiene:** large explorations or long implementation loops whose details you don't need.
- **Other models:** pass `model` (`provider/id[:thinking]`, any model pi can use, e.g.
  `anthropic/claude-haiku-4-5` or `openai/gpt-5-mini`) to run a task on a cheaper or different model.
- **Second opinions:** a `reviewer` pass over a diff, independent of your own reasoning.

## When it doesn't

- Small, local edits you can make directly. Delegation costs a cold start and a fresh context.
- Tasks that need this conversation's history, unless you paste the necessary context into the task.

## Writing good tasks

- Be self-contained: goal, paths, constraints, relevant findings, expected output format.
- Pass `cwd` = the absolute project path on every call.
- Only one agent that modifies files (edit/write) per `delegate_parallel` batch. Use `delegate_chain`
  (`{previous}` inserts the prior step's output) for scout → planner → worker flows.
- Long calls may be moved to the background by Claude Code. Wait for the task notification.

Call `list_agents` to see the agents available in this project. Projects can add their own in
`.pi/orchestrator/agents/*.md`.
