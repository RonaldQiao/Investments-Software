from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..db import get_conn, get_setting
from ..fx import fx_rate_for, trade_fx_rate
from ..nav import compute_portfolio, history_series
from ..pricing import (
    fetch_fx_rates,
    fetch_yahoo_meta,
    price_instrument,
    refresh_fx_rate,
    refresh_prices,
)
from ..web import row_dicts

router = APIRouter()


def _currency_value(currency, other=""):
    value = str(other if currency == "other" else currency).strip().upper()
    if len(value) != 3 or not value.isalpha():
        raise ValueError("Enter a 3-letter currency code")
    return value


def _positive_fx(value):
    if value in (None, ""):
        return None
    result = float(value)
    if result <= 0:
        raise ValueError("FX rate must be positive")
    return result


@router.get("/api/lookup")
async def instrument_lookup(symbol: str = ""):
    symbol = symbol.strip().upper()
    if not symbol:
        return JSONResponse({"error": "symbol required"}, status_code=400)
    result = await fetch_yahoo_meta(symbol)
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    conn = get_conn()
    base = str(get_setting(conn, "base_currency", "USD") or "USD").upper()
    currency = str(result.get("currency") or base).upper()
    rate = fx_rate_for(conn, currency)
    if rate is None:
        rate = (await fetch_fx_rates([currency], base)).get(currency)
    if rate is not None and currency != base:
        conn.execute(
            "INSERT INTO fx_rates(currency,rate,ts,source) VALUES (?,?,?,'yahoo') "
            "ON CONFLICT(currency) DO UPDATE SET rate=excluded.rate,ts=excluded.ts,"
            "source=excluded.source",
            (currency, rate, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    conn.close()
    result["fx_rate"] = 1.0 if currency == base else rate
    return result


@router.get("/api/portfolio")
def portfolio_api():
    conn = get_conn()
    result = compute_portfolio(conn)
    conn.close()
    return result


@router.get("/api/history")
def history_api():
    conn = get_conn()
    result = history_series(conn)
    conn.close()
    return result


@router.post("/api/prices/refresh")
async def prices_refresh_api():
    conn = get_conn()
    failed = await refresh_prices(conn)
    result = compute_portfolio(conn)
    conn.close()
    return {"failed": failed, "portfolio": result}


@router.get("/api/instruments")
def instruments_api():
    conn = get_conn()
    rows = row_dicts(conn.execute("SELECT * FROM instruments ORDER BY symbol"))
    conn.close()
    return rows


@router.post("/api/instruments")
async def instruments_create_api(request: Request):
    payload = await request.json()
    side = str(payload.get("side", "LONG")).strip().upper()
    try:
        currency = _currency_value(payload.get("currency", "USD"), payload.get("currency_other", ""))
        provided_fx = _positive_fx(payload.get("fx_rate"))
        price_scale = float(payload.get("price_scale", 1) or 1)
        if price_scale <= 0:
            raise ValueError("Price scale must be positive")
        quantity = float(payload["quantity"]) if payload.get("quantity") not in (None, "") else None
        avg_price = (
            float(payload["avg_price"]) if payload.get("avg_price") not in (None, "") else None
        )
        fees = float(payload.get("fees", 0) or 0)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if side not in {"LONG", "SHORT"}:
        return JSONResponse({"error": "invalid side"}, status_code=400)
    if quantity is not None and quantity <= 0:
        return JSONResponse({"error": "quantity must be positive"}, status_code=400)
    if quantity is not None and avg_price is None:
        return JSONResponse({"error": "avg_price required with quantity"}, status_code=400)
    if avg_price is not None and avg_price < 0:
        return JSONResponse({"error": "avg_price cannot be negative"}, status_code=400)
    conn = get_conn()
    base = str(get_setting(conn, "base_currency", "USD")).upper()
    if currency != base:
        await refresh_fx_rate(conn, currency)
        if quantity is not None and provided_fx is None and fx_rate_for(conn, currency) is None:
            conn.close()
            return JSONResponse(
                {"error": f"No FX rate for {currency} — enter one"}, status_code=400
            )
    manual_mark = payload.get("manual_mark")
    manual_mark_at = datetime.now(UTC).isoformat() if manual_mark not in (None, "") else None
    fields = (
        payload.get("symbol", "").strip().upper(),
        payload.get("name", "").strip(),
        payload.get("asset_class", "other"),
        currency,
        float(payload.get("multiplier", 1)),
        payload.get("pricing_source", "yahoo"),
        payload.get("yahoo_symbol") or None,
        manual_mark,
        manual_mark_at,
        price_scale,
        payload.get("notes", ""),
        payload.get("underlying") or None,
        payload.get("expiry") or None,
        payload.get("strike"),
        payload.get("option_type") or None,
    )
    cursor = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source,"
        "yahoo_symbol,manual_mark,manual_mark_at,price_scale,notes,underlying,expiry,"
        "strike,option_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        fields,
    )
    if quantity is not None:
        conn.execute(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,fx_rate,notes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                cursor.lastrowid,
                datetime.now(UTC).isoformat(),
                "BUY" if side == "LONG" else "SELL",
                quantity,
                avg_price,
                fees,
                trade_fx_rate(conn, cursor.lastrowid, provided_fx),
                "Opening position",
            ),
        )
    conn.commit()
    price = await price_instrument(conn, cursor.lastrowid)
    row = conn.execute("SELECT * FROM instruments WHERE id=?", (cursor.lastrowid,)).fetchone()
    conn.close()
    result = dict(row)
    result["price"] = price
    return result


@router.delete("/api/instruments/{instrument_id}")
def instruments_delete_api(instrument_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM instruments WHERE id=?", (instrument_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.put("/api/instruments/{instrument_id}")
@router.patch("/api/instruments/{instrument_id}")
async def instruments_update_api(instrument_id: int, request: Request):
    payload = await request.json()
    allowed = {
        key: payload[key]
        for key in (
            "symbol",
            "name",
            "asset_class",
            "currency",
            "multiplier",
            "pricing_source",
            "yahoo_symbol",
            "price_scale",
            "manual_mark",
            "manual_mark_at",
            "notes",
            "underlying",
            "expiry",
            "strike",
            "option_type",
        )
        if key in payload
    }
    if not allowed:
        return JSONResponse({"error": "no fields supplied"}, status_code=400)
    if "currency" in allowed:
        try:
            allowed["currency"] = _currency_value(
                allowed["currency"], payload.get("currency_other", "")
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    if "price_scale" in allowed:
        try:
            allowed["price_scale"] = float(allowed["price_scale"])
            if allowed["price_scale"] <= 0:
                raise ValueError("Price scale must be positive")
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    conn = get_conn()
    assignments = ", ".join(f"{key}=?" for key in allowed)
    conn.execute(
        f"UPDATE instruments SET {assignments} WHERE id=?",
        (*allowed.values(), instrument_id),
    )
    conn.commit()
    if "currency" in allowed:
        await refresh_fx_rate(conn, str(allowed["currency"]).upper())
    row = conn.execute(
        "SELECT * FROM instruments WHERE id=?", (instrument_id,)
    ).fetchone()
    conn.close()
    return JSONResponse(dict(row) if row else {"error": "not found"}, status_code=200 if row else 404)


@router.get("/api/trades")
def trades_api():
    conn = get_conn()
    rows = row_dicts(conn.execute("SELECT * FROM trades ORDER BY ts DESC,id DESC"))
    conn.close()
    return rows


@router.post("/api/trades")
async def trades_create_api(request: Request):
    payload = await request.json()
    try:
        provided_fx = _positive_fx(payload.get("fx_rate"))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,fx_rate,notes) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            payload["instrument_id"],
            payload.get("ts") or datetime.now(UTC).isoformat(),
            payload["side"],
            payload["quantity"],
            payload["price"],
            payload.get("fees", 0),
            trade_fx_rate(conn, payload["instrument_id"], provided_fx),
            payload.get("notes", ""),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM trades WHERE id=?", (cursor.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


@router.delete("/api/trades/{trade_id}")
def trades_delete_api(trade_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.put("/api/trades/{trade_id}")
@router.patch("/api/trades/{trade_id}")
async def trades_update_api(trade_id: int, request: Request):
    payload = await request.json()
    allowed = {
        key: payload[key]
        for key in (
            "instrument_id",
            "ts",
            "side",
            "quantity",
            "price",
            "fees",
            "fx_rate",
            "notes",
        )
        if key in payload
    }
    if not allowed:
        return JSONResponse({"error": "no fields supplied"}, status_code=400)
    if "fx_rate" in allowed:
        try:
            allowed["fx_rate"] = _positive_fx(allowed["fx_rate"])
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    conn = get_conn()
    assignments = ", ".join(f"{key}=?" for key in allowed)
    conn.execute(
        f"UPDATE trades SET {assignments} WHERE id=?",
        (*allowed.values(), trade_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    conn.close()
    return JSONResponse(dict(row) if row else {"error": "not found"}, status_code=200 if row else 404)
