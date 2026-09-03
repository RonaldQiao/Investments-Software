# Ledger

Ledger is a local-first, single-user portfolio tracker. It stores trades,
prices, NAV snapshots, capital flows, LP units, and fund-level fees in SQLite.
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

Contributions and withdrawals issue or redeem LP units at the current NAV/unit,
so capital flows do not change the time-weighted return series. Positions are
derived from trades using average cost; see [DESIGN.md](DESIGN.md) for the
complete accounting rules.

## Limitations

- Single-user, local SQLite application.
- Performance fees use a fund-level high-water mark.
- No FX conversion; values are treated as one currency.
