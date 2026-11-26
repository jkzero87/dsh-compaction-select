/**
 * A compaction that succeeded in a real session (1624f4db, compaction/start
 * seq 247) replayed with the summarizer giving the same checkpoint: it still
 * commits the same checkpoint over the same span in one call, and that call
 * carries the conversation's tools so the cached prefix is reused.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { FAILURES, eventsAfter, hasLog, readLog, replay, reply } from "./fixtures.mjs";

const today = FAILURES.find((fixture) => fixture.name.startsWith("1624f4db"));
const START_SEQ = 247;
const traceDir = fs.mkdtempSync(path.join(os.tmpdir(), "compaction-select-test-"));

async function replaySuccess() {
	const log = readLog(today.file);
	const recordedSummary = log.events.find((event) => event.type === "compaction/summary" && event.seq > START_SEQ);
	const recordedText = recordedSummary.data.rawOutput.filter((block) => block.type === "text").map((block) => block.text).join("");
	const traceFile = path.join(traceDir, "success.log");
	const run = replay(log, START_SEQ, function* (options) {
		yield* reply(recordedText, options);
	}, traceFile);
	const result = await run.engine.compactIfNeeded(run.agent, "pressure", new AbortController().signal);
	return { ...run, log, recordedSummary, result, appended: eventsAfter(run.session, START_SEQ), traceFile };
}

test("a compaction that succeeded still commits the same checkpoint over the same span", { skip: hasLog(today) ? false : `missing ${today.file}` }, async () => {
	const run = await replaySuccess();
	assert.ok(run.result !== null);
	assert.equal(run.adapter.requests.length, 1, "one summarizer call");
	const [summary] = run.appended.filter((event) => event.type === "compaction/summary");
	const [end] = run.appended.filter((event) => event.type === "compaction/end");
	assert.deepEqual(summary.data.summary, run.recordedSummary.data.summary);
	assert.deepEqual(summary.data.shadowedRange, run.recordedSummary.data.shadowedRange);
	assert.deepEqual(summary.data.shadowedSeqs, run.recordedSummary.data.shadowedSeqs);
	assert.equal(summary.data.llmStreamCall, true);
	assert.equal(end.data.error, void 0);
	assert.equal(end.data.mechanical, void 0);
	assert.equal(end.data.retryWithoutTools, void 0);
	const trace = fs.readFileSync(run.traceFile, "utf8");
	assert.match(trace, / INFO \[compaction-select\] compacted session=session-1624f4db/);
	assert.doesNotMatch(trace, /retry-without-tools|mechanical/);
});

test("the first summarizer attempt carries the conversation's tools", { skip: hasLog(today) ? false : `missing ${today.file}` }, async () => {
	const run = await replaySuccess();
	const header = run.session.requestHeader();
	const [first] = run.adapter.requests;
	assert.ok(header.tools.length > 0);
	assert.deepEqual(first.tools.map((tool) => tool.name), header.tools.map((tool) => tool.name));
	assert.equal(first.purpose, "compaction");
});
