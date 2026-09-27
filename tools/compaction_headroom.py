#!/usr/bin/env python3
"""Measure how much context headroom is left when dsh-compaction-select fires.

For every compaction in a session log it prints:

  - when it started, and whether it was automatic or manual
  - tokens in context right before it: the last provider-reported request
    (prompt + output) plus the meter-heuristic price of every surface node
    appended after that request, the same baseline + surface-delta shape
    dsh-token-meter uses for measure()
  - that number as a % of the context window
  - tokens right after: the prompt of the first model request after the
    compaction ends (provider-reported, exact)
  - wall time spent summarizing and the summary's output tokens

For every normal model request it prints the largest output produced in one
turn (max and p99) and the count of stop reasons, so the output reserve can
be sized from what the model actually emits.

With --rereads it also counts, for each compaction, the tool calls in the
next 10 model steps that re-read a file already read before the compaction.
File reads are recognised from read/grep file_path/path arguments and from
bash commands that run cat/sed/head/tail/less/nl/wc/grep on a path; that is
a heuristic, so the count is a lower bound for paths it cannot parse.

Usage:
  tools/compaction_headroom.py [--rereads] SESSION [SESSION ...]
  SESSION is an id prefix, session dir, .zstd, .jsonl or .zip (as check_session.py).

"Prompt" is totalTokens - outputTokens, because llama.cpp reports cached
prompt tokens separately (cacheReadTokens) and inputTokens alone omits them.
"""
import argparse
import datetime
import json
import math
import os
import re
import shlex
import sys

from check_session import estimate_message, find_session, load_events, message_of

DEFAULT_WINDOW = 65536
REREAD_STEPS = 10
READ_COMMANDS = {"cat", "sed", "head", "tail", "less", "nl", "wc", "grep", "rg", "awk", "bat"}
# A plain path word: no redirections, quotes, parens or code fragments.
PATH_WORD = re.compile(r"[~\w][\w.\-/~@+]*")
# Gaps longer than this are idle time (a session resumed later), not work.
IDLE_GAP_MS = 5 * 60 * 1000


def clock(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S")


def prompt_tokens(usage):
    return usage["totalTokens"] - usage["outputTokens"]


def model_usage(event):
    """Provider usage of a model-authored assistant message, or None."""
    if event["type"] != "assistant/message":
        return None
    data = event["data"]
    source = (data.get("message") or {}).get("source") or {}
    if source.get("kind") != "model" or "usage" not in data:
        return None
    return data["usage"]


def stop_reason(event):
    source = (event["data"].get("message") or {}).get("source") or {}
    return ((source.get("replayState") or {}).get("response") or {}).get("stopReason", "?")


def parse_when(text):
    return int(datetime.datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp() * 1000)


def percentile(values, pct):
    """Nearest-rank percentile."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


# ---------------------------------------------------------------- file reads

def paths_in_call(name, arguments, cwd):
    """Paths a tool call reads, normalised to absolute where possible."""
    try:
        args = json.loads(arguments)
    except (TypeError, ValueError):
        return set()
    found = set()
    if name in ("read", "grep") and isinstance(args.get("file_path") or args.get("path"), str):
        found.add(args.get("file_path") or args.get("path"))
    elif name == "bash" and isinstance(args.get("command"), str):
        # Heredoc bodies are program text, not shell words.
        command = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?^\s*\1\s*$", "", args["command"], flags=re.S | re.M)
        for part in re.split(r"&&|\|\||;|\||\n", command):
            try:
                words = shlex.split(part)
            except ValueError:
                continue
            if not words or os.path.basename(words[0]) not in READ_COMMANDS:
                continue
            operands = [w for w in words[1:] if not w.startswith("-")]
            if os.path.basename(words[0]) in ("grep", "rg"):
                operands = operands[1:]    # the first operand is the pattern
            for word in operands:
                if PATH_WORD.fullmatch(word) and ("/" in word or "." in word) and not re.fullmatch(r"[\d,]+p?", word):
                    found.add(word)
    # Directories are listings, not file reads.
    return {p for p in (normalise(p, cwd) for p in found) if not os.path.isdir(p)}


def normalise(path, cwd):
    path = os.path.expanduser(path)
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def session_cwd(header):
    for key in ("cwd", "workspace", "workingDirectory"):
        if isinstance((header or {}).get(key), str):
            return header[key]
    return None


# ---------------------------------------------------------------- analysis

def analyse(header, events, rereads, since=None, until=None):
    """Replay every event; count only compactions, requests and active time inside [since, until)."""
    def inside(ms):
        return (since is None or ms >= since) and (until is None or ms < until)
    window = DEFAULT_WINDOW
    cwd = session_cwd(header)
    last_usage = None          # provider usage of the latest model request
    appended_since = 0         # meter-priced surface tokens appended after it
    open_comps = {}
    comps = []
    outputs = []
    stops = {}
    reads_so_far = set()
    step_calls = []            # (step_index, set_of_paths) in order
    step_index = 0

    for e in events:
        kind, data = e["type"], e.get("data", {})
        if kind == "request/context" and isinstance(data.get("contextWindow"), int):
            window = data["contextWindow"]
        elif kind == "step/start":
            step_index += 1
        usage = model_usage(e)
        if usage is not None:
            if inside(e["time"]):
                outputs.append(usage["outputTokens"])
                reason = stop_reason(e)
                stops[reason] = stops.get(reason, 0) + 1
            for comp in comps:
                if comp["after"] is None and comp["end_time"] is not None:
                    comp["after"] = prompt_tokens(usage)
            last_usage = usage
            appended_since = 0
        elif e.get("surfaceOp") == "append" and last_usage is not None:
            appended_since += estimate_message(message_of(e))
        if kind == "tool/call":
            paths = paths_in_call(data.get("name"), data.get("arguments"), cwd)
            step_calls.append((step_index, paths, e["time"]))
        if kind == "compaction/start":
            before = None if last_usage is None else prompt_tokens(last_usage) + last_usage["outputTokens"] + appended_since
            comp = {"id": data["compactionId"][:8], "manual": data.get("turn") is None, "start": e["time"],
                    "start_step": step_index, "before": before, "window": window, "end_time": None,
                    "after": None, "out": None, "sum_input": None, "error": None,
                    "reads_before": None}
            open_comps[data["compactionId"]] = comp
            comps.append(comp)
            comp["counted"] = inside(e["time"])
        elif kind == "compaction/summary" and data.get("compactionId") in open_comps:
            comp = open_comps[data["compactionId"]]
            comp["out"] = (data.get("usage") or {}).get("outputTokens")
            comp["sum_input"] = ((data.get("diagnostics") or {}).get("budget") or {}).get("inputTokens")
        elif kind == "compaction/end" and data.get("compactionId") in open_comps:
            comp = open_comps.pop(data["compactionId"])
            comp["end_time"] = e["time"]
            comp["error"] = data.get("error")

    # Re-reads: a path read before the compaction started, read again by a
    # tool call in the next REREAD_STEPS model steps after it.
    if rereads:
        for comp in comps:
            before_paths = set()
            for step, paths, when in step_calls:
                if when < comp["start"]:
                    before_paths |= paths
            count, hits = 0, set()
            for step, paths, when in step_calls:
                if comp["end_time"] is None or when <= comp["end_time"]:
                    continue
                if step > comp["start_step"] + REREAD_STEPS:
                    break
                again = paths & before_paths
                if again:
                    count += 1
                    hits |= again
            comp["rereads"] = count
            comp["reread_paths"] = sorted(hits)

    comps = [c for c in comps if c["counted"]]
    times = [e["time"] for e in events if "time" in e and inside(e["time"])]
    span = sum(b - a for a, b in zip(times, times[1:]) if b - a <= IDLE_GAP_MS)
    comp_ms = sum(c["end_time"] - c["start"] for c in comps if c["end_time"] is not None)
    return {"window": window, "comps": comps, "outputs": outputs, "stops": stops,
            "span_ms": span, "comp_ms": comp_ms}


def report(path, result, rereads):
    comps, outputs, window = result["comps"], result["outputs"], result["window"]
    print(f"session  {path}")
    print(f"window   {window:,}  (from request/context)")
    print()
    header = f"{'#':>3} {'start':<15} {'kind':<6} {'before':>8} {'%win':>6} {'unused':>8} {'after':>8} {'sum_s':>6} {'sum_out':>7} {'sum_in':>7}"
    if rereads:
        header += f" {'rereads':>7}"
    print(header)
    for i, c in enumerate(comps, 1):
        before = "-" if c["before"] is None else f"{c['before']:,}"
        pct = "-" if c["before"] is None else f"{100 * c['before'] / c['window']:.1f}"
        unused = "-" if c["before"] is None else f"{c['window'] - c['before']:,}"
        after = "-" if c["after"] is None else f"{c['after']:,}"
        secs = "-" if c["end_time"] is None else f"{(c['end_time'] - c['start']) / 1000:.0f}"
        out = "ERR" if c["error"] else ("-" if c["out"] is None else str(c["out"]))
        sin = "-" if c["sum_input"] is None else f"{c['sum_input']:,}"
        row = f"{i:>3} {clock(c['start']):<15} {'manual' if c['manual'] else 'auto':<6} {before:>8} {pct:>6} {unused:>8} {after:>8} {secs:>6} {out:>7} {sin:>7}"
        if rereads:
            row += f" {c['rereads']:>7}"
        print(row)
        if rereads and c["reread_paths"]:
            print(f"      re-read: {', '.join(c['reread_paths'])}")
    if not comps:
        print("  (no compactions)")
    print()
    if outputs:
        print(f"normal requests  n={len(outputs)}  output tokens per request: max={max(outputs):,}  "
              f"p99={percentile(outputs, 99):,}  p95={percentile(outputs, 95):,}  p50={percentile(outputs, 50):,}")
    print(f"stop reasons     {result['stops']}")
    span, comp_ms = result["span_ms"], result["comp_ms"]
    share = 100 * comp_ms / span if span else 0
    print(f"time             active {span / 60000:.1f} min (gaps > {IDLE_GAP_MS // 60000} min dropped), "
          f"compaction {comp_ms / 60000:.1f} min ({share:.1f}%)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sessions", nargs="+", help="session id prefix, session dir, .zstd, .jsonl or .zip")
    parser.add_argument("--rereads", action="store_true", help="count post-compaction re-reads of files")
    parser.add_argument("--since", type=parse_when, help="count only from this local time, 'YYYY-MM-DD HH:MM'")
    parser.add_argument("--until", type=parse_when, help="count only before this local time, 'YYYY-MM-DD HH:MM'")
    args = parser.parse_args()
    by_window = {}
    for i, arg in enumerate(args.sessions):
        if i:
            print("\n" + "-" * 100 + "\n")
        path = find_session(arg)
        header, events = load_events(path)
        result = analyse(header, events, args.rereads, args.since, args.until)
        if args.since or args.until:
            print(f"slice    [{args.since and clock(args.since) or 'start'}, {args.until and clock(args.until) or 'end'})")
        report(path, result, args.rereads)
        total = by_window.setdefault(result["window"], {"outputs": [], "active": 0, "comp": 0, "n": 0})
        total["outputs"] += result["outputs"]
        total["active"] += result["span_ms"]
        total["comp"] += result["comp_ms"]
        total["n"] += 1
    if len(args.sessions) > 1:
        print("\n" + "=" * 100)
        for window, total in sorted(by_window.items()):
            outs = total["outputs"]
            if not outs:
                continue
            share = 100 * total["comp"] / total["active"] if total["active"] else 0
            print(f"window {window:,} ({total['n']} sessions)  n={len(outs)}  output tokens per request: "
                  f"max={max(outs):,}  p99={percentile(outs, 99):,}  p95={percentile(outs, 95):,}  "
                  f"p50={percentile(outs, 50):,}  compaction {total['comp'] / 60000:.1f} of "
                  f"{total['active'] / 60000:.1f} active min ({share:.1f}%)")


if __name__ == "__main__":
    main()
