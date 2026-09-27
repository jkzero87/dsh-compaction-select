#!/usr/bin/env python3
"""Scan a dsh session log and check dsh-compaction-select against the live-test criteria.

Reads every zstd frame of session.v3.jsonl.zstd (or a plain .jsonl, or a
"Session log" .zip downloaded from the dsh UI), then prints:

  - every compaction (auto/manual) with its diagnostics and checkpoint format
  - every /compact command and its result text
  - the post-compaction floor: system+tools, checkpoints, retained tail, total
  - PASS/FAIL for the five criteria

Usage:
  tools/check_session.py                      # newest session under ~/.dsh/sessions
  tools/check_session.py SESSION_ID_PREFIX    # e.g. 2c8676c7
  tools/check_session.py PATH                 # session dir, .zstd, .jsonl or .zip

Exit status: 0 = all criteria pass, 1 = a criterion failed,
2 = incomplete (for example /compact was not run twice after the turn).

The pressure threshold is computed the way lib/index.js resolveCompactSpec
does: contextWindow comes from the session's last request/context event, and
the policy (thresholdRatio or outputReserveTokens + safetyMarginTokens, plus a
matching modelPolicies entry) from the dsh-compaction-select row of the
session's agent preset as it is on disk NOW. A session recorded under an
older preset needs the old values passed as flags (--threshold-ratio, ...).

Token prices mirror the dsh-token-meter fixed heuristic (estimate.js,
dsh 0.1.5-rc.2): ceil(chars / 4) plus 4 per block and 4 per message, with
lengths in UTF-16 code units like JavaScript. They are the meter's
heuristic prices, not provider-reported usage.
"""
import argparse
import datetime
import io
import json
import math
import pathlib
import shutil
import subprocess
import sys
import zipfile

SESSIONS = pathlib.Path.home() / ".dsh" / "sessions"
PRESETS = pathlib.Path.home() / ".dsh" / ".agent-presets"
PLUGIN = "dsh-compaction-select"
# Mirrors DEFAULT_THRESHOLD_RATIO / DEFAULT_RETAIN_RATIO in lib/index.js.
DEFAULT_THRESHOLD_RATIO = 0.8
DEFAULT_RETAIN_RATIO = 0.16
HEADING = "## Files and Code"
NOOP_TEXT = "No compactable history yet."
CHARS_PER_TOKEN = 4
BLOCK_OVERHEAD = 4
ROLE_OVERHEAD = 4


# ---------------------------------------------------------------- loading

def find_session(arg):
    """Resolve an argument (or nothing) to a session log path."""
    if arg is None:
        logs = sorted(SESSIONS.glob("*/session-*/session.v3.jsonl.zstd"), key=lambda p: p.stat().st_mtime)
        if not logs:
            sys.exit(f"no session logs under {SESSIONS}")
        return logs[-1]
    path = pathlib.Path(arg).expanduser()
    if path.is_dir():
        return path / "session.v3.jsonl.zstd"
    if path.exists():
        return path
    matches = sorted(SESSIONS.glob(f"*/session-{arg}*/session.v3.jsonl.zstd"))
    if len(matches) != 1:
        sys.exit(f"{arg!r}: expected one matching session, found {len(matches)}")
    return matches[0]


def decompress_zstd(raw):
    """Decompress every frame of a multi-frame zstd stream."""
    try:
        import zstandard
        reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw), read_across_frames=True)
        return reader.read()
    except ImportError:
        if shutil.which("zstd") is None:
            sys.exit("need the python 'zstandard' module or the 'zstd' CLI")
        return subprocess.run(["zstd", "-dc"], input=raw, capture_output=True, check=True).stdout


def load_events(path):
    """Return the header line and the event list of a session log."""
    raw = path.read_bytes()
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.endswith(".jsonl")]
            if not names:
                sys.exit(f"{path}: no .jsonl inside")
            text = archive.read(names[0])
    elif raw[:4] == b"\x28\xb5\x2f\xfd":
        text = decompress_zstd(raw)
    else:
        text = raw
    lines = [json.loads(line) for line in text.decode("utf-8").splitlines() if line.strip()]
    return lines[0], lines[1:]


# ---------------------------------------------------------------- pricing

def js_len(text):
    """String length in UTF-16 code units, as JavaScript's .length counts."""
    return len(text.encode("utf-16-le")) // 2


def js_json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def estimate_content(blocks):
    tokens = 0
    for block in blocks:
        kind = block.get("type")
        if kind in ("text", "reasoning"):
            tokens += math.ceil(js_len(block["text"]) / CHARS_PER_TOKEN) + BLOCK_OVERHEAD
        elif kind == "tool-call":
            tokens += (math.ceil(js_len(block["name"]) / CHARS_PER_TOKEN)
                       + math.ceil(js_len(block["arguments"]) / CHARS_PER_TOKEN) + BLOCK_OVERHEAD)
        elif kind == "tool-result":
            tokens += estimate_content(block.get("content", [])) + BLOCK_OVERHEAD
        else:
            tokens += BLOCK_OVERHEAD + math.ceil(js_len(js_json(block)) / CHARS_PER_TOKEN)
    return tokens


def estimate_message(message):
    content = message.get("content", [])
    if message.get("role") == "system":
        if not content:
            return 0
        chars = sum(js_len(b["text"]) if b.get("type") == "text" else js_len(js_json(b)) for b in content)
        return math.ceil(chars / CHARS_PER_TOKEN) + ROLE_OVERHEAD
    return estimate_content(content) + ROLE_OVERHEAD


def estimate_tools(header):
    tools = (header or {}).get("tools")
    if not tools:
        return 0
    return math.ceil(js_len(js_json(tools)) / CHARS_PER_TOKEN) + BLOCK_OVERHEAD


def message_of(event):
    data = event["data"]
    if event["type"] == "system/message":
        return data.get("message") or {"role": "system", "content": data.get("content", [])}
    if event["type"] == "user/message":
        return {"role": "user", "content": data.get("content") or (data.get("message") or {}).get("content", [])}
    return data.get("message") or {"role": "user", "content": []}


def is_checkpoint(event):
    source = event["data"].get("source") or {}
    return event["type"] == "user/message" and source.get("kind") == "plugin" and source.get("plugin") == "compact"


# ---------------------------------------------------------------- analysis

def clock(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


def summary_text(summary_data):
    return "".join(b.get("text", "") for b in summary_data.get("summary", [])).strip()


def scan(header, events, threshold):
    by_seq = {e["seq"]: e for e in events if "seq" in e}
    compactions = {}
    order = []
    commands = {}
    command_order = []
    turn_end_seq = None
    for e in events:
        kind, data = e["type"], e.get("data", {})
        if kind == "compaction/start":
            cid = data["compactionId"]
            compactions[cid] = {"start": e, "manual": data.get("turn") is None, "summary": None, "end": None}
            order.append(cid)
        elif kind == "compaction/summary":
            compactions[data["compactionId"]]["summary"] = e
        elif kind == "compaction/end":
            compactions[data["compactionId"]]["end"] = e
        elif kind == "command/run" and data.get("name") == "compact":
            commands[data["commandId"]] = {"run": e, "done": None}
            command_order.append(data["commandId"])
        elif kind == "command/done" and data.get("commandId") in commands:
            commands[data["commandId"]]["done"] = e
        elif kind == "turn/end":
            turn_end_seq = e["seq"]

    # Replay the surface and price it right after each checkpoint lands.
    price = {}
    surface = []
    request_header = None
    floor = []
    for e in events:
        if e["type"] == "request/header":
            request_header = e["data"].get("header")
        op = e.get("surfaceOp")
        if op is None:
            continue
        if op == "append":
            surface.append(e["seq"])
        else:
            i, j = surface.index(op["startSeq"]), surface.index(op["endSeq"])
            surface[i:j + 1] = [e["seq"]]
        if op != "append" and is_checkpoint(e):
            def cost(seq):
                if seq not in price:
                    price[seq] = estimate_message(message_of(by_seq[seq]))
                return price[seq]
            system = sum(cost(s) for s in surface if by_seq[s]["type"] == "system/message")
            checkpoints = [s for s in surface if is_checkpoint(by_seq[s])]
            tail = [s for s in surface if by_seq[s]["type"] != "system/message" and not is_checkpoint(by_seq[s])]
            fixed = system + estimate_tools(request_header)
            cp_tokens = sum(cost(s) for s in checkpoints)
            tail_tokens = sum(cost(s) for s in tail)
            floor.append({"seq": e["seq"], "fixed": fixed, "checkpoints": cp_tokens, "count": len(checkpoints),
                          "tail": tail_tokens, "tail_nodes": len(tail), "total": fixed + cp_tokens + tail_tokens})

    return {"compactions": [compactions[c] for c in order], "commands": [commands[c] for c in command_order],
            "floor": floor, "turn_end_seq": turn_end_seq, "threshold": threshold}


# ---------------------------------------------------------------- trigger

def routed_context(events):
    """(provider, model, contextWindow) from the last request/context event."""
    for e in reversed(events):
        if e["type"] == "request/context":
            d = e["data"]
            return d.get("provider"), d.get("model"), d.get("contextWindow")
    return None, None, None


def find_plugin_config(rows):
    """The config of the first dsh-compaction-select row, searching groups."""
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("name") == PLUGIN and not row.get("disabled"):
            return row.get("config") or {}
        if isinstance(row.get("config"), list):
            found = find_plugin_config(row["config"])
            if found is not None:
                return found
    return None


def preset_policy(preset):
    """Plugin config from ~/.dsh/.agent-presets/<preset>/agent.cordis.yml, or (None, reason)."""
    path = PRESETS / str(preset) / "agent.cordis.yml"
    if not path.exists():
        return None, f"{path} not found"
    try:
        import yaml
    except ImportError:
        return None, "PyYAML not installed"

    class Loader(yaml.SafeLoader):
        pass
    # Cordis rows use `!!js <expr>`; keep the expression text, it is never evaluated here.
    Loader.add_constructor("tag:yaml.org,2002:js", lambda loader, node: loader.construct_scalar(node))
    config = find_plugin_config(yaml.load(path.read_text(), Loader=Loader))
    if config is None:
        return None, f"no enabled {PLUGIN} row in {path}"
    return config, str(path)


def resolve_threshold(config, provider, model, window):
    """thresholdTokens and retainTokens as resolveConfig + resolveTargetPolicy + resolveCompactSpec compute them."""
    override = next((p for p in config.get("modelPolicies") or []
                     if p.get("provider") == provider and p.get("model") == model), {})
    if override.get("thresholdRatio") is not None:
        reserve = None
    else:
        source = override if override.get("outputReserveTokens") is not None else config
        reserve = source.get("outputReserveTokens")
        margin = source.get("safetyMarginTokens") or 0
    if reserve is not None:
        threshold = window - reserve - margin
        formula = f"{window:,} - outputReserveTokens {reserve:,} - safetyMarginTokens {margin:,}"
    else:
        ratio = override.get("thresholdRatio", config.get("thresholdRatio", DEFAULT_THRESHOLD_RATIO))
        threshold = math.floor(window * ratio)
        formula = f"floor({window:,} x thresholdRatio {ratio})"
    if override.get("retainTokens") is not None or override.get("retainRatio") is not None:
        retain_src = override
    else:
        retain_src = config
    if retain_src.get("retainTokens") is not None:
        retain = retain_src["retainTokens"]
    else:
        retain = math.floor(window * retain_src.get("retainRatio", DEFAULT_RETAIN_RATIO))
    return threshold, retain, formula


# ---------------------------------------------------------------- report

def report(path, header, events, result):
    comps, cmds, floor, threshold = result["compactions"], result["commands"], result["floor"], result["threshold"]
    print(f"session  {header.get('id')}  preset={header.get('agentPreset')}  "
          f"created {clock(header['createdAt']) if 'createdAt' in header else '?'}")
    print(f"source   {path}  ({len(events)} events)")
    print(f"trigger  {result['trigger']}\n")

    print("compactions")
    errors = []
    bad_format = []
    for n, c in enumerate(comps, 1):
        start, summary, end = c["start"], c["summary"], c["end"]
        cid = start["data"]["compactionId"][:8]
        who = "MANUAL" if c["manual"] else f"auto turn {start['data'].get('turn')}"
        print(f"  #{n:<2} {clock(start['time'])} {who:<11} {cid}", end="")
        if summary is not None:
            d = summary["data"]
            text = summary_text(d)
            ok = text.startswith(HEADING)
            if not ok:
                bad_format.append(cid)
            diag = d.get("diagnostics") or {}
            budget = diag.get("budget") or {}
            out = (d.get("usage") or {}).get("outputTokens")
            print(f"  {len(d['shadowedSeqs'])} nodes ~{d['shadowedTokenCount']} tok -> {text.count(chr(10)) + 1} lines"
                  f" out={out} format={'ok' if ok else 'BAD'}")
            print(f"       budget={budget.get('maxTokens', d.get('maxTokens'))} [{budget.get('limitedBy', budget.get('source', '-'))}]"
                  f" input~{budget.get('inputTokens', '-')} span~{budget.get('spanTokens', '-')}"
                  f" effort={diag.get('reasoningEffort', '-')} attempts={diag.get('attempts', '-')}"
                  f" rejections={diag.get('rejections', '-')}")
        else:
            print()
        if end is None:
            print("       END MISSING (unmatched compaction/start)")
            errors.append(cid)
        elif "error" in end["data"]:
            errors.append(cid)
            print(f"       ERROR {end['data']['error']!r}")
            if end["data"].get("diagnostics"):
                print(f"       diagnostics {json.dumps(end['data']['diagnostics'])}")
    if not comps:
        print("  (none)")

    print("\n/compact commands")
    for n, c in enumerate(cmds, 1):
        done = c["done"]
        text = done["data"].get("text") if done else "(no command/done yet: not flushed, or still running)"
        kind = done["data"].get("kind") if done else "?"
        print(f"  #{n} {clock(c['run']['time'])} seq {c['run']['seq']}  {kind}: {text}")
    if not cmds:
        print("  (none)")

    print(f"\npost-compaction floor (meter heuristic; threshold {threshold:,})")
    print("   #  seq   sys+tools  checkpoints(n)   tail(nodes)    total  headroom")
    for n, f in enumerate(floor, 1):
        print(f"  {n:>2} {f['seq']:>5} {f['fixed']:>10,} {f['checkpoints']:>8,} ({f['count']})"
              f" {f['tail']:>8,} ({f['tail_nodes']:>2}) {f['total']:>8,} {threshold - f['total']:>+9,}")
    if floor:
        totals = [f["total"] for f in floor]
        print(f"  floor {min(totals):,}-{max(totals):,}, mean {sum(totals) // len(totals):,};"
              f" checkpoints on surface max {max(f['count'] for f in floor)}")

    # criteria
    auto = [c for c in comps if not c["manual"] and c["summary"] is not None]
    committed = [c for c in comps if c["summary"] is not None]
    after_turn = [c for c in cmds if result["turn_end_seq"] is not None and c["run"]["seq"] > result["turn_end_seq"]]
    first = after_turn[0]["done"] if len(after_turn) >= 1 else None
    second = after_turn[1]["done"] if len(after_turn) >= 2 else None
    criteria = [
        (">=1 automatic compaction", "PASS" if auto else "FAIL", f"{len(auto)} committed"),
        ("0 errors in compaction/end", "PASS" if not errors else "FAIL",
         f"{len(comps) - len(errors)}/{len(comps)} clean" + (f", errors: {', '.join(errors)}" if errors else "")),
        (f"every checkpoint starts with {HEADING!r}", "PASS" if not bad_format else "FAIL",
         f"{len(committed) - len(bad_format)}/{len(committed)}" + (f", bad: {', '.join(bad_format)}" if bad_format else "")),
    ]
    if first is None:
        criteria.append(("/compact #1 = manual compaction", "NOT RUN", "run /compact after the turn ends"))
    else:
        ok = first["data"].get("kind") == "success" and str(first["data"].get("text", "")).startswith("Compacted ")
        criteria.append(("/compact #1 = manual compaction", "PASS" if ok else "FAIL", first["data"].get("text")))
    if second is None:
        criteria.append(("/compact #2 = clean no-op", "NOT RUN", "run /compact a second time"))
    else:
        ok = second["data"].get("kind") == "success" and second["data"].get("text") == NOOP_TEXT
        criteria.append(("/compact #2 = clean no-op", "PASS" if ok else "FAIL", second["data"].get("text")))

    print("\ncriteria")
    for name, verdict, detail in criteria:
        print(f"  {verdict:<8} {name}  ({detail})")
    verdicts = {v for _, v, _ in criteria}
    overall = "FAIL" if "FAIL" in verdicts else "INCOMPLETE" if "NOT RUN" in verdicts else "PASS"
    print(f"\nOVERALL  {overall}")
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[overall]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session", nargs="?", help="session id prefix, session dir, .zstd, .jsonl or .zip")
    parser.add_argument("--threshold", type=int, help="pressure threshold in tokens (skips resolution)")
    parser.add_argument("--context-window", type=int, help="override the session's recorded contextWindow")
    parser.add_argument("--threshold-ratio", type=float, help="override the preset's policy with this ratio")
    parser.add_argument("--output-reserve", type=int, help="override the preset's policy with this outputReserveTokens")
    parser.add_argument("--safety-margin", type=int, help="safetyMarginTokens for --output-reserve (default 0)")
    args = parser.parse_args()
    path = find_session(args.session)
    header, events = load_events(path)

    if args.threshold is not None:
        threshold, trigger = args.threshold, f"{args.threshold:,} (--threshold)"
    else:
        provider, model, window = routed_context(events)
        window = args.context_window or window
        if not window:
            sys.exit("no request/context contextWindow in this session; pass --context-window or --threshold")
        if args.threshold_ratio is not None or args.output_reserve is not None:
            if args.threshold_ratio is not None and args.output_reserve is not None:
                sys.exit("--threshold-ratio and --output-reserve are mutually exclusive")
            config, source = ({"thresholdRatio": args.threshold_ratio} if args.threshold_ratio is not None else
                              {"outputReserveTokens": args.output_reserve, "safetyMarginTokens": args.safety_margin or 0}), "flags"
        else:
            config, source = preset_policy(header.get("agentPreset"))
            if config is None:
                sys.exit(f"cannot read the compaction policy ({source}); pass --threshold-ratio or --output-reserve")
        threshold, retain, formula = resolve_threshold(config, provider, model, window)
        trigger = f"{threshold:,} = {formula}  (retain {retain:,}; policy from {source})"
    result = scan(header, events, threshold)
    result["trigger"] = trigger
    sys.exit(report(path, header, events, result))


if __name__ == "__main__":
    main()
