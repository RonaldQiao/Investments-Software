from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import get_conn, get_setting, init_db, set_setting
from .fees import (
    crystallize_perf_fee,
    fee_scenario,
    ownership,
    record_cash_flow,
    settle_fees,
)
from .nav import (
    compute_portfolio,
    exposure_by_class,
    history_csv,
    history_series,
    take_snapshot,
)
from .pricing import refresh_prices
from .positions import build_positions
from .scheduler import (
    catch_up_async,
    next_snapshot_label,
    start as start_scheduler,
    stop as stop_scheduler,
)

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=ROOT / "templates")
app = FastAPI(title="Ledger")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.on_event("startup")
async def startup():
    init_db()
    if os.environ.get("LEDGER_NO_SCHEDULER") != "1":
        await catch_up_async()
        start_scheduler()


@app.on_event("shutdown")
def shutdown():
    stop_scheduler()


def context(request: Request, **kwargs):
    conn = get_conn()
    kwargs.setdefault("fund_name", get_setting(conn, "fund_name", "Ledger"))
    conn.close()
    if kwargs.get("error") is None:
        kwargs["error"] = request.query_params.get("error")
    if kwargs.get("ok") is None:
        kwargs["ok"] = request.query_params.get("ok")
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


def age(value):
    if value is None:
        return ""
    seconds = int(value)
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    return f"{max(1, seconds // 3600)}h"


def flash_redirect(path, key, message):
    return RedirectResponse(f"{path}?{key}={quote(str(message))}", status_code=303)


templates.env.filters["number"] = number
templates.env.filters["percent"] = percent
templates.env.filters["age"] = age


def render(request: Request, name: str, status_code: int = 200, **kwargs):
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context(request, **kwargs),
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = get_conn()
    portfolio = compute_portfolio(conn)
    latest = get_setting(conn, "last_refresh_at", "") or conn.execute(
        "SELECT MAX(ts) ts FROM prices"
    ).fetchone()["ts"]
    fee_liability = float(get_setting(conn, "fee_liability", 0) or 0)
    snapshot = conn.execute(
        "SELECT date,nav_per_unit,daily_return FROM nav_snapshots ORDER BY date DESC LIMIT 1"
    ).fetchone()
    next_snapshot = next_snapshot_label(conn)
    conn.close()
    portfolio["fees_accrued"] = fee_liability
    return render(
        request,
        "dashboard.html",
        portfolio=portfolio,
        latest_refresh=latest,
        latest_snapshot=snapshot,
        exposure=exposure_by_class(portfolio),
        next_snapshot=next_snapshot,
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


@app.get("/trades.csv")
def trades_csv():
    conn = get_conn()
    rows = conn.execute(
        "SELECT t.ts,i.symbol,t.side,t.quantity,t.price,t.fees,t.notes "
        "FROM trades t JOIN instruments i ON i.id=t.instrument_id ORDER BY t.ts,t.id"
    ).fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ts", "symbol", "side", "quantity", "price", "fees", "notes"])
    writer.writerows(
        [row["ts"], row["symbol"], row["side"], row["quantity"], row["price"], row["fees"], row["notes"] or ""]
        for row in rows
    )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    conn = get_conn()
    settings = {
        key: get_setting(conn, key, default)
        for key, default in (
            ("fund_name", "Ledger"),
            ("leverage", "1.0"),
            ("borrow_rate", "0.05"),
            ("snapshot_enabled", "1"),
        )
    }
    conn.close()
    return render(request, "settings.html", settings=settings)


def capital_context(conn):
    flows = row_dicts(
        conn.execute(
            "SELECT c.*, l.name lp_name, u.units, u.nav_per_unit "
            "FROM cash_flows c LEFT JOIN lps l ON l.id=c.lp_id "
            "LEFT JOIN lp_units u ON u.cash_flow_id=c.id "
            "ORDER BY c.ts DESC,c.id DESC"
        )
    )
    for flow in flows:
        flow["date"] = flow["ts"][:10]
    lps = row_dicts(conn.execute("SELECT * FROM lps ORDER BY name"))
    totals = {
        "contributed": float(
            conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END),0) FROM cash_flows"
            ).fetchone()[0]
        ),
        "withdrawn": float(
            conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END),0) FROM cash_flows"
            ).fetchone()[0]
        ),
    }
    totals["net_capital"] = totals["contributed"] - totals["withdrawn"]
    totals["units"] = float(
        conn.execute("SELECT COALESCE(SUM(units),0) FROM lp_units").fetchone()[0]
    )
    totals["nav_per_unit"] = ownership(conn)["nav_per_unit"]
    return {"flows": flows, "lps": lps, "totals": totals}


@app.get("/capital", response_class=HTMLResponse)
def capital_page(request: Request, error: str | None = None):
    conn = get_conn()
    data = capital_context(conn)
    conn.close()
    return render(
        request,
        "capital.html",
        error=error,
        today=datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
        **data,
    )


@app.post("/capital")
def add_capital_flow(
    request: Request,
    flow_date: str = Form(""),
    lp_id: str = Form(""),
    new_lp: str = Form(""),
    flow_type: str = Form("Contribution"),
    amount: float = Form(...),
    note: str = Form(""),
):
    conn = get_conn()
    try:
        if lp_id == "new":
            name = new_lp.strip()
            if not name:
                raise ValueError("Enter a name for the new LP")
            cursor = conn.execute("INSERT INTO lps(name,is_gp) VALUES (?,0)", (name,))
            selected_lp = cursor.lastrowid
        else:
            selected_lp = int(lp_id)
        if amount <= 0:
            raise ValueError("Amount must be greater than zero")
        timestamp = flow_date.strip() or datetime.now(timezone.utc).date().isoformat()
        signed_amount = amount if flow_type == "Contribution" else -amount
        record_cash_flow(conn, timestamp, signed_amount, selected_lp, note.strip())
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        conn.close()
        return flash_redirect("/capital", "error", str(exc))
    conn.close()
    return RedirectResponse("/capital", status_code=303)


@app.post("/capital/{flow_id}/delete")
def delete_capital_flow(flow_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM lp_units WHERE cash_flow_id=?", (flow_id,))
    conn.execute("DELETE FROM cash_flows WHERE id=?", (flow_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/capital", status_code=303)


def fees_context(conn, request: Request):
    calc = None
    params = request.query_params
    if params.get("starting_nav"):
        try:
            calc = fee_scenario(
                params["starting_nav"],
                params.get("starting_nav_per_unit", 1000),
                params.get("hwm_per_unit", params.get("hwm")),
                params.get("gross_return_pct", params.get("gross_return", 0)),
                params.get("mgmt_pct", params.get("mgmt", 2)),
                params.get("perf_pct", params.get("perf", 20)),
            )
        except (TypeError, ValueError):
            calc = None
    settings = {
        key: get_setting(conn, key, default)
        for key, default in (
            ("mgmt_fee_bps", "200"),
            ("perf_fee_pct", "20"),
            ("hwm_per_unit", "1000"),
            ("fee_liability", "0"),
        )
    }
    return {
        "settings": settings,
        "lps": row_dicts(conn.execute("SELECT * FROM lps ORDER BY name")),
        "ownership": ownership(conn),
        "mgmt_accrued": float(
            conn.execute("SELECT COALESCE(SUM(amount),0) FROM fee_events WHERE kind='mgmt'").fetchone()[0]
        ),
        "perf_accrued": float(
            conn.execute("SELECT COALESCE(SUM(amount),0) FROM fee_events WHERE kind='perf'").fetchone()[0]
        ),
        "calc": calc,
    }


@app.get("/fees", response_class=HTMLResponse)
def fees_page(request: Request, error: str | None = None):
    conn = get_conn()
    data = fees_context(conn, request)
    conn.close()
    return render(request, "fees.html", error=error, **data)


@app.post("/fees/terms")
def save_fee_terms(
    gp_id: str = Form(""),
    mgmt_fee_bps: float = Form(...),
    perf_fee_pct: float = Form(...),
):
    conn = get_conn()
    set_setting(conn, "mgmt_fee_bps", mgmt_fee_bps)
    set_setting(conn, "perf_fee_pct", perf_fee_pct)
    if gp_id:
        conn.execute("UPDATE lps SET is_gp=0")
        conn.execute("UPDATE lps SET is_gp=1 WHERE id=?", (int(gp_id),))
        conn.commit()
    conn.close()
    return RedirectResponse("/fees", status_code=303)


@app.post("/fees/lps")
def add_lp(name: str = Form(...)):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO lps(name,is_gp) VALUES (?,0)", (name.strip(),))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return RedirectResponse("/fees", status_code=303)


@app.post("/fees/crystallize")
def crystallize_fees(request: Request):
    conn = get_conn()
    crystallize_perf_fee(conn, datetime.now(timezone.utc))
    conn.close()
    return RedirectResponse("/fees", status_code=303)


@app.post("/fees/settle")
def settle_fee_liability(request: Request):
    conn = get_conn()
    try:
        settle_fees(conn, datetime.now(timezone.utc))
    except ValueError as exc:
        conn.close()
        return flash_redirect("/fees", "error", str(exc))
    conn.close()
    return RedirectResponse("/fees", status_code=303)


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
    snapshot_enabled: str = Form("0"),
):
    conn = get_conn()
    set_setting(conn, "fund_name", fund_name.strip() or "Ledger")
    set_setting(conn, "leverage", min(5.0, max(1.0, leverage)))
    set_setting(conn, "borrow_rate", borrow_rate)
    set_setting(conn, "snapshot_enabled", "1" if snapshot_enabled == "1" else "0")
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
    try:
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
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        conn.close()
        return flash_redirect("/positions", "error", str(exc))
    conn.close()
    return flash_redirect("/positions", "ok", "Instrument added")


@app.post("/instruments/{instrument_id}/edit")
def edit_instrument(
    instrument_id: int,
    name: str = Form(...),
    asset_class: str = Form(...),
    multiplier: float = Form(...),
    pricing_source: str = Form(...),
    yahoo_symbol: str = Form(""),
    notes: str = Form(""),
):
    conn = get_conn()
    instrument = conn.execute(
        "SELECT * FROM instruments WHERE id=?", (instrument_id,)
    ).fetchone()
    if not instrument:
        conn.close()
        return flash_redirect("/positions", "error", "Instrument not found")
    position = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END),0) "
        "FROM trades WHERE instrument_id=?",
        (instrument_id,),
    ).fetchone()[0]
    conn.execute(
        "UPDATE instruments SET name=?,asset_class=?,multiplier=?,pricing_source=?,"
        "yahoo_symbol=?,notes=? WHERE id=?",
        (
            name.strip(),
            asset_class,
            multiplier,
            pricing_source,
            yahoo_symbol.strip() or None,
            notes.strip(),
            instrument_id,
        ),
    )
    conn.commit()
    conn.close()
    message = "Instrument updated"
    if float(multiplier) != float(instrument["multiplier"]) and abs(float(position)) > 1e-12:
        message += "; multiplier changed; NAV recomputed"
    return flash_redirect("/positions", "ok", message)


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


@app.post("/trades/import")
async def import_trades(file: UploadFile = File(...)):
    conn = get_conn()
    try:
        content = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        expected = ["ts", "symbol", "side", "quantity", "price", "fees", "notes"]
        if reader.fieldnames != expected:
            raise ValueError("CSV columns must be: " + ", ".join(expected))
        instruments = {
            row["symbol"]: row["id"]
            for row in conn.execute("SELECT id,symbol FROM instruments").fetchall()
        }
        pending = []
        unknown = []
        for row_number, row in enumerate(reader, start=2):
            symbol = (row.get("symbol") or "").strip().upper()
            if symbol not in instruments:
                unknown.append(f"row {row_number}: {symbol or '(blank)'}")
                continue
            pending.append(
                (
                    instruments[symbol],
                    row["ts"],
                    row["side"].strip().upper(),
                    float(row["quantity"]),
                    float(row["price"]),
                    float(row["fees"] or 0),
                    row["notes"] or "",
                )
            )
        if unknown:
            raise ValueError("Unknown symbols: " + ", ".join(unknown))
        conn.executemany(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) "
            "VALUES (?,?,?,?,?,?,?)",
            pending,
        )
        conn.commit()
    except (UnicodeDecodeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        conn.rollback()
        conn.close()
        return flash_redirect("/trades", "error", str(exc))
    conn.close()
    return flash_redirect("/trades", "ok", f"Imported {len(pending)} trades")


@app.post("/trades/{trade_id}/delete")
def delete_trade(trade_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/trades", status_code=303)


@app.post("/prices/refresh")
async def prices_refresh_page():
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM instruments WHERE pricing_source='yahoo'"
    ).fetchone()[0]
    failed = await refresh_prices(conn)
    conn.close()
    if failed:
        return flash_redirect(
            "/",
            "error",
            f"Refreshed {total - len(failed)} prices, {len(failed)} failed: {', '.join(failed)}",
        )
    return flash_redirect("/", "ok", f"Refreshed {total} prices, 0 failed")


@app.post("/snapshots/now")
async def snapshot_now():
    conn = get_conn()
    await refresh_prices(conn)
    today_ny = datetime.now(ZoneInfo("America/New_York")).date()
    take_snapshot(conn, today_ny, "manual", refresh=False)
    conn.close()
    return flash_redirect(
        "/history", "ok", f"Snapshot written for {today_ny.isoformat()}"
    )


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
