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
- Safari caches `app.js`; after restarting uvicorn do one hard reload (Cmd+Opt+R; confirm the "reload this page?" dialog if form fields are dirty).
- The `/positions` Add-instrument form shifts vertically when a flash message appears/disappears and when the lookup status ("Yahoo · P USD" / "not on Yahoo") renders under Symbol. Always take a fresh screenshot before clicking fields/buttons on that form, or typing lands in the wrong input.
- Yahoo lookup on the Add form fires on Symbol blur/change; only Name/Yahoo symbol (if empty) and Class/Currency (if not touched) get filled. Prices differ by a few cents between the lookup, the creation-time `prices` row, and the snapshot — compute unrealized/P&L against the specific mark shown/stored, not the lookup number.

## Verify state directly
`sqlite3 data/ledger.db` — useful tables: trades, instruments (manual_mark/_at), nav_snapshots, snapshot_marks, lp_units, cash_flows, fee_events, settings (fee_liability, last_refresh_failures).

## Known seed caveat
Older seeds random-walked history from cost basis while "Snapshot now" uses live Yahoo marks, so the first live snapshot could show a huge daily return (e.g. +24%). Since commit 3120aef the seed anchors history to live prices and the first live snapshot's daily return is ~0%; if you see a huge jump, check `git log` for the seed version before reporting a bug.

## Handy checks for Add-instrument flow
- Atomicity: `select count(*) from instruments; select count(*) from trades;` before/after negative cases.
- New Yahoo instrument: `select p.price,p.source from prices p join instruments i on i.id=p.instrument_id where i.symbol='X'` should return a row immediately; manual instruments should return none.
- NAV delta for an opening trade at avg A with mark P: LONG `q×(P−A)−fees`, SHORT `q×(A−P)−fees`; cash moves by `∓q×A−fees`.

## Multiple funds
- Fund selection is cookie `fund=<slug>`; default `ledger` → data/ledger.db, others data/funds/<slug>.db. Header <select> appears only when >1 fund. For non-UI status checks: `curl -b fund=<slug> localhost:8000/<page>`.
- xdotool `type` also drops Shift for capital letters in Safari ("Fund II" → "fund ii"); paste names via pbcopy+cmd+v when casing matters.
- Safari file picker: click "Choose File", Cmd+Shift+G, paste the absolute path (pbcopy), Return, Return.
- `/funds/switch` redirects to the referer including any `?ok=`/`?error=` flash, so a stale flash from the previous fund can reappear after switching.
- Safari window: `osascript -e 'tell application "Safari" to set bounds of front window to {0, 25, 1600, 1200}'` (real screen 1600×1200; screenshot coords are 1024×768).

## Track record import
- CSV `date,nav,flow`; navpu chain `navpu[i]=navpu[i-1]*(nav[i]-flow[i])/nav[i-1]`. Fresh fund: sets `settings.inception_nav_per_unit` to the endpoint; fund with live rows: scaled so last imported navpu == first live navpu.
- A fresh fund needs capital (/capital contribution) BEFORE "Snapshot now"; with only a position and no LP units, NAV ≈ 0, units stay 0 and the live row shows NAV/unit 0.00 / −100%.

## Devin Secrets Needed
none

## FX / multi-currency testing notes
- Create a fresh fund via `curl -X POST -d name=FX localhost:8000/funds` (DB at data/funds/fx.db) and switch Safari to it with the header select; add capital first so NAV is meaningful.
- Probe Yahoo currency/FX quickly with `curl "localhost:8000/api/lookup?symbol=SAP.DE"` (also stores the fx_rates row). LSE symbols (VOD.L) return currency `GBp` with pence prices — the Add form maps this to "other…"/GBP; check MV scale.
- Check `trades.fx_rate`, `fx_rates`, `snapshot_marks.fx_rate`, `settings.base_currency`. Realized P&L is not shown in any page; compute with `app.positions.build_positions` over `select t.*,i.multiplier from trades t join instruments i ...`.
- Attribution on /pnl needs ≥2 snapshots; to test within one day, backdate the first snapshot AND the trades/cash_flows (`update ... set ts=replace(ts,'<today>','<yesterday>')`) or the identity Σattr == NAV Δ − flows will not hold. NAV Δ must be taken gross of mgmt_fee_accrued.
- In the Settings FX form the "Set manual FX" row shifts down when a "use Yahoo" button appears — re-screenshot before clicking. Typing a value into a `type=number` field that was autofilled appends (e.g. "1"+"0" = "10"); select-all first.
