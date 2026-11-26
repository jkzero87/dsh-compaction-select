/**
 * Replay harness for recorded compaction failures: rebuilds a session from a
 * real zstd JSONL session log up to a chosen seq, mounts the real token meter,
 * and answers summarizer calls from a script, recording every request.
 *
 * Session logs are read in place (zstd -dc), never copied into the repo. The
 * default locations follow the desktop app's home; DSH_HOME and
 * DSH_COMPARE_FIXTURES override them.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Context } from "@deepseek-ai/cordis";
import LlmRuntime, { LlmAdapter } from "@deepseek-ai/dsh-llm";
import TokenMeter from "@deepseek-ai/dsh-token-meter";
import { Session, SessionId } from "@deepseek-ai/dsh-session";
import BasicCompactionEngine from "../lib/index.js";

const DSH_HOME = process.env.DSH_HOME ?? path.join(os.homedir(), ".local/share/deepseek-harness");
const COMPARE_FIXTURES = process.env.DSH_COMPARE_FIXTURES ?? path.join(os.homedir(), "dsh-compare/fixtures");

/** The desktop profile's compaction-select config (threshold 65536 - 2048 - 6144 = 57344). */
export const PROFILE_CONFIG = Object.freeze({
	maxTokens: 8192,
	outputReserveTokens: 2048,
	safetyMarginTokens: 6144,
	compactionRetries: 2
});
/** Pressure threshold of the 65536-token route under {@link PROFILE_CONFIG}. */
export const THRESHOLD_65K = 65536 - 2048 - 6144;
/** Context windows of the recorded routes. */
const WINDOWS = { "llamacpp-local": 65536, "llamacpp-vision": 49152 };
/** Framing that opens every landed checkpoint message. */
const PREAMBLE = "This is an automatically generated checkpoint condensing an earlier span of the conversation to free up context.";

/** The five recorded failures: session, failed compaction/end seq, and the two replies the summarizer gave. */
export const FAILURES = [
	{ name: "54548ded seq95", file: path.join(DSH_HOME, "sessions/--home-jkzero-deepseek-harness-apps-desktop-.desktop-build--/session-54548ded-4ff3-419c-ab63-86a12f0fe6d5/session.v4.jsonl.zstd"), endSeq: 95, replies: ["tool-call", "truncated"] },
	{ name: "54548ded seq102", file: path.join(DSH_HOME, "sessions/--home-jkzero-deepseek-harness-apps-desktop-.desktop-build--/session-54548ded-4ff3-419c-ab63-86a12f0fe6d5/session.v4.jsonl.zstd"), endSeq: 102, replies: ["unchanged", "tool-call"] },
	{ name: "abf97961 seq417", file: path.join(DSH_HOME, "sessions/--home-jkzero-dsh-scratch--/session-abf97961-f9b5-4805-aea2-9685fad4b35c/session.v4.jsonl.zstd"), endSeq: 417, replies: ["format", "tool-call"] },
	{ name: "907fd2cd seq1172", file: path.join(DSH_HOME, "sessions/--home-jkzero-dsh-scratch--/session-907fd2cd-c6bc-4533-a35e-5d3f2151f7b6/session.v4.jsonl.zstd"), endSeq: 1172, replies: ["format", "format"] },
	{ name: "1624f4db seq1668", file: path.join(COMPARE_FIXTURES, "compaction-fail-20261125/session-1624f4db-dd31-42a4-b7c8-1a2d19c12cfe.session.v4.jsonl.zstd"), endSeq: 1668, replies: ["tool-call", "tool-call"] }
];

const logCache = new Map();
/** Parse a zstd JSONL session log into its header line and events. */
export function readLog(file) {
	if (!logCache.has(file)) {
		const lines = execFileSync("zstd", ["-dc", file], { maxBuffer: 1 << 30 }).toString("utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line));
		logCache.set(file, { header: lines[0], events: lines.slice(1) });
	}
	return logCache.get(file);
}

/** Whether a fixture's log is present on this machine. */
export const hasLog = (fixture) => fs.existsSync(fixture.file);

/** The compaction/start event matching the compaction/end at `endSeq`. */
export function startOf(events, endSeq) {
	const end = events.find((event) => event.seq === endSeq);
	if (end?.type !== "compaction/end") throw new Error(`seq ${endSeq} is not a compaction/end`);
	const start = events.find((event) => event.type === "compaction/start" && event.data.compactionId === end.data.compactionId);
	return { start, end };
}

/** Text of a landed checkpoint's summary (between the tags), or undefined. */
export function checkpointSummary(message) {
	const text = (Array.isArray(message.content) ? message.content : []).filter((block) => block.type === "text").map((block) => block.text).join("\n");
	if (!text.startsWith(PREAMBLE)) return void 0;
	const open = text.indexOf("<compacted-summary>");
	const close = text.lastIndexOf("</compacted-summary>");
	return open === -1 ? void 0 : text.slice(open + "<compacted-summary>".length, close === -1 ? void 0 : close).trim();
}

/** Model adapter that answers each summarizer call from `script(options)` and records the requests. */
class ScriptedAdapter extends LlmAdapter {
	requests = [];
	constructor(script) {
		super();
		this.script = script;
	}
	resolveModel(provider, model) {
		return Promise.resolve({ provider, id: model, name: model, context: { contextWindow: WINDOWS[provider] ?? 65536 } });
	}
	async *stream(options) {
		this.requests.push(options);
		yield* this.script(options);
	}
}

/** Stream chunks for one scripted reply kind. */
export function* reply(kind, options) {
	switch (kind) {
		case "tool-call":
			yield { type: "block-start", index: 0, blockType: "tool-call" };
			yield { type: "block-end", index: 0, block: { type: "tool-call", id: "call-1", name: "bash", arguments: "{\"command\":\"ls\"}" } };
			yield { type: "finish", reason: { kind: "tool-calls" } };
			return;
		case "format":
			yield { type: "block-start", index: 0, blockType: "text" };
			yield { type: "block-end", index: 0, block: { type: "text", text: "Next I will read the remaining files and continue the task." } };
			yield { type: "finish", reason: { kind: "stop" } };
			return;
		case "truncated":
			yield { type: "block-start", index: 0, blockType: "text" };
			yield { type: "block-end", index: 0, block: { type: "text", text: "## Files and Code\n- lib/index.js: the summarizer" } };
			yield { type: "finish", reason: { kind: "max-tokens" } };
			return;
		case "unchanged": {
			const summaries = options.messages.map(checkpointSummary).filter((summary) => summary !== void 0);
			if (summaries.length === 0) throw new Error("scripted 'unchanged' reply needs a checkpoint in the request");
			yield { type: "block-start", index: 0, blockType: "text" };
			yield { type: "block-end", index: 0, block: { type: "text", text: summaries[summaries.length - 1] } };
			yield { type: "finish", reason: { kind: "stop" } };
			return;
		}
		default:
			yield { type: "block-start", index: 0, blockType: "text" };
			yield { type: "block-end", index: 0, block: { type: "text", text: kind } };
			yield { type: "finish", reason: { kind: "stop" } };
	}
}

/**
 * Rebuild the session just before `cutoffSeq` and mount the engine on it.
 * @param log - parsed session log.
 * @param cutoffSeq - first seq left out (the replayed compaction/start).
 * @param script - generator of stream chunks per summarizer request.
 * @param traceFile - absolute trace path for this run.
 */
export function replay(log, cutoffSeq, script, traceFile) {
	const ctx = new Context();
	void new LlmRuntime(ctx);
	void new TokenMeter(ctx);
	const adapter = new ScriptedAdapter(script);
	ctx.llm.registerAdapter(Object.keys(WINDOWS), adapter);
	// Manual compaction checkpoints durability through the session store; the replayed session has none.
	ctx.provide("sessions");
	ctx.set("sessions", { flush: async () => {} });
	const session = Session.create(SessionId(log.header.id), log.events.filter((event) => event.seq < cutoffSeq));
	const engine = new BasicCompactionEngine(ctx, { ...PROFILE_CONFIG, auto: false, traceFile });
	const agent = {
		session,
		options: {},
		runMaintenance: (task) => task(new AbortController().signal)
	};
	return { ctx, adapter, session, engine, agent };
}

/** Events appended after `fromSeq`. */
export const eventsAfter = (session, fromSeq) => session.snapshotEvents().filter((event) => event.seq >= fromSeq);
