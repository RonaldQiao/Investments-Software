import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..db import get_conn
from ..nav import compute_portfolio
from ..positions import build_positions
from ..web import flash_redirect, render, row_dicts

router = APIRouter()

@router.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request):
    conn = get_conn()
    instruments = row_dicts(conn.execute("SELECT * FROM instruments ORDER BY symbol"))
    portfolio = compute_portfolio(conn)
    conn.close()
    by_id = {item["instrument_id"]: item for item in portfolio["positions"]}
    return render(request, "positions.html", instruments=instruments, positions=by_id)


@router.post("/instruments")
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
    underlying: str = Form(""),
    expiry: str = Form(""),
    strike: str = Form(""),
    option_type: str = Form(""),
):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source,"
            "yahoo_symbol,manual_mark,notes,underlying,expiry,strike,option_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                underlying.strip() or None,
                expiry.strip() or None,
                float(strike) if strike.strip() else None,
                option_type.strip().upper() or None,
            ),
        )
        conn.commit()
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        conn.close()
        return flash_redirect("/positions", "error", str(exc))
    conn.close()
    return flash_redirect("/positions", "ok", "Instrument added")


@router.post("/instruments/{instrument_id}/edit")
def edit_instrument(
    instrument_id: int,
    name: str = Form(...),
    asset_class: str = Form(...),
    multiplier: float = Form(...),
    pricing_source: str = Form(...),
    yahoo_symbol: str = Form(""),
    notes: str = Form(""),
    underlying: str = Form(""),
    expiry: str = Form(""),
    strike: str = Form(""),
    option_type: str = Form(""),
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
        "yahoo_symbol=?,notes=?,underlying=?,expiry=?,strike=?,option_type=? WHERE id=?",
        (
            name.strip(),
            asset_class,
            multiplier,
            pricing_source,
            yahoo_symbol.strip() or None,
            notes.strip(),
            underlying.strip() or None,
            expiry.strip() or None,
            float(strike) if strike.strip() else None,
            option_type.strip().upper() or None,
            instrument_id,
        ),
    )
    conn.commit()
    conn.close()
    message = "Instrument updated"
    if float(multiplier) != float(instrument["multiplier"]) and abs(float(position)) > 1e-12:
        message += "; multiplier changed; NAV recomputed"
    return flash_redirect("/positions", "ok", message)


@router.post("/instruments/{instrument_id}/mark")
def set_mark(instrument_id: int, manual_mark: float = Form(...)):
    conn = get_conn()
    conn.execute(
        "UPDATE instruments SET manual_mark=?,manual_mark_at=? WHERE id=?",
        (manual_mark, datetime.now(UTC).isoformat(), instrument_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)


@router.post("/instruments/{instrument_id}/position")
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
                datetime.now(UTC).isoformat(),
                "BUY" if adjustment > 0 else "SELL",
                abs(adjustment),
                price,
            ),
        )
        conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)


@router.post("/instruments/{instrument_id}/delete")
def delete_instrument(instrument_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM instruments WHERE id=?", (instrument_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)
