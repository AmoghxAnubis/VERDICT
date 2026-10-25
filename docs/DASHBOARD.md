# Dashboard and browser verification

The Next.js dashboard reads retained diagnostic assessments through FastAPI.
Overview, source details, history, alerts, dependency inspection, evidence export,
and replay controls share the selected dataset and assessment. Publication remains
disabled, including for synthetic healthy-agreement scenarios.

## Local operation

From the repository root, apply migrations and seed the optional synthetic cases:

```powershell
.venv/Scripts/verdict.exe db upgrade
.venv/Scripts/verdict.exe demo benchmarks/adversarial-v1.json
.venv/Scripts/verdict.exe serve
```

In a separate terminal:

```powershell
cd frontend
npm.cmd ci
npm.cmd run dev
```

Open `http://127.0.0.1:3000`. HTTP requests use the Next.js proxy; set
`VERDICT_API_URL` when the backend is elsewhere. The browser WebSocket defaults to
port 8000 on the page's hostname. Set `NEXT_PUBLIC_VERDICT_WS_URL` **before building**
to override it. Configure the backend's `VERDICT_DASHBOARD_ORIGINS` as a JSON array
of permitted page origins when using a different frontend host or port.

Synthetic seeding writes only `replay:*` datasets. Replay playback opens retained
assessments; it does not generate provider observations or write to live storage.
The live dataset starts without assessments until the diagnostic pipeline saves
them. An empty live dashboard is expected for a replay-only database.

## Reproducible browser checks

Validated on 25 October 2026: **10 passed** against a production Next.js build,
using installed Chrome on Windows at desktop and emulated mobile sizes.

With dependencies installed and Chrome available:

```powershell
cd frontend
npm.cmd run test:e2e
```

The runner starts a production frontend on `127.0.0.1:3011` and a fixture API on
`127.0.0.1:8011`. Both ports must be free. A new database under
`.cache/browser-tests/` is migrated and seeded on every invocation; an existing
database is never reused. The ordinary `data/verdict.db` is untouched. The frontend
build uses `.next-e2e/` so a development server can keep using `.next/`.
Playwright owns and stops its test servers. Test databases remain for inspection.
Windows sandbox environments may require allowing the test command to launch
Chrome and its child processes outside the sandbox.

`VERDICT_E2E_PYTHON` overrides the project-local Python executable.
`VERDICT_E2E_CHANNEL` overrides the installed `chrome` channel; CI installs and uses
`chromium`. Mobile coverage uses Chrome with an iPhone 13 viewport and touch
emulation, rather than Safari or a physical phone.

The checks cover:

- An unavailable live feed and a six-source healthy synthetic diagnostic round.
- Source details, exact round identifiers, dependency-node selection, gate reasons,
  and a downloaded evidence bundle with linked registry IDs and null usable output.
- History filtering and pinning, a single-source price spike, quarantine, stale
  observations during an outage, recovery, playback, and switching back to live.
- An injected HTTP 503 and successful retry in the rendered page.
- Closing a proxied **real** WebSocket, committing a new retained assessment via a
  separate Python process, reconnecting from the previous cursor, and displaying
  the new history entry. Equal-cutoff assessments retain deterministic report-ID
  ordering, so commit order does not redefine the default selected assessment.
- The mobile page's scroll width after source inspection and history selection.

Full-page screenshots are retained in `frontend/test-results/`. On failure,
Playwright also saves a screenshot and trace. Open the HTML report with
`npx playwright show-report`; CI uploads reports and screenshots as artifacts.

The mobile checks found and fixed a hidden source-table header label escaping its
scroll container. Making the container a positioned ancestor keeps that label
within its scrolling region and restores taps after source inspection.

These checks establish the diagnostic dashboard workflow. Live-provider endurance,
upstream independence, calibrated confidence, and testnet publication remain
separate roadmap gates.
