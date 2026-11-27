/**
 * A rejected summarizer reply is kept on disk for diagnosis: replay the
 * recorded format/format failure of 907fd2cd with the rejects directory
 * pointed at a scratch dir. Both rejected attempts land as files with a
 * three-line header (reason, failed format rule, character length), the
 * retry without tools still runs, and a mechanical checkpoint commits. A
 * rejects directory that cannot be created never fails the compaction.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { after, test } from "node:test";
import { FAILURES, hasLog, readLog, replay, reply, startOf } from "./fixtures.mjs";

const FIXTURE = FAILURES.find((fixture) => fixture.name.startsWith("907fd2cd"));
const REJECT_TEXT = "Next I will read the remaining files and continue the task.";
const scratch = fs.mkdtempSync(path.join(process.cwd(), "scratch", "compaction-rejects-test-"));
const rejectsDir = path.join(scratch, "rejects");
after(() => fs.rmSync(scratch, { recursive: true, force: true }));

/** Replay the fixture with the scripted "format" reply on every attempt. */
async function runFormatFailure(traceFile) {
	const log = readLog(FIXTURE.file);
	const { start, end } = startOf(log.events, FIXTURE.endSeq);
	const run = replay(log, start.seq, function* (options) {
		yield* reply("format", options);
	}, traceFile);
	const signal = new AbortController().signal;
	const result = end.data.turn === null
		? await run.engine.compactNow(run.agent, signal, end.data.sourceCommandId)
		: await run.engine.compactIfNeeded(run.agent, "pressure", signal);
	const appended = run.session.snapshotEvents().filter((event) => event.seq >= start.seq);
	return { ...run, start, end, result, appended, traceFile };
}

test("a format-rejected summary is saved and the compaction still retries and commits", { skip: hasLog(FIXTURE) ? false : `missing ${FIXTURE.file}` }, async () => {
	process.env.DSH_COMPACT_REJECTS_DIR = rejectsDir;
	const run = await runFormatFailure(path.join(scratch, "save.log"));
	assert.ok(run.result !== null, "a compaction committed");
	const end = run.appended.find((event) => event.type === "compaction/end");
	assert.equal(end.data.error, void 0, "no failed compaction");
	assert.equal(end.data.mechanical, true, "mechanical fallback committed");
	assert.equal(end.data.retryWithoutTools, true, "the no-tools retry ran");
	const retries = run.adapter.requests.filter((options) => Array.isArray(options.tools) && options.tools.length === 0);
	assert.equal(retries.length, 1, "one retry without tools");

	// Both rejected attempts are on disk, named by session, span start seq, and attempt.
	const spanStart = run.appended.find((event) => event.type === "compaction/summary").data.shadowedRange.start;
	const session = String(run.session.id).replace(/[^A-Za-z0-9._-]/g, "-");
	const files = fs.readdirSync(rejectsDir).sort();
	assert.deepEqual(files, [`${session}-${spanStart}-1.txt`, `${session}-${spanStart}-2.txt`].sort());
	for (const attempt of [1, 2]) {
		const content = fs.readFileSync(path.join(rejectsDir, `${session}-${spanStart}-${attempt}.txt`), "utf8");
		const lines = content.split("\n");
		assert.equal(lines[0], `reason: format (the reply does not start with the required heading "## Files and Code")`);
		assert.equal(lines[1], `rule: the reply must start with the heading "## Files and Code"`);
		assert.equal(lines[2], `length: ${REJECT_TEXT.length} characters`);
		assert.equal(lines.slice(3).join("\n"), `${REJECT_TEXT}\n`, "the rejected text follows the header");
	}

	// The retry-without-tools trace line names both saved copies.
	const file1 = path.join(rejectsDir, `${session}-${spanStart}-1.txt`);
	const file2 = path.join(rejectsDir, `${session}-${spanStart}-2.txt`);
	const trace = fs.readFileSync(run.traceFile, "utf8");
	assert.match(trace, /retry-without-tools .*rejectedFiles=/);
	assert.ok(trace.includes(`rejectedFiles=${file1},${file2}`), "the retry line names both saved copies");
});

test("an unwritable rejects directory never fails the compaction", { skip: hasLog(FIXTURE) ? false : `missing ${FIXTURE.file}` }, async () => {
	const blocker = path.join(scratch, "blocker");
	fs.writeFileSync(blocker, "not a directory");
	process.env.DSH_COMPACT_REJECTS_DIR = path.join(blocker, "rejects");
	const run = await runFormatFailure(path.join(scratch, "unwritable.log"));
	assert.ok(run.result !== null, "a compaction committed");
	const end = run.appended.find((event) => event.type === "compaction/end");
	assert.equal(end.data.error, void 0, "no failed compaction");
	assert.equal(fs.existsSync(path.join(blocker, "rejects")), false, "no rejects directory created");
	const trace = fs.readFileSync(run.traceFile, "utf8");
	assert.match(trace, /retry-without-tools .*rejections=format,format\n/, "no rejectedFiles field when nothing was saved");
});
