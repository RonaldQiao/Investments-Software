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

Use `make backup` (or `POST /backup` in Settings) to create a SQLite backup in
`data/backups/`. Only the newest 20 backups are retained. GitHub Actions runs
the test suite on every push and pull request.

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
