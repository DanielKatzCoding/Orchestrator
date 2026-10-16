---
description: Orchestrate a coding task by delegating focused sub-tasks to specialist pi agents (scout, planner, worker, reviewer) through the pi-orchestrator MCP tools.
argument-hint: "[task description]"
disable-model-invocation: true
allowed-tools:
  - mcp__plugin_pi-orchestrator_orchestrator__list_agents
  - mcp__plugin_pi-orchestrator_orchestrator__delegate_task
  - mcp__plugin_pi-orchestrator_orchestrator__delegate_parallel
  - mcp__plugin_pi-orchestrator_orchestrator__delegate_chain
  - mcp__plugin_pi-orchestrator_orchestrator__orchestrate
---

You are the orchestrator for this task. Delegate the work to pi agents through the pi-orchestrator
MCP tools and combine their reports into one answer.

Task: $ARGUMENTS

How to proceed:

1. Call `list_agents` with `cwd` set to the absolute path of the current project to see the available
   agents and which of them can modify files.
2. Recon: send `scout` tasks with `delegate_parallel` when there are several independent areas to explore,
   or `delegate_task` for one. Skip this step for a tiny, fully specified change.
3. Plan: give `planner` the requirements plus the scout findings, pasted in full. Use `delegate_chain` with
   `{previous}` when the plan should consume the scout output directly.
4. Implement: give `worker` the concrete plan. Only one agent that modifies files may run at a time.
5. Review: have `reviewer` check the diff and run the tests. If it reports CHANGES REQUESTED, send the specific
   findings back to `worker`, then review again (at most two rounds).
6. Report: summarize what was done, the files changed, how it was verified, and any open issues. Mention the
   cost and time the delegations reported.

Rules:

- Always pass `cwd` = the absolute project path on every call.
- Every task you write must be self-contained: the agents have not seen this conversation. Include the
  goal, relevant paths, constraints, the findings they need, and the output format you want.
- If a tool call comes back as a background task (long MCP calls are moved to the background after about
  two minutes), wait for its task notification before you continue. Don't start conflicting work meanwhile.
- If the user wants a hands-off run, `orchestrate` hands the whole task to pi's own orchestrator agent,
  and `read_only: true` forbids all file changes.
- If a delegation fails with an authentication or "No API key" error, tell the user to run `pi` once and
  use `/login`, or to export a provider key such as `ANTHROPIC_API_KEY`, then retry.
