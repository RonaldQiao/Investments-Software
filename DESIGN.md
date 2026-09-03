# Ledger — design

Single-user, local-first portfolio tracker. One Python process, one SQLite file,
server-rendered HTML with a few hundred lines of vanilla JS. No build step.

## Stack

- Python 3.12, FastAPI + uvicorn, Jinja2 templates, stdlib `sqlite3` (WAL mode).
- `httpx` for prices (Yahoo Finance chart endpoint, no yfinance dependency).
- APScheduler for the 16:00 America/New_York end-of-day NAV snapshot.
- `holidays` (NYSE calendar) to skip market holidays.
- pytest for the accounting engine.

## Layout

```
app/
  main.py        FastAPI app: page routes + JSON API, startup hooks
  db.py          connection, schema (idempotent CREATE), helpers
  positions.py   trades -> open positions (net qty, avg cost, realized P&L)
  pricing.py     Yahoo fetch (batched, async), manual marks, `prices` cache
  nav.py         NAV, exposure, snapshots, daily return series, CSV export
  fees.py        units-based LP ledger, mgmt fee accrual, perf fee w/ HWM
  attribution.py P&L attribution from audited snapshot marks and trades
  scheduler.py   EOD job (16:00 NY, weekdays, non-holiday), missed-run catch-up
  templates/     base, dashboard, positions, trades, capital, fees, history, settings
  static/        style.css, app.js
tests/
data/ledger.db   (gitignored)
```

## Data model

```
instruments   id, symbol, name, asset_class, currency, multiplier (1; 100 options; 50 ES ...),
              pricing_source ('yahoo'|'manual'), yahoo_symbol, manual_mark,
              manual_mark_at, notes
              asset_class ∈ equity, etf, bond, credit, option, future, commodity, crypto, fx, other
trades        id, instrument_id, ts, side ('BUY'|'SELL'), quantity, price, fees, notes
prices        instrument_id PK, price, ts, source          (latest mark only)
cash_flows    id, ts, amount (+contribution / -withdrawal), lp_id, note
lps           id, name, is_gp
lp_units      id, lp_id, ts, units (+/-), nav_per_unit, cash_flow_id
nav_snapshots date PK, ts, nav, cash, gross_long, gross_short, net_exposure,
              flows_today, units_outstanding, nav_per_unit, daily_return,
              levered_return, mgmt_fee_accrued, source ('scheduled'|'manual')
snapshot_marks date, instrument_id, mark, source ('yahoo'|'manual'|'snapshot'|'fallback')
fee_events    id, ts, kind ('mgmt'|'perf'|'settle'), amount, hwm_before, hwm_after, note
job_log       id, ts, job ('scheduled'|'catch-up'), status ('ok'|'partial'|'failed'),
              detail (failed symbols or error text)
settings      key PK, value   (leverage, borrow_rate, fund_name, mgmt_fee_bps,
                               perf_fee_pct, hwm_per_unit, inception_nav_per_unit=1000,
                               last_refresh_failures, last_refresh_at)
```

## Accounting rules

- **Positions are derived from trades.** Signed quantity = Σ(BUY qty) − Σ(SELL qty).
  Positive = long, negative = short. Average cost is the weighted average of the
  trades that opened/increased the position; trades that reduce it realize P&L
  against that average and leave it unchanged. Crossing through zero closes the
  old position and opens a new one at the crossing trade's price.
  A "Set position" shortcut on the Positions page generates the adjusting trade.
- **Cash** = Σ cash_flows + Σ(SELL proceeds) − Σ(BUY cost) − Σ fees, all scaled by
  multiplier. Shorts add proceeds to cash, so NAV is correct without a margin model.
- **Mark** = manual_mark for manual instruments, else the latest fetched Yahoo price.
  A failed or missing Yahoo mark is shown as unavailable in the live portfolio and
  never silently contributes zero to a snapshot. Snapshots audit each mark in
  `snapshot_marks`; failed/missing marks use the prior snapshot's mark, or
  `avg_price` with source `fallback` when no prior mark exists. Manual instruments
  are never touched by the fetcher.
- **NAV** = cash + Σ signed_qty × mark × multiplier.
  Gross long / gross short / net exposure are reported alongside.
- **Units.** Inception NAV/unit = 1000. Every cash flow issues (or redeems) units at
  the current NAV/unit, credited to an LP ("Principal" by default). Because NAV/unit
  is unaffected by flows, daily return = NAV/unit_t / NAV/unit_{t−1} − 1. This is the
  headline return series and is exactly time-weighted.
- **Leverage slider** (1.0×–5.0×, settings). Reported *levered return* =
  L·r − (L−1)·borrow_rate/252. It also sets the target gross exposure (L × NAV)
  shown on the dashboard. It does not change cash or positions.
- **Management fee** (default 2%/yr) accrues each snapshot day: NAV × bps/10000/252,
  reducing NAV/unit; recorded as a fee_event owed to the GP.
- **Performance fee** (default 20%) with a fund-level high-water mark on NAV/unit.
  Crystallized on demand from the Fees page (or annually): fee = 20% × max(0,
  NAV/unit − HWM) × units; HWM ← NAV/unit after. Per-LP HWM is a documented
  simplification; ownership table shows units, % and value per LP.
- **EOD snapshot** runs 16:00 America/New_York on NYSE trading days: refresh
  Yahoo marks, compute NAV, accrue mgmt fee, write nav_snapshots row (idempotent per
  date). On startup, if the last trading day has no snapshot and it is past 16:00,
  run a catch-up snapshot using the latest available closes. A "Snapshot now" button
  writes a manual snapshot for today.
- **Export**: `/history.csv` — the nav_snapshots table.
- **P&L attribution.** `/pnl` compares consecutive audited `snapshot_marks`.
  For each date and instrument it marks the position quantity at the end of the
  Eastern date against the prior mark, then adds realized P&L from trades
  executed that date. The page and `/pnl.csv` aggregate by instrument and asset
  class. Missing audited marks are omitted rather than fetched during
  attribution.
- **Job audit.** Scheduled and startup catch-up refreshes retry failed symbols
  up to three attempts, then snapshot using the normal audited fallback logic.
  Each run is recorded in `job_log`; this table is operational metadata and
  does not affect accounting.
- **Backups.** `make backup` and Settings use SQLite's online `Connection.backup`
  API to write timestamped files under `data/backups/`, retaining the newest 20.

## Aesthetics

The interface follows paradigm.xyz's type system: white background, black text,
muted `#666` and faint `#999` text, `#ccc` controls, `#ddd`/`#eee` table rules,
`#f6f6f6` table headers/totals, and `#e04545` for negative values. No other
colors, cards, shadows, gradients, icons, or rounded corners are used.

Serif copy and headings use `Georgia, "Times New Roman", serif`; headings are
400 weight with `letter-spacing: -0.03em`, and body copy is 16px with 1.25
line-height. Numbers, labels, table cells, controls, buttons, inputs, and meta
lines use `"SFMono-Regular", Menlo, Monaco, Consolas, monospace` at 12px with
tabular number features. Labels and table headers are 10px uppercase mono.

The desktop gutter is 36px with a fluid content width. Navigation uses a mono
uppercase wordmark and 16px serif links with 24px gaps. Headline strips use
black top/bottom rules, 24px vertical padding, 36px column gaps, and 28px
serif display values. Section headings have a black top rule and 16px padding.
Tables use black header rules, `#eee` row rules, 10px by 8px cell padding, and
`#f6f6f6` row hover. Buttons are 30px sentence-case mono controls with black
borders and black hover fill. Focus-visible controls use a black outline.

Every page is server-rendered; JavaScript is only for inline edits and the
leverage slider. Keep CSS under 8 KB, with no external fonts or CDN assets.
