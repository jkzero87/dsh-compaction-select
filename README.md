# dsh-compaction-select

A fork of `@deepseek-ai/dsh-compaction-basic` for a local Qwen3.8-27B (GSQ,
65k window). Each compaction produces one bounded checkpoint. Compared with
upstream:

- the summarizer's output budget is fitted to the context window and to the
  span being replaced
- summarization runs with thinking off
- a reply that isn't a checkpoint (a tool call, reasoning only, or prose) is
  retried once, without tool schemas, so the model gets no tool-call format
  to answer with; the first attempt keeps them to reuse the cached prefix
- when the retry also fails, or the checkpoint is cut off at the token cap,
  a mechanical checkpoint replaces the span: the latest earlier checkpoint's
  summary whole, the latest 50 user messages (harness notices skipped), the
  files and commands from tool calls, and the last assistant text;
  `compaction/end` records `mechanical: true` and the reason
- earlier checkpoints merge into the next one instead of piling up
- every compaction records its budget and diagnostics in the session log
- the trigger can be sized by output reserve instead of a fixed ratio

It's loaded by the `standard-light` preset as `dsh-compaction-select`. After
any dsh upgrade, follow [UPGRADING.md](UPGRADING.md).

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

## Trace

Each compaction, failure, retry without tools, mechanical checkpoint, and skip
appends one line to `traceFile` (default `logs/compaction-select.log` under the
DeepSeek Harness home), in the `auto-continue.log` format. The replayed
failure of session `1624f4db` writes:

```
WARN [compaction-select] retry-without-tools session=session-1624f4db-… trigger=pressure threshold=57344 seq=1671 tokens=28619 before=59164 shadowed=1516-1607 rejections=tool-call,tool-call
WARN [compaction-select] mechanical session=session-1624f4db-… trigger=pressure threshold=57344 seq=1671 tokens=28619 before=59164 shadowed=1516-1607 userMessages=0 reason="summarizer rejected twice (tool-call, then tool-call)"
INFO [compaction-select] compacted session=session-1624f4db-… trigger=pressure threshold=57344 seq=1671 tokens=28619 before=59164 shadowed=1516-1607
```

Each line starts with its ISO time. `tokens` is the measured context after
the event and `before` the measurement before it.

Skips are traced when no span fits the window, nothing is left to compact, or
the session stays above the threshold after every retry. The returns below the
threshold are not traced.

## Tests

`npm test` replays five recorded summarizer failures and one recorded success
from real session logs, read in place from the desktop app's home
(`DSH_HOME`) and `~/dsh-compare/fixtures` (`DSH_COMPARE_FIXTURES`); a test
whose log is missing is skipped. They need `node_modules` from
`scripts/link-deps.sh` plus `zod`, which `dsh-token-meter` imports.

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
