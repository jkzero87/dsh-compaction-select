/**
 * The five recorded summarizer failures, replayed from their real session logs
 * with the summarizer answering the same kind of reply it gave that day:
 * the retry goes out without tools, a second failure commits a mechanical
 * checkpoint, the session ends below the 65k route's pressure threshold, and
 * an earlier checkpoint inside the span survives whole.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { FAILURES, THRESHOLD_65K, checkpointSummary, eventsAfter, hasLog, readLog, replay, reply, startOf } from "./fixtures.mjs";

const traceDir = fs.mkdtempSync(path.join(os.tmpdir(), "compaction-select-test-"));

/** Replay one fixture: first attempts (with tools) get replies[0], retries (tools: []) get replies[1]. */
async function runFailure(fixture) {
	const log = readLog(fixture.file);
	const { start, end } = startOf(log.events, fixture.endSeq);
	const traceFile = path.join(traceDir, `${fixture.endSeq}.log`);
	const run = replay(log, start.seq, function* (options) {
		yield* reply(options.tools?.length > 0 ? fixture.replies[0] : fixture.replies[1], options);
	}, traceFile);
	const signal = new AbortController().signal;
	const result = end.data.turn === null
		? await run.engine.compactNow(run.agent, signal, end.data.sourceCommandId)
		: await run.engine.compactIfNeeded(run.agent, "pressure", signal);
	const appended = eventsAfter(run.session, start.seq);
	return { ...run, start, end, result, appended, traceFile };
}

for (const fixture of FAILURES) {
	test(`${fixture.name}: retry without tools, mechanical checkpoint, below ${THRESHOLD_65K}, earlier checkpoint whole`, { skip: hasLog(fixture) ? false : `missing ${fixture.file}` }, async () => {
		const run = await runFailure(fixture);
		assert.ok(run.result !== null, "a compaction committed");
		const summaries = run.appended.filter((event) => event.type === "compaction/summary");
		const ends = run.appended.filter((event) => event.type === "compaction/end");
		assert.ok(summaries.length >= 1, "at least one checkpoint committed");
		assert.ok(ends.every((event) => event.data.error === void 0), `no failed compaction: ${JSON.stringify(ends.map((event) => event.data.error))}`);

		// (1) every first attempt carries the conversation's tools; every retry sends tools: [].
		const firsts = run.adapter.requests.filter((options) => options.tools?.length > 0);
		const retries = run.adapter.requests.filter((options) => Array.isArray(options.tools) && options.tools.length === 0);
		assert.equal(firsts.length, summaries.length, "one first attempt with tools per compaction");
		assert.equal(retries.length, summaries.length, "one retry without tools per compaction");
		assert.equal(firsts.length + retries.length, run.adapter.requests.length);
		for (const retry of retries) assert.match(retry.messages.at(-1).content[0].text, /Your previous reply/);
		assert.ok(ends.every((event) => event.data.retryWithoutTools === true), "compaction/end records retryWithoutTools");

		// (2) the retry failed too, so the committed checkpoint is the mechanical one.
		for (const event of ends) assert.equal(event.data.mechanical, true);
		for (const event of summaries) {
			assert.equal(event.data.llmStreamCall, void 0);
			assert.match(event.data.summary[0].text, /^mechanical checkpoint \(summarizer (rejected twice|failed)/);
		}

		// (3) the measured context ends below the 65k route's pressure threshold.
		assert.ok(run.ctx.tokenMeter.measure(run.session).totalTokens < THRESHOLD_65K, `measured ${run.ctx.tokenMeter.measure(run.session).totalTokens}`);

		// (4) an earlier checkpoint inside the first compacted span survives whole in the new checkpoint.
		const first = summaries[0];
		const shadowed = new Set(first.data.shadowedSeqs);
		const earlier = run.session.snapshotEvents().filter((event) => shadowed.has(event.seq) && event.type === "user/message").map((event) => checkpointSummary(event.data)).filter((summary) => summary !== void 0);
		if (earlier.length > 0) assert.ok(first.data.summary[0].text.includes(`Previous checkpoint:\n${earlier.at(-1)}`), "latest earlier summary kept whole");

		// The trace records the retry, the fallback, and the compaction.
		const trace = fs.readFileSync(run.traceFile, "utf8");
		assert.match(trace, / WARN \[compaction-select\] retry-without-tools session=/);
		assert.match(trace, / WARN \[compaction-select\] mechanical session=/);
		assert.match(trace, / INFO \[compaction-select\] compacted session=\S+ trigger=\S+ threshold=\S+ seq=\d+ tokens=\d+ before=\d+/);
	});
}

test("the earlier checkpoint is in the span of the fixtures that had one", { skip: FAILURES.every(hasLog) ? false : "missing fixture logs" }, async () => {
	const withEarlier = [];
	for (const fixture of FAILURES) {
		const run = await runFailure(fixture);
		const first = run.appended.find((event) => event.type === "compaction/summary");
		const shadowed = new Set(first.data.shadowedSeqs);
		const earlier = run.session.snapshotEvents().filter((event) => shadowed.has(event.seq) && event.type === "user/message").map((event) => checkpointSummary(event.data)).filter((summary) => summary !== void 0);
		if (earlier.length > 0) withEarlier.push(fixture.name);
	}
	// 54548ded seq102 rejected an "unchanged" reply, so its span held a checkpoint; today's span started at one.
	assert.ok(withEarlier.includes("54548ded seq102"), JSON.stringify(withEarlier));
	assert.ok(withEarlier.includes("1624f4db seq1668"), JSON.stringify(withEarlier));
});
