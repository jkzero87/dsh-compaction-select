# dsh-compaction-select

A context-compaction plugin for the dsh agent harness (`@deepseek-ai/dsh`).
When a session gets close to the model's context window, compaction replaces
the oldest history with a written checkpoint so the conversation can go on.

This is a fork of `@deepseek-ai/dsh-compaction-basic` for a local Qwen3.8-27B (GSQ,
65k window). Each compaction produces one bounded checkpoint. Compared with
upstream:

- the summarizer's output budget is fitted to the context window and to the
  span being replaced
- summarization runs with thinking off
- a reply that isn't a checkpoint (a tool call, reasoning only, or prose) is
  retried once, then rejected
- earlier checkpoints merge into the next one instead of piling up
- every compaction records its budget and diagnostics in the session log
- the trigger can be sized by output reserve instead of a fixed ratio

It's loaded by the `standard-light` preset as `dsh-compaction-select`. After
any dsh upgrade, follow [UPGRADING.md](UPGRADING.md).

## Install

Clone to `~/dsh-compaction-select`, then run `scripts/link-deps.sh`: it links
the dsh packages the fork imports from your global dsh install and ends with
`IMPORT OK`. [UPGRADING.md](UPGRADING.md) walks through the full check,
including the preset entry and a live test.

## Trigger

Upstream compacts at `contextWindow × thresholdRatio`. Set
`outputReserveTokens` (and optionally `safetyMarginTokens`) instead and it
compacts at `contextWindow − outputReserveTokens − safetyMarginTokens`: only
when the next request plus its output would no longer fit. The two forms are
mutually exclusive in one scope; a `modelPolicies` entry may use either.

Size the reserve from real sessions with `tools/compaction_headroom.py`: it
prints, per compaction, the tokens in context before and after, % of the
window, summarizing time and summary size, plus the max and p99 output per
request and post-compaction file re-reads (`--rereads`).

## Verified

dsh 0.1.5-rc.2, session `2c8676c7` (standard-light, one long read turn, then
`/compact` twice), checked with `tools/check_session.py`:

| Criterion | Result |
|---|---|
| ≥1 automatic compaction | PASS: 18 |
| 0 errors in `compaction/end` | PASS: 19/19 clean |
| every checkpoint starts with `## Files and Code` | PASS: 19/19 |
| `/compact` #1 compacts | PASS: "Compacted 19 history items (~8259 tokens)." |
| `/compact` #2 is a no-op | PASS: "No compactable history yet." |

- **Flat floor:** after each automatic compaction, context sat at
  12.2k–14.6k tokens (one outlier at 16.3k), against an 18,022 threshold.
- **One merged checkpoint:** there was never more than one checkpoint on the
  surface. The previous build piled up 8 in one turn, which raised the floor
  and drove compactions every 15–45 s.
- **Retry caught a tool-call reply:** one summarizer reply was a tool call
  (the model acting as the agent). It was rejected and retried once, and the
  retry produced a good checkpoint.

## Files

| Path | What it is |
|---|---|
| `lib/index.js` | The plugin |
| `cordis.patch.yml` | Default config loaded with the bundle (`maxTokens`, `outputReserveTokens`, `safetyMarginTokens`, ...) |
| `scripts/link-deps.sh` | Links dsh's bundled packages into `node_modules`; re-run after every dsh upgrade |
| `tools/check_session.py` | Scans a session log and prints PASS/FAIL for the criteria in "Verified" |
| `tools/compaction_headroom.py` | Per-compaction headroom report, used to size `outputReserveTokens` |
| `UPGRADING.md` | Steps after a dsh upgrade, and the checklist for switching the 27B model |
