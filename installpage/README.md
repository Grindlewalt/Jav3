# installpage

A white page with a title and the one-line install, meant to face the
internet. One file, Python 3.11+, standard library only; it does not import
the Jav3 backend and needs no checkout or venv where it runs.

| Route | Answer |
|---|---|
| `GET /` | `text/html` — title, one sentence, the one-liner in a `<pre>`. No CSS, no JS, nothing external. |
| `GET /bootstrap.sh` | the bootstrap script, `text/x-shellscript`, read once at startup |
| `GET /healthz` | `ok` |
| `HEAD` on any of those | same headers, no body |
| any other path | `404` |
| any other method | `405`, `Allow: GET, HEAD` |

## Run it

```sh
python3 installpage/server.py                          # 127.0.0.1:8080
python3 installpage/server.py --bind :: --port 8080    # all addresses, v4 + v6
```

| Flag | Env | Default |
|---|---|---|
| `--bind` | `INSTALLPAGE_BIND` | `127.0.0.1` |
| `--port` | `INSTALLPAGE_PORT` | `8080` |
| `--bootstrap` | `INSTALLPAGE_BOOTSTRAP` | `../scripts/bootstrap.sh` next to this file |
| `--title` | `INSTALLPAGE_TITLE` | `Jav3` |
| `--command` | `INSTALLPAGE_COMMAND` | the GitHub raw bootstrap one-liner |
| `--rate` | `INSTALLPAGE_RATE` | `30` requests/minute per client IP |
| `--max-conns` | `INSTALLPAGE_MAX_CONNS` | `64` concurrent connections |

Flags win over env. If the bootstrap script cannot be read (missing, empty, or
over 1 MiB) the server refuses to start (exit 2).

To have the page point at its own copy of the script instead of GitHub, set
the command explicitly, e.g.
`INSTALLPAGE_COMMAND='curl -fsSL https://install.example/bootstrap.sh | sh'`.
The page never builds that URL from the request's `Host` header: that header
is the client's to choose, and the page is cacheable.

## Run it as a service

`installpage.service` is a system unit with `DynamicUser=yes` and a tight
sandbox (`systemd-analyze security` scores it 1.3). Install steps are in the
unit's header comment; the files go under `/opt/installpage/` because
`ProtectHome=yes` hides `/home`.

- **High port (default):** listens on 8080 with an empty capability set.
  Point your TLS terminator / tunnel / port-forward at it.
- **Port 80:** set `INSTALLPAGE_PORT=80` and replace the empty
  `CapabilityBoundingSet=` with
  `CapabilityBoundingSet=CAP_NET_BIND_SERVICE` +
  `AmbientCapabilities=CAP_NET_BIND_SERVICE` (both — the ambient grant is
  dropped if the bounding set lacks it).

## TLS is out of scope

The server speaks plain HTTP only. Put it behind whatever already terminates
TLS for you (a reverse proxy, a Cloudflare/other tunnel). A `curl | sh` served
over plain HTTP to the internet is only as trustworthy as every network in
between, so don't publish the one-liner as `http://` beyond a LAN.

Behind a proxy, every request arrives from the proxy's address. The server
takes the client IP from the socket only and never trusts `X-Forwarded-For`
(which any client can forge), so the per-IP rate limit then applies to the
proxy as a whole — effectively a global limit. Raise `--rate` accordingly, or
rate-limit per client at the proxy.

## Hardening, exactly

- One request per connection (`Connection: close`), no keep-alive, no pipelining.
- Request line + headers capped at 8 KiB total: over it is `414` (line never
  ended) or `431` (headers). The cap is enforced while reading, so an
  oversized request is refused after 8 KiB + 1 bytes, never buffered whole.
- The head must arrive within 5 s **wall-clock** (not per `recv`), so a
  byte-at-a-time trickle gets `408` as surely as silence does.
- The body is never read, whatever `Content-Length` claims.
- Strict request line: `METHOD SP target SP HTTP/1.0|1.1`, ASCII, uppercase
  method, no control characters in the target. Anything else `400`/`505`.
- Exact-match routing on the raw target: no path normalisation, no decoding,
  no filesystem access after startup, no directory listing. `/?x`, `//`,
  `/%2e%2e/` are all just `404`.
- Every response is pre-rendered at startup; nothing from a request is echoed
  back. `Content-Length` is always set.
- Headers on every response: `X-Content-Type-Options: nosniff`,
  `Content-Security-Policy: default-src 'none'`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY`. `Cache-Control: public, max-age=300` on `200` and
  `404`; `no-store` on refusals (`4xx`/`5xx` other than 404) so a shared cache
  can't replay one client's `429` to everyone. No `Server` header.
- Per-IP token bucket (default 30/min, burst 30) → `429` + `Retry-After`. The
  bucket table is bounded (full buckets pruned, then the oldest half).
- At most `--max-conns` handler threads; past that the accept loop answers
  `503` itself without spawning anything.
- Lingering close (stop writing, drain ≤ 64 KiB for ≤ 1 s) so refused clients
  still see the response instead of a reset.
- Access log: one JSON line per request on stdout — `ts`, `method`, `path`
  (truncated to 200), `status`, `peer` (socket address), `ms`. JSON escaping
  means control characters in a request can't forge log lines. No tracebacks.
- `SIGTERM`/`SIGINT` stop accepting, finish in-flight requests (each bounded
  by the timeouts above), exit 0.

## Tests

`.venv/bin/python -m pytest -q tests/test_installpage.py` — runs real servers
on loopback and asserts the bytes on the wire.
