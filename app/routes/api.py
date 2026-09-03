from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..db import get_conn
from ..nav import compute_portfolio, history_series
from ..pricing import fetch_yahoo_meta, refresh_prices
from ..web import row_dicts

router = APIRouter()


@router.get("/api/lookup")
async def instrument_lookup(symbol: str = ""):
    symbol = symbol.strip().upper()
    if not symbol:
        return JSONResponse({"error": "symbol required"}, status_code=400)
    result = await fetch_yahoo_meta(symbol)
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)
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
        quantity = float(payload["quantity"]) if payload.get("quantity") not in (None, "") else None
        avg_price = (
            float(payload["avg_price"]) if payload.get("avg_price") not in (None, "") else None
        )
        fees = float(payload.get("fees", 0) or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid opening position"}, status_code=400)
    if side not in {"LONG", "SHORT"}:
        return JSONResponse({"error": "invalid side"}, status_code=400)
    if quantity is not None and quantity <= 0:
        return JSONResponse({"error": "quantity must be positive"}, status_code=400)
    if quantity is not None and avg_price is None:
        return JSONResponse({"error": "avg_price required with quantity"}, status_code=400)
    if avg_price is not None and avg_price < 0:
        return JSONResponse({"error": "avg_price cannot be negative"}, status_code=400)
    conn = get_conn()
    fields = (
        payload.get("symbol", "").strip().upper(),
        payload.get("name", "").strip(),
        payload.get("asset_class", "other"),
        payload.get("currency", "USD"),
        float(payload.get("multiplier", 1)),
        payload.get("pricing_source", "yahoo"),
        payload.get("yahoo_symbol") or None,
        payload.get("manual_mark"),
        payload.get("notes", ""),
        payload.get("underlying") or None,
        payload.get("expiry") or None,
        payload.get("strike"),
        payload.get("option_type") or None,
    )
    cursor = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source,"
        "yahoo_symbol,manual_mark,notes,underlying,expiry,strike,option_type) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        fields,
    )
    if quantity is not None:
        conn.execute(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                cursor.lastrowid,
                datetime.now(UTC).isoformat(),
                "BUY" if side == "LONG" else "SELL",
                quantity,
                avg_price,
                fees,
                "Opening position",
            ),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM instruments WHERE id=?", (cursor.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


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
    conn = get_conn()
    assignments = ", ".join(f"{key}=?" for key in allowed)
    conn.execute(
        f"UPDATE instruments SET {assignments} WHERE id=?",
        (*allowed.values(), instrument_id),
    )
    conn.commit()
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
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) VALUES (?,?,?,?,?,?,?)",
        (
            payload["instrument_id"],
            payload.get("ts") or datetime.now(UTC).isoformat(),
            payload["side"],
            payload["quantity"],
            payload["price"],
            payload.get("fees", 0),
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
        for key in ("instrument_id", "ts", "side", "quantity", "price", "fees", "notes")
        if key in payload
    }
    if not allowed:
        return JSONResponse({"error": "no fields supplied"}, status_code=400)
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
