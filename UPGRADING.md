# Upgrading dsh

This fork was verified against **dsh 0.1.5-rc.2**. It imports dsh packages
directly, so a dsh upgrade can break it without warning. Run these steps
after every `npm install -g @deepseek-ai/dsh`, before relying on compaction.

## 1. Relink dependencies

```sh
cd ~/dsh-compaction-select
scripts/link-deps.sh
```

Every import must print `linked`. A `MISSING` line means the new dsh no
longer bundles a package the fork imports, and a `DANGLING` line means an old
link points at a removed path. Fix those before going on.

## 2. IMPORT OK

`link-deps.sh` ends by loading the plugin the same way dsh does, from
`~/.dsh/profiles/web`. It must print:

```
IMPORT OK
```

`FAIL <message>` usually means an export the fork uses was renamed or
removed. Check it against the new upstream `dsh-compaction-basic`.

## 3. Check the preset still names the fork

`~/.dsh/.agent-presets/standard-light/agent.cordis.yml`, in the
`compaction` group, must load the fork, not the stock plugin:

```yaml
    - id: compaction-select
      name: 'dsh-compaction-select'
      config:
        summarizationProvider: llamacpp-local
        summarizationModel: <model id from settings.yaml>   # e.g. .../Qwen3.8-27B-UD-IQ4_XS.gguf
        maxTokens: 8192
        thresholdRatio: 0.55
        compactionRetries: 2
```

An upgrade that rewrites presets can quietly bring back
`@deepseek-ai/dsh-compaction-basic`.

Also check `~/.dsh/settings.yaml`: the 27B model's `reasoningEfforts` must
still include `off: none`. The summarizer uses it to turn thinking off, and
without it summarization falls back to the provider's default effort.

### Agent output budget: `maxTokens = contextWindow - 4096`

Every model entry in `~/.dsh/settings.yaml` sets `maxTokens` to
`contextWindow - 4096` (27B UD-IQ4_XS: 32768 -> 28672; GSQ: 65536 -> 61440).
That value is a ceiling, not the budget. pi-ai's `clampMaxTokensToContext`
(`@earendil-works/pi-ai/dist/api/simple-options.js`) sends
`min(maxTokens, contextWindow - estimateContextTokens(ctx) - 4096)` as
`max_tokens` on every request. So output uses whatever context is left and
prompt + output never exceeds the window.

This retires the old manual invariant `threshold + maxTokens <= contextWindow`
(the 09-19 value 11264 = 32768 - 0.65 * 32768). Nothing enforced it, and it
went stale when `thresholdRatio` moved to 0.55. The fork's `maxTokens` above
is the summarizer's budget and is unrelated.

The session's `request/header` records the configured ceiling (28672), not
the clamped value sent on the wire. After an upgrade, check that the clamp
still exists and that `openai-completions` still goes through
`buildBaseOptions`:

```sh
PA=$(readlink -f ~/.dsh/profiles/node_modules/@earendil-works/pi-ai)/dist
grep -n 'CONTEXT_SAFETY_TOKENS =\|clampMaxTokensToContext(model' $PA/api/simple-options.js
grep -n 'buildBaseOptions(model' $PA/api/openai-completions.js
```

If either line is gone, the ceiling becomes the literal `max_tokens` and a
large prompt overflows the window. dsh reports that overflow as "Output
token limit reached". In that case, go back to
`maxTokens <= contextWindow - floor(thresholdRatio * contextWindow)`.

## 4. Restart dsh by PID

The model servers (llama-server on 8092, imgproxy on 8091) are systemd user
units and don't need restarting. Only dsh does:

```sh
PID=$(ss -ltnp | grep ':3080\b' | grep -oP 'pid=\K[0-9]+')
kill "$PID"
nohup dsh --profile web > /tmp/dsh_web_debug.log 2>&1 &
curl -s localhost:8092/health   # {"status":"ok"}
```

`/tmp/dsh_web_debug.log` should contain only the startup banner. Plugin log
messages don't reach it; the evidence is in the session log (step 5).

## 5. One live test

1. In the web UI, start a new **standard-light** session and give it a long
   read task, enough to trigger several automatic compactions.
2. Wait for the turn to end, then run `/compact` twice.
3. Scan the session:

   ```sh
   tools/check_session.py            # newest session
   tools/check_session.py 2c8676c7   # or by id prefix / path / UI .zip
   ```

All five criteria must PASS (exit status 0):

| Criterion | Expected |
|---|---|
| ≥1 automatic compaction | at least one committed `auto` row |
| 0 errors in `compaction/end` | every compaction clean |
| every checkpoint starts with `## Files and Code` | `format=ok` on every row |
| `/compact` #1 | `Compacted N history items (~T tokens).` |
| `/compact` #2 | `No compactable history yet.` |

Also look at the floor table: at most one checkpoint on the surface and a
flat total (12–15k on the 32k window). A growing checkpoint count means
checkpoint merging stopped working.

`/compact` results are written to the session log, but a no-op doesn't flush
to disk right away. If `/compact #2` shows `NOT RUN`, click **Session log** in
the UI (it flushes first) and scan again.

## Upstream internals the fork relies on

If a step above fails, these are the first things to compare against the new
upstream:

- `@deepseek-ai/dsh-compaction`: `CompactionEngine`, `ManualCompactionError`,
  `compactCheckpointSource`, `isCompactCheckpointSource`,
  `toolPairingBalancedBefore/After`
- `@deepseek-ai/dsh-llm`: `BlockAssembler` block types (`text`, `reasoning`,
  `tool-call`), `resolveModelInfo()` returning `context.contextWindow` and
  `reasoning.efforts`
- `dsh-token-meter` measurement fields: `totalTokens`, `surfaceTokens`,
  `nodes[].tokens`, `heuristicTokens`
- `dsh-command-compact` reporting a `null` result as "No compactable history yet."
