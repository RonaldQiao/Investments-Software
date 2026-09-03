from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime

import httpx

from .db import set_setting


def occ_symbol(underlying, expiry, option_type, strike):
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    elif not isinstance(expiry, date):
        expiry = date.fromisoformat(str(expiry))
    option_type = str(option_type).upper()
    if option_type not in {"C", "P"}:
        raise ValueError("option_type must be C or P")
    return (
        f"{str(underlying).upper()}{expiry:%y%m%d}{option_type}"
        f"{round(float(strike) * 1000):08d}"
    )


def yahoo_symbol_for(instrument):
    explicit = instrument["yahoo_symbol"]
    if explicit:
        return explicit
    if (
        instrument["asset_class"] == "option"
        and instrument["underlying"]
        and instrument["expiry"]
        and instrument["option_type"]
        and instrument["strike"] is not None
    ):
        return occ_symbol(
            instrument["underlying"],
            instrument["expiry"],
            instrument["option_type"],
            instrument["strike"],
        )
    return instrument["symbol"]


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


async def fetch_benchmark_history(symbol: str, range_: str = "5d") -> dict[str, float | None]:
    if not symbol:
        return {}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=10.0, follow_redirects=True
        ) as client:
            response = await client.get(
                f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"range": range_, "interval": "1d"},
            )
            response.raise_for_status()
            result = response.json().get("chart", {}).get("result") or []
            if not result:
                return {}
            timestamps = result[0].get("timestamp") or []
            quote = (result[0].get("indicators", {}).get("quote") or [{}])[0]
            closes = quote.get("close") or []
            return {
                datetime.fromtimestamp(timestamp, UTC).date().isoformat(): (
                    float(close) if close is not None else None
                )
                for timestamp, close in zip(timestamps, closes)
            }
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, OSError):
        return {}


async def fetch_benchmark_close(symbol: str, target_date: date) -> float | None:
    return (await fetch_benchmark_history(symbol, "5d")).get(target_date.isoformat())


async def refresh_prices(conn) -> list[str]:
    rows = conn.execute(
        "SELECT id,symbol,yahoo_symbol,asset_class,underlying,expiry,strike,option_type "
        "FROM instruments WHERE pricing_source='yahoo'"
    ).fetchall()
    symbols = [yahoo_symbol_for(row) for row in rows]
    quotes = await fetch_yahoo(symbols) if symbols else {}
    now = datetime.now(UTC).isoformat()
    failed = []
    for row in rows:
        symbol = yahoo_symbol_for(row)
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
    set_setting(conn, "last_refresh_failures", json.dumps(failed))
    set_setting(conn, "last_refresh_at", now)
    conn.commit()
    return failed


def mark_for(instrument, price_row) -> float | None:
    source = instrument["pricing_source"] if hasattr(instrument, "keys") else instrument.get("pricing_source")
    manual = instrument["manual_mark"] if hasattr(instrument, "keys") else instrument.get("manual_mark")
    if source == "manual" or price_row is None:
        return float(manual) if manual is not None else None
    return float(price_row["price"] if hasattr(price_row, "keys") else price_row.get("price"))
