/**
 * Test-only pi extension: registers a deterministic "scripted" model provider so the
 * real `pi` binary (and the real orchestrator shim) can be exercised without API keys.
 *
 * Rules come from the JSON file named by SCRIPTED_PI_RULES:
 *   [{ "system_contains": "...", "turn": 0, "reply": { "text": "..." } },
 *    { "system_contains": "...", "turn": 0, "reply": { "tool": "delegate_task", "args": {...} } },
 *    { "system_contains": "...", "turn": 1, "reply": { "text": "Done: {last_tool_result}" } }]
 * `turn` is the number of assistant messages already in the context. The first matching
 * rule wins; `{last_tool_result}` expands to the text of the most recent tool result.
 */
import { readFileSync } from "node:fs";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { fauxAssistantMessage, fauxText, fauxToolCall, registerFauxProvider } from "@mariozechner/pi-ai";

interface Rule {
	system_contains?: string;
	turn?: number;
	reply: { text?: string; tool?: string; args?: Record<string, unknown> };
}

function textOf(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.filter((block) => block && block.type === "text")
		.map((block) => block.text)
		.join("\n");
}

export default function (pi: ExtensionAPI) {
	const rulesPath = process.env.SCRIPTED_PI_RULES;
	const rules: Rule[] = rulesPath ? JSON.parse(readFileSync(rulesPath, "utf8")) : [];

	const registration = registerFauxProvider({
		provider: "scripted",
		api: "scripted-api",
		models: [{ id: "scripted-1", name: "Scripted test model" }],
	});

	const respond = (context: { systemPrompt?: string; messages: Array<{ role: string; content: unknown }> }) => {
		const system = context.systemPrompt ?? "";
		const turn = context.messages.filter((m) => m.role === "assistant").length;
		const lastToolResult = [...context.messages].reverse().find((m) => m.role === "toolResult");
		const rule = rules.find(
			(r) => (r.system_contains === undefined || system.includes(r.system_contains)) && (r.turn === undefined || r.turn === turn),
		);
		if (!rule) return fauxAssistantMessage(`[scripted] no rule for turn ${turn}`);
		if (rule.reply.tool) {
			return fauxAssistantMessage([fauxToolCall(rule.reply.tool, rule.reply.args ?? {})], { stopReason: "toolUse" });
		}
		const text = (rule.reply.text ?? "").replace("{last_tool_result}", lastToolResult ? textOf(lastToolResult.content) : "");
		return fauxAssistantMessage([fauxText(text)]);
	};
	registration.setResponses(Array.from({ length: 500 }, () => respond));

	pi.registerProvider("scripted", {
		baseUrl: "http://scripted.invalid",
		apiKey: "scripted",
		api: "scripted-api",
		models: [
			{
				id: "scripted-1",
				name: "Scripted test model",
				reasoning: false,
				input: ["text"],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: 128000,
				maxTokens: 16384,
			},
		],
	});
}
