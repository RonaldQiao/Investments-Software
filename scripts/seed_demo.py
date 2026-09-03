from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import holidays

from app.db import get_conn, init_db
from app.fees import record_cash_flow
from app.nav import take_snapshot
from app.positions import build_positions
from app.pricing import refresh_prices

NYSE_HOLIDAYS = holidays.financial_holidays("NYSE")


def trading_days(count):
    days = []
    day = datetime.now(UTC).date() - timedelta(days=1)
    while len(days) < count:
        if day.weekday() < 5 and day not in NYSE_HOLIDAYS:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def backfill_history(conn, count, history_days=None):
    if count <= 0:
        return
    trades = conn.execute(
        "SELECT t.*, i.multiplier FROM trades t JOIN instruments i ON i.id=t.instrument_id "
        "ORDER BY t.ts,t.id"
    ).fetchall()
    positions = build_positions(trades)
    marks = {
        instrument_id: position.avg_price
        for instrument_id, position in positions.items()
        if position.qty
    }
    instruments = {
        row["id"]: row
        for row in conn.execute("SELECT * FROM instruments").fetchall()
    }
    original_manual = {
        instrument_id: row["manual_mark"]
        for instrument_id, row in instruments.items()
        if row["pricing_source"] == "manual"
    }
    rng = random.Random(1)
    days = history_days or trading_days(count)
    middle = count // 2
    investor = conn.execute(
        "SELECT id FROM lps WHERE name='Investor A'"
    ).fetchone()["id"]
    investor_b = conn.execute(
        "SELECT id FROM lps WHERE name='Investor B'"
    ).fetchone()["id"]
    for index, day in enumerate(days):
        for instrument_id, mark in list(marks.items()):
            instrument = instruments[instrument_id]
            if instrument["asset_class"] in {"equity", "etf"}:
                sigma = 0.012
            elif instrument["asset_class"] == "crypto":
                sigma = 0.025
            elif instrument["asset_class"] in {"bond", "commodity"}:
                sigma = 0.008
            elif instrument["asset_class"] == "option":
                sigma = 0.03
            else:
                sigma = 0
            marks[instrument_id] = mark * (1 + rng.gauss(0, sigma))
            if instrument["pricing_source"] == "manual":
                conn.execute(
                    "UPDATE instruments SET manual_mark=?,manual_mark_at=? WHERE id=?",
                    (marks[instrument_id], day.isoformat(), instrument_id),
                )
            else:
                conn.execute(
                    "INSERT INTO prices(instrument_id,price,ts,source) VALUES (?,?,?,?) "
                    "ON CONFLICT(instrument_id) DO UPDATE SET price=excluded.price,"
                    "ts=excluded.ts,source=excluded.source",
                    (instrument_id, marks[instrument_id], day.isoformat(), "demo-history"),
                )
        conn.commit()
        if index == middle:
            record_cash_flow(
                conn,
                f"{day.isoformat()}T12:00:00",
                250_000,
                investor,
                "Demo history contribution",
            )
            record_cash_flow(
                conn,
                f"{day.isoformat()}T12:00:00",
                250_000,
                investor_b,
                "Investor B mid-history contribution",
            )
        take_snapshot(conn, day, "scheduled", refresh=False)
    for instrument_id, mark in original_manual.items():
        conn.execute(
            "UPDATE instruments SET manual_mark=?,manual_mark_at=? WHERE id=?",
            (mark, datetime.now(UTC).isoformat(), instrument_id),
        )
    conn.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=int, default=0)
    args = parser.parse_args()
    history_days = trading_days(args.history) if args.history else []
    init_db()
    conn = get_conn()
    conn.execute("DELETE FROM fee_events")
    conn.execute("DELETE FROM lp_fee_accruals")
    conn.execute("DELETE FROM nav_snapshots")
    conn.execute("DELETE FROM lp_units")
    conn.execute("DELETE FROM trades")
    conn.execute("DELETE FROM cash_flows")
    conn.execute("DELETE FROM prices")
    conn.execute("DELETE FROM instruments")
    conn.execute("DELETE FROM settings WHERE key='fee_liability'")
    conn.execute("UPDATE lps SET hwm=NULL")
    conn.execute("UPDATE settings SET value='1000' WHERE key='hwm_per_unit'")
    conn.execute("INSERT OR IGNORE INTO lps(name,is_gp) VALUES ('Investor A',0)")
    conn.execute(
        "INSERT OR IGNORE INTO lps(name,is_gp,mgmt_fee_bps,perf_fee_pct) VALUES "
        "('Investor B',0,150,15)"
    )
    conn.execute(
        "UPDATE lps SET mgmt_fee_bps=NULL,perf_fee_pct=NULL,hwm=NULL "
        "WHERE name='Investor A'"
    )
    conn.execute(
        "UPDATE lps SET mgmt_fee_bps=150,perf_fee_pct=15,hwm=NULL "
        "WHERE name='Investor B'"
    )
    conn.execute("INSERT OR IGNORE INTO lps(name,is_gp) VALUES ('GP',1)")
    conn.execute("UPDATE lps SET is_gp=0 WHERE name='Principal'")
    conn.execute("UPDATE lps SET is_gp=1 WHERE name='GP'")
    now = datetime.now(UTC).isoformat()
    specs = [
        ("AAPL", "Apple", "equity", 1, "yahoo", None, None),
        ("NVDA", "NVIDIA", "equity", 1, "yahoo", None, None),
        ("TLT", "iShares 20+ Year Treasury Bond ETF", "bond", 1, "yahoo", None, None),
        ("ES=F", "E-mini S&P 500 Future", "future", 50, "yahoo", "ES=F", None),
        ("GC=F", "Gold Future", "commodity", 100, "yahoo", "GC=F", None),
        ("BTC-USD", "Bitcoin", "crypto", 1, "yahoo", None, None),
        ("AAPL 240117C200", "AAPL Jan 2024 200 Call", "option", 100, "manual", None, 6.10),
        ("Private credit note", "Private credit note", "other", 1, "manual", None, 101500),
    ]
    ids = {}
    for symbol, name, asset_class, multiplier, source, yahoo_symbol, manual_mark in specs:
        cursor = conn.execute(
            "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source,"
            "yahoo_symbol,manual_mark,manual_mark_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                symbol,
                name,
                asset_class,
                "USD",
                multiplier,
                source,
                yahoo_symbol,
                manual_mark,
                now if manual_mark is not None else None,
            ),
        )
        ids[symbol] = cursor.lastrowid
    lp_id = conn.execute("SELECT id FROM lps WHERE name='Principal'").fetchone()["id"]
    record_cash_flow(conn, now, 1_000_000, lp_id, "Initial demo capital")
    investor_id = conn.execute("SELECT id FROM lps WHERE name='Investor A'").fetchone()["id"]
    record_cash_flow(conn, now, 500_000, investor_id, "Investor A capital")
    trades = [
        ("AAPL", "BUY", 200, 190),
        ("NVDA", "SELL", 50, 120),
        ("TLT", "BUY", 300, 92),
        ("ES=F", "BUY", 2, 5400),
        ("GC=F", "BUY", 1, 2350),
        ("BTC-USD", "BUY", 0.5, 62000),
        ("AAPL 240117C200", "BUY", 10, 4.20),
        ("Private credit note", "BUY", 1, 100000),
    ]
    trade_timestamp = (
        f"{history_days[0].isoformat()}T12:00:00" if history_days else now
    )
    for symbol, side, quantity, price in trades:
        conn.execute(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) VALUES (?,?,?,?,?,0,'Demo seed')",
            (ids[symbol], trade_timestamp, side, quantity, price),
        )
    conn.commit()
    backfill_history(conn, args.history, history_days)
    failed = asyncio.run(refresh_prices(conn))
    conn.close()
    print(f"Seeded demo book. Failed price symbols: {', '.join(failed) or 'none'}")


if __name__ == "__main__":
    main()
