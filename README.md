# Transaction & Page-Load Tests — Errors Monitor

> A local Flask dashboard for ThousandEyes TAMs to spot Browser Synthetic
> tests (Page-Load and Web-Transactions) that are erroring or slowing down,
> across one or many organizations, in near real-time.
>
> **Cisco internal tool — best-effort, not officially supported.**

---

## What it does

1. Authenticates with the **ThousandEyes API v7** using your bearer token(s).
2. Lets you pick **one or many organizations**, optionally **across multiple
   tokens** (e.g. when you cover several customer tenants).
3. Runs a background loop that, for every selected organization, scans **every
   account group** and pulls the latest results for each enabled
   `page-load` and `web-transactions` test that is **not** a `liveShare` and
   **not** a `savedEvent`.
4. Caches per-test history under `test_results/{organizationId}_{testId}.json`,
   merging only new rounds (deduped on `agentId` + `roundId`).
5. Aggregates everything into 15-minute blocks (local timezone) and renders a
   live dashboard with executive KPIs, four organization-level filled-line
   widgets, and four "outstanding tests" tables.
6. Lets you **ignore** specific tests so they stop polluting the metrics, and
   **re-include** them later from a dedicated "Ignored Tests" section.

The whole thing runs locally on your laptop, talks only to
`api.thousandeyes.com`, and never persists tokens to disk.

---

## Features

### Authentication & multi-tenant
- **Login** with a single ThousandEyes API v7 bearer token.
- On the **Select Organizations** page you can paste **additional tokens** via
  *Load organization from another token*. Their organizations show up in the
  same list, tagged with the token they came from. Each org is later queried
  with the right token.

### Background data-collection loop
- Configurable interval: **5 / 10 / 15 / 20 / 30 / 60 minutes** (default 5).
- Rate-limit aware: reads `x-organization-rate-limit-*` headers and
  back-off-sleeps proportionally as headroom drops (5/10/20/50/80% tiers).
- Logs each cycle, each account-group progress (`N/total — pct%` : "pct" = percentage), and the
  total cycle duration in seconds (or `Mm Ss` past 1 minute).

### Outstanding-test detection
- **Web-Transactions:** any test whose latest round in the most recent 15-min
  block has `errorType`.
- **Page-Load:** any test whose latest round in the most recent 15-min block
  is missing the `pageLoadTime` property (i.e. the page didn't finish
  loading).
- Plus the **Top 5** highest `transactionTime` and highest `pageLoadTime`
  tests, even when those tests are not technically "in error".

### Dashboard surface
- **Executive Snapshot**: tests tracked vs. tests in error per
  test type. The "in error" numbers turn **red** when > 0, **green** otherwise.
- **Four widgets**
  1. Transaction tests with errors
  2. Transaction time (per-block average, seconds)
  3. Page-Load completion problems
  4. Page-Load time (per-block average, seconds)
- **Four tables**, each row = one test:
  1. Outstanding Transaction Tests — Errors (availability timeline 0/100%)
  2. Top 5 Transaction Tests — Highest `transactionTime`
  3. Outstanding Page-Load Tests — Missing `pageLoadTime`
  4. Top 5 Page-Load Tests — Highest `pageLoadTime`
- **Ignored Tests** section at the bottom with Account Group, Test Name and
  Test Type. Each row has its own re-include checkbox; the **Re-Include**.

### Ignore workflow
- Ignored tests are excluded from **every** metric on the dashboard
  (executive summary, KPIs, widgets, all four tables).
- They are listed in the bottom **Ignored Tests** table where they can be
  re-included.

---

## Architecture

```
┌─────────────┐      bearer tokens       ┌─────────────────────────┐
│  Browser    │ ──────────────────────▶  │  Flask app  (app.py)    │
│  (dashboard)│ ◀─── HTML + /api JSON ── │  • routes               │
└─────────────┘                          │  • server-side session  │
                                         │  • MonitorScheduler ────┼──► ThousandEyes
                                         │                         │     API v7
                                         │  analyzer.py            │
                                         │  • 15-min block agg     │
                                         │  • ignored-tests filter │
                                         └─────────┬───────────────┘
                                                   │
                                                   ▼
                                  test_results/{orgId}_{testId}.json
                                  test_results/ignored_tests_{orgId}.txt
```

### File layout

| File | Purpose |
|---|---|
| `app.py` | Flask routes, server-side session store, background `MonitorScheduler` |
| `te_client.py` | ThousandEyes API v7 client + `retry` / `require_token` decorators + adaptive rate-limit sleep |
| `analyzer.py` | 15-minute block aggregation, on-disk cache merge, ignored-tests file helpers |
| `templates/login.html` | Token login page |
| `templates/select_org.html` | Multi-token / multi-org picker |
| `templates/dashboard.html` | Hero, KPIs, widgets, tables |
| `static/styles.css` | Dark theme |
| `static/dashboard.js` | Polling, Chart.js rendering, Ignore/Re-Include flows |
| `requirements.txt` | Pinned deps |
| `test_results/` | On-disk cache (auto-created, **do not commit**) |

### Cache file shape

```
test_results/{OrgId}_{testId}.json
{
  "type": "page-load" | "web-transactions",
  "test": { "testId", "testName", "aid", "accountGroupName", "orgId", "organizationName" },
  "results": [ ... ThousandEyes round payloads, newest first ... ]
}
```

### Ignored-tests file

```
test_results/ignored_tests_{OrgId}.txt
123456
234567
...
```

One test id per line, sorted, newline-terminated.

---

## Running

```bash
git clone <this-repo>
cd app_page-load_and_tx-test_errors_monitor

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Optional: pin a stable Flask cookie secret across restarts.
export FLASK_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

python app.py
# open http://127.0.0.1:5050/
```

By default the app binds to `127.0.0.1:5050` (local only). Don't change that
for shared machines.

### Workflow

1. Paste your ThousandEyes bearer token → **Sign in**.
2. (Optional) Paste additional tokens and click **Load organization from
   another token** to merge their orgs into the same list.
3. Tick the organizations you want to monitor and click **Load Dashboard**.
4. Wait for the first cycle (`Getting data…`). Subsequent cycles run on the
   chosen interval; the dashboard auto-refreshes every 10 seconds.
5. Click any test's **Ignore** button to drop it from all metrics; re-include
   from the bottom *Ignored Tests* table.

---

## Configuration

| Knob | Where | Notes |
|---|---|---|
| Refresh interval | UI dropdown | 5/10/15/20/30/60 min, default 5 |
| Display range | UI dropdown | 1h / 2h / 4h / 8h / 12h / 1d / 2d / 7d, default 8h. Ranges ≤ 1 day use 15-min blocks; 2d/7d use hourly blocks. |
| Active org | UI dropdown | Switch between organizations selected at login |
| API window | derived from selected range | `1d` for ranges ≤ 1d; `2d`/`7d` to backfill the cache when the wider ranges are chosen |
| Cache directory | `app.py` (`CACHE_DIR`) | Defaults to `./test_results/` |
| Flask secret | `FLASK_SECRET` env var | Random per-process if unset |

---

## Security & privacy notes

- Bearer tokens live **only** in process memory and the in-memory
  server-side session map. They are never written to disk and never logged.
- Cached JSON files contain test metadata and result rounds. They can include
  customer URLs, agent ids, and error messages. Treat the `test_results/`
  folder as sensitive — it is in `.gitignore` for that reason.
- The only outbound destination is `https://api.thousandeyes.com/v7`.
- The dashboard has no per-user auth beyond "you have the bearer token". **Run it on your own laptop, not on a shared host**.

---

## Limitations / known gaps

- Page-load *errors* are detected as "missing `pageLoadTime`". Slow-but-
  completing loads will show up only in the average-time widget and the
  Top-5 table, not in the "outstanding" table.
- No alerting / notifications. The dashboard must be open in a browser.
- No per-test baseline / day-over-day comparison yet.

---

## Roadmap ideas

- Configurable timeline range (1h–7d).
- Day-over-day delta to flag tests that *got worse* vs. yesterday.
- Optional CSV / JSON export of the current outstanding list.
- Per-test sparkline of `responseTime` / `domLoadTime` for richer page-load
  triage.
- Webhook hook for pushing a Webex / Slack message when the count of
  outstanding tests crosses a threshold.

---

## Contributing

PRs and issues welcome. Please:

- run with `python -m flake8` (or your linter of choice) before pushing,
- never commit the `test_results/` folder or any real bearer tokens,
- avoid pasting customer-identifying screenshots in issues.

---

## Credits

- **Author:** Mario Dueñas (jduenasp@cisco.com)
- **Co-pilot:** GitHub Copilot (scaffolding + iteration)
- **Cisco internal use only.** Not officially supported by ThousandEyes.
