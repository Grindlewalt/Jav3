# Live UI smoke (Playwright)

Real-browser click-through of the security surfaces, run **on the Pi** against the
deployed SPA at `localhost:8000`. Complements `scripts/e2e_smoke.py` (API-level)
with actual DOM interaction — the "feel the UX" pass.

Covers: log in → **Network** view renders the live egress feed (verdict chips +
host-approval queue) → **Review Center** aggregates staged changes / gate flags /
egress approvals → **Context** loads with the taint/promote wiring.

## Run (on the Pi)

```bash
# one-time: install the headless browser (~110 MB, arm64)
cd ~/pwtest && npm i -D @playwright/test && npx playwright install --with-deps chromium

# copy this dir's files next to the install, make a throwaway user, run
cp ~/jarvis/e2e-ui/* ~/pwtest/
cd ~/jarvis && .venv/bin/python -m backend.cli create-user pw-tester pwpass123
cd ~/pwtest && PW_USER=pw-tester PW_PASS=pwpass123 npx playwright test
# screenshots land in ~/pwtest/shot-*.png; delete the throwaway user afterward
```

The feed assertions expect at least one recent egress event to exist (flip
`JARVIS_VM_EGRESS=true`, boot the guest, and let it fetch — or generate one with
`curl -x http://10.201.0.1:8443 https://github.com/`). With no events the feed is
empty and the Network assertions are skipped-by-emptiness.
