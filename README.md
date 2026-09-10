# Ledger

Ledger is a local-first, single-user portfolio tracker. It stores trades,
prices, NAV snapshots, capital flows, LP units, and per-LP fees in SQLite.
The accounting rules are specified in [DESIGN.md](DESIGN.md).

## Run

```sh
make setup
make seed
make run
make test
```

Requires Python 3.11+ (`brew install python@3.12`). `make setup` picks the
first of `python3.12`, `python3.11`, `python3` on your PATH; override with
`make setup SYSTEM_PYTHON=/path/to/python3`.

`make run` serves the application at <http://127.0.0.1:8000>. Yahoo marks use
the symbol stored on each instrument. Futures use symbols such as `ES=F`,
crypto uses `BTC-USD`, and options are manual instruments.

At 16:00 America/New_York on NYSE trading days, the scheduler refreshes prices
and writes an idempotent EOD snapshot. On startup, it catches up the latest
eligible trading day when its snapshot is missing. The History page exports
the snapshot series as CSV.

The P&L page (`/pnl`) attributes daily mark and realized P&L by instrument and
asset class, with a CSV export. The History page can compare daily returns with
the configurable benchmark (SPY by default), including excess return and beta;
clear the benchmark symbol in Settings to disable it. Capital rows link to LP statements showing
units, value, flows, fee terms, high-water mark, fees paid, and simple return
on net contributions. Money-weighted return is intentionally out of scope.

The Update page (`/update`) is the daily workflow: review marks, enter a
one-line-per-trade blotter, refresh prices, and write a snapshot. Blotter lines
use `BUY 100 AAPL @ 189.5` or `SELL 2 ES=F @ 5400 fee 4.5`; `SHORT` is an alias
for `SELL` and `COVER` is an alias for `BUY`. Contract instruments may include
an underlying, expiry, strike, and call/put type. Yahoo options without an
explicit symbol use OCC symbols such as `AAPL261218C00200000`.

The Positions Add instrument form supports optional LONG/SHORT opening fields
(quantity, average price, and fees); Yahoo instruments are priced immediately on
creation, and `/api/lookup?symbol=NBIS` provides Yahoo symbol metadata and a
current price for autofill.

## Currencies

Settings defines the three-letter accounting base currency (USD by default). Foreign-currency positions use Yahoo FX rates or a manual override; each trade records the base-per-local `fx_rate` used for its cash and P&L accounting.

## Multiple funds

Each fund is a separate SQLite file. Use the header selector or Settings →
New fund to create and switch funds; `LEDGER_DB` still points to the default
fund, and `python -m app.snapshot --fund x` targets one fund explicitly.

## Track record import

History accepts CSV files with `date,nav,flow` columns; dates may use
`YYYY-MM-DD` or `M/D/YY`, and currency-formatted NAV values are supported.
Flow is capital added (+) or withdrawn (−) during the period ending on that
date. Imported NAV/unit history starts at the configured inception level and
scales to the first live snapshot when one exists; otherwise its final level
becomes the next live inception level.

Use `make backup` (or `POST /backup` in Settings) to create a SQLite backup in
`data/backups/`. Only the newest 20 backups are retained. GitHub Actions runs
the test suite on every push and pull request.

## Running unattended

The snapshot CLI writes a snapshot through the same retry and job-log path as
the scheduler:

```sh
python -m app.snapshot [--date YYYY-MM-DD] [--catch-up]
```

`--catch-up` is safe to run hourly: it is a no-op before 16:00 New York time
and when today's trading-day snapshot already exists. To install the macOS
launchd agents for the server and snapshot CLI, run `make install-agent`; remove
them with `make uninstall-agent`. The snapshot agent runs at minute 5 of every
local hour on weekdays because launchd uses the machine's local timezone. The
CLI's New York time gate ensures that only the 16:05 ET-or-later invocation
writes the daily snapshot.

`make backfill-benchmark` downloads one year of benchmark closes for existing
snapshot dates. Demo history seeding performs the same backfill when online and
continues silently when Yahoo is unavailable.

Contributions and withdrawals issue or redeem LP units at the current NAV/unit,
so capital flows do not change the time-weighted return series. Each LP may
override the default management fee (basis points) and performance fee
(percent); a GP LP is excluded from both. Performance fees redeem the paying
LP's units and issue equivalent GP units, leaving NAV/unit unchanged. Each LP
has its own NAV/unit high-water mark, initialized on first contribution.
Positions are derived from trades using average cost; see
[DESIGN.md](DESIGN.md) for the complete accounting rules.

## Limitations

- Single-user, local SQLite application.
- Performance fees use per-LP high-water marks and fee terms.
- No FX conversion; values are treated as one currency.
