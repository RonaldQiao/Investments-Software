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
fee_events    id, ts, kind ('mgmt'|'perf'), amount, hwm_before, hwm_after, note
settings      key PK, value   (leverage, borrow_rate, fund_name, mgmt_fee_bps,
                               perf_fee_pct, hwm_per_unit, inception_nav_per_unit=1000)
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
- **Mark** = manual_mark if pricing_source = manual (or if no fetched price exists),
  else latest fetched price. Manual instruments are never touched by the fetcher.
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

## Aesthetics

White, near-black text, 1px #e5e5e5 rules, no cards/shadows/radii/gradients/icons.
System sans with tabular numerals; 11px uppercase letter-spaced labels; dense
tables. Top nav is plain text links. Every page is server-rendered; JS is only for
inline edits and the leverage slider. Target: < 20 KB total CSS+JS, TTFB < 20 ms.
