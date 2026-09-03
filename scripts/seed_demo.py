from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import get_conn, init_db
from app.fees import record_cash_flow
from app.pricing import refresh_prices


def main():
    init_db()
    conn = get_conn()
    conn.execute("DELETE FROM trades")
    conn.execute("DELETE FROM cash_flows")
    conn.execute("DELETE FROM prices")
    conn.execute("DELETE FROM instruments")
    conn.execute("INSERT OR IGNORE INTO lps(name,is_gp) VALUES ('Investor A',0)")
    conn.execute("INSERT OR IGNORE INTO lps(name,is_gp) VALUES ('GP',1)")
    conn.execute("UPDATE lps SET is_gp=0 WHERE name='Principal'")
    conn.execute("UPDATE lps SET is_gp=1 WHERE name='GP'")
    now = datetime.now(timezone.utc).isoformat()
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
    for symbol, side, quantity, price in trades:
        conn.execute(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) VALUES (?,?,?,?,?,0,'Demo seed')",
            (ids[symbol], now, side, quantity, price),
        )
    conn.commit()
    failed = asyncio.run(refresh_prices(conn))
    conn.close()
    print(f"Seeded demo book. Failed price symbols: {', '.join(failed) or 'none'}")


if __name__ == "__main__":
    main()
