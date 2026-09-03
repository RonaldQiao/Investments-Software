from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from .db import get_conn


async def fetch_yahoo(symbols: list[str]) -> dict[str, float | None]:
    semaphore = asyncio.Semaphore(8)
    headers = {"User-Agent": "Mozilla/5.0"}

    async def fetch(client: httpx.AsyncClient, symbol: str):
        async with semaphore:
            try:
                response = await client.get(
                    f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
                    params={"range": "1d", "interval": "1d"},
                )
                response.raise_for_status()
                result = response.json().get("chart", {}).get("result") or []
                price = result[0].get("meta", {}).get("regularMarketPrice")
                return symbol, float(price) if price is not None else None
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                return symbol, None

    async with httpx.AsyncClient(
        headers=headers, timeout=10.0, follow_redirects=True
    ) as client:
        results = await asyncio.gather(*(fetch(client, symbol) for symbol in symbols))
    return dict(results)


async def refresh_prices(conn) -> list[str]:
    rows = conn.execute(
        "SELECT id,symbol,yahoo_symbol FROM instruments WHERE pricing_source='yahoo'"
    ).fetchall()
    symbols = [row["yahoo_symbol"] or row["symbol"] for row in rows]
    quotes = await fetch_yahoo(symbols) if symbols else {}
    now = datetime.now(timezone.utc).isoformat()
    failed = []
    for row in rows:
        symbol = row["yahoo_symbol"] or row["symbol"]
        price = quotes.get(symbol)
        if price is None:
            failed.append(symbol)
            continue
        conn.execute(
            "INSERT INTO prices(instrument_id,price,ts,source) VALUES (?,?,?,'yahoo') "
            "ON CONFLICT(instrument_id) DO UPDATE SET price=excluded.price,"
            "ts=excluded.ts,source=excluded.source",
            (row["id"], price, now),
        )
    conn.commit()
    return failed


def mark_for(instrument, price_row) -> float | None:
    source = instrument["pricing_source"] if hasattr(instrument, "keys") else instrument.get("pricing_source")
    manual = instrument["manual_mark"] if hasattr(instrument, "keys") else instrument.get("manual_mark")
    if source == "manual" or price_row is None:
        return float(manual) if manual is not None else None
    return float(price_row["price"] if hasattr(price_row, "keys") else price_row.get("price"))
