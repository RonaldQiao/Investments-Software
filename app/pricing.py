from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

import httpx

from .db import get_setting, set_setting
from .fx import fx_rate_for

YAHOO_INSTRUMENT_TYPES = {
    "EQUITY": "equity",
    "ETF": "etf",
    "MUTUALFUND": "etf",
    "FUTURE": "future",
    "CRYPTOCURRENCY": "crypto",
    "CURRENCY": "fx",
    "OPTION": "option",
    "INDEX": "other",
}


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


async def fetch_yahoo_meta(symbol: str) -> dict | None:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=10.0, follow_redirects=True
        ) as client:
            response = await client.get(
                f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"range": "1d", "interval": "1d"},
            )
            response.raise_for_status()
            result = response.json().get("chart", {}).get("result") or []
            if not result:
                return None
            meta = result[0]["meta"]
            yahoo_symbol = meta["symbol"]
            if not yahoo_symbol:
                return None
            price = meta.get("regularMarketPrice")
            raw_currency = str(meta.get("currency") or "USD").strip()
            if raw_currency == "GBp":
                currency, price_scale = "GBP", 0.01
            elif raw_currency == "ZAc":
                currency, price_scale = "ZAR", 0.01
            elif raw_currency == "ILA":
                currency, price_scale = "ILS", 0.01
            else:
                currency, price_scale = raw_currency.upper(), 1
            return {
                "symbol": yahoo_symbol,
                "name": meta.get("shortName") or meta.get("longName") or "",
                "asset_class": YAHOO_INSTRUMENT_TYPES.get(
                    meta.get("instrumentType"), "other"
                ),
                "currency": currency,
                "price": float(price) if price is not None else None,
                "price_scale": price_scale,
            }
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return None


async def fetch_fx_rates(
    currencies: list[str], base: str
) -> dict[str, float | None]:
    base = str(base or "USD").strip().upper()
    currencies = sorted(
        {str(currency).strip().upper() for currency in currencies if currency}
        - {base}
    )
    if not currencies:
        return {}
    direct_symbols = {currency: f"{currency}{base}=X" for currency in currencies}
    direct = await fetch_yahoo(list(direct_symbols.values()))
    missing = [currency for currency in currencies if direct.get(direct_symbols[currency]) is None]
    inverse_symbols = {currency: f"{base}{currency}=X" for currency in missing}
    inverse = await fetch_yahoo(list(inverse_symbols.values())) if inverse_symbols else {}
    rates = {}
    for currency in currencies:
        direct_rate = direct.get(direct_symbols[currency])
        if direct_rate is not None:
            rates[currency] = float(direct_rate)
            continue
        inverse_rate = inverse.get(inverse_symbols.get(currency))
        rates[currency] = (
            1 / float(inverse_rate)
            if inverse_rate not in (None, 0)
            else None
        )
    return rates


async def fetch_benchmark_history(symbol: str, range_: str = "5d") -> dict[str, float | None]:
    if not symbol:
        return {}
    return await _fetch_benchmark_chart(
        symbol,
        {"range": range_, "interval": "1d"},
    )


async def _fetch_benchmark_chart(
    symbol: str, params: dict[str, str | int]
) -> dict[str, float | None]:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=10.0, follow_redirects=True
        ) as client:
            response = await client.get(
                f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
                params=params,
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


async def fetch_benchmark_closes(
    symbol: str, start: date, end: date
) -> dict[str, float | None]:
    if not symbol:
        return {}
    period1 = int(datetime.combine(start, datetime.min.time(), UTC).timestamp())
    period2 = int(
        datetime.combine(end + timedelta(days=2), datetime.min.time(), UTC).timestamp()
    )
    return await _fetch_benchmark_chart(
        symbol,
        {"period1": period1, "period2": period2, "interval": "1d"},
    )


async def fetch_benchmark_close(symbol: str, target_date: date) -> float | None:
    return (await fetch_benchmark_history(symbol, "5d")).get(target_date.isoformat())


def _upsert_price(conn, instrument_id: int, price: float, now: str) -> None:
    conn.execute(
        "INSERT INTO prices(instrument_id,price,ts,source) VALUES (?,?,?,'yahoo') "
        "ON CONFLICT(instrument_id) DO UPDATE SET price=excluded.price,"
        "ts=excluded.ts,source=excluded.source",
        (instrument_id, price, now),
    )


async def price_instrument(conn, instrument_id: int) -> float | None:
    row = conn.execute(
        "SELECT id,symbol,yahoo_symbol,asset_class,underlying,expiry,strike,option_type,"
        "pricing_source,price_scale FROM instruments WHERE id=?",
        (instrument_id,),
    ).fetchone()
    if row is None or row["pricing_source"] != "yahoo":
        return None
    symbol = yahoo_symbol_for(row)
    price = (await fetch_yahoo([symbol])).get(symbol)
    if price is None:
        return None
    scaled_price = price * float(row["price_scale"] or 1)
    _upsert_price(
        conn,
        instrument_id,
        scaled_price,
        datetime.now(UTC).isoformat(),
    )
    conn.commit()
    return scaled_price


async def refresh_fx_rate(conn, currency: str) -> float | None:
    currency = str(currency or "").strip().upper()
    base = str(get_setting(conn, "base_currency", "USD") or "USD").strip().upper()
    existing = fx_rate_for(conn, currency)
    if currency == base or existing is not None:
        return existing
    rate = (await fetch_fx_rates([currency], base)).get(currency)
    if rate is not None:
        conn.execute(
            "INSERT INTO fx_rates(currency,rate,ts,source) VALUES (?,?,?,'yahoo') "
            "ON CONFLICT(currency) DO UPDATE SET rate=excluded.rate,ts=excluded.ts,"
            "source=excluded.source",
            (currency, rate, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    return rate


async def refresh_prices(conn) -> list[str]:
    rows = conn.execute(
        "SELECT id,symbol,yahoo_symbol,asset_class,underlying,expiry,strike,option_type,"
        "price_scale "
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
        _upsert_price(conn, row["id"], price * float(row["price_scale"] or 1), now)
    base = str(get_setting(conn, "base_currency", "USD") or "USD").strip().upper()
    currencies = []
    for row in conn.execute(
        "SELECT DISTINCT currency FROM instruments "
        "WHERE UPPER(currency) != ?",
        (base,),
    ).fetchall():
        currency = str(row["currency"]).upper()
        existing = conn.execute(
            "SELECT source FROM fx_rates WHERE currency=?", (currency,)
        ).fetchone()
        if existing is None or existing["source"] != "manual":
            currencies.append(currency)
    rates = await fetch_fx_rates(currencies, base) if currencies else {}
    for currency, rate in rates.items():
        if rate is not None:
            conn.execute(
                "INSERT INTO fx_rates(currency,rate,ts,source) VALUES (?,?,?,'yahoo') "
                "ON CONFLICT(currency) DO UPDATE SET rate=excluded.rate,ts=excluded.ts,"
                "source=excluded.source",
                (currency, rate, now),
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
