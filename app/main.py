from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import get_conn, get_setting, init_db, set_setting
from .nav import compute_portfolio, history_csv, history_series, take_snapshot
from .pricing import refresh_prices
from .positions import build_positions
from .scheduler import catch_up_async, start as start_scheduler, stop as stop_scheduler

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=ROOT / "templates")
app = FastAPI(title="Ledger")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.on_event("startup")
async def startup():
    init_db()
    await catch_up_async()
    start_scheduler()


@app.on_event("shutdown")
def shutdown():
    stop_scheduler()


def context(request: Request, **kwargs):
    conn = get_conn()
    kwargs.setdefault("fund_name", get_setting(conn, "fund_name", "Ledger"))
    conn.close()
    return {"request": request, **kwargs}


def row_dicts(rows):
    return [dict(row) for row in rows]


def number(value, decimals=2):
    value = float(value or 0)
    sign = "−" if value < 0 else ""
    return f"{sign}{abs(value):,.{decimals}f}"


def percent(value):
    if value is None:
        return "—"
    value = float(value)
    return f"{'−' if value < 0 else '+'}{abs(value) * 100:.2f}%"


templates.env.filters["number"] = number
templates.env.filters["percent"] = percent


def render(request: Request, name: str, **kwargs):
    return templates.TemplateResponse(
        request=request, name=name, context=context(request, **kwargs)
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = get_conn()
    portfolio = compute_portfolio(conn)
    latest = conn.execute("SELECT MAX(ts) ts FROM prices").fetchone()["ts"]
    fee_liability = float(get_setting(conn, "fee_liability", 0) or 0)
    snapshot = conn.execute(
        "SELECT date,nav_per_unit,daily_return FROM nav_snapshots ORDER BY date DESC LIMIT 1"
    ).fetchone()
    conn.close()
    portfolio["fees_accrued"] = fee_liability
    return render(
        request,
        "dashboard.html",
        portfolio=portfolio,
        latest_refresh=latest,
        latest_snapshot=snapshot,
    )


@app.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request):
    conn = get_conn()
    instruments = row_dicts(conn.execute("SELECT * FROM instruments ORDER BY symbol"))
    portfolio = compute_portfolio(conn)
    conn.close()
    by_id = {item["instrument_id"]: item for item in portfolio["positions"]}
    return render(request, "positions.html", instruments=instruments, positions=by_id)


@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request):
    conn = get_conn()
    instruments = row_dicts(conn.execute("SELECT id,symbol FROM instruments ORDER BY symbol"))
    trades = row_dicts(
        conn.execute(
            "SELECT t.*, i.symbol FROM trades t JOIN instruments i ON i.id=t.instrument_id "
            "ORDER BY t.ts DESC,t.id DESC"
        )
    )
    conn.close()
    return render(request, "trades.html", instruments=instruments, trades=trades)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    conn = get_conn()
    settings = {
        key: get_setting(conn, key, default)
        for key, default in (
            ("fund_name", "Ledger"),
            ("leverage", "1.0"),
            ("borrow_rate", "0.05"),
        )
    }
    conn.close()
    return render(request, "settings.html", settings=settings)


@app.get("/capital", response_class=HTMLResponse)
@app.get("/fees", response_class=HTMLResponse)
def placeholder_page(request: Request):
    section = request.url.path.strip("/").title()
    return render(request, "placeholder.html", section=section)


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    conn = get_conn()
    series = history_series(conn)
    conn.close()
    return render(request, "history.html", **series)


@app.post("/settings")
def save_settings(
    fund_name: str = Form("Ledger"),
    leverage: float = Form(1.0),
    borrow_rate: float = Form(0.05),
):
    conn = get_conn()
    set_setting(conn, "fund_name", fund_name.strip() or "Ledger")
    set_setting(conn, "leverage", min(5.0, max(1.0, leverage)))
    set_setting(conn, "borrow_rate", borrow_rate)
    conn.close()
    return RedirectResponse("/settings", status_code=303)


@app.post("/instruments")
def add_instrument(
    symbol: str = Form(...),
    name: str = Form(...),
    asset_class: str = Form(...),
    currency: str = Form("USD"),
    multiplier: float = Form(1),
    pricing_source: str = Form("yahoo"),
    yahoo_symbol: str = Form(""),
    manual_mark: str = Form(""),
    notes: str = Form(""),
):
    conn = get_conn()
    conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source,"
        "yahoo_symbol,manual_mark,notes) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            symbol.strip().upper(),
            name.strip(),
            asset_class,
            currency.strip().upper(),
            multiplier,
            pricing_source,
            yahoo_symbol.strip() or None,
            float(manual_mark) if manual_mark.strip() else None,
            notes.strip(),
        ),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)


@app.post("/instruments/{instrument_id}/mark")
def set_mark(instrument_id: int, manual_mark: float = Form(...)):
    conn = get_conn()
    conn.execute(
        "UPDATE instruments SET manual_mark=?,manual_mark_at=? WHERE id=?",
        (manual_mark, datetime.now(timezone.utc).isoformat(), instrument_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)


@app.post("/instruments/{instrument_id}/position")
def set_position(
    instrument_id: int, target_qty: float = Form(...), price: float = Form(...)
):
    conn = get_conn()
    trades = conn.execute(
        "SELECT t.*,i.multiplier FROM trades t JOIN instruments i ON i.id=t.instrument_id "
        "WHERE instrument_id=? ORDER BY ts,id",
        (instrument_id,),
    ).fetchall()
    current = build_positions(trades).get(instrument_id)
    current_qty = current.qty if current else 0
    adjustment = target_qty - current_qty
    if adjustment:
        conn.execute(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) "
            "VALUES (?,?,?,?,?,0,'Set position')",
            (
                instrument_id,
                datetime.now(timezone.utc).isoformat(),
                "BUY" if adjustment > 0 else "SELL",
                abs(adjustment),
                price,
            ),
        )
        conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)


@app.post("/instruments/{instrument_id}/delete")
def delete_instrument(instrument_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM instruments WHERE id=?", (instrument_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)


@app.post("/trades")
def add_trade(
    instrument_id: int = Form(...),
    ts: str = Form(""),
    side: str = Form(...),
    quantity: float = Form(...),
    price: float = Form(...),
    fees: float = Form(0),
    notes: str = Form(""),
):
    conn = get_conn()
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) VALUES (?,?,?,?,?,?,?)",
        (
            instrument_id,
            ts or datetime.now(timezone.utc).isoformat(),
            side,
            quantity,
            price,
            fees,
            notes,
        ),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/trades", status_code=303)


@app.post("/trades/{trade_id}/delete")
def delete_trade(trade_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/trades", status_code=303)


@app.post("/snapshots/now")
async def snapshot_now():
    conn = get_conn()
    await refresh_prices(conn)
    today_ny = datetime.now(ZoneInfo("America/New_York")).date()
    take_snapshot(conn, today_ny, "manual", refresh=False)
    conn.close()
    return RedirectResponse("/history", status_code=303)


@app.post("/snapshots/{snapshot_date}/delete")
def delete_snapshot(snapshot_date: str):
    conn = get_conn()
    conn.execute("DELETE FROM nav_snapshots WHERE date=?", (snapshot_date,))
    conn.commit()
    conn.close()
    return RedirectResponse("/history", status_code=303)


@app.get("/api/portfolio")
def portfolio_api():
    conn = get_conn()
    result = compute_portfolio(conn)
    conn.close()
    return result


@app.get("/api/history")
def history_api():
    conn = get_conn()
    result = history_series(conn)
    conn.close()
    return result


@app.get("/history.csv")
def history_csv_download():
    conn = get_conn()
    content = history_csv(conn)
    conn.close()
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=nav_history.csv"},
    )


@app.post("/api/prices/refresh")
async def prices_refresh_api():
    conn = get_conn()
    failed = await refresh_prices(conn)
    result = compute_portfolio(conn)
    conn.close()
    return {"failed": failed, "portfolio": result}


@app.get("/api/instruments")
def instruments_api():
    conn = get_conn()
    rows = row_dicts(conn.execute("SELECT * FROM instruments ORDER BY symbol"))
    conn.close()
    return rows


@app.post("/api/instruments")
async def instruments_create_api(request: Request):
    payload = await request.json()
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
    )
    cursor = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source,"
        "yahoo_symbol,manual_mark,notes) VALUES (?,?,?,?,?,?,?,?,?)",
        fields,
    )
    conn.commit()
    row = conn.execute("SELECT * FROM instruments WHERE id=?", (cursor.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


@app.delete("/api/instruments/{instrument_id}")
def instruments_delete_api(instrument_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM instruments WHERE id=?", (instrument_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/instruments/{instrument_id}")
@app.patch("/api/instruments/{instrument_id}")
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


@app.get("/api/trades")
def trades_api():
    conn = get_conn()
    rows = row_dicts(conn.execute("SELECT * FROM trades ORDER BY ts DESC,id DESC"))
    conn.close()
    return rows


@app.post("/api/trades")
async def trades_create_api(request: Request):
    payload = await request.json()
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) VALUES (?,?,?,?,?,?,?)",
        (
            payload["instrument_id"],
            payload.get("ts") or datetime.now(timezone.utc).isoformat(),
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


@app.delete("/api/trades/{trade_id}")
def trades_delete_api(trade_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/trades/{trade_id}")
@app.patch("/api/trades/{trade_id}")
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
