from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_conn, get_setting, init_db
from app.pricing import fetch_benchmark_history


async def backfill(conn, symbol: str) -> int:
    closes = await fetch_benchmark_history(symbol, "1y")
    dates = [
        row["date"]
        for row in conn.execute("SELECT date FROM nav_snapshots ORDER BY date").fetchall()
    ]
    conn.executemany(
        "INSERT INTO benchmark_closes(symbol,date,close) VALUES (?,?,?) "
        "ON CONFLICT(symbol,date) DO UPDATE SET close=excluded.close",
        [(symbol, snapshot_date, closes.get(snapshot_date)) for snapshot_date in dates],
    )
    conn.commit()
    return sum(closes.get(snapshot_date) is not None for snapshot_date in dates)


def main():
    init_db()
    conn = get_conn()
    symbol = str(get_setting(conn, "benchmark_symbol", "") or "").strip().upper()
    if not symbol:
        conn.close()
        return
    try:
        count = asyncio.run(backfill(conn, symbol))
    except (RuntimeError, OSError):
        conn.close()
        return
    conn.close()
    print(f"Backfilled {count} {symbol} closes")


if __name__ == "__main__":
    main()
