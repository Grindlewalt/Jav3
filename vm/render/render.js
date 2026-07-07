// Beacon-catcher render harness. Runs INSIDE the sandbox VM (Node + jsdom).
//
// Loads an agent-built HTML file and executes its scripts with every network
// API instrumented. Each attempt is (a) logged with its URL and payload size
// — visible here because we intercept at the app layer, before the tap drops
// the handshake — and (b) actually fired via Node's fetch so the deny-by-default
// tap sees and blocks it too. The shim log is best-effort (a hostile script
// could grab a fresh API); the tap is the ground-truth backstop.
//
// Usage:  NODE_PATH=/opt/jarvis/node_modules node - <file.html>   (script on stdin)
// Emits one line:  JARVIS_RENDER {"attempts":[...]}
const fs = require("fs");
const { JSDOM } = require("jsdom");

const file = process.argv[2];
const html = fs.readFileSync(file, "utf8");
const attempts = [];
const realFetch = globalThis.fetch;   // Node 18+ global — reaches the tap

function record(api, method, url, bytes) {
  try { url = String(url); } catch (_) { return; }
  attempts.push({ api, method: method || "GET", url, bytes: bytes | 0 });
  try {                                // actually attempt so the tap logs it
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), 2500);
    realFetch(url, { method: method || "GET", signal: ac.signal })
      .catch(() => {}).finally(() => clearTimeout(t));
  } catch (_) {}
}
const blen = (b) => { try { return b == null ? 0 : (b.length || String(b).length); } catch (_) { return 0; } };

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  resources: "usable",                 // fetches <img>/<script src> -> tap
  url: "file://" + file,
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = (url, opts = {}) => {
      record("fetch", opts.method, url, blen(opts.body));
      return Promise.resolve({ ok: true, status: 200,
        text: () => Promise.resolve(""), json: () => Promise.resolve({}) });
    };
    if (window.navigator) {
      window.navigator.sendBeacon = (url, data) => {
        record("sendBeacon", "POST", url, blen(data)); return true;
      };
    }
    const OrigXHR = window.XMLHttpRequest;
    function XHR() {
      const x = new OrigXHR(); let m = "GET", u = "";
      const open = x.open.bind(x);
      x.open = (mm, uu, ...r) => { m = mm; u = uu; return open(mm, uu, ...r); };
      const send = x.send.bind(x);
      x.send = (body) => { record("xhr", m, u, blen(body)); try { return send(body); } catch (_) {} };
      return x;
    }
    window.XMLHttpRequest = XHR;
    window.WebSocket = function (url) { record("websocket", "WS", url, 0); return { send() {}, close() {} }; };
    window.EventSource = function (url) { record("eventsource", "GET", url, 0); return { close() {} }; };
  },
});

// give on-load and short setTimeout beacons time to fire, then also sweep the
// DOM for external resource URLs the parser may have queued
setTimeout(() => {
  try {
    const doc = dom.window.document;
    doc.querySelectorAll("[src],[href]").forEach((el) => {
      const u = el.getAttribute("src") || el.getAttribute("href") || "";
      if (/^(https?:)?\/\//i.test(u)) {
        if (!attempts.some((a) => a.url === u)) record("resource", "GET", u, 0);
      }
    });
  } catch (_) {}
  setTimeout(() => {
    process.stdout.write("\nJARVIS_RENDER " + JSON.stringify({ attempts }) + "\n");
    process.exit(0);
  }, 400);
}, 3500);
