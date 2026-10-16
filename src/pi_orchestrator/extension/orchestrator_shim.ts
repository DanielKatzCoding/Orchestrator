/**
 * pi-orchestrator shim.
 *
 * Loaded into the orchestrator's pi process by the Python host
 * (`pi --mode rpc -e orchestrator_shim.ts`). It registers the delegation tools
 * and forwards every call to the host over pi's RPC extension-UI channel:
 * `ctx.ui.input(CHANNEL, <json request>)` becomes an `extension_ui_request` on
 * stdout, and the host answers with an `extension_ui_response` whose `value` is
 * a JSON reply `{ ok, text, details?, error? }`.
 *
 * All orchestration logic (spawning workers, concurrency, limits) lives in Python.
 * The shim is inert unless PI_ORCHESTRATOR_HOST=1, so loading it anywhere else is harmless.
 */
import type { ExtensionAPI, ExtensionContext } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";

const CHANNEL = "pi-orchestrator:rpc";

interface HostReply {
	ok: boolean;
	text?: string;
	details?: Record<string, unknown>;
	error?: string;
}

async function callHost(ctx: ExtensionContext, payload: unknown, signal?: AbortSignal): Promise<HostReply> {
	const raw = await ctx.ui.input(CHANNEL, JSON.stringify(payload), { signal });
	if (raw === undefined) {
		throw new Error(signal?.aborted ? "Delegation aborted" : "The orchestrator host did not answer the request");
	}
	let reply: HostReply;
	try {
		reply = JSON.parse(raw) as HostReply;
	} catch {
		throw new Error("Malformed reply from the orchestrator host");
	}
	if (!reply.ok) {
		throw new Error(reply.error ?? "Delegation failed");
	}
	return reply;
}

const agentField = Type.String({ description: "Name of the agent to run (see the agents table in your instructions)" });
const taskField = Type.String({
	description: "Complete, self-contained instructions. The agent has not seen this conversation.",
});
const taskSchema = Type.Object({ agent: agentField, task: taskField });

export default function (pi: ExtensionAPI) {
	if (process.env.PI_ORCHESTRATOR_HOST !== "1") return;

	const delegate = (op: string) => async (_id: string, params: unknown, signal: AbortSignal | undefined, _onUpdate: unknown, ctx: ExtensionContext) => {
		const reply = await callHost(ctx, { op, params }, signal);
		return { content: [{ type: "text" as const, text: reply.text ?? "" }], details: reply.details ?? {} };
	};

	pi.registerTool({
		name: "delegate_task",
		label: "Delegate task",
		description: "Run one agent on one task in an isolated context and return its final report.",
		promptSnippet: "delegate_task(agent, task): run one specialist agent and get its report",
		parameters: taskSchema,
		execute: delegate("delegate_task"),
	});

	pi.registerTool({
		name: "delegate_parallel",
		label: "Delegate in parallel",
		description:
			"Run several independent agent tasks concurrently and return all reports. At most one task may use a write-capable agent.",
		promptSnippet: "delegate_parallel(tasks[]): run independent agent tasks concurrently",
		parameters: Type.Object({ tasks: Type.Array(taskSchema, { minItems: 1 }) }),
		execute: delegate("delegate_parallel"),
	});

	pi.registerTool({
		name: "delegate_chain",
		label: "Delegate chain",
		description:
			"Run agent steps sequentially. Use {previous} in a step's task to insert the previous step's output. Stops at the first failure.",
		promptSnippet: "delegate_chain(steps[]): run agent steps in order, piping {previous} output forward",
		parameters: Type.Object({ steps: Type.Array(taskSchema, { minItems: 1 }) }),
		execute: delegate("delegate_chain"),
	});

	pi.registerCommand("pi-orchestrator-ping", {
		description: "Check the channel between this pi process and the pi-orchestrator host",
		handler: async (_args, ctx) => {
			const reply = await callHost(ctx, { op: "ping" });
			ctx.ui.notify(`pi-orchestrator:pong ${reply.text ?? ""}`, "info");
		},
	});
}
