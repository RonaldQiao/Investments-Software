---
name: testing-ledger-ui
description: How to run and browser-test the local FastAPI/SQLite ledger (seed, server, Safari quirks, DB checks).
---

# Testing the ledger UI end-to-end

## Start
- `make seed && make run` (or `nohup .venv/bin/uvicorn app.main:app --port 8000 &`). Fresh seed = 8 instruments, 8 trades, 60 scheduled snapshots, LPs Principal / Investor A / Investor B / GP.
- Never `make install-agent`.

## Browser quirks (macOS box, Safari is the only browser)
- xdotool `type` drops Shift in Safari (":"→";", "@"→"2", "/"→"?"). For any text with symbols, `printf '...' | pbcopy` then `cmd+v` into the field. Plain digits/letters type fine.
- Navigate with `osascript -e 'tell application "Safari" to set URL of front document to "http://localhost:8000/…"'` (first call prompts an Automation permission dialog — click Allow). `get URL of front document` reveals the full `?error=`/`?ok=` flash query string the address bar hides.
- `read_dom`/`browser_console` don't work (Chrome-only); use zoom screenshots + sqlite3.

## Verify state directly
`sqlite3 data/ledger.db` — useful tables: trades, instruments (manual_mark/_at), nav_snapshots, snapshot_marks, lp_units, cash_flows, fee_events, settings (fee_liability, last_refresh_failures).

## Known seed caveat
The 60-day history is a random walk from cost basis while "Snapshot now" uses live Yahoo marks, so the first live snapshot can show a huge daily return (e.g. +24%) and the seed's initial 1.5M capital flows are timestamped today (Flows column). Don't mistake this for an app bug; test return sanity against a second live snapshot instead.

## Devin Secrets Needed
none
