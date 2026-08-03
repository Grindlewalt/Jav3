"""Is the 4B smart enough for the voice tier? Runs the REAL slim voice
prompt + the REAL LOCAL_TOOLS schemas against a candidate model, with faked
tool results — Jarvis itself is never involved, nothing is persisted.

Usage (on the Pi): .venv/bin/python scripts/voice_model_gauntlet.py <model> [base_url]
Judging: watch for fabricated actions (says it played, no tool call), tool
choice, escalation on the heavy asks, and first-word latency."""
import asyncio, json, sys, time
import urllib.request

sys.path.insert(0, "/home/grindlewalt/jarvis")

MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:4b"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://10.0.0.58:11437/v1"

# canned tool results — realistic shapes lifted from the live transcripts
FAKE_RESULTS = {
    "music_play": "playing Yellow — Coldplay in the Jarvis player on Mac.",
    "music_search": ("11 matches:\n  [22] Yellow — Coldplay #drive\n  [19] Hey "
                     "Driver — Zach Bryan #drive\n  [15] Motorcycle Drive By — "
                     "Zach Bryan #drive"),
    "music_status": ("library: 30 tracks\nJarvis player: playing Yellow — "
                     "Coldplay at 1:02 of 4:29 at 80% volume\nmusic app: "
                     "nothing actually playing (no player open)"),
    "music_control": "paused in the Jarvis player on Mac · Chrome.",
    "computer_play": "playing Harry Potter DH Prt 1.",
    "computer_status": "2 machines connected: linux, macbook. macbook: volume 45%.",
    "computer_volume": "macbook volume set to 25%.",
    "computer_playback": "paused on macbook.",
    "computer_open_link": "opened on macbook.",
    "web_search": ("1. Mars weather — NASA: dust storm season begins...\n"
                   "2. Perseverance MEDA readings..."),
    "web_read": "Mars: -60C average, 6 mbar pressure, dust storms rising.",
}

SCENARIOS = [
    ("greeting",      "Jarvis, are you there?"),
    ("quick fact",    "How many feet are in a mile?"),
    ("play music",    "Could you play some driving music for me?"),
    ("pause",         "Pause the music please."),
    ("queue",         "Queue up something by Zach Bryan next."),
    ("movie",         "Put on Harry Potter on the MacBook."),
    ("volume",        "Turn the MacBook volume down to 25 percent."),
    ("escalate-heavy","Research the best NAS setup for my house and write it up."),
    ("escalate-code", "Refactor the egress proxy to add per-host rate limits."),
    ("ambiguous",     "That's too loud."),
]


async def build_prompt():
    from backend.db import get_db
    from backend.memory import assemble_system_prompt
    from backend.voice import LOCAL_CONTEXT_EXCLUDE, LOCAL_TOOLS
    from backend.voice_text import LOCAL_PROMPT, VOICE_PROMPT
    from backend.agent.tools.registry import load_registry, openai_tool_specs
    db = await get_db()
    try:
        base = await assemble_system_prompt(db, exclude=set(LOCAL_CONTEXT_EXCLUDE))
    finally:
        await db.close()
    entries = [e for e in load_registry() if e["name"] in LOCAL_TOOLS]
    tools = openai_tool_specs(entries)
    return f"{base}\n\n{VOICE_PROMPT}\n\n{LOCAL_PROMPT}", tools


def call(messages, tools):
    body = {"model": MODEL, "messages": messages, "tools": tools,
            "stream": True, "max_tokens": 300, "reasoning_effort": "none"}
    req = urllib.request.Request(f"{BASE}/chat/completions",
                                 json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    t0 = time.time(); first = None
    content = ""; tool_calls = {}
    with urllib.request.urlopen(req, timeout=120) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            delta = json.loads(line[6:])["choices"][0].get("delta", {})
            if delta.get("content"):
                content += delta["content"]
                if first is None:
                    first = time.time() - t0
            for tc in delta.get("tool_calls") or []:
                i = tc.get("index", 0)
                slot = tool_calls.setdefault(i, {"name": "", "args": ""})
                fn = tc.get("function") or {}
                slot["name"] = fn.get("name") or slot["name"]
                slot["args"] += fn.get("arguments") or ""
                if first is None:
                    first = time.time() - t0
    return content, list(tool_calls.values()), first, time.time() - t0


async def main():
    system, tools = await build_prompt()
    print(f"model={MODEL}  prompt={len(system)} chars  tools={len(tools)}")
    for tag, user in SCENARIOS:
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        print(f"\n=== {tag}: {user!r}")
        for hop in range(3):
            content, calls, first, total = call(messages, tools)
            lat = f"[first {first:.2f}s total {total:.2f}s]" if first else "[no output!]"
            if content.strip():
                print(f"  say {lat}: {content.strip()[:220]!r}")
            if not calls:
                break
            assistant = {"role": "assistant", "content": content or None,
                         "tool_calls": [
                             {"id": f"c{hop}{i}", "type": "function",
                              "function": {"name": c["name"],
                                           "arguments": c["args"] or "{}"}}
                             for i, c in enumerate(calls)]}
            messages.append(assistant)
            for i, c in enumerate(calls):
                print(f"  tool {lat}: {c['name']}({c['args'][:140]})")
                messages.append({"role": "tool", "tool_call_id": f"c{hop}{i}",
                                 "content": FAKE_RESULTS.get(c["name"],
                                                             "ok")})

asyncio.run(main())
