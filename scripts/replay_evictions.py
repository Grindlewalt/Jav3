"""Replay real tool_calls through the loop's eviction policy (RUNS-03).

Usage: replay_evictions.py DATA.json [pressure_chars ...]

DATA.json is a dump of tool_calls (read-only) as a list of
{"conv", "calls": [{"id", "tool", "args", "n", "at", "text"}]}: `n` is the stored
result length (the DB keeps 10,000 chars at most), `text` the stored result of
a read_file. Each conversation is replayed as one turn, calls sharing a
created_at second forming a round. The REAL loop._evict_stale_results decides
what is dropped; a read whose lines overlap a dropped read of the same file is
counted as a re-read. Pressure 0 is the old age-only rule (2 rounds).

Reported per policy: re-reads, evictions, and `resent` = the sum over rounds of
the context size (what the model is sent, before any cache discount) plus the
chars re-reads add. Truncated stored results (n == 10,000) are scaled x2.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.agent import loop  # noqa: E402
from backend.config import settings  # noqa: E402

BASE_CHARS = 40_000       # system prompt + tool schemas + the brief
ASSISTANT_CHARS = 500     # per round
TRUNC_SCALE = 2
CACHE_HIT_RATIO = 0.02    # DeepSeek: cache hit $0.003/M vs miss $0.15/M (config.py)


def replay(convs, pressure):
    settings.tool_result_pressure_chars = pressure
    tot = dict(rereads=0, reads=0, evictions=0, resent=0, reread_chars=0, turns=0, cost=0)
    for conv in convs:
        msgs = [{"role": "system", "content": "s" * BASE_CHARS}]
        prev = 0
        tool_msgs, edited, spans = [], {}, []
        rounds = []
        for call in conv["calls"]:
            if rounds and rounds[-1][0] == call["at"]:
                rounds[-1][1].append(call)
            else:
                rounds.append((call["at"], [call]))
        tot["turns"] += 1
        for r, (_at, calls) in enumerate(rounds):
            msgs.append({"role": "assistant", "content": "a" * ASSISTANT_CHARS})
            for call in calls:
                try:
                    args = json.loads(call["args"] or "{}")
                except ValueError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                name, path = call["tool"], args.get("path")
                n = call["n"] * (TRUNC_SCALE if call["n"] >= 10_000 else 1)
                entry = {"round": r, "name": name}
                if isinstance(path, str) and path:
                    entry["path"] = path
                if name == "read_file" and "path" in entry:
                    tot["reads"] += 1
                    span = loop._read_span(args)
                    entry["span"] = span
                    if any(p == path and span[0] <= b and a <= span[1] for p, (a, b) in spans):
                        tot["rereads"] += 1
                        tot["reread_chars"] += n
                    text = call["text"] or ""
                    content = (text + "x" * max(0, n - len(text)))[:max(n, len(text))]
                else:
                    content = "x" * n
                if name in ("edit_file", "write_file") and "path" in entry:
                    edited[path] = r
                msgs.append({"role": "tool", "content": content})
                entry["idx"] = len(msgs) - 1
                tool_msgs.append(entry)
            dropped = loop._evict_stale_results(msgs, tool_msgs, r, edited)
            first_changed = min((t["idx"] for t in dropped), default=None)
            for t in dropped:
                tot["evictions"] += 1
                if t["name"] == "read_file" and "path" in t:
                    spans.append((t["path"], t["span"]))
            size = loop._context_chars(msgs)
            tot["resent"] += size
            # the provider caches the prefix that did not change since the last
            # call; an eviction rewrites everything from its message onward
            stable = prev if first_changed is None else min(
                prev, loop._context_chars(msgs[:first_changed]))
            tot["cost"] += (size - stable) + stable * CACHE_HIT_RATIO
            prev = size
    return tot


def main():
    convs = json.loads(Path(sys.argv[1]).read_text())
    pressures = [int(p) for p in sys.argv[2:]] or [0, 200_000]
    print(f"{len(convs)} conversations, "
          f"{sum(len(c['calls']) for c in convs)} tool calls")
    print(f"{'pressure':>10} {'reads':>6} {'rereads':>8} {'evicted':>8} "
          f"{'resent (M chars)':>17} {'cache-aware cost':>17}")
    for p in pressures:
        t = replay(convs, p)
        print(f"{p:>10} {t['reads']:>6} {t['rereads']:>8} {t['evictions']:>8} "
              f"{t['resent'] / 1e6:>17.1f} {t['cost'] / 1e6:>17.1f}")


if __name__ == "__main__":
    main()
