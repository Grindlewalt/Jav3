"""Rebuild backend/providers_catalog.json from models.dev (MIT) — the provider
catalogue backend/providers.py serves.

    python scripts/refresh_providers.py [SRC] [OUT] [DATE]

SRC is a models.dev api.json path or URL (default https://models.dev/api.json),
OUT defaults to backend/providers_catalog.json, DATE to today. Host-side egress
to models.dev only when SRC is a URL. An operator's own additions belong in
~/.config/jarvis/providers.json (merged on top at load), not in this file.
"""
import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = sys.argv[1] if len(sys.argv) > 1 else "https://models.dev/api.json"
OUT = sys.argv[2] if len(sys.argv) > 2 else str(REPO / "backend" / "providers_catalog.json")
DATE = sys.argv[3] if len(sys.argv) > 3 else datetime.date.today().isoformat()
if SRC.startswith(("http://", "https://")):
    req = urllib.request.Request(SRC, headers={"User-Agent": "jav3-refresh-providers"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
else:
    d = json.load(open(SRC))

EXCLUDE = {  # not reachable with one pasted key / plain URL -> listed in providers.md
 "amazon-bedrock": "AWS sigv4 / IAM credential chain (bearer token variant exists)",
 "azure": "resource name + deployment URLs; mixed OpenAI/Anthropic wire",
 "azure-cognitive-services": "resource name + deployment URLs; mixed wire",
 "google-vertex": "GCP service-account OAuth (GOOGLE_APPLICATION_CREDENTIALS)",
 "google-vertex-anthropic": "GCP service-account OAuth, Anthropic-on-Vertex wire",
 "sap-ai-core": "service-key JSON + OAuth client-credentials",
 "watsonx": "IBM IAM token exchange + project id",
 "cloudflare-ai-gateway": "3 values (token, account, gateway) + mixed per-model wire",
 "gitlab": "GitLab Duo OAuth/PAT via gitlab-ai-provider, own wire",
 "github-copilot": "OAuth device flow + Copilot token exchange (not a pasted key)",
 "perplexity-agent": "Responses-API-only Agent API",
 "qvac": "custom SDK (@qvac/ai-sdk-provider), no documented HTTP base",
 "salad-cloud": "custom SDK, no base URL in models.dev",
}
DEFAULT_BASE = {
 "openai": "https://api.openai.com/v1",
 "anthropic": "https://api.anthropic.com/v1",
 "google": "https://generativelanguage.googleapis.com/v1beta",
 "groq": "https://api.groq.com/openai/v1",
 "mistral": "https://api.mistral.ai/v1",
 "xai": "https://api.x.ai/v1",
 "togetherai": "https://api.together.xyz/v1",
 "deepinfra": "https://api.deepinfra.com/v1/openai",
 "cerebras": "https://api.cerebras.ai/v1",
 "cohere": "https://api.cohere.ai/compatibility/v1",
 "perplexity": "https://api.perplexity.ai",
 "venice": "https://api.venice.ai/api/v1",
 "vercel": "https://ai-gateway.vercel.sh/v1",
 "v0": "https://api.v0.dev/v1",
 "aihubmix": "https://aihubmix.com/v1",
}
NONCHAT = re.compile(r"(embed|image|realtime|tts|whisper|transcri|moderation|rerank|audio-preview|dall-e|imagen|veo|sora)", re.I)
LOCAL_NOAUTH = {"lmstudio", "atomic-chat", "lynkr"}
NO_LIST = {"perplexity", "cohere", "v0", "zai", "zai-coding-plan", "zhipuai", "zhipuai-coding-plan",
           "bailing", "freemodel", "subconscious", "thinkingmachines",
           "minimax", "minimax-cn", "minimax-coding-plan", "minimax-cn-coding-plan"}
PRIORITY = ["opencode", "opencode-go", "openai", "anthropic", "google",  # OpenCode /connect "Popular"
            "deepseek", "openrouter", "groq", "mistral", "xai", "togetherai", "fireworks-ai",
            "ollama", "lmstudio", "llamacpp"]  # brief seed
OPENAI_RESPONSES_ONLY = re.compile(r"(-pro($|-)|codex|deep-research|computer-use|o1-pro|o3-pro)")

def kind_of(npm):
    if npm == "@ai-sdk/anthropic": return "anthropic"
    if npm == "@ai-sdk/google": return "google"
    return "openai"

def pick_env(envs):
    for e in envs:
        if re.search(r"(KEY|TOKEN|PAT)$", e) and not re.search(r"(HOST|ACCOUNT|_ID|BASE_URL|ENDPOINT)$", e):
            return e
    return envs[0] if envs else None

def norm_base(u):
    u = re.sub(r"/chat/completions/?$", "", u)
    u = re.sub(r"\$\{([A-Z0-9_]+)\}", r"{\1}", u)
    return u.rstrip("/")

stats = {"dropped_deprecated": 0, "dropped_wire": 0, "dropped_nontext": 0, "missing_price": 0}
providers = []
for pid, p in d.items():
    if pid in EXCLUDE: continue
    kind = kind_of(p["npm"])
    base = p.get("api") or DEFAULT_BASE.get(pid)
    if not base:
        print("NO BASE", pid, file=sys.stderr); continue
    base = norm_base(base)
    pnpm = p["npm"]
    models = []
    for mid, m in p["models"].items():
        label = m["name"]
        if m.get("status") == "deprecated":
            if pid != "deepseek": stats["dropped_deprecated"] += 1; continue
            label += " (deprecated)"  # keep Jav3's current default id resolvable
        if "text" not in m["modalities"]["output"] or NONCHAT.search(mid): stats["dropped_nontext"] += 1; continue
        ov = m.get("provider") or {}
        mnpm = ov.get("npm", pnpm)
        wire_ok = True
        if "api" in ov and norm_base(ov["api"]) != base: wire_ok = False
        if ov.get("shape") == "responses": wire_ok = False
        if kind_of(mnpm) != kind and pid != "cohere": wire_ok = False
        if mnpm == "@ai-sdk/openai" and pnpm != "@ai-sdk/openai": wire_ok = False  # per-model Responses API
        if pid == "vivgrid" and mnpm != "@ai-sdk/openai-compatible": wire_ok = False
        if pid == "openai" and OPENAI_RESPONSES_ONLY.search(mid): wire_ok = False
        if pid == "google" and re.search(r"deep-research|computer-use", mid): wire_ok = False
        if not wire_ok: stats["dropped_wire"] += 1; continue
        c = m.get("cost") or {}
        pi, po = c.get("input"), c.get("output")
        if pi is None: stats["missing_price"] += 1
        models.append({"id": mid, "label": label, "ctx": m["limit"].get("context"),
                       "price_in": pi, "price_out": po,
                       "tools": bool(m.get("tool_call")),
                       "vision": "image" in m["modalities"]["input"],
                       "reasoning": bool(m.get("reasoning")),
                       # optional extras the model gateway uses: the output
                       # cap (max_tokens), the cache-read price (cost + budget
                       # weighting) and whether sampling params are accepted
                       **({"max_output": m["limit"]["output"]}
                          if m["limit"].get("output") else {}),
                       **({"price_cache": c["cache_read"]}
                          if c.get("cache_read") is not None else {}),
                       **({"temperature": False}
                          if m.get("temperature") is False else {})})
    if not models and pid not in LOCAL_NOAUTH:
        print("NO MODELS LEFT", pid, file=sys.stderr); continue
    models.sort(key=lambda x: x["label"].lower())
    providers.append({
        "id": pid, "label": p["name"], "kind": kind, "base_url": base,
        "key_env": pick_env(p["env"]),
        "auth": "none" if pid in LOCAL_NOAUTH else {"anthropic": "x-api-key", "google": "query"}.get(kind, "bearer"),
        "docs": p.get("doc"),
        "lists_models": pid not in NO_LIST,
        "models": models})

providers += [
 {"id": "ollama", "label": "Ollama (local)", "kind": "ollama", "base_url": "http://localhost:11434",
  "key_env": None, "auth": "none", "docs": "https://docs.ollama.com/api", "lists_models": True, "models": []},
 {"id": "llamacpp", "label": "llama.cpp server (local)", "kind": "openai", "base_url": "http://127.0.0.1:8080/v1",
  "key_env": None, "auth": "none", "docs": "https://github.com/ggml-org/llama.cpp/tree/master/tools/server",
  "lists_models": True, "models": []},
]
rank = {k: i for i, k in enumerate(PRIORITY)}
providers.sort(key=lambda p: (rank.get(p["id"], 99), p["label"].lower()))
out = {"version": 1, "source": f"models.dev @ {DATE}", "providers": providers}
with open(OUT, "w") as f:
    f.write('{"version": 1, "source": %s, "providers": [\n' % json.dumps(out["source"]))
    for i, p in enumerate(providers):
        head = {k: v for k, v in p.items() if k != "models"}
        f.write(json.dumps(head)[:-1] + ', "models": [')
        f.write(",".join("\n  " + json.dumps(m) for m in p["models"]))
        f.write("\n]}" + (",\n" if i < len(providers) - 1 else "\n"))
    f.write("]}\n")
print(len(providers), "providers", sum(len(p["models"]) for p in providers), "models", stats, file=sys.stderr)
